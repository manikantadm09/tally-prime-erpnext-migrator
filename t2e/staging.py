"""SQLite staging store.

Every Tally master/voucher is parsed into a normalized row and stored here as
JSON, keyed by Tally GUID. Staging decouples extraction from loading: we can
re-run the loader against ERPNext without re-hitting Tally, and the per-record
``load_status`` / ``erp_name`` / ``error`` columns drive the reconciliation
report and retries.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_config


def _payload_alter_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("AlterId", payload.get("ALTERID"))
    return str(value).strip() if value not in (None, "") else None


def source_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

SCHEMA = """
CREATE TABLE IF NOT EXISTS master (
    guid        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,         -- group | ledger | item | unit | godown | costcentre | vouchertype
    name        TEXT NOT NULL,
    parent      TEXT,
    payload     TEXT NOT NULL,         -- full parsed JSON
    erp_doctype TEXT,
    erp_name    TEXT,
    load_status TEXT DEFAULT 'pending',-- pending | loaded | skipped | error
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_master_kind ON master(kind);
CREATE INDEX IF NOT EXISTS idx_master_status ON master(load_status);

CREATE TABLE IF NOT EXISTS voucher (
    guid        TEXT PRIMARY KEY,
    vtype       TEXT NOT NULL,         -- Tally voucher type name
    vnumber     TEXT,
    vdate       TEXT,                  -- yyyy-mm-dd
    party       TEXT,
    amount      REAL,
    payload     TEXT NOT NULL,         -- full parsed JSON (ledger + inventory entries)
    alter_id    TEXT,
    source_hash TEXT,
    source_present INTEGER NOT NULL DEFAULT 1,
    source_state TEXT NOT NULL DEFAULT 'new',
                                     -- resolved is terminal after an approved cancellation
    erp_doctype TEXT,
    erp_name    TEXT,
    load_status TEXT DEFAULT 'pending',
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_voucher_type ON voucher(vtype);
CREATE INDEX IF NOT EXISTS idx_voucher_status ON voucher(load_status);
CREATE INDEX IF NOT EXISTS idx_voucher_date ON voucher(vdate);

-- Bill (invoice) reference index: maps a Tally party + bill name to the ERPNext
-- invoice created for it, so payments/journals can allocate against it and drive
-- the invoice's paid / partially-paid status.
CREATE TABLE IF NOT EXISTS bill_ref (
    party    TEXT NOT NULL,   -- ERPNext party name
    billname TEXT NOT NULL,   -- Tally bill reference (New Ref name)
    doctype  TEXT NOT NULL,   -- Sales Invoice | Purchase Invoice
    invoice  TEXT NOT NULL,   -- ERPNext invoice name
    -- Tally can reuse the same bill reference for multiple invoices belonging
    -- to one party. Keep every invoice and allocate settlements FIFO.
    PRIMARY KEY (party, billname, invoice)
);

-- One Tally ledger can legitimately act as both a Customer and a Supplier, or
-- be grouped under an advance account while still being the party on an
-- invoice. Keep ERPNext party roles separately from the ledger's source GL
-- mapping so invoice workflow does not destroy Tally account classification.
CREATE TABLE IF NOT EXISTS party_role (
    ledger_guid TEXT NOT NULL,
    ledger_name TEXT NOT NULL,
    party_type  TEXT NOT NULL,  -- Customer | Supplier
    party       TEXT NOT NULL,
    PRIMARY KEY (ledger_guid, party_type)
);
CREATE INDEX IF NOT EXISTS idx_party_role_name
    ON party_role(ledger_name, party_type);

-- Durable state for incremental extraction.  Its checkpoint is advanced only
-- after a source delta report has no records requiring an operator decision.
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Staging:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_config().staging_db
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_bill_ref_schema()
        self._migrate_voucher_schema()
        self.conn.commit()

    def _migrate_bill_ref_schema(self) -> None:
        """Upgrade the original one-invoice-per-bill schema in place."""
        info = self.conn.execute("PRAGMA table_info(bill_ref)").fetchall()
        pk = [r["name"] for r in sorted(
            (r for r in info if r["pk"]), key=lambda r: r["pk"])]
        if pk != ["party", "billname"]:
            return
        self.conn.execute("ALTER TABLE bill_ref RENAME TO bill_ref_legacy")
        self.conn.execute(
            """CREATE TABLE bill_ref (
                 party TEXT NOT NULL,
                 billname TEXT NOT NULL,
                 doctype TEXT NOT NULL,
                 invoice TEXT NOT NULL,
                 PRIMARY KEY (party, billname, invoice)
               )""")
        self.conn.execute(
            """INSERT OR IGNORE INTO bill_ref(party,billname,doctype,invoice)
               SELECT party,billname,doctype,invoice FROM bill_ref_legacy""")
        self.conn.execute("DROP TABLE bill_ref_legacy")

    def _migrate_voucher_schema(self) -> None:
        """Upgrade old staging databases without resetting target load state."""
        columns = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(voucher)").fetchall()}
        for name, definition in {
            "alter_id": "TEXT",
            "source_hash": "TEXT",
            "source_present": "INTEGER NOT NULL DEFAULT 1",
            "source_state": "TEXT NOT NULL DEFAULT 'new'",
        }.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE voucher ADD COLUMN {name} {definition}")
        for row in self.conn.execute(
                "SELECT guid,payload,alter_id,source_hash,source_state FROM voucher"):
            payload = json.loads(row["payload"])
            state = row["source_state"] or "baseline"
            if state == "new" and row["source_hash"] is None:
                state = "baseline"
            self.conn.execute(
                "UPDATE voucher SET alter_id=?,source_hash=?,source_state=? WHERE guid=?",
                (row["alter_id"] or _payload_alter_id(payload),
                 row["source_hash"] or source_payload_hash(payload), state, row["guid"]))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- writes ----------------------------------------------------------
    def upsert_master(self, kind: str, guid: str, name: str,
                      parent: str | None, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO master(guid,kind,name,parent,payload)
                 VALUES(?,?,?,?,?)
               ON CONFLICT(guid) DO UPDATE SET
                 kind=excluded.kind, name=excluded.name,
                 parent=excluded.parent, payload=excluded.payload""",
            (guid, kind, name, parent, json.dumps(payload, ensure_ascii=False)),
        )

    def upsert_voucher(self, guid: str, vtype: str, vnumber: str | None,
                       vdate: str | None, party: str | None, amount: float,
                       payload: dict[str, Any], *, source_present: bool = True,
                       source_state: str | None = None) -> None:
        """Stage source data without automatically reloading changed documents."""
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        source_hash = source_payload_hash(payload)
        existing = self.conn.execute(
            "SELECT source_hash,source_state,load_status,erp_name FROM voucher WHERE guid=?",
            (guid,)).fetchone()
        if source_state is None:
            if existing is None:
                state = "new"
            elif existing["source_hash"] != source_hash:
                state = ("new" if existing["load_status"] in ("pending", "error")
                         and not existing["erp_name"] else "changed")
            elif existing["source_state"] in ("changed", "missing", "cancelled", "optional"):
                state = existing["source_state"]
            elif existing["load_status"] in ("pending", "error"):
                state = "new"
            else:
                state = "unchanged"
        else:
            state = source_state
        self.conn.execute(
            """INSERT INTO voucher(
                   guid,vtype,vnumber,vdate,party,amount,payload,alter_id,
                   source_hash,source_present,source_state)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(guid) DO UPDATE SET
                 vtype=excluded.vtype, vnumber=excluded.vnumber,
                 vdate=excluded.vdate, party=excluded.party,
                 amount=excluded.amount, payload=excluded.payload,
                 alter_id=excluded.alter_id, source_hash=excluded.source_hash,
                 source_present=excluded.source_present,
                 source_state=excluded.source_state""",
            (guid, vtype, vnumber, vdate, party, amount, encoded,
             _payload_alter_id(payload), source_hash, int(source_present), state),
        )

    def mark(self, table: str, guid: str, status: str,
             erp_doctype: str | None = None, erp_name: str | None = None,
             error: str | None = None) -> None:
        assert table in ("master", "voucher")
        self.conn.execute(
            f"""UPDATE {table} SET load_status=?, erp_doctype=COALESCE(?,erp_doctype),
                  erp_name=COALESCE(?,erp_name), error=? WHERE guid=?""",
            (status, erp_doctype, erp_name, error, guid),
        )
        if table == "voucher" and status in ("loaded", "skipped"):
            self.conn.execute(
                "UPDATE voucher SET source_state='unchanged' "
                "WHERE guid=? AND source_state='new'", (guid,))

    # ---- reads -----------------------------------------------------------
    def masters(self, kind: str | None = None,
                status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM master WHERE 1=1"
        args: list[Any] = []
        if kind:
            q += " AND kind=?"; args.append(kind)
        if status:
            q += " AND load_status=?"; args.append(status)
        q += " ORDER BY name"
        return self.conn.execute(q, args).fetchall()

    def vouchers(self, vtype: str | None = None, status: str | None = None,
                 order_by_date: bool = True, *, include_inactive: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM voucher WHERE 1=1"
        args: list[Any] = []
        if not include_inactive:
            q += " AND source_present=1"
        if vtype:
            q += " AND vtype=?"; args.append(vtype)
        if status:
            q += " AND load_status=?"; args.append(status)
        q += " ORDER BY vdate, vnumber" if order_by_date else " ORDER BY guid"
        return self.conn.execute(q, args).fetchall()

    # ---- bill references -------------------------------------------------
    def add_bill_ref(self, party: str, billname: str, doctype: str, invoice: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO bill_ref(party,billname,doctype,invoice)
               VALUES(?,?,?,?)""",
            (party, billname, doctype, invoice))

    def get_bill_refs(self, party: str, billname: str):
        """All matching invoices in insertion order for FIFO allocation."""
        return self.conn.execute(
            """SELECT doctype, invoice FROM bill_ref
               WHERE party=? AND billname=? ORDER BY rowid""",
            (party, billname)).fetchall()

    def get_bill_ref(self, party: str, billname: str):
        """Backward-compatible single-reference lookup."""
        refs = self.get_bill_refs(party, billname)
        return refs[0] if refs else None

    def clear_bill_refs(self) -> None:
        self.conn.execute("DELETE FROM bill_ref")
        self.conn.commit()

    # ---- party roles -----------------------------------------------------
    def add_party_role(self, ledger_guid: str, ledger_name: str,
                       party_type: str, party: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO party_role
                 (ledger_guid,ledger_name,party_type,party)
               VALUES(?,?,?,?)""",
            (ledger_guid, ledger_name, party_type, party))

    def party_roles(self):
        return self.conn.execute(
            """SELECT ledger_guid,ledger_name,party_type,party
                 FROM party_role ORDER BY ledger_name,party_type"""
        ).fetchall()

    def clear_party_roles(self) -> None:
        self.conn.execute("DELETE FROM party_role")
        self.conn.commit()

    # ---- source replacement ---------------------------------------------
    def clear_voucher_window(self, from_iso: str, to_iso: str) -> int:
        """Mark a re-exported source window absent before staging it.

        Retaining rows exposes a missing/cancelled source voucher instead of
        silently orphaning its submitted ERPNext document.
        """
        cur = self.conn.execute(
            """UPDATE voucher SET source_present=0,
                   source_state=CASE WHEN source_present=1 THEN 'missing'
                                     ELSE source_state END
               WHERE vdate BETWEEN ? AND ?""",
            (from_iso, to_iso))
        return cur.rowcount

    def source_delta_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT guid,vtype,vnumber,vdate,party,amount,alter_id,
                      source_present,source_state,load_status,erp_doctype,erp_name
                 FROM voucher
                WHERE source_state NOT IN ('unchanged', 'baseline', 'resolved')
                ORDER BY vdate,vnumber,guid"""
        ).fetchall()

    def voucher_by_guid(self, guid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM voucher WHERE guid=?", (guid,)).fetchone()

    def reopen_for_reload(self, guid: str) -> None:
        """Make a human-approved changed voucher eligible for the normal loader."""
        self.conn.execute(
            """UPDATE voucher SET load_status='pending', source_state='new'
               WHERE guid=?""", (guid,))

    def resolve(self, guid: str) -> None:
        """Close an approved missing/cancelled source voucher without a replacement."""
        self.conn.execute(
            """UPDATE voucher SET load_status='cancelled', source_state='resolved'
               WHERE guid=?""", (guid,))

    # ---- checkpoint --------------------------------------------------------
    def get_checkpoint(self) -> str | None:
        """Last extraction boundary (yyyymmdd) confirmed clean by sync-report."""
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key='last_checkpoint'"
        ).fetchone()
        return row["value"] if row else None

    def set_checkpoint(self, value: str) -> None:
        """Record a new confirmed-clean extraction boundary (yyyymmdd)."""
        self.conn.execute(
            """INSERT INTO sync_state(key, value) VALUES('last_checkpoint', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (value,))
        self.conn.commit()

    def duplicate_bill_key_count(self, party: str, billname: str) -> int:
        """How many staged invoice vouchers use this party + bill reference."""
        import json
        from .lines import parse_entries

        total = 0
        rows = self.conn.execute(
            """SELECT payload FROM voucher
                WHERE party=? AND vtype IN
                      ('Sales','Credit Note','Purchase','Debit Note')""",
            (party,)).fetchall()
        for row in rows:
            for entry in parse_entries(json.loads(row["payload"])):
                if any(
                    b.get("name") == billname
                    and b.get("type") in ("New Ref", "Agst Ref")
                    for b in entry.get("bills", [])
                ):
                    total += 1
                    break
        return total

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for table, col in (("master", "kind"), ("voucher", "vtype")):
            rows = self.conn.execute(
                f"SELECT {col} k, load_status s, COUNT(*) c FROM {table} "
                + ("WHERE source_present=1 " if table == "voucher" else "")
                + f"GROUP BY {col}, load_status"
            ).fetchall()
            for r in rows:
                out.setdefault(f"{table}:{r['k']}", {})[r["s"]] = r["c"]
        return out

    def close(self) -> None:
        self.conn.close()
