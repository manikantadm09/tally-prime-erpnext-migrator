"""Split generic migrated invoice items into exact GST-rate taxable bases.

India Compliance requires one item-wise GST rate per taxable item.  Tally can
post several GST rates against one purchase ledger line, so the migration's
generic item must be split into rate-wise rows for an accurate HSN breakup.

This repair accepts only invoices for which source tax amount / rate derives
exact taxable bases whose sum equals the current invoice net total exactly.
It is plan-only unless ``--confirm`` is supplied, and apply mode requires a
fresh database backup.  Parent totals, tax rows, GL, payment allocations,
outstanding amounts, and grand totals are not changed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import re


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def gst_kind(value) -> str | None:
    text = norm(value)
    for kind in ("CGST", "SGST", "IGST"):
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


def expected_rate_groups(row: dict) -> dict[Decimal, dict]:
    grouped: dict[Decimal, dict[str, Decimal]] = defaultdict(dict)
    for tax in row.get("expected_taxes") or []:
        kind = gst_kind(tax.get("ledger"))
        rate = tax_rate(tax.get("ledger"))
        if not kind or rate is None:
            continue
        amount = money(tax.get("amount")) * int(tax.get("count") or 0)
        grouped[rate][kind] = grouped[rate].get(kind, Decimal("0.00")) + amount
    if len(grouped) <= 1:
        raise RuntimeError(f"{row.get('name')}: not a multi-rate GST invoice")

    result = {}
    for rate, kinds in grouped.items():
        if set(kinds) != {"CGST", "SGST"} or kinds["CGST"] != kinds["SGST"]:
            raise RuntimeError(
                f"{row.get('name')}: multi-rate scope requires equal CGST/SGST")
        taxable = money(kinds["CGST"] * Decimal("100") / rate)
        if money(taxable * rate / Decimal("100")) != kinds["CGST"]:
            raise RuntimeError(f"{row.get('name')}: rate-wise taxable base has residual")
        result[rate] = {"taxable": taxable, "taxes": kinds}
    base_sum = sum((value["taxable"] for value in result.values()), Decimal("0.00"))
    residual = money(row.get("net_total")) - base_sum
    if residual:
        # Tally stores exact ledger tax while an inferred taxable base can have
        # a few paise of rate-rounding freedom.  Absorb the residual only when
        # the adjusted base still reproduces every source tax amount exactly.
        absorbed = False
        for rate, value in sorted(
            result.items(), key=lambda pair: pair[1]["taxable"], reverse=True
        ):
            adjusted = value["taxable"] + residual
            if adjusted <= 0:
                continue
            if all(
                money(adjusted * rate / Decimal("100")) == amount
                for amount in value["taxes"].values()
            ):
                value["taxable"] = adjusted
                absorbed = True
                break
        if not absorbed:
            if residual < 0 and len(result) == 2:
                pairs = list(result.items())
                cents = int(abs(residual / PENNY))
                for first_cents in range(cents + 1):
                    deltas = (
                        -PENNY * first_cents,
                        residual + PENNY * first_cents,
                    )
                    adjusted = [
                        value["taxable"] + delta
                        for (_, value), delta in zip(pairs, deltas)
                    ]
                    if any(value <= 0 for value in adjusted):
                        continue
                    valid = all(
                        all(
                            money(base * rate / Decimal("100")) == amount
                            for amount in value["taxes"].values()
                        )
                        for ((rate, value), base) in zip(pairs, adjusted)
                    )
                    if valid:
                        for (_, value), base in zip(pairs, adjusted):
                            value["taxable"] = base
                        absorbed = True
                        break
        if not absorbed:
            if residual <= 0:
                bases = ", ".join(
                    f"{rate}: {value['taxable']}" for rate, value in result.items())
                raise RuntimeError(
                    f"{row.get('name')}: negative unallocated taxable residual "
                    f"bases={{{bases}}} residual={residual}")
            # A positive amount with no source GST is explicitly represented
            # as a Nil-Rated rate-wise row rather than being assigned a made-up
            # rate.  This covers genuine mixed taxable/non-taxable vouchers.
            result[Decimal("0")] = {"taxable": residual, "taxes": {}}
    return dict(sorted(result.items()))


def expected_gst(row: dict) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for tax in row.get("expected_taxes") or []:
        kind = gst_kind(tax.get("ledger"))
        if kind:
            totals[kind] = totals.get(kind, Decimal("0.00")) + (
                money(tax.get("amount")) * int(tax.get("count") or 0)
            )
    return totals


def manifest_rows(path: Path, expected_count: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("mode") != "read-only":
        raise RuntimeError("manifest is not a read-only audit report")
    rows = []
    for row in payload.get("details") or []:
        if row.get("gst_breakup_classification") == "correct":
            continue
        try:
            expected_rate_groups(row)
        except RuntimeError:
            continue
        actual: dict[str, Decimal] = {}
        for tax in row.get("actual_taxes") or []:
            kind = gst_kind(tax.get("ledger"))
            if kind:
                actual[kind] = actual.get(kind, Decimal("0.00")) + (
                    money(tax.get("amount")) * int(tax.get("count") or 0)
                )
        if actual != expected_gst(row):
            continue
        rows.append(row)
    if len(rows) != expected_count:
        raise RuntimeError(
            f"manifest scope drift: expected={expected_count} actual={len(rows)}")
    if len({row.get("guid") for row in rows}) != len(rows):
        raise RuntimeError("manifest contains duplicate GUIDs")
    return rows


def snapshot(doc) -> dict:
    return {
        "name": doc.name,
        "guid": doc.tally_guid,
        "grand_total": f"{money(doc.grand_total):.2f}",
        "net_total": f"{money(doc.net_total):.2f}",
        "outstanding_amount": f"{money(doc.outstanding_amount):.2f}",
        "status": doc.status,
        "items": len(doc.items),
        "taxes": len(doc.taxes),
    }


def rate_accounts(doc, groups: dict[Decimal, dict]) -> dict[Decimal, dict[str, str]]:
    result: dict[Decimal, dict[str, str]] = defaultdict(dict)
    for tax in doc.taxes:
        kind = gst_kind(tax.description or tax.account_head)
        rate = tax_rate(tax.description or tax.account_head)
        if kind and rate in groups:
            result[rate][kind] = tax.account_head
    for rate in groups:
        if rate == 0:
            continue
        if set(result[rate]) != {"CGST", "SGST"}:
            raise RuntimeError(f"{doc.name}: cannot map GST accounts for rate {rate}")
    return dict(result)


def item_values(item, rate: Decimal, group: dict, accounts: dict[str, str]) -> dict:
    taxable = group["taxable"]
    values = {
        "qty": 1,
        "stock_qty": 1,
        "conversion_factor": 1,
        "price_list_rate": taxable,
        "base_price_list_rate": taxable,
        "rate": taxable,
        "base_rate": taxable,
        "net_rate": taxable,
        "base_net_rate": taxable,
        "amount": taxable,
        "base_amount": taxable,
        "net_amount": taxable,
        "base_net_amount": taxable,
        "discount_percentage": 0,
        "discount_amount": 0,
        "base_rate_with_margin": taxable,
        "rate_with_margin": taxable,
        "gst_treatment": "Taxable" if rate else "Nil-Rated",
        "taxable_value": taxable,
        "item_tax_rate": json.dumps(
            {account: float(rate) for account in accounts.values()}, sort_keys=True),
        "cgst_rate": rate if rate else 0,
        "cgst_amount": group["taxes"].get("CGST", 0),
        "sgst_rate": rate if rate else 0,
        "sgst_amount": group["taxes"].get("SGST", 0),
        "igst_rate": 0,
        "igst_amount": 0,
        "cess_rate": 0,
        "cess_amount": 0,
    }
    return values


def plan_invoice(frappe, company: str, row: dict) -> dict:
    if row.get("doctype") != "Purchase Invoice":
        raise RuntimeError(f"{row.get('guid')}: only Purchase Invoice is supported")
    doc = frappe.get_doc("Purchase Invoice", row["name"])
    if doc.company != company or doc.docstatus != 1 or doc.tally_guid != row["guid"]:
        raise RuntimeError(f"{doc.name}: company/docstatus/GUID drift")
    groups = expected_rate_groups(row)
    accounts = rate_accounts(doc, groups)
    expected = expected_gst(row)
    actual = defaultdict(lambda: Decimal("0.00"))
    for tax in doc.taxes:
        kind = gst_kind(tax.description or tax.account_head)
        if kind in expected:
            actual[kind] += money(tax.tax_amount)
    if dict(actual) != expected:
        raise RuntimeError(f"{doc.name}: live GST tax-row totals drift")

    items = [item for item in doc.items if money(item.net_amount)]
    if not items or any(not item.gst_hsn_code for item in items):
        raise RuntimeError(f"{doc.name}: items/HSN require manual review")
    if sum((money(item.net_amount) for item in items), Decimal("0.00")) != money(doc.net_total):
        raise RuntimeError(f"{doc.name}: item net total drift")

    by_amount: dict[Decimal, list] = defaultdict(list)
    for item in items:
        by_amount[money(item.net_amount)].append(item)
    assignments = []
    if Counter(by_amount.keys()) == Counter():
        raise AssertionError("unreachable")
    bases = Counter(value["taxable"] for value in groups.values())
    item_amounts = Counter(money(item.net_amount) for item in items)
    if item_amounts == bases:
        for rate, group in groups.items():
            assignments.append((by_amount[group["taxable"]].pop(), rate, group, False))
    elif len(items) == 1:
        original = items[0]
        for index, (rate, group) in enumerate(groups.items()):
            assignments.append((original, rate, group, index > 0))
    else:
        sorted_items = sorted(items, key=lambda item: money(item.net_amount), reverse=True)
        sorted_groups = sorted(
            groups.items(), key=lambda pair: pair[1]["taxable"], reverse=True)
        if len(sorted_items) != len(sorted_groups):
            raise RuntimeError(f"{doc.name}: existing item rows do not map to rate bases")
        for item, (rate, group) in zip(sorted_items, sorted_groups):
            current = money(item.net_amount)
            proposed = group["taxable"]
            if abs(current - proposed) > Decimal("0.10"):
                raise RuntimeError(
                    f"{doc.name}: rate-base row drift exceeds 10 paise "
                    f"current={current} proposed={proposed}")
            assignments.append((item, rate, group, False))

    planned = []
    for item, rate, group, clone in assignments:
        planned.append({
            "item": item,
            "clone": clone,
            "rate": rate,
            "values": item_values(item, rate, group, accounts.get(rate, {})),
        })
    return {
        "doc": doc,
        "before": snapshot(doc),
        "expected": expected,
        "groups": groups,
        "planned": planned,
    }


SYSTEM_FIELDS = {
    "name", "creation", "modified", "modified_by", "owner",
    "_user_tags", "_comments", "_assign", "_liked_by",
}


def apply_invoice(frappe, plan: dict) -> None:
    doc = plan["doc"]
    child_doctype = "Purchase Invoice Item"
    next_idx = max(int(item.idx or 0) for item in doc.items)
    for row in plan["planned"]:
        if not row["clone"]:
            frappe.db.set_value(
                child_doctype, row["item"].name, row["values"], update_modified=False)
            continue
        next_idx += 1
        data = row["item"].as_dict()
        for field in SYSTEM_FIELDS:
            data.pop(field, None)
        data.update(row["values"])
        data.update({
            "doctype": child_doctype,
            "parent": doc.name,
            "parenttype": "Purchase Invoice",
            "parentfield": "items",
            "docstatus": 1,
            "idx": next_idx,
        })
        child = frappe.get_doc(data)
        child.flags.ignore_permissions = True
        child.db_insert()
    frappe.db.set_value(
        "Purchase Invoice", doc.name,
        "total_qty", len(plan["planned"]), update_modified=True,
    )
    frappe.clear_document_cache("Purchase Invoice", doc.name)


def verify_invoice(frappe, plan: dict) -> dict:
    doc = frappe.get_doc("Purchase Invoice", plan["doc"].name)
    actual = {
        kind: sum(
            (money(item.get(f"{kind.lower()}_amount")) for item in doc.items),
            Decimal("0.00"),
        )
        for kind in plan["expected"]
    }
    rendered_groups = Counter(
        (money(item.cgst_rate), money(item.net_amount))
        for item in doc.items if money(item.net_amount)
    )
    expected_groups = Counter(
        (money(rate), value["taxable"]) for rate, value in plan["groups"].items()
    )
    after = snapshot(doc)
    passed = (
        actual == plan["expected"]
        and rendered_groups == expected_groups
        and all(
            item.gst_treatment == ("Taxable" if money(item.cgst_rate) else "Nil-Rated")
            for item in doc.items if money(item.net_amount)
        )
        and after["net_total"] == plan["before"]["net_total"]
        and after["grand_total"] == plan["before"]["grand_total"]
        and after["outstanding_amount"] == plan["before"]["outstanding_amount"]
    )
    return {
        "name": doc.name,
        "guid": doc.tally_guid,
        "before": plan["before"],
        "after": after,
        "expected_gst": {key: f"{value:.2f}" for key, value in plan["expected"].items()},
        "actual_gst": {key: f"{value:.2f}" for key, value in actual.items()},
        "rate_bases": [
            {"rate": str(rate), "taxable": f"{value['taxable']:.2f}"}
            for rate, value in plan["groups"].items()
        ],
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
        documents = [verify_invoice(frappe, plan) for plan in plans] if args.confirm else []
        gl_after = gl_signature(frappe, args.company)
        passed = (
            len(plans) == args.expected_count
            and (not args.confirm or all(row["pass"] for row in documents))
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
