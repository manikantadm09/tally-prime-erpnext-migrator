"""Populate exact item-wise GST fields for migrated single-rate invoices.

India Compliance renders the HSN/GST breakup from item child fields, not from
the invoice Taxes table.  This guarded repair covers only submitted migrated
invoices whose source GST rows exactly match their current invoice tax rows and
whose Tally tax ledgers contain one unambiguous percentage rate.

Plan-only by default.  Apply mode requires a fresh database backup.  The
operation changes no parent totals, GL rows, payment rows, outstanding amounts,
or invoice tax rows.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import re


PENNY = Decimal("0.01")
GST_KINDS = ("CGST", "SGST", "IGST", "CESS")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def gst_kind(value) -> str | None:
    text = norm(value)
    for kind in GST_KINDS:
        if kind.casefold() in text:
            return kind
    return None


def tax_rate(value) -> Decimal | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(value or ""))
    return Decimal(match.group(1)) if match else None


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


def expected_gst(row: dict) -> tuple[dict[str, Decimal], Decimal]:
    totals: dict[str, Decimal] = {}
    rates: set[Decimal] = set()
    missing_rate = False
    for tax in row.get("expected_taxes") or []:
        kind = gst_kind(tax.get("ledger"))
        if not kind:
            continue
        rate = tax_rate(tax.get("ledger"))
        if rate is None:
            missing_rate = True
        else:
            rates.add(rate)
        totals[kind] = totals.get(kind, Decimal("0.00")) + (
            money(tax.get("amount")) * int(tax.get("count") or 0)
        )
    if not totals:
        raise RuntimeError(f"{row.get('name')}: no GST amounts")
    if missing_rate and rates:
        raise RuntimeError(f"{row.get('name')}: partially specified GST rates")
    if not rates:
        net_total = money(row.get("net_total"))
        if not net_total:
            raise RuntimeError(f"{row.get('name')}: cannot infer rate from zero net total")
        inferred = {
            (amount * Decimal("100") / net_total).quantize(Decimal("0.01"))
            for amount in totals.values()
        }
        allowed = {
            Decimal("2.50"), Decimal("6.00"), Decimal("9.00"),
            Decimal("14.00"), Decimal("18.00"),
        }
        if len(inferred) != 1 or not inferred <= allowed:
            raise RuntimeError(f"{row.get('name')}: GST rate cannot be inferred exactly")
        rate = next(iter(inferred))
        if any(money(net_total * rate / Decimal("100")) != amount
               for amount in totals.values()):
            raise RuntimeError(f"{row.get('name')}: inferred GST rate has a residual")
        rates.add(rate)
    if len(rates) != 1:
        raise RuntimeError(f"{row.get('name')}: not an unambiguous single-rate invoice")
    return totals, next(iter(rates))


def manifest_rows(path: Path, expected_count: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("mode") != "read-only":
        raise RuntimeError("manifest is not a read-only audit report")
    rows = []
    for row in payload.get("details") or []:
        if row.get("gst_breakup_classification") == "correct":
            continue
        try:
            expected, _ = expected_gst(row)
        except RuntimeError:
            continue
        actual: dict[str, Decimal] = {}
        for tax in row.get("actual_taxes") or []:
            kind = gst_kind(tax.get("ledger"))
            if kind:
                actual[kind] = actual.get(kind, Decimal("0.00")) + (
                    money(tax.get("amount")) * int(tax.get("count") or 0)
                )
        if actual != expected:
            continue
        rows.append(row)
    if len(rows) != expected_count:
        raise RuntimeError(
            f"manifest scope drift: expected={expected_count} actual={len(rows)}")
    if len({row.get("guid") for row in rows}) != len(rows):
        raise RuntimeError("manifest contains duplicate GUIDs")
    return rows


def allocate(total: Decimal, items: list) -> list[Decimal]:
    weights = [abs(money(item.net_amount)) for item in items]
    denominator = sum(weights, Decimal("0.00"))
    if not denominator:
        raise RuntimeError("cannot allocate GST across zero-value items")
    result = []
    remaining = total
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            value = remaining
        else:
            value = money(total * weight / denominator)
            remaining -= value
        result.append(value)
    if sum(result, Decimal("0.00")) != total:
        raise RuntimeError("GST allocation residual")
    return result


def snapshot(doc) -> dict:
    return {
        "name": doc.name,
        "guid": doc.tally_guid,
        "grand_total": f"{money(doc.grand_total):.2f}",
        "outstanding_amount": f"{money(doc.outstanding_amount):.2f}",
        "status": doc.status,
        "items": len(doc.items),
        "taxes": len(doc.taxes),
        "place_of_supply": doc.get("place_of_supply") or "",
    }


def plan_invoice(frappe, company: str, row: dict) -> dict:
    doctype = row.get("doctype")
    if doctype not in ("Purchase Invoice", "Sales Invoice"):
        raise RuntimeError(f"{row.get('guid')}: unsupported doctype {doctype}")
    doc = frappe.get_doc(doctype, row["name"])
    if doc.company != company or doc.docstatus != 1:
        raise RuntimeError(f"{doc.name}: company/docstatus drift")
    if doc.tally_guid != row["guid"]:
        raise RuntimeError(f"{doc.name}: tally_guid drift")
    expected, rate = expected_gst(row)

    actual: dict[str, Decimal] = {}
    account_rates: dict[str, Decimal] = {}
    for tax in doc.taxes:
        kind = gst_kind(tax.description or tax.account_head)
        if kind not in expected:
            continue
        actual[kind] = actual.get(kind, Decimal("0.00")) + money(tax.tax_amount)
        account_rates[tax.account_head] = rate
    if actual != expected:
        raise RuntimeError(
            f"{doc.name}: source/current invoice GST rows differ "
            f"expected={expected} actual={actual}")
    if not account_rates:
        raise RuntimeError(f"{doc.name}: no matching GST tax accounts")

    items = [item for item in doc.items if money(item.net_amount)]
    if not items:
        raise RuntimeError(f"{doc.name}: no non-zero invoice items")
    if sum((money(item.net_amount) for item in items), Decimal("0.00")) != money(doc.net_total):
        raise RuntimeError(f"{doc.name}: item net total drift")
    if any(not item.gst_hsn_code for item in items):
        raise RuntimeError(f"{doc.name}: blank HSN/SAC requires manual review")

    allocations = {kind: allocate(amount, items) for kind, amount in expected.items()}
    item_values = []
    for index, item in enumerate(items):
        values = {
            "gst_treatment": "Taxable",
            "taxable_value": money(item.net_amount),
            "item_tax_rate": json.dumps(
                {account: float(value) for account, value in account_rates.items()},
                sort_keys=True,
            ),
            "cgst_rate": 0,
            "cgst_amount": 0,
            "sgst_rate": 0,
            "sgst_amount": 0,
            "igst_rate": 0,
            "igst_amount": 0,
            "cess_rate": 0,
            "cess_amount": 0,
        }
        for kind in expected:
            values[f"{kind.lower()}_rate"] = rate
            values[f"{kind.lower()}_amount"] = allocations[kind][index]
        item_values.append({"name": item.name, "values": values})
    return {
        "doc": doc,
        "before": snapshot(doc),
        "rate": rate,
        "expected": expected,
        "items": item_values,
        "expected_place_of_supply": row.get("expected_place_of_supply") or "",
    }


def apply_invoice(frappe, plan: dict) -> None:
    child_doctype = plan["doc"].doctype + " Item"
    for row in plan["items"]:
        frappe.db.set_value(
            child_doctype, row["name"], row["values"], update_modified=False)
    expected_place = plan.get("expected_place_of_supply")
    if expected_place and plan["doc"].get("place_of_supply") != expected_place:
        frappe.db.set_value(
            plan["doc"].doctype, plan["doc"].name,
            "place_of_supply", expected_place, update_modified=False)
    frappe.clear_document_cache(plan["doc"].doctype, plan["doc"].name)


def verify_invoice(frappe, plan: dict) -> dict:
    doc = frappe.get_doc(plan["doc"].doctype, plan["doc"].name)
    actual = {
        kind: sum(
            (money(item.get(f"{kind.lower()}_amount")) for item in doc.items),
            Decimal("0.00"),
        )
        for kind in plan["expected"]
    }
    rates_ok = all(
        money(item.get(f"{kind.lower()}_rate")) == money(plan["rate"])
        for item in doc.items if money(item.net_amount)
        for kind in plan["expected"]
    )
    taxable_ok = all(
        item.gst_treatment == "Taxable"
        and money(item.taxable_value) == money(item.net_amount)
        for item in doc.items if money(item.net_amount)
    )
    after = snapshot(doc)
    passed = (
        actual == plan["expected"]
        and rates_ok
        and taxable_ok
        and after["grand_total"] == plan["before"]["grand_total"]
        and after["outstanding_amount"] == plan["before"]["outstanding_amount"]
        and (
            not plan.get("expected_place_of_supply")
            or after["place_of_supply"] == plan["expected_place_of_supply"]
        )
    )
    return {
        "name": doc.name,
        "guid": doc.tally_guid,
        "rate": str(plan["rate"]),
        "expected": {key: f"{value:.2f}" for key, value in plan["expected"].items()},
        "actual": {key: f"{value:.2f}" for key, value in actual.items()},
        "before": plan["before"],
        "after": after,
        "pass": passed,
    }


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
    rows = manifest_rows(manifest, args.expected_count)

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
        plans = [plan_invoice(frappe, args.company, row) for row in rows]
        if args.confirm:
            for plan in plans:
                apply_invoice(frappe, plan)
            frappe.db.commit()

        documents = []
        if args.confirm:
            documents = [verify_invoice(frappe, plan) for plan in plans]
        passed = (
            len(plans) == args.expected_count
            and (not args.confirm or all(row["pass"] for row in documents))
        )
        gl_after = gl_signature(frappe, args.company)
        passed = passed and (not args.confirm or gl_after == gl_before)
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
        shown = result if not args.summary_only else {
            key: value for key, value in result.items() if key != "documents"
        }
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
