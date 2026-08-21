"""Repair one proved submitted Sales Invoice GST metadata defect.

Plan-only by default.  The confirmed path is intentionally bound to the exact
site, company, invoice, Tally GUID, linked Address GSTIN, and before/after
metadata values.  It updates no accounting field and rolls back unless the
invoice totals and its complete GL row signature remain unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


SITE = "dev.spaceki.com"
COMPANY = "Spaceki Designs LLP"
INVOICE = "SINV-26-00153"
TALLY_GUID = "78f56868-5614-4b27-86f5-c41d61c95e4d-000007a0"
GSTIN = "29AAXCS8027K1ZD"
BEFORE = {"billing_address_gstin": None, "gst_category": "Unregistered"}
AFTER = {"billing_address_gstin": GSTIN, "gst_category": "Registered Regular"}
TOTAL_FIELDS = (
    "net_total", "total_taxes_and_charges", "grand_total",
    "rounding_adjustment", "rounded_total", "outstanding_amount",
)


def discover_sites_path(site: str, cwd: Path) -> Path:
    for candidate in (cwd, cwd / "sites", cwd.parent / "sites"):
        if (candidate / "apps.txt").is_file() and (candidate / site).is_dir():
            return candidate.resolve()
    raise RuntimeError(f"could not locate sites directory for {site}")


def gl_signature(frappe) -> list[dict]:
    rows = frappe.db.sql(
        """select name, posting_date, account, party_type, party,
                  debit, credit, is_cancelled
             from `tabGL Entry`
            where voucher_type='Sales Invoice' and voucher_no=%s
            order by name""",
        INVOICE,
        as_dict=True,
    )
    return [
        {
            **{key: row.get(key) for key in (
                "name", "account", "party_type", "party", "is_cancelled")},
            "posting_date": str(row.posting_date),
            "debit": str(row.debit),
            "credit": str(row.credit),
        }
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default=SITE)
    parser.add_argument("--backup", help="existing pre-change database backup")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.site != SITE:
        raise RuntimeError(f"script is bound to {SITE}")
    if args.confirm:
        if not args.backup:
            raise RuntimeError("--backup is required with --confirm")
        backup = Path(args.backup)
        if not backup.is_file() or not backup.name.endswith("database.sql.gz"):
            raise RuntimeError(f"database backup is missing or invalid: {backup}")

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        doc = frappe.get_doc("Sales Invoice", INVOICE)
        current = {key: doc.get(key) for key in BEFORE}
        if doc.company != COMPANY or doc.docstatus != 1:
            raise RuntimeError("invoice identity, company, or docstatus drift")
        if doc.get("tally_guid") != TALLY_GUID:
            raise RuntimeError("invoice Tally GUID drift")
        if current != BEFORE:
            raise RuntimeError(f"invoice metadata drift: {current!r}")
        address_gstin = frappe.db.get_value("Address", doc.customer_address, "gstin")
        if address_gstin != GSTIN:
            raise RuntimeError(f"linked Address GSTIN drift: {address_gstin!r}")

        totals_before = {key: str(doc.get(key)) for key in TOTAL_FIELDS}
        gl_before = gl_signature(frappe)
        if args.confirm:
            frappe.db.set_value(
                "Sales Invoice", INVOICE, AFTER, update_modified=False)
            frappe.clear_cache(doctype="Sales Invoice")

        after_doc = frappe.get_doc("Sales Invoice", INVOICE)
        current_after = {key: after_doc.get(key) for key in AFTER}
        totals_after = {key: str(after_doc.get(key)) for key in TOTAL_FIELDS}
        gl_after = gl_signature(frappe)
        expected_after = AFTER if args.confirm else BEFORE
        passed = (
            current_after == expected_after
            and totals_after == totals_before
            and gl_after == gl_before
        )
        if not passed:
            frappe.db.rollback()
            raise RuntimeError("post-update invariant failed; transaction rolled back")
        if args.confirm:
            frappe.db.commit()
        else:
            frappe.db.rollback()

        report = {
            "site": args.site,
            "company": COMPANY,
            "invoice": INVOICE,
            "mode": "live" if args.confirm else "plan-only",
            "backup": args.backup,
            "before": BEFORE,
            "after": current_after,
            "totals_before": totals_before,
            "totals_after": totals_after,
            "gl_rows_before": len(gl_before),
            "gl_rows_after": len(gl_after),
            "gl_unchanged": gl_before == gl_after,
            "pass": passed,
        }
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
