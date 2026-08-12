"""Server-side purge of six cancelled Payment Reconciliation return JEs.

Run with the target bench Python after taking a fresh site backup. The scope is
hardcoded to the production documents created and then cancelled on 2026-08-12.
The script is plan-only unless ``--confirm`` is supplied.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path


EXPECTED = {
    "ACC-JV-2026-11600": ("1242677.00", {"SINV-26-00090", "SRET-26-00006"}),
    "ACC-JV-2026-11601": ("390630.00", {"SINV-26-00091", "SRET-26-00005"}),
    "ACC-JV-2026-11602": ("16567.00", {"PINV-26-01249", "PRET-26-00009"}),
    "ACC-JV-2026-11603": ("124.00", {"PINV-26-01381", "PRET-26-00010"}),
    "ACC-JV-2026-11604": ("16107.00", {"PINV-26-01398", "PRET-26-00010"}),
    "ACC-JV-2026-11605": ("76225.00", {"PINV-26-01491", "PRET-26-00011"}),
}


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def active_gl_signature(frappe, company: str) -> dict:
    row = frappe.db.sql(
        """select count(*) as rows, coalesce(sum(debit), 0) as debit,
                  coalesce(sum(credit), 0) as credit
             from `tabGL Entry`
            where company=%s and is_cancelled=0""",
        company, as_dict=True,
    )[0]
    return {
        "rows": int(row.rows),
        "debit": f"{money(row.debit):.2f}",
        "credit": f"{money(row.credit):.2f}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    if args.company != args.confirm_company:
        raise SystemExit("Confirmation does not exactly match company")

    import frappe

    already_connected = bool(
        getattr(frappe.local, "initialised", False)
        and getattr(frappe.local, "db", None))
    if not already_connected:
        frappe.init(site=args.site, sites_path=str(Path.cwd() / "sites"), force=True)
        frappe.connect()
    elif frappe.local.site != args.site:
        raise RuntimeError(
            f"Connected site {frappe.local.site!r} is not {args.site!r}")
    try:
        if not frappe.db.exists("Company", args.company):
            raise RuntimeError(f"Company does not exist: {args.company}")
        before = active_gl_signature(frappe, args.company)
        validated = []
        for name, (expected_amount, expected_refs) in EXPECTED.items():
            doc = frappe.get_doc("Journal Entry", name)
            if doc.company != args.company or doc.docstatus != 2 or doc.tally_guid:
                raise RuntimeError(f"{name}: company/status/tag validation failed")
            if money(doc.total_debit) != money(expected_amount):
                raise RuntimeError(f"{name}: amount validation failed")
            active = frappe.db.count(
                "GL Entry", {"voucher_type": "Journal Entry",
                             "voucher_no": name, "is_cancelled": 0})
            gl = frappe.get_all(
                "GL Entry",
                filters={"voucher_type": "Journal Entry", "voucher_no": name,
                         "is_cancelled": 1},
                fields=["account", "party_type", "party", "against_voucher"],
                limit_page_length=0,
            )
            refs = {str(row.against_voucher or "") for row in gl}
            signatures = {(row.account, row.party_type, row.party) for row in gl}
            if active or len(gl) != 4 or len(signatures) != 1 or refs != expected_refs:
                raise RuntimeError(f"{name}: inactive ledger shape validation failed")
            validated.append({
                "name": name, "amount": expected_amount,
                "references": sorted(expected_refs),
                "gl_rows": len(gl),
                "payment_ledger_rows": frappe.db.count(
                    "Payment Ledger Entry",
                    {"voucher_type": "Journal Entry", "voucher_no": name}),
            })

        deleted_derived = {"GL Entry": 0, "Payment Ledger Entry": 0,
                           "Stock Ledger Entry": 0}
        if args.confirm:
            for name in EXPECTED:
                for doctype in deleted_derived:
                    count = frappe.db.count(
                        doctype, {"voucher_type": "Journal Entry", "voucher_no": name})
                    if count:
                        frappe.db.delete(
                            doctype,
                            {"voucher_type": "Journal Entry", "voucher_no": name})
                        deleted_derived[doctype] += count
                frappe.delete_doc(
                    "Journal Entry", name, force=True, for_reload=True,
                    ignore_permissions=True, ignore_missing=False,
                    delete_permanently=True,
                )
                frappe.db.commit()

        after = active_gl_signature(frappe, args.company)
        remaining = [name for name in EXPECTED
                     if frappe.db.exists("Journal Entry", name)]
        passed = before == after and (not args.confirm or not remaining)
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": not args.confirm,
            "validated": validated,
            "deleted_derived": deleted_derived,
            "deleted_parents": 0 if not args.confirm else len(EXPECTED) - len(remaining),
            "remaining": remaining,
            "active_gl_before": before,
            "active_gl_after": after,
            "pass": passed,
        }
        text = json.dumps(result, indent=2)
        print(text, flush=True)
        if args.report:
            Path(args.report).write_text(text + "\n", encoding="utf-8")
        return 0 if passed else 2
    finally:
        if not already_connected:
            frappe.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
