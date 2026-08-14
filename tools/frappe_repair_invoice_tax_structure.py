"""Restore tax-as-item Purchase Invoices to standard ERPNext tax rows.

This is a narrowly guarded production repair for invoices produced by the
loader's old value-preserving fallback.  It is plan-only unless ``--confirm``
is supplied.  Apply mode requires a fresh database backup and an audit
manifest produced by ``tools.audit_invoice_tax_structure_api``.

The repair is metadata-only: it removes the exact tax item children, inserts
equivalent ``Purchase Taxes and Charges`` children, and corrects invoice header
net/tax totals.  It never recreates GL or Payment Ledger Entries, changes the
grand total, or changes the outstanding amount.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import re


PENNY = Decimal("0.01")
PARENT_FIELDS = (
    "total", "base_total", "net_total", "base_net_total",
    "total_taxes_and_charges", "base_total_taxes_and_charges",
    "taxes_and_charges_added", "base_taxes_and_charges_added",
    "taxes_and_charges_deducted", "base_taxes_and_charges_deducted",
    "grand_total", "base_grand_total", "rounded_total",
    "base_rounded_total", "rounding_adjustment", "base_rounding_adjustment",
    "outstanding_amount", "total_qty",
)


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def tax_rate(value) -> Decimal:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(value or ""))
    return Decimal(match.group(1)) if match else Decimal("0")


def discover_sites_path(site: str, cwd: Path | None = None) -> Path:
    cwd = (cwd or Path.cwd()).resolve()
    for candidate in (cwd / "sites", cwd):
        if (candidate / site / "site_config.json").is_file():
            return candidate
    raise RuntimeError(f"Cannot locate site {site!r} below {cwd}")


def gl_signature(frappe, company: str) -> dict:
    row = frappe.db.sql(
        """select count(*) row_count, coalesce(sum(debit),0) debit,
                  coalesce(sum(credit),0) credit
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


