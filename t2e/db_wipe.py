"""Fast DB-level wipe of migrated transactions (MariaDB direct).

The REST wipe cancels+deletes each document one HTTP call at a time, which is
slow for thousands of rows. This module deletes the transaction rows directly
from the Frappe/ERPNext tables for the target company -- orders of magnitude
faster -- and is the recommended way to reset the sandbox before a re-migration.

It only ever touches *transactional* tables (parents, their child tables, and
the ledger tables). Masters (accounts, parties, items) are never touched.
Child rows are deleted company-scoped via a JOIN to their parent, so a
multi-company site stays safe. Gated behind --confirm like every other write.
"""
from __future__ import annotations

import pymysql

from .config import get_config
from .staging import Staging

# doctype -> child tables (filtered by parent's company via JOIN + parenttype)
TXN_PARENTS: dict[str, list[str]] = {
    "Journal Entry": ["Journal Entry Account"],
    "Payment Entry": ["Payment Entry Reference", "Payment Entry Deduction",
                       "Advance Taxes and Charges"],
    "Sales Invoice": ["Sales Invoice Item", "Sales Taxes and Charges",
                       "Sales Invoice Advance", "Sales Invoice Payment",
                       "Payment Schedule"],
    "Purchase Invoice": ["Purchase Invoice Item", "Purchase Taxes and Charges",
                          "Purchase Invoice Advance", "Payment Schedule"],
}

# ledger/side tables that carry a `company` column
LEDGER_TABLES = ["GL Entry", "Payment Ledger Entry", "Stock Ledger Entry"]

# naming-series prefixes the migration consumes, each mapped to the doctype whose
# remaining rows set the safe reset floor. Journal Entry also covers the
# closing-stock and opening-balance JEs; Sales Invoice is Prompt-named so its
# SINV/SRET series rarely moves, but we reset it too for consistency.
SERIES_DOCTYPES: list[tuple[str, str]] = [
    ("ACC-JV-%", "Journal Entry"),
    ("ACC-PAY-%", "Payment Entry"),
    ("PINV-%", "Purchase Invoice"),
    ("PRET-%", "Purchase Invoice"),
    ("SINV-%", "Sales Invoice"),
    ("SRET-%", "Sales Invoice"),
]


def reset_naming_series(cur, dry_run: bool = True) -> dict[str, int]:
    """Rewind each migration series counter (`tabSeries.current`) to the highest
    suffix still present for its doctype -- 0 once the wipe removed every row --
    so the next migration renumbers from the start.

    Must run AFTER the deletes on the same connection: MariaDB lets a connection
    see its own uncommitted DELETEs, so the MAX below reflects the post-wipe
    state. It never lowers a series below a name still in use, so a multi-company
    site stays collision-safe (a shared series just holds at the other company's
    high-water mark).
    """
    out: dict[str, int] = {}
    for pattern, doctype in SERIES_DOCTYPES:
        _exec(cur, "SELECT name, current FROM `tabSeries` WHERE name LIKE %s", (pattern,))
        for series, current in list(cur.fetchall()):
            n = _exec(
                cur,
                f"SELECT COALESCE(MAX(CAST(SUBSTRING(name, %s) AS UNSIGNED)), 0) "
                f"FROM `tab{doctype}` WHERE name LIKE %s",
                (len(series) + 1, series + "%"))
            floor = int((cur.fetchone() or [0])[0] or 0) if n else 0
            if int(current) != floor:
                out[series] = floor
                if not dry_run:
                    _exec(cur, "UPDATE `tabSeries` SET current=%s WHERE name=%s",
                          (floor, series))
    return out


def _connect():
    p = get_config().db_params
    return pymysql.connect(
        host=p["host"], port=p["port"], user=p["user"],
        password=p["password"], database=p["database"],
        connect_timeout=20, autocommit=False,
    )


def _exec(cur, sql: str, args=()) -> int:
    try:
        cur.execute(sql, args)
        return cur.rowcount
    except pymysql.err.ProgrammingError as exc:
        # table doesn't exist on this version -> skip quietly
        if exc.args and exc.args[0] == 1146:
            return 0
        raise


def db_wipe(company: str, dry_run: bool = True, progress=print) -> dict[str, int]:
    result: dict[str, int] = {}
    conn = _connect()
    try:
        cur = conn.cursor()
        def count(sql_from: str, args) -> int:
            n = _exec(cur, f"SELECT COUNT(*) FROM {sql_from}", args)
            row = cur.fetchone()
            return row[0] if row else 0

        # 1) child tables first (company-scoped via JOIN to parent)
        for parent, children in TXN_PARENTS.items():
            for child in children:
                frm = (f"`tab{child}` c JOIN `tab{parent}` p ON c.parent = p.name "
                       f"WHERE p.company = %s AND c.parenttype = %s")
                if dry_run:
                    result[f"{child} (child)"] = count(frm, (company, parent))
                else:
                    result[f"{child} (child)"] = _exec(
                        cur, f"DELETE c FROM {frm}", (company, parent))
        # 2) parent transaction tables
        for parent in TXN_PARENTS:
            frm = f"`tab{parent}` WHERE company=%s"
            result[parent] = (count(frm, (company,)) if dry_run
                              else _exec(cur, f"DELETE FROM {frm}", (company,)))
        # 3) ledger tables
        for led in LEDGER_TABLES:
            frm = f"`tab{led}` WHERE company=%s"
            result[led] = (count(frm, (company,)) if dry_run
                           else _exec(cur, f"DELETE FROM {frm}", (company,)))

        # 4) rewind naming-series counters so the next migration renumbers from
        # the start (runs after the deletes, sees them via the open transaction).
        for series, floor in reset_naming_series(cur, dry_run).items():
            result[f"series {series}"] = floor

        if dry_run:
            conn.rollback()
            progress("  (dry-run: counts of rows that WOULD be deleted)")
        else:
            conn.commit()
    finally:
        conn.close()
    return result


def reset_staging() -> None:
    s = Staging()
    s.conn.execute("UPDATE voucher SET load_status='pending', erp_name=NULL, error=NULL")
    s.conn.commit()
    s.close()
