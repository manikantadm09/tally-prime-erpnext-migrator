"""Stamp item GST rates on UAT invoices where Tally tax rupees already posted.

India Compliance leaves migrated Actual tax rows at rate 0 and marks items
Nil-Rated, so Desk GST breakup shows 0% even though the Taxes table has the
Tally amounts. This writes only item GST display fields (and gst_tax_type on
tax rows). Grand total, outstanding, and GL must stay unchanged.

Bound to site dev-site.local. Plan-only unless --confirm.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t2e.load_invoices import _allocate_money, _gst_kind, _tax_rate  # noqa: E402


PENNY = Decimal("0.01")
SITE = "dev-site.local"
COMPANY = "Spaceki Designs LLP"
GST_KINDS = ("cgst", "sgst", "igst", "cess")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY)


def discover_sites_path(site: str, cwd: Path) -> Path:
    for candidate in (cwd / "sites", cwd):
        if (candidate / site / "site_config.json").is_file():
            return candidate.resolve()
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


def invoice_names(frappe, doctype: str, company: str) -> list[str]:
    tax_dt = (
        "Purchase Taxes and Charges" if doctype == "Purchase Invoice"
        else "Sales Taxes and Charges"
    )
    item_dt = f"{doctype} Item"
    names = {row.name for row in frappe.db.sql(
        f"""select distinct p.name
              from `tab{doctype}` p
              join `tab{tax_dt}` t on t.parent=p.name
             where p.docstatus=1 and p.company=%s
               and ifnull(t.tax_amount,0) <> 0""",
        company,
        as_dict=True,
    )}
    names.update(row.name for row in frappe.db.sql(
        f"""select distinct p.name
              from `tab{doctype}` p
              join `tab{item_dt}` i on i.parent=p.name
             where p.docstatus=1 and p.company=%s
               and (i.item_name regexp 'CGST|SGST|IGST|CESS'
                    or i.description regexp 'CGST|SGST|IGST|CESS')""",
        company,
        as_dict=True,
    ))
    return sorted(names)


def grouped_gst(doc) -> dict[str, list]:
    grouped: dict[str, list] = defaultdict(list)
    for tax in doc.taxes or []:
        if not money(tax.tax_amount):
            continue
        kind = _gst_kind(tax.description or tax.account_head)
        if not kind:
            continue
        grouped[kind].append(tax)
    return grouped


HALF_SLABS = (
    Decimal("2.50"), Decimal("6.00"), Decimal("9.00"), Decimal("14.00"),
)
FULL_SLABS = (
    Decimal("5.00"), Decimal("12.00"), Decimal("18.00"), Decimal("28.00"),
)


def _single_kind_rate(rows) -> Decimal | None:
    """One GST kind may have several Actual rows at the same parsed rate."""
    rates = [_tax_rate(row.description or row.account_head) for row in rows]
    if not rates or any(rate <= 0 for rate in rates):
        return None
    unique = {money(rate) for rate in rates}
    if len(unique) != 1:
        return None
    return money(rates[0])


def _nearest_slab(implied: Decimal, slabs: tuple[Decimal, ...]) -> Decimal | None:
    for slab in slabs:
        if abs(implied - slab) <= Decimal("0.05"):
            return slab
    return None


def _kind_rate(kind: str, rows, item_base: Decimal) -> Decimal | None:
    parsed = [_tax_rate(row.description or row.account_head) for row in rows]
    if any(rate > 0 for rate in parsed):
        return _single_kind_rate(rows)
    total = sum((money(row.tax_amount) for row in rows), Decimal("0"))
    if item_base <= 0 or total <= 0:
        return None
    implied = total * Decimal("100") / item_base
    slabs = FULL_SLABS if kind == "igst" else HALF_SLABS
    return _nearest_slab(implied, slabs)


def _is_gst_item(item) -> bool:
    return bool(_gst_kind(item.item_name or item.description or ""))


def _stamp_values(goods, kind_rates, allocations, account_rates) -> list[dict]:
    weights = [abs(money(item.net_amount)) for item in goods]
    item_values = []
    for index, item in enumerate(goods):
        values = {
            "gst_treatment": "Taxable",
            "taxable_value": float(weights[index]),
            "item_tax_rate": json.dumps(
                {account: float(rate) for account, rate in account_rates.items()},
                sort_keys=True,
            ),
        }
        for kind in GST_KINDS:
            values[f"{kind}_rate"] = 0
            values[f"{kind}_amount"] = 0
        for kind in kind_rates:
            values[f"{kind}_rate"] = float(kind_rates[kind])
            values[f"{kind}_amount"] = float(allocations[kind][index])
        item_values.append({"name": item.name, "values": values})
    return item_values


def _already_stamped(goods, kind_rates) -> bool:
    first = next(iter(kind_rates))
    return all(
        item.gst_treatment == "Taxable"
        and all(
            money(item.get(f"{kind}_rate")) == money(kind_rates[kind])
            for kind in kind_rates
        )
        and money(item.get(f"{first}_amount"))
        for item in goods
    )


def plan_tax_as_items(doc, items) -> dict:
    tax_items = [item for item in items if _is_gst_item(item)]
    goods = [item for item in items if not _is_gst_item(item)]
    if not tax_items or not goods:
        return {"status": "skip", "reason": "no_gst_tax_rows"}
    grouped: dict[str, list] = defaultdict(list)
    for item in tax_items:
        kind = _gst_kind(item.item_name or item.description)
        if kind:
            grouped[kind].append(item)
    kind_rates = {}
    for kind, rows in grouped.items():
        rates = [_tax_rate(row.item_name or row.description) for row in rows]
        if not rates or any(rate <= 0 for rate in rates):
            return {"status": "skip", "reason": "multi_rate_or_unparsed"}
        unique = {money(rate) for rate in rates}
        if len(unique) != 1:
            return {"status": "skip", "reason": "multi_rate_or_unparsed"}
        kind_rates[kind] = money(rates[0])
    if _already_stamped(goods, kind_rates):
        return {"status": "skip", "reason": "already_stamped"}
    weights = [abs(money(item.net_amount)) for item in goods]
    allocations = {
        kind: _allocate_money(
            sum((money(row.net_amount) for row in rows), Decimal("0")),
            weights,
        )
        for kind, rows in grouped.items()
    }
    account_rates = {}
    for kind, rows in grouped.items():
        row = rows[0]
        account = (
            row.get("expense_account")
            or row.get("income_account")
            or row.item_name
        )
        account_rates[account] = kind_rates[kind]
    return {
        "status": "stamp",
        "grand_total": f"{money(doc.grand_total):.2f}",
        "outstanding": f"{money(doc.outstanding_amount):.2f}",
        "items": _stamp_values(goods, kind_rates, allocations, account_rates),
        "taxes": [],
        "kinds": {kind: float(rate) for kind, rate in kind_rates.items()},
    }


def plan_doc(doc) -> dict | None:
    items = [item for item in doc.items if money(item.net_amount)]
    if not items:
        return {"status": "skip", "reason": "no_item_base"}
    grouped = grouped_gst(doc)
    if not grouped:
        return plan_tax_as_items(doc, items)
    item_base = sum((abs(money(item.net_amount)) for item in items), Decimal("0"))
    kind_rates = {
        kind: _kind_rate(kind, rows, item_base) for kind, rows in grouped.items()
    }
    if any(rate is None for rate in kind_rates.values()):
        return {"status": "skip", "reason": "multi_rate_or_unparsed"}
    if _already_stamped(items, kind_rates):
        return {"status": "skip", "reason": "already_stamped"}
    weights = [abs(money(item.net_amount)) for item in items]
    account_rates = {
        rows[0].account_head: kind_rates[kind]
        for kind, rows in grouped.items()
    }
    allocations = {
        kind: _allocate_money(
            sum((money(row.tax_amount) for row in rows), Decimal("0")),
            weights,
        )
        for kind, rows in grouped.items()
    }
    tax_values = []
    for kind, rows in grouped.items():
        for row in rows:
            tax_values.append({
                "name": row.name,
                "values": {
                    "gst_tax_type": kind,
                    "rate": float(kind_rates[kind]),
                },
            })
    return {
        "status": "stamp",
        "grand_total": f"{money(doc.grand_total):.2f}",
        "outstanding": f"{money(doc.outstanding_amount):.2f}",
        "items": _stamp_values(items, kind_rates, allocations, account_rates),
        "taxes": tax_values,
        "kinds": {kind: float(rate) for kind, rate in kind_rates.items()},
    }


def apply_plan(frappe, doctype: str, plan: dict) -> None:
    child = f"{doctype} Item"
    tax_dt = (
        "Purchase Taxes and Charges" if doctype == "Purchase Invoice"
        else "Sales Taxes and Charges"
    )
    for row in plan["items"]:
        frappe.db.set_value(child, row["name"], row["values"], update_modified=False)
    for row in plan["taxes"]:
        frappe.db.set_value(tax_dt, row["name"], row["values"], update_modified=False)


def verify(frappe, doctype: str, name: str, plan: dict) -> bool:
    doc = frappe.get_doc(doctype, name)
    if f"{money(doc.grand_total):.2f}" != plan["grand_total"]:
        return False
    if f"{money(doc.outstanding_amount):.2f}" != plan["outstanding"]:
        return False
    for kind, rate in plan["kinds"].items():
        total = sum((money(item.get(f"{kind}_amount")) for item in doc.items), Decimal("0"))
        expected = sum(
            (money(row["values"][f"{kind}_amount"]) for row in plan["items"]),
            Decimal("0"),
        )
        if total != expected:
            return False
        stamped = {row["name"] for row in plan["items"]}
        if any(
            item.name in stamped
            and money(item.get(f"{kind}_rate")) != money(rate)
            for item in doc.items
        ):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default=SITE)
    parser.add_argument("--company", default=COMPANY)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--backup")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.site != SITE:
        raise SystemExit(f"bound to {SITE}")
    if args.company != args.confirm_company:
        raise SystemExit("company confirmation mismatch")
    if args.confirm:
        backup = Path(args.backup or "")
        if not backup.is_file() or backup.stat().st_size == 0:
            raise SystemExit("--confirm requires a non-empty --backup file")

    import frappe

    original = Path.cwd()
    sites_path = discover_sites_path(args.site, original)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        gl_before = gl_signature(frappe, args.company)
        summary = {
            "stamp": 0, "skip": 0, "error": 0, "verified": 0, "failed_verify": 0,
        }
        reasons: dict[str, int] = defaultdict(int)
        details = []
        for doctype in ("Purchase Invoice", "Sales Invoice"):
            names = invoice_names(frappe, doctype, args.company)
            if args.limit:
                names = names[: args.limit]
            for name in names:
                doc = frappe.get_doc(doctype, name)
                try:
                    plan = plan_doc(doc)
                except Exception as exc:
                    summary["error"] += 1
                    details.append({"doctype": doctype, "name": name,
                                    "status": "error", "reason": str(exc)[:200]})
                    continue
                if plan["status"] != "stamp":
                    summary["skip"] += 1
                    reasons[plan["reason"]] += 1
                    continue
                summary["stamp"] += 1
                if args.confirm:
                    apply_plan(frappe, doctype, plan)
                    frappe.clear_document_cache(doctype, name)
                    if verify(frappe, doctype, name, plan):
                        summary["verified"] += 1
                    else:
                        summary["failed_verify"] += 1
                        details.append({"doctype": doctype, "name": name,
                                        "status": "verify_fail", "kinds": plan["kinds"]})
                else:
                    details.append({"doctype": doctype, "name": name,
                                    "status": "stamp", "kinds": plan["kinds"]})
        if args.confirm:
            if summary["failed_verify"]:
                frappe.db.rollback()
                passed = False
            else:
                frappe.db.commit()
                passed = True
        else:
            frappe.db.rollback()
            passed = True
        gl_after = gl_signature(frappe, args.company)
        if args.confirm and gl_after != gl_before:
            frappe.db.rollback()
            passed = False
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": not args.confirm,
            "summary": summary,
            "skip_reasons": dict(reasons),
            "gl_before": gl_before,
            "gl_after": gl_after,
            "gl_unchanged": gl_after == gl_before,
            "pass": passed and gl_after == gl_before,
            "details": details if not args.confirm or summary["failed_verify"] else [],
        }
        text = json.dumps(result, indent=2, default=str)
        print(text)
        if args.report:
            Path(args.report).resolve().write_text(text + "\n", encoding="utf-8")
        return 0 if result["pass"] else 2
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()
        os.chdir(original)


if __name__ == "__main__":
    raise SystemExit(main())
