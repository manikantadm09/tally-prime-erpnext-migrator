"""Load staged Tally vouchers into ERPNext as GL-faithful documents.

Strategy (decided with the user):
* Receipt / Payment  -> Payment Entry when the voucher fits the clean
  bank/cash-to-party shape, otherwise a Journal Entry (so nothing is ever dropped).
* Composite Tally vouchers (TDS, bank charges, extra ledgers) stay Journal Entry.
* Party lines always post to Debtors/Creditors with party_type/party.
* Agst Ref is NOT attached during insert. A later GUID-backed bill-reference
  phase links exact named invoices; New Ref / genuine advances stay unallocated.

A Journal Entry reproduces the exact Tally ledger postings: every
ALLLEDGERENTRIES line becomes a debit or credit row, party lines post against
the company receivable/payable control account with party_type/party set.
Debits must equal credits (Tally guarantees this); a sub-rupee residue is
absorbed into the round-off account.
"""
from __future__ import annotations

import json

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .mapping import CompanyDefaults, LedgerResolver, Resolved
from .staging import Staging

ROUND_TOL = 0.05  # residual up to this is pushed to round-off; above => flagged


def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _f(x) -> float:
    try:
        return float(_s(x))
    except (TypeError, ValueError):
        return 0.0


def _s(x) -> str:
    """Coerce a staged payload value to a scalar string (defensive)."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return str(x.get("#text", "")) if "#text" in x else ""
    if isinstance(x, list):
        return _s(x[0]) if x else ""
    return str(x)


class VoucherLoader:
    def __init__(self, erp: ERPNextClient, store: Staging,
                 defaults: CompanyDefaults, resolver: LedgerResolver):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.r = resolver
        cfg = get_config()
        self.field = cfg.idempotency_field
        self.vmap = cfg.yaml["voucher_map"]
        self.unresolved: set[str] = set()

    # ---- ledger entry extraction ----------------------------------------
    def _entries(self, payload: dict) -> list[dict]:
        """Return normalized GL lines: [{ledger, magnitude, is_debit, bills}]."""
        out = []
        raw = _as_list(payload.get("ALLLEDGERENTRIES.LIST")) \
            or _as_list(payload.get("LEDGERENTRIES.LIST"))
        for le in raw:
            if not isinstance(le, dict):
                continue
            ledger = le.get("LEDGERNAME")
            if not ledger:
                continue
            amt = _f(le.get("AMOUNT"))
            # In Tally XML the AMOUNT sign is authoritative: negative = debit,
            # positive = credit. (ISDEEMEDPOSITIVE reflects ledger nature and
            # disagrees on rounding lines, so we do NOT use it here.)
            is_debit = amt < 0
            bills = []
            for b in _as_list(le.get("BILLALLOCATIONS.LIST")):
                if isinstance(b, dict) and b.get("NAME"):
                    # Preserve the signed amount for exact Tally-reference
                    # reconciliation. Existing loaders still use ``amount`` as
                    # a magnitude, so this is backward compatible.
                    signed_amount = _f(b.get("AMOUNT"))
                    bills.append({"name": b.get("NAME"), "type": b.get("BILLTYPE"),
                                  "amount": abs(signed_amount),
                                  "signed_amount": signed_amount})
            out.append({"ledger": ledger, "magnitude": abs(amt),
                        "is_debit": is_debit, "bills": bills})
        return out

    def _resolve(self, ledger: str) -> Resolved | None:
        res = self.r.get(ledger)
        if res is None:
            self.unresolved.add(ledger)
        return res

    def _is_pl_account(self, res: Resolved) -> bool:
        # Income/Expense accounts require a cost center on the JE row. We don't
        # know root_type cheaply here, so attach cost center to every non-party
        # GL line; ERPNext ignores it for balance-sheet accounts.
        return res.kind == "account"

    # ---- Journal Entry ---------------------------------------------------
    def _build_je(self, vrow, payload: dict, voucher_type="Journal Entry") -> dict:
        accounts = []
        total_dr = total_cr = 0.0
        for e in self._entries(payload):
            res = self._resolve(e["ledger"])
            if res is None:
                # Never drop a voucher: post the line to the suspense account
                # (flagged via self.unresolved + report) so the books still tie.
                res = Resolved("account", self.d.suspense)
            if res.kind == "party":
                rows = self._party_je_rows(res, e)
            else:
                row = {"account": res.account, "cost_center": self.d.cost_center}
                _put(row, e["is_debit"], round(e["magnitude"], 2))
                rows = [row]
            for row in rows:
                total_dr += row.get("debit_in_account_currency", 0.0)
                total_cr += row.get("credit_in_account_currency", 0.0)
                accounts.append(row)

        _balance(accounts, total_dr, total_cr, self.d)

        return {
            "company": self.d.name,
            "posting_date": vrow["vdate"],
            "voucher_type": voucher_type,
            # cheque_no must be non-empty when cheque_date is set; fall back to
            # the voucher number then the Tally GUID so it is never blank.
            "cheque_no": (_s(payload.get("REFERENCE")) or vrow["vnumber"]
                          or vrow["guid"])[:140],
            "cheque_date": vrow["vdate"],
            "user_remark": _s(payload.get("NARRATION"))[:1000],
            "title": f"{vrow['vtype']} {vrow['vnumber'] or ''}".strip(),
            "accounts": accounts,
            self.field: vrow["guid"],
        }

    def _party_je_rows(self, res: Resolved, e: dict) -> list[dict]:
        """Post the party line to the control account only.

        Agst Ref is applied later by the GUID-backed bill-reference phase so
        insert never FIFO-allocates or invents a bill.
        """
        row = {"account": res.account, "party_type": res.party_type,
               "party": res.party}
        _put(row, e["is_debit"], round(e["magnitude"], 2))
        return [row]

    def _invoice_outstanding(self, doctype: str, name: str) -> float:
        rows = self.erp.get_list(doctype, fields=["outstanding_amount"],
                                filters=[["name", "=", name]], limit=1)
        return float(rows[0]["outstanding_amount"]) if rows else 0.0

    def _bill_references(self, party: str, bills: list[dict], cap: float) -> list[dict]:
        """Payment Entry reference rows for Agst-Ref bills that map to invoices.

        Allocation is capped to the invoice's live outstanding so tiny rounding
        gaps between Tally's bill amount and ERPNext's rounded invoice total
        never trigger an over-allocation error.
        """
        refs, remaining = [], cap
        for b in bills:
            if b.get("type") != "Agst Ref":
                continue
            bill_remaining = round(min(b["amount"], remaining), 2)
            for ref in self.store.get_bill_refs(party, b["name"]):
                outstanding = self._invoice_outstanding(
                    ref["doctype"], ref["invoice"])
                alloc = round(
                    min(bill_remaining, remaining, outstanding), 2)
                if alloc <= 0:
                    continue
                refs.append({"reference_doctype": ref["doctype"],
                             "reference_name": ref["invoice"],
                             "allocated_amount": alloc})
                remaining = round(remaining - alloc, 2)
                bill_remaining = round(bill_remaining - alloc, 2)
                if remaining <= 0.001 or bill_remaining <= 0.001:
                    break
        return refs

    # ---- Payment Entry ---------------------------------------------------
    def _build_payment(self, vrow, payload: dict, mode: str) -> dict | None:
        """Return a Payment Entry doc, or None to signal JE fallback."""
        entries = self._entries(payload)
        party_lines, bank_lines, other = [], [], []
        for e in entries:
            res = self._resolve(e["ledger"])
            if res is None:
                return None
            if res.kind == "party":
                party_lines.append((e, res))
            elif _looks_like_bank(e["ledger"], res):
                bank_lines.append((e, res))
            else:
                other.append((e, res))
        # clean shape: exactly one party + one bank/cash account and nothing
        # else. A voucher with extra ledgers (TDS withheld, bank charges) is
        # posted as a Journal Entry instead: ERPNext's Payment Entry
        # "deductions" derive the party amount as received + deductions, which
        # double-counts the deduction (the Tally party line already includes it)
        # and inflates the Debtors/Creditors posting. A JE replays Tally's exact
        # Dr/Cr, so the control accounts stay faithful.
        if len(party_lines) != 1 or len(bank_lines) != 1 or other:
            return None
        (pe, pres) = party_lines[0]
        (be, bres) = bank_lines[0]

        ptype = "Receive" if mode == "receive" else "Pay"
        party_account = pres.account
        bank_account = bres.account
        amount = round(pe["magnitude"], 2)

        doc = {
            "payment_type": ptype,
            "company": self.d.name,
            "posting_date": vrow["vdate"],
            "party_type": pres.party_type,
            "party": pres.party,
            "paid_amount": amount,
            "received_amount": amount,
            "reference_no": (_s(payload.get("REFERENCE")) or vrow["vnumber"]
                             or vrow["guid"][:20])[:140],
            "reference_date": vrow["vdate"],
            "remarks": _s(payload.get("NARRATION"))[:1000],
            self.field: vrow["guid"],
        }
        if ptype == "Receive":
            doc["paid_from"] = party_account   # debtor control
            doc["paid_to"] = bank_account
            doc["paid_to_account_currency"] = self.d.currency
        else:
            doc["paid_from"] = bank_account
            doc["paid_to"] = party_account     # creditor control
            doc["paid_from_account_currency"] = self.d.currency

        # Do not attach Agst Ref here. The GUID-backed bill-reference phase
        # allocates the exact named invoice after every document exists.
        return doc

    # ---- public entry point ---------------------------------------------
    def load_one(self, vrow) -> tuple[str, str]:
        payload = json.loads(vrow["payload"])
        spec = self.vmap.get(vrow["vtype"], {"doctype": "Journal Entry", "mode": "journal"})
        mode = spec.get("mode", "journal")

        if mode in ("receive", "pay"):
            doc = self._build_payment(vrow, payload, mode)
            if doc is not None:
                try:
                    res = self.erp.submit_doc("Payment Entry", doc)
                    name = _name_of(res)
                    if not self.erp.dry_run and not name:
                        name = self.erp.find_by_field(
                            "Payment Entry", self.field, vrow["guid"])
                    if not self.erp.dry_run and not name:
                        raise ERPNextError(
                            "Payment Entry submitted but no document name returned")
                    return "Payment Entry", name
                except ERPNextError:
                    # A timeout may occur after the server committed the Payment
                    # Entry. Re-check the GUID before considering JE fallback.
                    if not self.erp.dry_run:
                        existing = self.erp.find_by_field(
                            "Payment Entry", self.field, vrow["guid"])
                        if existing:
                            return "Payment Entry", existing
                    # Payment Entry rejected (e.g. difference amount): never drop
                    # the record -- fall back to an equivalent Journal Entry.
                    pass
        voucher_type = _je_voucher_type(vrow["vtype"])
        je = self._build_je(vrow, payload, voucher_type)
        res = self.erp.submit_doc("Journal Entry", je)
        name = _name_of(res)
        if not self.erp.dry_run and not name:
            name = self.erp.find_by_field(
                "Journal Entry", self.field, vrow["guid"])
        if not self.erp.dry_run and not name:
            raise ERPNextError(
                "Journal Entry submitted but no document name returned")
        return "Journal Entry", name

    def _find_existing(self, guid: str) -> tuple[str, str] | None:
        """Find a prior settlement import regardless of PE/JE fallback path."""
        for doctype in ("Payment Entry", "Journal Entry"):
            name = self.erp.find_by_field(
                doctype, self.field, guid, exclude_cancelled=True)
            if name:
                return doctype, name
        return None

    def run(self, vtype: str | None = None, limit: int = 0,
            progress=lambda *a: None) -> dict[str, int]:
        rows = self.store.vouchers(vtype=vtype, status="pending")
        if limit:
            rows = rows[:limit]
        stats = {"planned": 0, "loaded": 0, "skipped": 0, "error": 0}
        for i, vrow in enumerate(rows, 1):
            try:
                existing = self._find_existing(vrow["guid"])
                if existing:
                    doctype, name = existing
                    if not self.erp.dry_run:
                        self.store.mark(
                            "voucher", vrow["guid"], "loaded", doctype, name)
                    stats["skipped"] += 1
                    continue
                doctype, name = self.load_one(vrow)
                if self.erp.dry_run:
                    stats["planned"] += 1
                else:
                    self.store.mark("voucher", vrow["guid"], "loaded", doctype, name)
                    stats["loaded"] += 1
            except Exception as exc:  # noqa: BLE001
                if not self.erp.dry_run:
                    self.store.mark("voucher", vrow["guid"], "error",
                                    error=str(exc)[:800])
                stats["error"] += 1
            if i % 50 == 0:
                self.store.conn.commit()
                progress(i, len(rows), stats)
        self.store.conn.commit()
        progress(len(rows), len(rows), stats)
        return stats


# ---- module helpers ------------------------------------------------------
def _put(row: dict, is_debit: bool, amount: float) -> None:
    if is_debit:
        row["debit_in_account_currency"] = amount
    else:
        row["credit_in_account_currency"] = amount


def _balance(accounts, total_dr, total_cr, d: CompanyDefaults):
    diff = round(total_dr - total_cr, 2)
    if abs(diff) < 1e-9:
        return
    if abs(diff) <= ROUND_TOL:
        row = {"account": d.round_off, "cost_center": d.cost_center}
        if diff > 0:   # debit exceeds credit -> add a credit to round off
            row["credit_in_account_currency"] = abs(diff)
        else:
            row["debit_in_account_currency"] = abs(diff)
        accounts.append(row)
    else:
        raise ERPNextError(f"Voucher unbalanced by {diff} (Dr {total_dr} / Cr {total_cr})")


def _looks_like_bank(ledger: str, res: Resolved) -> bool:
    if res.kind != "account":
        return False
    if (res.account_type or "") in ("Bank", "Cash"):
        return True
    # Name fallback for bank/cash ledgers whose Tally group was not mapped.
    name = ledger.lower()
    keys = ("bank", "cash", "hdfc", "icici", "axis", "canara", "sbi", "kotak",
            "loan a/c", "od a/c", "occ", "current a/c", "petty cash")
    return any(k in name for k in keys)


def _je_voucher_type(tally_vtype: str) -> str:
    # ERPNext's "Contra Entry"/"Bank Entry" types impose account-type checks
    # (all rows must be Cash/Bank) that some Tally vouchers violate. The
    # voucher_type label is cosmetic for a GL-faithful import, so use the
    # unconstrained "Journal Entry" everywhere except the note types, whose
    # ERPNext JE types carry no such restriction.
    mapping = {
        "Credit Note": "Credit Note",
        "Debit Note": "Debit Note",
    }
    return mapping.get(tally_vtype, "Journal Entry")


def _name_of(res) -> str | None:
    if isinstance(res, dict):
        msg = res.get("message")
        if isinstance(msg, dict):
            return msg.get("name")
        data = res.get("data")
        if isinstance(data, dict):
            return data.get("name")
    return None
