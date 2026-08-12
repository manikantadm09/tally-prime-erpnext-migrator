"""Read-only export of invoice and Payment Ledger state for site comparison."""
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


def export_state(frappe, site: str, company: str) -> dict:
    invoices = []
    for doctype, party_field, account_field in (
        ("Sales Invoice", "customer", "debit_to"),
        ("Purchase Invoice", "supplier", "credit_to"),
    ):
        rows = frappe.get_all(
            doctype,
            filters={
                "company": company,
                "docstatus": 1,
                "tally_guid": ["is", "set"],
            },
            fields=[
                "name", "tally_guid", "posting_date", "status",
                "outstanding_amount", "grand_total", "rounded_total",
                "is_return", party_field, account_field,
            ],
            limit_page_length=0,
        )
        for row in rows:
            invoices.append({
                "doctype": doctype,
                "name": row.name,
                "tally_guid": row.tally_guid,
                "posting_date": str(row.posting_date),
                "status": row.status,
                "outstanding_amount": money(row.outstanding_amount),
                "grand_total": money(row.grand_total),
                "rounded_total": money(row.rounded_total),
                "is_return": int(row.is_return or 0),
                "party": row.get(party_field),
                "account": row.get(account_field),
            })

    transactions = []
    for doctype, fields in (
        ("Payment Entry", [
            "name", "tally_guid", "posting_date", "party_type", "party",
            "payment_type", "paid_amount", "received_amount",
            "unallocated_amount", "status",
        ]),
        ("Journal Entry", [
            "name", "tally_guid", "posting_date", "voucher_type",
            "total_debit", "total_credit",
        ]),
    ):
        rows = frappe.get_all(
            doctype,
            filters={"company": company, "docstatus": 1},
            fields=fields,
            limit_page_length=0,
        )
        for row in rows:
            values = {field: row.get(field) for field in fields}
            for field in (
                "paid_amount", "received_amount", "unallocated_amount",
                "total_debit", "total_credit",
            ):
                if field in values:
                    values[field] = money(values[field])
            values["posting_date"] = str(values["posting_date"])
            transactions.append({"doctype": doctype, **values})

    payment_ledger = frappe.db.sql(
        """select name, posting_date, account, party_type, party,
                  voucher_type, voucher_no,
                  against_voucher_type, against_voucher_no,
                  amount, amount_in_account_currency, delinked
             from `tabPayment Ledger Entry`
            where company=%s and delinked=0
              and (voucher_type in ('Sales Invoice', 'Purchase Invoice',
                                    'Payment Entry', 'Journal Entry')
                   or against_voucher_type in ('Sales Invoice', 'Purchase Invoice'))
            order by posting_date, creation, name""",
        company,
        as_dict=True,
    )
    ple_rows = []
    for row in payment_ledger:
        ple_rows.append({
            **{key: row.get(key) for key in (
                "name", "account", "party_type", "party", "voucher_type",
                "voucher_no", "against_voucher_type", "against_voucher_no",
            )},
            "posting_date": str(row.posting_date),
            "amount": money(row.amount),
            "amount_in_account_currency": money(row.amount_in_account_currency),
            "delinked": int(row.delinked or 0),
        })

    gl = frappe.db.sql(
        """select count(*) as row_count, coalesce(sum(debit), 0) as debit,
                  coalesce(sum(credit), 0) as credit
             from `tabGL Entry`
            where company=%s and is_cancelled=0""",
        company,
        as_dict=True,
    )[0]
    return {
        "site": site,
        "company": company,
        "mode": "read-only",
        "invoices": invoices,
        "transactions": transactions,
        "payment_ledger": ple_rows,
        "active_gl": {
            "rows": int(gl.row_count),
            "debit": money(gl.debit),
            "credit": money(gl.credit),
        },
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
            "company": args.company,
            "invoices": len(payload["invoices"]),
            "transactions": len(payload["transactions"]),
            "active_payment_ledger_rows": len(payload["payment_ledger"]),
            "active_gl": payload["active_gl"],
            "output": str(output),
        }, indent=2))
        frappe.db.rollback()
        return 0
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
