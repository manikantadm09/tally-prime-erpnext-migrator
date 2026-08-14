"""Read-only export of submitted invoice fields, child rows, and GL postings.

Run with a Frappe bench Python interpreter.  The export is intended for
cross-site migration audits and performs no writes.
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        documents = []
        for doctype, party_field in (
            ("Sales Invoice", "customer"),
            ("Purchase Invoice", "supplier"),
        ):
            names = frappe.get_all(
                doctype,
                filters={
                    "company": args.company,
                    "docstatus": 1,
                    "tally_guid": ["is", "set"],
                },
                pluck="name",
                limit_page_length=0,
            )
            for name in names:
                doc = frappe.get_doc(doctype, name)
                gl_rows = frappe.get_all(
                    "GL Entry",
                    filters={
                        "company": args.company,
                        "voucher_type": doctype,
                        "voucher_no": name,
                        "is_cancelled": 0,
                    },
                    fields=[
                        "account", "party_type", "party", "debit", "credit",
                        "debit_in_account_currency",
                        "credit_in_account_currency", "remarks",
                    ],
                    limit_page_length=0,
                )
                documents.append({
                    "doctype": doctype,
                    "name": name,
                    "tally_guid": doc.tally_guid,
                    "posting_date": str(doc.posting_date),
                    "due_date": str(doc.due_date),
                    "party": doc.get(party_field),
                    "status": doc.status,
                    "is_return": int(doc.is_return or 0),
                    "grand_total": money(doc.grand_total),
                    "rounded_total": money(doc.rounded_total),
                    "outstanding_amount": money(doc.outstanding_amount),
                    "disable_rounded_total": int(doc.disable_rounded_total or 0),
                    "remarks": doc.remarks or "",
                    "bill_no": doc.get("bill_no") or "",
                    "bill_date": str(doc.get("bill_date") or ""),
                    "tally_supplier_invoice_no": (
                        doc.get("tally_supplier_invoice_no") or ""
                    ),
                    "company_gstin": doc.get("company_gstin") or "",
                    "party_gstin": (
                        doc.get("billing_address_gstin")
                        if doctype == "Sales Invoice"
                        else doc.get("supplier_gstin")
                    ) or "",
                    "place_of_supply": doc.get("place_of_supply") or "",
                    "items": [
                        {
                            "item_code": row.item_code,
                            "item_name": row.item_name,
                            "description": row.description,
                            "qty": str(row.qty),
                            "rate": money(row.rate),
                            "amount": money(row.amount),
                            "net_amount": money(row.net_amount),
                            "account": (
                                row.get("income_account")
                                or row.get("expense_account")
                                or ""
                            ),
                            "gst_hsn_code": row.get("gst_hsn_code") or "",
                            "cgst_amount": money(row.get("cgst_amount")),
                            "sgst_amount": money(row.get("sgst_amount")),
                            "igst_amount": money(row.get("igst_amount")),
                        }
                        for row in doc.items
                    ],
                    "taxes": [
                        {
                            "charge_type": row.charge_type,
                            "account_head": row.account_head,
                            "description": row.description,
                            "rate": str(row.rate),
                            "tax_amount": money(row.tax_amount),
                            "total": money(row.total),
                        }
                        for row in doc.taxes
                    ],
                    "gl": [
                        {
                            **{
                                key: row.get(key) or ""
                                for key in (
                                    "account", "party_type", "party",
                                    "remarks",
                                )
                            },
                            "debit": money(row.debit),
                            "credit": money(row.credit),
                            "debit_in_account_currency": money(
                                row.debit_in_account_currency
                            ),
                            "credit_in_account_currency": money(
                                row.credit_in_account_currency
                            ),
                        }
                        for row in gl_rows
                    ],
                })

        payload = {
            "site": args.site,
            "company": args.company,
            "mode": "read-only",
            "documents": documents,
        }
        output = Path(args.output).resolve()
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "site": args.site,
            "company": args.company,
            "documents": len(documents),
            "gl_rows": sum(len(row["gl"]) for row in documents),
            "output": str(output),
        }, indent=2))
        frappe.db.rollback()
        return 0
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
