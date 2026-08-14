"""Remove a guarded set of balance-neutral ledger-fidelity bridge JEs.

This is plan-only unless ``--confirm`` is supplied. Apply mode requires a fresh
database backup and exact expected count/turnover/net-reclassification guards.
It is intended for bridges made unnecessary by a configured canonical account
alias; it never edits invoices or payment allocations.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json
import os
from pathlib import Path


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY)


def discover_sites_path(site: str, cwd: Path | None = None) -> Path:
    cwd = (cwd or Path.cwd()).resolve()
    for candidate in (cwd / "sites", cwd):
        if (candidate / site / "site_config.json").is_file():
            return candidate
    raise RuntimeError(f"Cannot locate site {site!r} below {cwd}")


def gl_signature(frappe, company: str) -> dict:
    row = frappe.db.sql(
        """select count(*) as row_count, coalesce(sum(debit),0) as debit,
                  coalesce(sum(credit),0) as credit
             from `tabGL Entry`
            where company=%s and is_cancelled=0""",
        company,
        as_dict=True,
    )[0]
    return {
        "rows": int(row.row_count),
        "debit": f"{money(row.debit):.2f}",
        "credit": f"{money(row.credit):.2f}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--source-account", required=True)
    parser.add_argument("--canonical-account", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--expected-turnover", required=True)
    parser.add_argument("--expected-reclassification", required=True)
    parser.add_argument("--backup")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    if args.company != args.confirm_company:
        raise SystemExit("Confirmation does not exactly match company")
    backup = Path(args.backup).resolve() if args.backup else None
    if args.confirm and (
        backup is None or not backup.is_file() or backup.stat().st_size == 0
    ):
        raise SystemExit("--confirm requires a non-empty --backup file")

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        if not frappe.db.exists("Company", args.company):
            raise RuntimeError(f"Company does not exist: {args.company}")
        before = gl_signature(frappe, args.company)
        rows = frappe.get_all(
            "Journal Entry",
            filters={
                "company": args.company,
                "docstatus": 1,
                "posting_date": ["between", [args.from_date, args.to_date]],
                "tally_guid": ["like", "%:ledger-fidelity-bridge"],
            },
            fields=["name", "posting_date", "tally_guid", "total_debit"],
            order_by="posting_date, name",
            limit_page_length=0,
        )
        if len(rows) != args.expected_count:
            raise RuntimeError(
                f"bridge count drift: expected={args.expected_count} live={len(rows)}")

        turnover = Decimal("0.00")
        reclassification = Decimal("0.00")
        validated = []
        for row in rows:
            doc = frappe.get_doc("Journal Entry", row.name)
            if doc.docstatus != 1 or doc.company != args.company:
                raise RuntimeError(f"{row.name}: status/company drift")
            if doc.voucher_type != "Journal Entry":
                raise RuntimeError(f"{row.name}: unexpected voucher type")
            account_net = defaultdict(lambda: Decimal("0.00"))
            for line in doc.accounts:
                if line.party_type or line.party or line.reference_type or line.reference_name:
                    raise RuntimeError(f"{row.name}: bridge contains party/reference data")
                account_net[line.account] += (
                    money(line.debit_in_account_currency)
                    - money(line.credit_in_account_currency)
                )
            unexpected_net = {
                account: f"{value:.2f}" for account, value in account_net.items()
                if account not in (args.source_account, args.canonical_account) and value
            }
            if unexpected_net:
                raise RuntimeError(f"{row.name}: unexpected net rows {unexpected_net}")
            if account_net[args.source_account] != -account_net[args.canonical_account]:
                raise RuntimeError(f"{row.name}: canonical reclassification is unbalanced")
            turnover += money(doc.total_debit)
            reclassification += account_net[args.source_account]
            validated.append({
                "name": row.name,
                "posting_date": str(row.posting_date),
                "turnover": f"{money(doc.total_debit):.2f}",
                "source_account_net": f"{account_net[args.source_account]:.2f}",
            })

        if turnover != money(args.expected_turnover):
            raise RuntimeError(
                f"turnover drift: expected={money(args.expected_turnover)} live={turnover}")
        if reclassification != money(args.expected_reclassification):
            raise RuntimeError(
                "reclassification drift: "
                f"expected={money(args.expected_reclassification)} live={reclassification}")

        deleted = 0
        if args.confirm:
            for row in rows:
                doc = frappe.get_doc("Journal Entry", row.name)
                doc.flags.ignore_permissions = True
                doc.cancel()
                for doctype in ("GL Entry", "Payment Ledger Entry", "Stock Ledger Entry"):
                    frappe.db.delete(
                        doctype,
                        {"voucher_type": "Journal Entry", "voucher_no": row.name},
                    )
                frappe.delete_doc(
                    "Journal Entry", row.name, force=True, for_reload=True,
                    ignore_permissions=True, ignore_missing=False,
                    delete_permanently=True,
                )
                deleted += 1
            frappe.db.commit()

        after = gl_signature(frappe, args.company)
        remaining = frappe.db.count(
            "Journal Entry",
            {
                "company": args.company,
                "posting_date": ["between", [args.from_date, args.to_date]],
                "tally_guid": ["like", "%:ledger-fidelity-bridge"],
            },
        )
        expected_after_debit = money(before["debit"]) - turnover
        expected_after_credit = money(before["credit"]) - turnover
        passed = (
            not args.confirm
            or (
                deleted == args.expected_count
                and remaining == 0
                and money(after["debit"]) == expected_after_debit
                and money(after["credit"]) == expected_after_credit
            )
        )
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": not args.confirm,
            "backup": str(backup) if backup else None,
            "validated_count": len(validated),
            "turnover": f"{turnover:.2f}",
            "source_to_canonical_reclassification": f"{reclassification:.2f}",
            "documents": validated,
            "deleted": deleted,
            "remaining": int(remaining),
            "active_gl_before": before,
            "active_gl_after": after,
            "expected_active_gl_after": {
                "debit": f"{expected_after_debit:.2f}",
                "credit": f"{expected_after_credit:.2f}",
            },
            "pass": passed,
        }
        text = json.dumps(result, indent=2)
        print(text, flush=True)
        if args.report:
            Path(args.report).resolve().write_text(text + "\n", encoding="utf-8")
        return 0 if passed else 2
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
