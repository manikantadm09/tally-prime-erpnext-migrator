"""Read-only production GL/document health snapshot for a fiscal period."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path


def money(value) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"


def discover_sites_path(site: str, cwd: Path | None = None) -> Path:
    cwd = (cwd or Path.cwd()).resolve()
    for candidate in (cwd / "sites", cwd):
        if (candidate / site / "site_config.json").is_file():
            return candidate
    raise RuntimeError(f"Cannot locate site {site!r} below {cwd}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    args = parser.parse_args()

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        gl = frappe.db.sql(
            """select count(*) row_count, coalesce(sum(debit),0) debit,
                      coalesce(sum(credit),0) credit
                 from `tabGL Entry`
                where company=%s and is_cancelled=0
                  and posting_date between %s and %s""",
            (args.company, args.from_date, args.to_date),
            as_dict=True,
        )[0]
        doctypes = {}
        for doctype in (
            "Sales Invoice", "Purchase Invoice", "Journal Entry",
            "Payment Entry", "Period Closing Voucher",
        ):
            doctypes[doctype] = {
                "submitted": frappe.db.count(
                    doctype, {"company": args.company, "docstatus": 1}),
                "cancelled": frappe.db.count(
                    doctype, {"company": args.company, "docstatus": 2}),
            }
        result = {
            "site": args.site,
            "company": args.company,
            "period": {"from": args.from_date, "to": args.to_date},
            "active_gl": {
                "rows": int(gl.row_count),
                "debit": money(gl.debit),
                "credit": money(gl.credit),
                "balanced": money(gl.debit) == money(gl.credit),
            },
            "documents": doctypes,
            "ledger_fidelity_bridges": {
                label: frappe.db.count(
                    "Journal Entry",
                    {"company": args.company, "docstatus": status,
                     "tally_guid": ["like", "%:ledger-fidelity-bridge"]},
                )
                for label, status in (
                    ("draft", 0), ("submitted", 1), ("cancelled", 2))
            },
        }
        print(json.dumps(result, indent=2))
        frappe.db.rollback()
        return 0
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
