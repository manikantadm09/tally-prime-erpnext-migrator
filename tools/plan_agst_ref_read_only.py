"""Read-only Agst Ref plan from live Tally staging + native bills + ERPNext.

Maps only source-proven payment/journal-to-invoice links. Does not FIFO, does
not call Payment Reconciliation, and does not write to ERPNext. Optional Tally
vouchers are excluded. Ambiguous shared bill names are listed, not guessed.
"""
from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.lines import parse_entries
from t2e.staging import Staging
from tools.verify_bill_outstandings_api import latest_native_report, parse_tally_bills


PENNY = Decimal("0.01")
INVOICE_TYPES = {"Sales", "Purchase", "Credit Note", "Debit Note"}
SETTLEMENT_TYPES = {"Receipt", "Payment", "Journal", "Contra"}
OPTIONAL_GUIDS = {
    "78f56868-5614-4b27-86f5-c41d61c95e4d-0000046c",
    "78f56868-5614-4b27-86f5-c41d61c95e4d-000013cd",
}


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def is_optional(payload: dict, guid: str) -> bool:
    if guid in OPTIONAL_GUIDS:
        return True
    flag = str(payload.get("ISOPTIONAL") or "").strip().casefold()
    return flag in {"yes", "y", "1", "true"}


def main() -> int:
    cfg = get_config()
    reports = cfg.staging_db.parent / "reports"
    raw = cfg.staging_db.parent / "raw"
    staging_path = cfg.staging_db.parent / "full_audit.sqlite"
    store = Staging(staging_path)

    native = {
        "Receivable": parse_tally_bills(latest_native_report(raw, "bills-receivable")),
        "Payable": parse_tally_bills(latest_native_report(raw, "bills-payable")),
    }

    erp = ERPNextClient(dry_run=True)
    company = cfg.erpnext["company"]
    live: dict[str, dict] = {}
    for doctype, extra in (
        ("Sales Invoice", ["outstanding_amount", "status", "customer"]),
        ("Purchase Invoice", ["outstanding_amount", "status", "supplier"]),
        ("Payment Entry", ["unallocated_amount", "paid_amount", "party", "payment_type"]),
        ("Journal Entry", ["total_debit"]),
    ):
        rows = erp.get_list(
            doctype,
            fields=["name", "tally_guid", "posting_date", "docstatus", *extra],
            filters=[["company", "=", company], ["docstatus", "=", 1],
                     ["tally_guid", "is", "set"]],
            limit=0,
        )
        for row in rows:
            live[str(row["tally_guid"])] = {**row, "doctype": doctype}

    origins: dict[tuple[str, str], list[dict]] = defaultdict(list)
    settlements: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped_optional = 0
    voucher_rows = [dict(row) for row in store.vouchers()]
    store.close()

    for row in voucher_rows:
        payload = json.loads(row["payload"])
        if is_optional(payload, row["guid"]):
            skipped_optional += 1
            continue
        party_hint = norm(row.get("party") or "")
        for entry in parse_entries(payload):
            ledger_key = norm(entry["ledger"])
            for bill in entry["bills"]:
                bill_type = str(bill.get("type") or "").strip()
                bill_name = str(bill.get("name") or "").strip()
                if not bill_name or not bill_type:
                    continue
                key = (ledger_key, norm(bill_name))
                rec = {
                    "guid": row["guid"],
                    "vtype": row["vtype"],
                    "vnumber": row["vnumber"],
                    "vdate": row["vdate"],
                    "party": entry["ledger"],
                    "voucher_party": row.get("party") or "",
                    "bill_name": bill_name,
                    "bill_type": bill_type,
                    "amount": f"{money(bill.get('amount')):.2f}",
                    "signed_amount": f"{money(bill.get('signed_amount')):.2f}",
                    "erp": live.get(row["guid"]),
                }
                if bill_type in ("New Ref", "Advance"):
                    origins[key].append(rec)
                elif bill_type == "Agst Ref":
                    settlements[key].append(rec)

    proven = []
    ambiguous = []
    advances = []
    impossible = []

    invoice_origins: dict[tuple[str, str], list[dict]] = {}
    advance_origins: dict[tuple[str, str], list[dict]] = {}
    for key, rows in origins.items():
        inv = [r for r in rows if r["vtype"] in INVOICE_TYPES]
        adv = [r for r in rows if r["vtype"] in SETTLEMENT_TYPES]
        if inv:
            invoice_origins[key] = inv
        if adv:
            advance_origins[key] = adv

    consumed_advance_keys = set()
    for key, agst_rows in settlements.items():
        invs = invoice_origins.get(key, [])
        erp_invoices = [r for r in invs if r.get("erp") and r["erp"]["doctype"] in
                        ("Sales Invoice", "Purchase Invoice")]
        unique_invoices = {(r["erp"]["doctype"], r["erp"]["name"]) for r in erp_invoices}
        sett_docs = [
            r for r in agst_rows
            if r["vtype"] in SETTLEMENT_TYPES and r.get("erp")
            and r["erp"]["doctype"] in ("Payment Entry", "Journal Entry")
        ]
        if len(unique_invoices) > 1:
            ambiguous.append({
                "party": (invs or agst_rows)[0]["party"],
                "bill_ref": (invs or agst_rows)[0]["bill_name"],
                "invoice_count": len(unique_invoices),
                "invoices": [
                    {"doctype": d, "name": n}
                    for d, n in sorted(unique_invoices)
                ],
                "settlements": [
                    {"doctype": r["erp"]["doctype"], "name": r["erp"]["name"],
                     "tally_vtype": r["vtype"], "vnumber": r["vnumber"],
                     "amount": r["amount"]}
                    for r in sett_docs
                ],
                "reason": "Shared Tally bill name maps to more than one ERP invoice; not allocated",
            })
            continue
        if unique_invoices and sett_docs:
            invoice = erp_invoices[0]
            for sett in sett_docs:
                proven.append({
                    "party": invoice["party"],
                    "bill_ref": invoice["bill_name"],
                    "invoice_doctype": invoice["erp"]["doctype"],
                    "invoice": invoice["erp"]["name"],
                    "invoice_guid": invoice["guid"],
                    "invoice_outstanding": f"{money(invoice['erp'].get('outstanding_amount')):.2f}",
                    "settlement_doctype": sett["erp"]["doctype"],
                    "settlement": sett["erp"]["name"],
                    "settlement_guid": sett["guid"],
                    "tally_vtype": sett["vtype"],
                    "tally_vnumber": sett["vnumber"],
                    "tally_date": sett["vdate"],
                    "agst_ref_amount": sett["amount"],
                    "source_proven": True,
                })
            continue
        if unique_invoices and not sett_docs:
            if agst_rows:
                impossible.append({
                    "kind": "agst_ref_without_erp_settlement",
                    "party": unique_invoices and invoice_origins[key][0]["party"],
                    "bill_ref": invoice_origins[key][0]["bill_name"],
                    "invoice": erp_invoices[0]["erp"]["name"] if erp_invoices else None,
                    "tally_settlements": [
                        {"vtype": r["vtype"], "vnumber": r["vnumber"],
                         "guid": r["guid"], "amount": r["amount"],
                         "erp": None if not r.get("erp") else r["erp"]["name"]}
                        for r in agst_rows
                    ],
                    "reason": "Tally Agst Ref exists but settlement GUID is not a submitted PE/JE",
                })
            continue
        if not unique_invoices and agst_rows:
            if key in advance_origins:
                consumed_advance_keys.add(key)
                advances.append({
                    "kind": "advance_consumed_by_agst_ref_not_an_invoice",
                    "party": agst_rows[0]["party"],
                    "bill_ref": agst_rows[0]["bill_name"],
                    "advance_origins": [
                        {"vtype": r["vtype"], "vnumber": r["vnumber"],
                         "guid": r["guid"], "erp": (r.get("erp") or {}).get("name")}
                        for r in advance_origins[key]
                    ],
                    "agst_refs": [
                        {"vtype": r["vtype"], "vnumber": r["vnumber"],
                         "amount": r["amount"]}
                        for r in agst_rows
                    ],
                    "reason": "Tally Advance/New Ref on a bank voucher, never an invoice New Ref",
                })
            else:
                impossible.append({
                    "kind": "agst_ref_with_no_invoice_or_advance_origin",
                    "party": agst_rows[0]["party"],
                    "bill_ref": agst_rows[0]["bill_name"],
                    "agst_refs": [
                        {"vtype": r["vtype"], "vnumber": r["vnumber"],
                         "guid": r["guid"], "amount": r["amount"],
                         "erp": (r.get("erp") or {}).get("name")}
                        for r in agst_rows
                    ],
                    "reason": "Tally Agst Ref has no New Ref/Advance origin in non-optional vouchers",
                })

    for key, rows in advance_origins.items():
        if key in consumed_advance_keys or key in invoice_origins:
            continue
        if key in settlements:
            continue
        advances.append({
            "kind": "unconsumed_tally_advance_or_on_account",
            "party": rows[0]["party"],
            "bill_ref": rows[0]["bill_name"],
            "origins": [
                {"vtype": r["vtype"], "vnumber": r["vnumber"], "vdate": r["vdate"],
                 "guid": r["guid"], "amount": r["amount"],
                 "erp_doctype": (r.get("erp") or {}).get("doctype"),
                 "erp": (r.get("erp") or {}).get("name")}
                for r in rows
            ],
            "reason": "Tally New Ref/Advance on a settlement voucher with no later Agst Ref",
        })

    native_impossible = []
    invoice_ref_keys = set(invoice_origins)
    for direction, bills in native.items():
        for bill in bills:
            key = (norm(bill["party"]), norm(bill["bill_ref"]))
            if key in invoice_ref_keys:
                continue
            native_impossible.append({
                "kind": "native_bill_without_migrated_invoice_new_ref",
                "direction": direction,
                "party": bill["party"],
                "bill_ref": bill["bill_ref"],
                "tally_outstanding": f"{money(bill['amount']):.2f}",
                "bill_date": bill.get("bill_date") or "",
                "reason": (
                    "Native Tally outstanding bill has no Sales/Purchase New Ref "
                    "in the non-optional voucher extract; do not invent a payment"
                ),
            })

    proven_amount = sum((money(r["agst_ref_amount"]) for r in proven), Decimal("0.00"))
    payload = {
        "mode": "read-only",
        "writes": False,
        "fifo": False,
        "company": company,
        "staging": str(staging_path),
        "skipped_optional_vouchers": skipped_optional,
        "summary": {
            "proven_agst_ref_links": len(proven),
            "proven_agst_ref_amount": f"{proven_amount:.2f}",
            "ambiguous_shared_bill_names": len(ambiguous),
            "true_advances_or_on_account": len(advances),
            "impossible_agst_ref_or_origin": len(impossible),
            "native_bills_without_invoice_new_ref": len(native_impossible),
            "native_bills_receivable": len(native["Receivable"]),
            "native_bills_payable": len(native["Payable"]),
        },
        "proven_links": proven,
        "ambiguous_shared_bill_names": ambiguous,
        "true_advances": advances,
        "impossible_source_refs": impossible + native_impossible,
        "policy": {
            "optional_vouchers": "excluded",
            "do_not_fifo": True,
            "do_not_invent_payments": True,
            "do_not_equate_erp_overdue_to_tally_bills": True,
            "keep_unlinked_advances_unallocated": True,
        },
    }
    out = reports / "agst_ref_read_only_plan.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    print(f"REPORT {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
