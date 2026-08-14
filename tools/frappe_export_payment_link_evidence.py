"""Read-only export of Payment/Journal Entry evidence and invoice links.

Run with ``bench --site <site> execute`` or from ``bench console`` context via
the normal Python entry point.  The output is intended for offline three-way
comparison with staged Tally vouchers; it never writes to the site database.
"""
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


def _serialize_rows(rows, money_fields=()):
    result = []
    for row in rows:
        values = dict(row)
        for field in money_fields:
            if field in values:
                values[field] = money(values[field])
        for key, value in tuple(values.items()):
            if hasattr(value, "isoformat"):
                values[key] = value.isoformat()
        result.append(values)
    return result


def export_state(frappe, site: str, company: str) -> dict:
    payment_entries = frappe.db.sql(
        """select name, tally_guid, posting_date, party_type, party,
                  payment_type, paid_amount, received_amount,
                  unallocated_amount, reference_no, reference_date, remarks
             from `tabPayment Entry`
            where company=%s and docstatus=1
            order by posting_date, name""",
        company,
        as_dict=True,
    )
    payment_names = [row.name for row in payment_entries]
    payment_references = []
    if payment_names:
        payment_references = frappe.get_all(
            "Payment Entry Reference",
            filters={"parent": ["in", payment_names]},
            fields=[
                "parent", "reference_doctype", "reference_name",
                "total_amount", "outstanding_amount", "allocated_amount",
            ],
            order_by="parent, idx",
            limit_page_length=0,
        )

    journal_entries = frappe.db.sql(
        """select name, tally_guid, posting_date, voucher_type,
                  total_debit, total_credit, cheque_no, cheque_date,
                  user_remark, remark
             from `tabJournal Entry`
            where company=%s and docstatus=1
            order by posting_date, name""",
        company,
        as_dict=True,
    )
    journal_names = [row.name for row in journal_entries]
    journal_accounts = []
    if journal_names:
        journal_accounts = frappe.get_all(
            "Journal Entry Account",
            filters={"parent": ["in", journal_names]},
            fields=[
                "parent", "account", "party_type", "party",
                "reference_type", "reference_name", "against_account",
                "debit_in_account_currency", "credit_in_account_currency",
                "is_advance",
            ],
            order_by="parent, idx",
            limit_page_length=0,
        )

    return {
        "site": site,
        "company": company,
        "mode": "read-only",
        "payment_entries": _serialize_rows(
            payment_entries,
            ("paid_amount", "received_amount", "unallocated_amount"),
        ),
        "payment_references": _serialize_rows(
            payment_references,
            ("total_amount", "outstanding_amount", "allocated_amount"),
        ),
        "journal_entries": _serialize_rows(
            journal_entries,
            ("total_debit", "total_credit"),
        ),
        "journal_accounts": _serialize_rows(
            journal_accounts,
            ("debit_in_account_currency", "credit_in_account_currency"),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        payload = export_state(frappe, args.site, args.company)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "site": args.site,
            "payment_entries": len(payload["payment_entries"]),
            "payment_references": len(payload["payment_references"]),
            "journal_entries": len(payload["journal_entries"]),
            "journal_accounts": len(payload["journal_accounts"]),
            "output": str(output),
        }, indent=2))
        frappe.db.rollback()
        return 0
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
