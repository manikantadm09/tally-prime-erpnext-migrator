"""GUID-backed bill-reference phase.

Load invoices, payments, and journals with no Agst Ref attached. This module
then plans exact named links from Tally bill allocations:

* Agst Ref → the unique migrated invoice that originated that bill name
* New Ref / genuine Advance on a settlement → leave unallocated
* never FIFO, never invent a bill, never guess an ambiguous name

Source bills with no invoice/payment capacity stay on the exception list.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from typing import Any

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .exact_bill_reconciliation import _gl_signature
from .lines import parse_entries
from .load_masters import fetch_company_defaults
from .mapping import LedgerResolver, _norm
from .staging import Staging


PENNY = Decimal("0.01")
SETTLEMENT_TYPES = {"Receipt", "Payment", "Journal", "Contra"}
INVOICE_TYPES = {"Sales", "Purchase", "Credit Note", "Debit Note"}


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def plan_guid_bill_references(store: Staging) -> dict:
    """Pure staging plan. Does not call ERPNext."""
    origins: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for v in store.vouchers():
        if v["vtype"] not in INVOICE_TYPES:
            continue
        payload = json.loads(v["payload"] or "{}")
        for entry in parse_entries(payload):
            for bill in entry.get("bills") or []:
                if bill.get("type") not in ("New Ref", "Agst Ref", "Advance"):
                    continue
                if not bill.get("name"):
                    continue
                origins[(_norm(entry["ledger"]), _norm(bill["name"]))].append({
                    "guid": v["guid"],
                    "vtype": v["vtype"],
                    "erp_doctype": v["erp_doctype"],
                    "erp_name": v["erp_name"],
                    "bill_name": bill["name"],
                    "party": entry["ledger"],
                })

    allocations: list[dict] = []
    advances: list[dict] = []
    exceptions: list[dict] = []

    for v in store.vouchers():
        if v["vtype"] not in SETTLEMENT_TYPES:
            continue
        payload = json.loads(v["payload"] or "{}")
        for entry in parse_entries(payload):
            for bill in entry.get("bills") or []:
                btype = str(bill.get("type") or "").strip()
                bname = str(bill.get("name") or "").strip()
                if not bname:
                    continue
                rec = {
                    "settlement_guid": v["guid"],
                    "settlement_vtype": v["vtype"],
                    "settlement_doctype": v["erp_doctype"],
                    "settlement_name": v["erp_name"],
                    "party": entry["ledger"],
                    "bill_name": bname,
                    "bill_type": btype,
                    "amount": f"{money(bill.get('amount')):.2f}",
                }
                if btype in ("New Ref", "Advance"):
                    advances.append(rec)
                    continue
                if btype != "Agst Ref":
                    continue
                key = (_norm(entry["ledger"]), _norm(bname))
                staged_refs = list(store.get_bill_refs(entry["ledger"], bname))
                unique_invoices = []
                seen = set()
                for ref in staged_refs:
                    item = (ref["doctype"], ref["invoice"])
                    if item not in seen:
                        seen.add(item)
                        unique_invoices.append(
                            {"doctype": ref["doctype"], "name": ref["invoice"]})
                if not unique_invoices:
                    source_origins = origins.get(key) or []
                    loaded = [
                        o for o in source_origins if o.get("erp_name")
                    ]
                    if len(loaded) == 1:
                        unique_invoices = [{
                            "doctype": loaded[0]["erp_doctype"],
                            "name": loaded[0]["erp_name"],
                        }]
                    elif len(loaded) > 1:
                        exceptions.append({
                            **rec,
                            "reason": "ambiguous_bill_name",
                            "invoices": [
                                {"doctype": o["erp_doctype"], "name": o["erp_name"]}
                                for o in loaded
                            ],
                        })
                        continue
                    else:
                        exceptions.append({
                            **rec,
                            "reason": "no_invoice_capacity",
                            "source_origin_guids": [o["guid"] for o in source_origins],
                        })
                        continue
                if len(unique_invoices) > 1:
                    exceptions.append({
                        **rec,
                        "reason": "ambiguous_bill_name",
                        "invoices": unique_invoices,
                    })
                    continue
                if not rec["settlement_name"]:
                    exceptions.append({
                        **rec,
                        "reason": "settlement_not_loaded",
                    })
                    continue
                if rec["settlement_doctype"] not in ("Payment Entry", "Journal Entry"):
                    exceptions.append({
                        **rec,
                        "reason": "settlement_not_pe_or_je",
                    })
                    continue
                allocations.append({
                    **rec,
                    "invoice_doctype": unique_invoices[0]["doctype"],
                    "invoice_name": unique_invoices[0]["name"],
                })

    return {
        "mode": "guid-backed-bill-references",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "allocations": allocations,
        "unallocated_advances": advances,
        "exceptions": exceptions,
        "summary": {
            "allocations": len(allocations),
            "unallocated_advances": len(advances),
            "exceptions": len(exceptions),
            "exception_reasons": _count_by(exceptions, "reason"),
        },
    }


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[str(row.get(key) or "")] = out.get(str(row.get(key) or ""), 0) + 1
    return out


class GuidBillReferenceLoader:
    """Apply exact named Agst Ref links. Never FIFO. GL totals must not move."""

    def __init__(self, erp: ERPNextClient, store: Staging):
        self.erp = erp
        self.store = store
        self.defaults = fetch_company_defaults(erp)
        self.resolver = LedgerResolver(store, self.defaults)
        self.results: list[dict] = []

    def _party_type(self, ledger: str) -> str | None:
        res = self.resolver.get(ledger)
        if res and res.kind == "party":
            return res.party_type
        for party_type in ("Supplier", "Customer"):
            if self.resolver.get_party(ledger, party_type):
                return party_type
        return None

    def _base_doc(self, party_type: str, party: str) -> dict:
        account = (
            self.defaults.receivable if party_type == "Customer"
            else self.defaults.payable
        )
        return {
            "doctype": "Payment Reconciliation",
            "company": self.defaults.name,
            "party_type": party_type,
            "party": party,
            "receivable_payable_account": account,
        }

    def _fresh(self, party_type: str, party: str) -> dict:
        return self.erp.run_doc_method(
            "get_unreconciled_entries", self._base_doc(party_type, party))

    def _apply_one(self, row: dict) -> str:
        party_type = self._party_type(row["party"])
        if not party_type:
            return "unresolved_party"
        amount = money(row["amount"])
        doc = self._fresh(party_type, row["party"])
        invoices = {
            r["invoice_number"]: r for r in doc.get("invoices") or []
        }
        invoice = invoices.get(row["invoice_name"])
        payments = [
            p for p in (doc.get("payments") or [])
            if str(p.get("reference_type")) == row["settlement_doctype"]
            and str(p.get("reference_name")) == row["settlement_name"]
        ]
        if invoice is None:
            return "invoice_not_unreconciled"
        if money(invoice.get("outstanding_amount")) < amount:
            return "invoice_outstanding_short"
        if not payments:
            return "settlement_not_unreconciled"
        payment = payments[0]
        if money(payment.get("amount")) < amount:
            return "settlement_amount_short"
        invoice_arg = dict(invoice)
        invoice_arg["outstanding_amount"] = float(amount)
        invoice_arg["amount"] = float(amount)
        planned = self.erp.run_doc_method(
            "allocate_entries", doc,
            args={"invoices": [invoice_arg], "payments": [dict(payment)]},
        )
        rows = planned.get("allocation") or []
        if len(rows) != 1:
            return f"erpnext_returned_{len(rows)}_rows"
        got = rows[0]
        if (
            str(got.get("invoice_number")) != row["invoice_name"]
            or str(got.get("reference_name")) != row["settlement_name"]
            or money(got.get("allocated_amount")) != amount
        ):
            return "erpnext_allocation_mismatch"
        if not self.erp.dry_run:
            planned["allocation"] = [rows[0]]
            self.erp.run_doc_method("reconcile", planned)
        return "allocated" if not self.erp.dry_run else "validated"

    def run(self, plan: dict | None = None) -> dict:
        plan = plan or plan_guid_bill_references(self.store)
        gl_before = _gl_signature(self.erp, self.defaults.name)
        applied = 0
        skipped = 0
        errors = 0
        try:
            for row in plan["allocations"]:
                try:
                    status = self._apply_one(row)
                except (ERPNextError, RuntimeError) as exc:
                    status = f"error: {str(exc)[:400]}"
                    errors += 1
                else:
                    if status in {"allocated", "validated"}:
                        applied += 1
                    else:
                        skipped += 1
                self.results.append({**row, "status": status})
        finally:
            gl_after = _gl_signature(self.erp, self.defaults.name)
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "company": self.defaults.name,
                "mode": "dry-run" if self.erp.dry_run else "live",
                "plan_summary": plan.get("summary"),
                "exceptions": plan.get("exceptions") or [],
                "unallocated_advances": len(plan.get("unallocated_advances") or []),
                "applied": applied,
                "skipped": skipped,
                "errors": errors,
                "gl_before": gl_before,
                "gl_after": gl_after,
                "gl_unchanged": gl_after == gl_before,
                "results": self.results,
            }
            payload["pass"] = (
                errors == 0 and payload["gl_unchanged"]
            )
            report = (
                get_config().staging_db.parent / "reports"
                / "guid_bill_references.json"
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            payload["report"] = str(report)
        return payload
