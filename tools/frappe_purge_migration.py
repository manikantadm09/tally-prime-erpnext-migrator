"""Server-side purge of migration-tagged ERPNext records.

Run with the target bench's Python, not the migration virtualenv.  This exists
because Frappe's REST DELETE endpoint cannot force-delete cancelled accounting
documents while their cancelled GL/Payment Ledger rows still dynamically link
to the parent.  The script:

1. proves the exact site, company and migration field;
2. selects only records carrying that field (and company-scopes accounting
   doctypes);
3. cancels submitted transactions through their normal controllers;
4. deletes only derived ledger rows whose voucher type/name is in that verified
   transaction set;
5. force-deletes the now-cancelled tagged parents and tagged masters.

Take a native site backup before running this script.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Iterable


TXN_DOCTYPES = [
    "Period Closing Voucher",
    "Journal Entry",
    "Payment Entry",
    "Sales Invoice",
    "Purchase Invoice",
]
MASTER_DOCTYPES = [
    "Address",
    "Contact",
    "Customer",
    "Supplier",
    "Item",
    "Cost Center",
    "Account",
]
COMPANY_SCOPED = set(TXN_DOCTYPES + ["Cost Center", "Account"])
DERIVED_LEDGER_DOCTYPES = [
    "GL Entry",
    "Payment Ledger Entry",
    "Stock Ledger Entry",
]


def chunks(values: list[str], size: int = 200) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--field", default="tally_guid")
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--keep-masters", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()

    if args.confirm_company != args.company:
        raise SystemExit("Confirmation does not exactly match target company")

    import frappe

    already_connected = bool(
        getattr(frappe.local, "initialised", False)
        and getattr(frappe.local, "db", None)
    )
    if not already_connected:
        sites_path = Path.cwd() / "sites"
        frappe.init(site=args.site, sites_path=str(sites_path), force=True)
        frappe.connect()
    elif frappe.local.site != args.site:
        raise RuntimeError(
            f"Connected console site {frappe.local.site!r} is not "
            f"requested site {args.site!r}")
    try:
        frappe.set_user("Administrator")
        if not frappe.db.exists("Company", args.company):
            raise RuntimeError(
                f"Target company {args.company!r} does not exist on {args.site}")

        scopes: dict[str, list[dict]] = {}
        counts = Counter()
        for doctype in TXN_DOCTYPES + MASTER_DOCTYPES:
            meta = frappe.get_meta(doctype)
            if not meta.has_field(args.field):
                scopes[doctype] = []
                print(
                    f"scope {doctype}: 0 (field {args.field!r} absent)",
                    flush=True)
                continue
            filters: dict = {args.field: ["is", "set"]}
            if doctype in COMPANY_SCOPED:
                filters["company"] = args.company
            fields = ["name"]
            if doctype in TXN_DOCTYPES:
                fields.append("docstatus")
            rows = frappe.get_all(
                doctype,
                filters=filters,
                fields=fields,
                order_by="name asc",
                limit_page_length=0,
            )
            scopes[doctype] = rows
            counts[f"scope:{doctype}"] = len(rows)
            print(f"scope {doctype}: {len(rows)}", flush=True)

        if args.plan_only:
            result = {
                "site": args.site,
                "company": args.company,
                "field": args.field,
                "scoped": {
                    key.removeprefix("scope:"): value
                    for key, value in counts.items()
                },
                "plan_only": True,
                "keep_masters": args.keep_masters,
                "pass": True,
            }
            print(json.dumps(result, indent=2), flush=True)
            return 0

        # Normal cancellation first. Do not remove ledger rows or parents unless
        # every submitted migration transaction cancels successfully.
        cancel_errors = []
        cancelled = Counter()
        for doctype in TXN_DOCTYPES:
            rows = scopes[doctype]
            for index, row in enumerate(rows, 1):
                if int(row.get("docstatus") or 0) != 1:
                    continue
                try:
                    doc = frappe.get_doc(doctype, row["name"])
                    doc.flags.ignore_permissions = True
                    doc.cancel()
                    cancelled[doctype] += 1
                    frappe.db.commit()
                except Exception as exc:
                    frappe.db.rollback()
                    cancel_errors.append({
                        "doctype": doctype,
                        "name": row["name"],
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    break
                if index % 100 == 0:
                    print(
                        f"cancel {doctype}: {index}/{len(rows)}",
                        flush=True)
            if cancel_errors:
                break
        if cancel_errors:
            raise RuntimeError(
                "Cancellation failed; derived rows and parents were not purged: "
                + json.dumps(cancel_errors, ensure_ascii=False))

        remaining_submitted = {}
        for doctype in TXN_DOCTYPES:
            names = [r["name"] for r in scopes[doctype]]
            submitted = 0
            for batch in chunks(names):
                submitted += frappe.db.count(
                    doctype, {"name": ["in", batch], "docstatus": 1})
            if submitted:
                remaining_submitted[doctype] = submitted
        if remaining_submitted:
            raise RuntimeError(
                "Submitted migration transactions remain after cancellation: "
                + json.dumps(remaining_submitted))

        # Remove only derived ledger rows belonging to the verified tagged
        # transaction names. This leaves unrelated company ledger rows intact.
        derived_deleted = Counter()
        for doctype in TXN_DOCTYPES:
            names = [r["name"] for r in scopes[doctype]]
            for batch in chunks(names):
                for ledger_doctype in DERIVED_LEDGER_DOCTYPES:
                    meta = frappe.get_meta(ledger_doctype)
                    if not (
                        meta.has_field("voucher_type")
                        and meta.has_field("voucher_no")
                    ):
                        continue
                    before = frappe.db.count(
                        ledger_doctype,
                        {"voucher_type": doctype, "voucher_no": ["in", batch]})
                    if before:
                        frappe.db.delete(
                            ledger_doctype,
                            {"voucher_type": doctype,
                             "voucher_no": ["in", batch]})
                        derived_deleted[ledger_doctype] += before
                frappe.db.commit()
            print(
                f"derived ledger purge for {doctype}: {len(names)} parents",
                flush=True)

        deleted = Counter()
        for doctype in TXN_DOCTYPES:
            rows = scopes[doctype]
            for index, row in enumerate(rows, 1):
                frappe.delete_doc(
                    doctype,
                    row["name"],
                    force=True,
                    # The normal delete path enqueues one dynamic-link cleanup
                    # job per parent and overloads the queue for thousands of
                    # migration records. All dynamic-linking transaction
                    # parents are in this verified purge scope, and their
                    # derived ledger rows were explicitly removed above.
                    for_reload=True,
                    ignore_permissions=True,
                    ignore_missing=True,
                    delete_permanently=True,
                )
                deleted[doctype] += 1
                if index % 100 == 0:
                    frappe.db.commit()
                    print(
                        f"delete {doctype}: {index}/{len(rows)}",
                        flush=True)
            frappe.db.commit()

        if not args.keep_masters:
            # Never delete a tagged Account that still participates in any GL
            # row after the verified transaction ledger purge.
            account_names = [r["name"] for r in scopes["Account"]]
            external_account_links = 0
            for batch in chunks(account_names):
                external_account_links += frappe.db.count(
                    "GL Entry", {"account": ["in", batch]})
            if external_account_links:
                raise RuntimeError(
                    f"Refusing tagged Account purge: {external_account_links} "
                    "GL Entries still reference the scoped accounts")

            for doctype in MASTER_DOCTYPES:
                rows = scopes[doctype]
                for index, row in enumerate(rows, 1):
                    frappe.delete_doc(
                        doctype,
                        row["name"],
                        force=True,
                        for_reload=True,
                        ignore_permissions=True,
                        ignore_missing=True,
                        delete_permanently=True,
                    )
                    deleted[doctype] += 1
                    if index % 100 == 0:
                        frappe.db.commit()
                        print(
                            f"delete {doctype}: {index}/{len(rows)}",
                            flush=True)
                frappe.db.commit()

        remaining = {}
        verify_doctypes = (
            TXN_DOCTYPES
            if args.keep_masters
            else TXN_DOCTYPES + MASTER_DOCTYPES
        )
        for doctype in verify_doctypes:
            meta = frappe.get_meta(doctype)
            if not meta.has_field(args.field):
                continue
            filters: dict = {args.field: ["is", "set"]}
            if doctype in COMPANY_SCOPED:
                filters["company"] = args.company
            count = frappe.db.count(doctype, filters)
            if count:
                remaining[doctype] = count
        result = {
            "site": args.site,
            "company": args.company,
            "field": args.field,
            "scoped": {
                key.removeprefix("scope:"): value
                for key, value in counts.items()
            },
            "cancelled": dict(cancelled),
            "derived_deleted": dict(derived_deleted),
            "deleted": dict(deleted),
            "masters_preserved": args.keep_masters,
            "remaining": remaining,
            "pass": not remaining,
        }
        text = json.dumps(result, indent=2, ensure_ascii=False)
        print(text, flush=True)
        if args.report:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(text + "\n", encoding="utf-8")
        return 0 if result["pass"] else 2
    finally:
        if not already_connected:
            frappe.destroy()


if __name__ == "__main__":
    sys.exit(main())
