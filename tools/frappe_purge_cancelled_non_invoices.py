"""Permanently purge verified cancelled non-invoice migration transactions.

This is the companion to ``frappe_purge_cancelled_invoices.py``.  It targets
only cancelled Payment Entry, Journal Entry, and Period Closing Voucher rows,
plus inactive derived ledger rows linked to that exact scope.  Run it with the
target bench's Python after taking a native site backup.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Iterable


ALL_TARGET_DOCTYPES = (
    "Sales Invoice",
    "Purchase Invoice",
    "Period Closing Voucher",
    "Journal Entry",
    "Payment Entry",
)
ACTIVE_REPLACEMENT_DOCTYPES = (
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Journal Entry",
    "Period Closing Voucher",
)


def chunks(values: list[str], size: int = 200) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def money(value) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--field", default="tally_guid")
    parser.add_argument("--expected-sales", type=int)
    parser.add_argument("--expected-purchase", type=int)
    parser.add_argument("--expected-payment", type=int)
    parser.add_argument("--expected-journal", type=int)
    parser.add_argument("--expected-pcv", type=int)
    parser.add_argument("--sites-path", default="/apps/frappe-bench/sites")
    parser.add_argument("--allow-untagged-name", action="append", default=[])
    parser.add_argument(
        "--allow-without-replacement-name", action="append", default=[])
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--confirm-delete-phrase", default="")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    if args.confirm_company != args.company:
        raise SystemExit("confirmation does not exactly match target company")
    requested = {
        "Sales Invoice": args.expected_sales,
        "Purchase Invoice": args.expected_purchase,
        "Payment Entry": args.expected_payment,
        "Journal Entry": args.expected_journal,
        "Period Closing Voucher": args.expected_pcv,
    }
    expected = {key: value for key, value in requested.items() if value is not None}
    target_doctypes = tuple(
        doctype for doctype in ALL_TARGET_DOCTYPES if doctype in expected)
    if not target_doctypes:
        raise SystemExit("at least one --expected-* scope is required")
    expected_total = sum(expected.values())
    required_phrase = f"DELETE {expected_total} CANCELLED MIGRATION TRANSACTIONS"
    if not args.plan_only and args.confirm_delete_phrase != required_phrase:
        raise SystemExit(
            f"refusing permanent deletion: --confirm-delete-phrase must be {required_phrase!r}")

    import frappe

    already_connected = bool(
        getattr(frappe.local, "initialised", False)
        and getattr(frappe.local, "db", None)
    )
    if not already_connected:
        frappe.init(
            site=args.site, sites_path=args.sites_path, force=True)
        frappe.connect()
    elif frappe.local.site != args.site:
        raise RuntimeError(
            f"connected site {frappe.local.site!r} is not requested site {args.site!r}")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        frappe.set_user("Administrator")
        if not frappe.db.exists("Company", args.company):
            raise RuntimeError(
                f"target company {args.company!r} does not exist on {args.site}")

        scopes: dict[str, list[dict]] = {}
        all_rows: list[tuple[str, dict]] = []
        for doctype in target_doctypes:
            if not frappe.get_meta(doctype).has_field(args.field):
                raise RuntimeError(f"{doctype} has no {args.field!r} field")
            rows = frappe.get_all(
                doctype,
                filters={"company": args.company, "docstatus": 2},
                fields=["name", args.field, "docstatus"],
                order_by="name asc",
                limit_page_length=0,
            )
            if len(rows) != expected[doctype]:
                raise RuntimeError(
                    f"{doctype} count changed: expected {expected[doctype]}, got {len(rows)}")
            scopes[doctype] = rows
            all_rows.extend((doctype, row) for row in rows)

        names = [row["name"] for _, row in all_rows]
        actual_untagged = {
            row["name"] for _, row in all_rows if not row.get(args.field)
        }
        allowed_untagged = set(args.allow_untagged_name)
        if actual_untagged != allowed_untagged:
            raise RuntimeError(
                "untagged scope changed: "
                f"expected {sorted(allowed_untagged)!r}, got {sorted(actual_untagged)!r}")

        allowed_without_replacement = set(args.allow_without_replacement_name)
        observed_without_replacement = set()
        replacement_routes = Counter()
        invalid_replacements = []
        for source_doctype, row in all_rows:
            guid = str(row.get(args.field) or "")
            if not guid:
                continue
            targets = []
            for target_doctype in ACTIVE_REPLACEMENT_DOCTYPES:
                target = frappe.db.get_value(
                    target_doctype,
                    {args.field: guid, "company": args.company,
                     "docstatus": ["!=", 2]},
                    "name",
                )
                if target:
                    targets.append((target_doctype, target))
            if len(targets) == 1:
                replacement_routes[f"{source_doctype} -> {targets[0][0]}"] += 1
                continue

            # A superseded derived bridge may legitimately have no document
            # with the derived identity while its base source voucher remains
            # active.  These suffixes are the only inspectable derived keys.
            base_targets = []
            derived_suffix = None
            for suffix in (":party-control-bridge", ":ledger-fidelity-bridge"):
                if guid.endswith(suffix):
                    derived_suffix = suffix
                    break
            if derived_suffix:
                base_guid = guid.removesuffix(derived_suffix)
                for target_doctype in ACTIVE_REPLACEMENT_DOCTYPES:
                    target = frappe.db.get_value(
                        target_doctype,
                        {args.field: base_guid, "company": args.company,
                         "docstatus": ["!=", 2]},
                        "name",
                    )
                    if target:
                        base_targets.append((target_doctype, target))
            if not targets and len(base_targets) == 1:
                replacement_routes[
                    f"{source_doctype} bridge -> {base_targets[0][0]}"] += 1
                continue

            if not targets and row["name"] in allowed_without_replacement:
                observed_without_replacement.add(row["name"])
                continue
            invalid_replacements.append({
                "doctype": source_doctype,
                "name": row["name"],
                "guid": guid,
                "active_targets": targets,
                "base_targets": base_targets,
            })
        if invalid_replacements:
            raise RuntimeError(
                "invalid replacement closure: "
                + json.dumps(invalid_replacements[:20], ensure_ascii=False))
        if observed_without_replacement != allowed_without_replacement:
            raise RuntimeError(
                "replacement exception scope changed: expected "
                f"{sorted(allowed_without_replacement)!r}, got "
                f"{sorted(observed_without_replacement)!r}")

        derived_names: dict[str, set[str]] = {
            "GL Entry": set(),
            "Payment Ledger Entry": set(),
            "Stock Ledger Entry": set(),
        }
        active_links = Counter()
        for batch in chunks(names):
            gl_rows = frappe.get_all(
                "GL Entry",
                filters={"voucher_no": ["in", batch]},
                fields=["name", "is_cancelled"],
                limit_page_length=0,
            )
            derived_names["GL Entry"].update(row["name"] for row in gl_rows)
            active_links["GL Entry"] += sum(
                int(row.get("is_cancelled") or 0) == 0 for row in gl_rows)

            ple_by_name = {}
            for link_field in ("voucher_no", "against_voucher_no"):
                for row in frappe.get_all(
                    "Payment Ledger Entry",
                    filters={link_field: ["in", batch]},
                    fields=["name", "delinked"],
                    limit_page_length=0,
                ):
                    ple_by_name[row["name"]] = row
            derived_names["Payment Ledger Entry"].update(ple_by_name)
            active_links["Payment Ledger Entry"] += sum(
                int(row.get("delinked") or 0) == 0 for row in ple_by_name.values())

            if frappe.get_meta("Stock Ledger Entry").has_field("voucher_no"):
                sle_rows = frappe.get_all(
                    "Stock Ledger Entry",
                    filters={"voucher_no": ["in", batch]},
                    fields=["name", "is_cancelled"],
                    limit_page_length=0,
                )
                derived_names["Stock Ledger Entry"].update(
                    row["name"] for row in sle_rows)
                active_links["Stock Ledger Entry"] += sum(
                    int(row.get("is_cancelled") or 0) == 0 for row in sle_rows)
        active_links = Counter({k: v for k, v in active_links.items() if v})
        if active_links:
            raise RuntimeError(
                "active ledger rows reference purge scope: "
                + json.dumps(dict(active_links)))

        before_gl = frappe.db.sql(
            """select coalesce(sum(debit),0), coalesce(sum(credit),0), count(*)
               from `tabGL Entry`
               where company=%s and is_cancelled=0""",
            args.company,
        )[0]
        before_active = {
            doctype: frappe.db.count(
                doctype, {"company": args.company, "docstatus": ["!=", 2]})
            for doctype in target_doctypes
        }
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": args.plan_only,
            "scope": {doctype: len(rows) for doctype, rows in scopes.items()},
            "untagged_allowlist": sorted(allowed_untagged),
            "without_replacement_allowlist": sorted(
                allowed_without_replacement),
            "replacement_routes": dict(replacement_routes),
            "derived_inactive_rows": {
                doctype: len(values) for doctype, values in derived_names.items()
            },
            "active_gl_before": {
                "debit": money(before_gl[0]),
                "credit": money(before_gl[1]),
                "rows": int(before_gl[2]),
            },
            "active_counts_before": before_active,
        }
        report_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        if args.plan_only:
            return 0

        deleted_derived = Counter()
        for doctype, values in derived_names.items():
            for batch in chunks(sorted(values)):
                frappe.db.delete(doctype, {"name": ["in", batch]})
                deleted_derived[doctype] += len(batch)
                frappe.db.commit()
            print(f"deleted inactive {doctype}: {len(values)}", flush=True)

        deleted_parents = Counter()
        for doctype in target_doctypes:
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
                deleted_parents[doctype] += 1
                if index % 100 == 0:
                    frappe.db.commit()
                    print(f"delete {doctype}: {index}/{len(rows)}", flush=True)
            frappe.db.commit()

        remaining = {
            doctype: frappe.db.count(
                doctype, {"company": args.company, "docstatus": 2})
            for doctype in target_doctypes
        }
        after_active = {
            doctype: frappe.db.count(
                doctype, {"company": args.company, "docstatus": ["!=", 2]})
            for doctype in target_doctypes
        }
        after_gl = frappe.db.sql(
            """select coalesce(sum(debit),0), coalesce(sum(credit),0), count(*)
               from `tabGL Entry`
               where company=%s and is_cancelled=0""",
            args.company,
        )[0]
        after_gl_summary = {
            "debit": money(after_gl[0]),
            "credit": money(after_gl[1]),
            "rows": int(after_gl[2]),
        }
        result.update({
            "deleted_derived_rows": dict(deleted_derived),
            "deleted_parents": dict(deleted_parents),
            "remaining_cancelled": remaining,
            "active_counts_after": after_active,
            "active_gl_after": after_gl_summary,
            "pass": (
                not any(remaining.values())
                and after_active == before_active
                and after_gl_summary == result["active_gl_before"]
            ),
        })
        report_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        return 0 if result["pass"] else 2
    finally:
        if not already_connected:
            frappe.destroy()


if __name__ == "__main__":
    sys.exit(main())