def _manifest_rows(path: Path, expected_count: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("mode") != "read-only":
        raise RuntimeError("manifest is not a read-only audit report")
    rows = [
        row for row in payload.get("details") or []
        if row.get("classification") == "taxes_as_items"
    ]
    if len(rows) != expected_count:
        raise RuntimeError(
            f"manifest scope drift: expected={expected_count} actual={len(rows)}")
    if len({row.get("guid") for row in rows}) != len(rows):
        raise RuntimeError("manifest contains duplicate GUIDs")
    return rows


def _expected_counter(row: dict) -> Counter:
    result = Counter()
    for tax in row.get("expected_taxes") or []:
        result[(norm(tax.get("ledger")), money(tax.get("amount")))] += int(
            tax.get("count") or 0)
    return result


def _tax_item_counter(items: list) -> Counter:
    return Counter(
        (norm(item.item_name or item.description), money(item.net_amount))
        for item in items
    )


def _snapshot(doc) -> dict:
    return {
        "name": doc.name,
        "guid": doc.tally_guid,
        "posting_date": str(doc.posting_date),
        "grand_total": f"{money(doc.grand_total):.2f}",
        "net_total": f"{money(doc.net_total):.2f}",
        "tax_total": f"{money(doc.total_taxes_and_charges):.2f}",
        "outstanding_amount": f"{money(doc.outstanding_amount):.2f}",
        "status": doc.status,
        "items": len(doc.items),
        "taxes": len(doc.taxes),
    }


def _plan_invoice(frappe, company: str, row: dict) -> dict:
    if row.get("doctype") != "Purchase Invoice" or row.get("source_type") != "Purchase":
        raise RuntimeError(f"{row.get('guid')}: unsupported document/source type")
    doc = frappe.get_doc("Purchase Invoice", row["name"])
    if doc.company != company or doc.docstatus != 1:
        raise RuntimeError(f"{doc.name}: company/docstatus drift")
    if doc.tally_guid != row["guid"]:
        raise RuntimeError(f"{doc.name}: tally_guid drift")
    if doc.taxes:
        raise RuntimeError(f"{doc.name}: tax rows already exist")
    if money(doc.total_taxes_and_charges):
        raise RuntimeError(f"{doc.name}: header tax total is no longer zero")
    if money(doc.discount_amount) or money(doc.additional_discount_percentage):
        raise RuntimeError(f"{doc.name}: discounts require manual review")
    if money(doc.conversion_rate) != Decimal("1.00"):
        raise RuntimeError(f"{doc.name}: non-INR conversion requires manual review")

    expected = _expected_counter(row)
    expected_names = {name for name, _ in expected}
    tax_items = [
        item for item in doc.items
        if norm(item.item_name or item.description) in expected_names
    ]
    if _tax_item_counter(tax_items) != expected:
        raise RuntimeError(f"{doc.name}: tax item signature drift")
    retained = [item for item in doc.items if item.name not in {x.name for x in tax_items}]
    if not retained:
        raise RuntimeError(f"{doc.name}: repair would leave no invoice items")

    tax_total = sum((money(item.net_amount) for item in tax_items), Decimal("0.00"))
    base_tax_total = sum(
        (money(item.base_net_amount) for item in tax_items), Decimal("0.00"))
    net_total = sum((money(item.net_amount) for item in retained), Decimal("0.00"))
    base_net_total = sum(
        (money(item.base_net_amount) for item in retained), Decimal("0.00"))
    # ERPNext's grand_total is pre-rounding.  rounding_adjustment is the
    # separate delta from grand_total to rounded_total.
    grand = net_total + tax_total
    base_grand = base_net_total + base_tax_total
    if grand != money(doc.grand_total) or base_grand != money(doc.base_grand_total):
        raise RuntimeError(
            f"{doc.name}: reconstructed grand total does not match "
            f"net={net_total} tax={tax_total} rounding={money(doc.rounding_adjustment)} "
            f"reconstructed={grand} live={money(doc.grand_total)} "
            f"base_net={base_net_total} base_tax={base_tax_total} "
            f"base_rounding={money(doc.base_rounding_adjustment)} "
            f"base_reconstructed={base_grand} base_live={money(doc.base_grand_total)}"
        )
    if net_total + tax_total != money(doc.net_total):
        raise RuntimeError(f"{doc.name}: current tax-as-item net signature drift")

    taxes = []
    cumulative = net_total
    base_cumulative = base_net_total
    for item in sorted(tax_items, key=lambda value: value.idx):
        amount = money(item.net_amount)
        base_amount = money(item.base_net_amount)
        cumulative += amount
        base_cumulative += base_amount
        if not item.expense_account:
            raise RuntimeError(f"{doc.name}/{item.name}: missing expense account")
        taxes.append({
            "source_item_name": item.name,
            "description": str(item.item_name or item.description)[:140],
            "account_head": item.expense_account,
            "cost_center": item.cost_center,
            "rate": tax_rate(item.item_name or item.description),
            "tax_amount": amount,
            "base_tax_amount": base_amount,
            "total": cumulative,
            "base_total": base_cumulative,
        })
    return {
        "doc": doc,
        "before": _snapshot(doc),
        "retained_items": retained,
        "tax_items": tax_items,
        "taxes": taxes,
        "totals": {
            "net_total": net_total,
            "base_net_total": base_net_total,
            "tax_total": tax_total,
            "base_tax_total": base_tax_total,
            "grand_total": money(doc.grand_total),
            "base_grand_total": money(doc.base_grand_total),
            "total_qty": sum(
                (Decimal(str(item.qty or 0)) for item in retained), Decimal("0")
            ),
        },
    }


def _apply_invoice(frappe, plan: dict) -> None:
    doc = plan["doc"]
    totals = plan["totals"]
    for item in plan["tax_items"]:
        frappe.db.delete("Purchase Invoice Item", {"name": item.name})

    for idx, tax in enumerate(plan["taxes"], 1):
        account_currency = frappe.get_cached_value(
            "Account", tax["account_head"], "account_currency")
        child = frappe.get_doc({
            "doctype": "Purchase Taxes and Charges",
            "parent": doc.name,
            "parenttype": "Purchase Invoice",
            "parentfield": "taxes",
            "idx": idx,
            "category": "Total",
            "add_deduct_tax": "Add",
            "charge_type": "Actual",
            "account_head": tax["account_head"],
            "description": tax["description"],
            "included_in_print_rate": 0,
            "included_in_paid_amount": 0,
            "cost_center": tax["cost_center"],
            "account_currency": account_currency,
            "rate": tax["rate"],
            "tax_amount": tax["tax_amount"],
            "tax_amount_after_discount_amount": tax["tax_amount"],
            "base_tax_amount": tax["base_tax_amount"],
            "base_tax_amount_after_discount_amount": tax["base_tax_amount"],
            "total": tax["total"],
            "base_total": tax["base_total"],
        })
        child.flags.ignore_permissions = True
        child.db_insert()

    values = {
        "total": totals["net_total"],
        "base_total": totals["base_net_total"],
        "net_total": totals["net_total"],
        "base_net_total": totals["base_net_total"],
        "total_taxes_and_charges": totals["tax_total"],
        "base_total_taxes_and_charges": totals["base_tax_total"],
        "taxes_and_charges_added": totals["tax_total"],
        "base_taxes_and_charges_added": totals["base_tax_total"],
        "taxes_and_charges_deducted": 0,
        "base_taxes_and_charges_deducted": 0,
        "total_qty": totals["total_qty"],
    }
    frappe.db.set_value(
        "Purchase Invoice", doc.name, values,
        update_modified=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--backup")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    if args.company != args.confirm_company:
        raise SystemExit("Confirmation does not exactly match company")
    manifest = Path(args.manifest).resolve()
    if not manifest.is_file():
        raise SystemExit(f"manifest does not exist: {manifest}")
    backup = Path(args.backup).resolve() if args.backup else None
    if args.confirm and (
        backup is None or not backup.is_file() or backup.stat().st_size == 0
    ):
        raise SystemExit("--confirm requires a non-empty --backup file")
    manifest_rows = _manifest_rows(manifest, args.expected_count)

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        if not frappe.db.exists("Company", args.company):
            raise RuntimeError(f"Company does not exist: {args.company}")
        gl_before = gl_signature(frappe, args.company)
        plans = [_plan_invoice(frappe, args.company, row) for row in manifest_rows]
        if args.confirm:
            for plan in plans:
                _apply_invoice(frappe, plan)
            frappe.db.commit()

        documents = []
        for plan in plans:
            doc = frappe.get_doc("Purchase Invoice", plan["doc"].name)
            after = _snapshot(doc)
            expected = _expected_counter(next(
                row for row in manifest_rows if row["guid"] == doc.tally_guid
            ))
            actual = Counter(
                (norm(tax.description or tax.account_head), money(tax.tax_amount))
                for tax in doc.taxes
            )
            tax_item_names = {name for name, _ in expected}
            remaining_tax_items = [
                item.name for item in doc.items
                if norm(item.item_name or item.description) in tax_item_names
            ]
            passed = (
                (not args.confirm)
                or (
                    actual == expected
                    and not remaining_tax_items
                    and after["grand_total"] == plan["before"]["grand_total"]
                    and after["outstanding_amount"] == plan["before"]["outstanding_amount"]
                )
            )
            documents.append({
                "name": doc.name,
                "guid": doc.tally_guid,
                "before": plan["before"],
                "after": after,
                "planned_tax_rows": [
                    {key: (f"{value:.2f}" if isinstance(value, Decimal) else value)
                     for key, value in tax.items() if key != "source_item_name"}
                    for tax in plan["taxes"]
                ],
                "remaining_tax_items": remaining_tax_items,
                "pass": passed,
            })

        gl_after = gl_signature(frappe, args.company)
        passed = (
            len(plans) == args.expected_count
            and all(row["pass"] for row in documents)
            and (not args.confirm or gl_after == gl_before)
        )
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": not args.confirm,
            "backup": str(backup) if backup else None,
            "validated_count": len(plans),
            "documents": documents,
            "active_gl_before": gl_before,
            "active_gl_after": gl_after,
            "pass": passed,
        }
        text = json.dumps(result, indent=2, default=str)
        shown = result
        if args.summary_only:
            shown = {key: value for key, value in result.items() if key != "documents"}
        print(json.dumps(shown, indent=2, default=str), flush=True)
        if args.report:
            Path(args.report).resolve().write_text(text + "\n", encoding="utf-8")
        if not args.confirm:
            frappe.db.rollback()
        return 0 if passed else 2
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
