"""Plan removal of ERPNext bill allocations contradicted by native Tally bills.

The plan is read-only and intentionally unlinks whole ERPNext payment/invoice
pairs.  A second, fresh exact-allocation plan restores only the reductions that
Tally proves.  This is safer than trying to edit submitted payment rows or
guessing a partial allocation in place.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.load_masters import fetch_company_defaults
from t2e.tally_bill_unreconciliation import (
    SUPPORTED_PAYMENT_TYPES,
    linked_payments,
    money,
)


def main() -> None:
    cfg = get_config()
    report_dir = cfg.staging_db.parent / "reports"
    source_path = report_dir / "invoice_outstanding_verification.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    groups = [
        row for row in source["differences"]
        if money(row.get("difference")) < 0
    ]
    erp = ERPNextClient(dry_run=True)
    defaults = fetch_company_defaults(erp)
    live_invoice_totals = {}
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        for row in erp.get_list(
            doctype,
            fields=[
                "name", "grand_total", "rounded_total",
                "disable_rounded_total", "outstanding_amount",
            ],
            filters=[
                ["company", "=", defaults.name], ["docstatus", "=", 1],
                ["tally_guid", "is", "set"],
            ],
            limit=0,
        ):
            total = (
                money(row.get("grand_total"))
                if int(row.get("disable_rounded_total") or 0)
                else money(row.get("rounded_total") or row.get("grand_total"))
            )
            live_invoice_totals[(doctype, row["name"])] = total
    selections = []
    details = []
    errors = []
    excluded = []
    for group in groups:
        required = -money(group["difference"])
        invoice_type = (
            "Sales Invoice" if group["direction"] == "Receivable"
            else "Purchase Invoice"
        )
        group_selections = []
        unsupported = []
        for invoice in group["erp_documents"]:
            for link in linked_payments(
                erp, defaults.name, invoice_type, invoice
            ):
                payment_type = str(link.get("reference_doctype") or "")
                row = {
                    "company": defaults.name,
                    "voucher_type": payment_type,
                    "voucher_no": str(link.get("reference_name") or ""),
                    "against_voucher_type": invoice_type,
                    "against_voucher_no": invoice,
                    "allocated_amount": f"{money(link.get('allocated_amount')):.2f}",
                }
                if payment_type in SUPPORTED_PAYMENT_TYPES:
                    group_selections.append(row)
                else:
                    unsupported.append(row)
        # One pair can surface twice only if the native report joined duplicate
        # rows; collapse it fail-closed and retain the live grouped amount.
        unique = {}
        for row in group_selections:
            key = (
                row["voucher_type"], row["voucher_no"],
                row["against_voucher_type"], row["against_voucher_no"],
            )
            unique[key] = row
        group_selections = list(unique.values())
        reset_amount = sum(
            (money(row["allocated_amount"]) for row in group_selections),
            Decimal("0.00"),
        )
        invoice_total = sum(
            (live_invoice_totals.get((invoice_type, name), Decimal("0.00"))
             for name in group["erp_documents"]),
            Decimal("0.00"),
        )
        current = money(group["erp_outstanding"])
        expected = money(group["expected_erp_outstanding"])
        native_exceeds_invoice_value = (
            reset_amount == 0
            and current >= invoice_total - Decimal("0.01")
            and expected > invoice_total + Decimal("0.01")
        )
        safe = reset_amount >= required or native_exceeds_invoice_value
        if native_exceeds_invoice_value:
            excluded.append({
                "party": group["party"],
                "bill_refs": group["bill_refs"],
                "erp_documents": group["erp_documents"],
                "invoice_total": f"{invoice_total:.2f}",
                "tally_native_outstanding": f"{expected:.2f}",
                "reason": (
                    "Tally native bill balance exceeds the full submitted "
                    "invoice value; there is no ERPNext allocation to unlink"
                ),
            })
        elif not safe:
            errors.append(
                f"{group['party']} {','.join(group['bill_refs'])}: "
                f"need {required:.2f}, supported linked amount {reset_amount:.2f}")
        if not native_exceeds_invoice_value:
            selections.extend(group_selections)
        details.append({
            "direction": group["direction"],
            "party": group["party"],
            "bill_refs": group["bill_refs"],
            "erp_documents": group["erp_documents"],
            "current_outstanding": group["erp_outstanding"],
            "tally_expected_outstanding": group["expected_erp_outstanding"],
            "required_increase": f"{required:.2f}",
            "supported_reset_amount": f"{reset_amount:.2f}",
            "submitted_invoice_total": f"{invoice_total:.2f}",
            "pairs": len(group_selections),
            "unsupported_links": unsupported,
            "classification": (
                "native_bill_exceeds_invoice_value"
                if native_exceeds_invoice_value else "resettable"
            ),
            "safe": safe,
        })
    payload = {
        "mode": "read-only",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "company": defaults.name,
        "source_report": str(source_path),
        "source_report_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "negative_difference_groups": len(groups),
        "resettable_groups": sum(
            row["classification"] == "resettable" for row in details),
        "excluded_unactionable_groups": len(excluded),
        "excluded_unactionable_details": excluded,
        "pairs": len(selections),
        "required_increase": f"{sum((-money(row['difference']) for row in groups), Decimal('0.00')):.2f}",
        "reset_amount": f"{sum((money(row['allocated_amount']) for row in selections), Decimal('0.00')):.2f}",
        "safe_to_apply": bool(groups) and not errors,
        "errors": errors,
        "details": details,
        "selections": selections,
    }
    output = report_dir / "tally_bill_unreconciliation_plan.json"
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({
        key: payload[key] for key in (
            "negative_difference_groups", "resettable_groups",
            "excluded_unactionable_groups", "pairs", "required_increase",
            "reset_amount", "safe_to_apply", "errors",
        )
    }, indent=2))
    print(f"REPORT {output}")


if __name__ == "__main__":
    main()
