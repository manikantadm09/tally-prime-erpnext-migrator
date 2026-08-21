"""Align four purchase invoices to Tally lump-sum IGST (not per-line 18%).

Moves the paise difference from Input Tax IGST into Rounded Off so the
voucher still balances. Party credit and outstanding stay the same.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path


ALLOWED_SITES = ("dev.spaceki.com", "erp.spaceki.com")
PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def discover_sites_path(site: str, cwd: Path) -> Path:
    for candidate in (cwd / "sites", cwd):
        if (candidate / site / "site_config.json").is_file():
            return candidate
    raise RuntimeError(f"Cannot locate site {site!r} below {cwd}")


def allocate_igst(items: list, target: Decimal) -> list[Decimal]:
    if not items:
        raise RuntimeError("no items")
    if len(items) == 1:
        return [target]
    parts = []
    remain = target
    for item in items[:-1]:
        part = money(money(item.net_amount) * Decimal("18") / Decimal("100"))
        parts.append(part)
        remain -= part
    parts.append(remain)
    if sum(parts, Decimal("0.00")) != target:
        raise RuntimeError("item IGST allocation does not equal Tally tax")
    return parts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.site not in ALLOWED_SITES:
        raise SystemExit(f"bound to {ALLOWED_SITES}, not {args.site}")
    backup = Path(args.backup)
    if args.confirm and (not backup.is_file() or backup.stat().st_size == 0):
        raise SystemExit("confirm requires a non-empty backup")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if manifest.get("company") != args.company:
        raise SystemExit("manifest company mismatch")

    import frappe

    original = Path.cwd()
    sites_path = discover_sites_path(args.site, original)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    results = []
    try:
        for source in manifest["documents"]:
            doc = frappe.get_doc("Purchase Invoice", source["name"])
            if doc.company != args.company or doc.docstatus != 1:
                raise RuntimeError(f"{doc.name}: not a submitted company invoice")
            if doc.tally_guid != source["guid"]:
                raise RuntimeError(f"{doc.name}: GUID drift")
            igst = money(source["igst"])
            rnd = money(source["rounding_adjustment"])
            party = money(source["party_total"])
            net = money(source["net_total"])
            if net + igst + rnd != party:
                raise RuntimeError(f"{doc.name}: Tally source does not balance")
            if money(doc.net_total) != net:
                raise RuntimeError(f"{doc.name}: net_total drift")
            if money(doc.outstanding_amount) != money(source["expected_outstanding"]):
                raise RuntimeError(f"{doc.name}: outstanding drift")
            tax = [t for t in doc.taxes if "IGST" in (t.description or t.account_head).upper()]
            if len(tax) != 1:
                raise RuntimeError(f"{doc.name}: expected one IGST tax row")
            tax = tax[0]
            gl = frappe.get_all(
                "GL Entry",
                filters={"voucher_type": "Purchase Invoice", "voucher_no": doc.name,
                         "is_cancelled": 0},
                fields=["name", "account", "party_type", "debit", "credit"],
            )
            tax_gl = [r for r in gl if r.account == tax.account_head]
            party_gl = [r for r in gl if r.party_type == "Supplier"]
            round_gl = [r for r in gl if "ROUND" in r.account.upper()]
            if len(tax_gl) != 1 or len(party_gl) != 1 or len(round_gl) != 1:
                raise RuntimeError(f"{doc.name}: unexpected GL shape")
            if money(party_gl[0].credit) != party:
                raise RuntimeError(f"{doc.name}: party GL is not Tally party total")
            item_igst = allocate_igst(list(doc.items), igst)
            grand = net + igst
            before = {
                "tax": f"{money(tax.tax_amount):.2f}",
                "item_igst": f"{sum((money(i.igst_amount) for i in doc.items), Decimal('0.00')):.2f}",
                "round_gl": f"{money(round_gl[0].debit) - money(round_gl[0].credit):.2f}",
            }
            if args.confirm:
                frappe.db.set_value(
                    "Purchase Invoice", doc.name,
                    {
                        "total_taxes_and_charges": igst,
                        "base_total_taxes_and_charges": igst,
                        "taxes_and_charges_added": igst,
                        "base_taxes_and_charges_added": igst,
                        "grand_total": grand,
                        "base_grand_total": grand,
                        "rounding_adjustment": rnd,
                        "base_rounding_adjustment": rnd,
                        "rounded_total": party,
                        "base_rounded_total": party,
                    },
                    update_modified=True,
                )
                frappe.db.set_value(
                    "Purchase Taxes and Charges", tax.name,
                    {
                        "description": "IGST INPUT @ 18 %",
                        "tax_amount": igst,
                        "tax_amount_after_discount_amount": igst,
                        "base_tax_amount": igst,
                        "base_tax_amount_after_discount_amount": igst,
                        "total": grand,
                        "base_total": grand,
                    },
                    update_modified=False,
                )
                for item, amount in zip(doc.items, item_igst):
                    frappe.db.set_value(
                        "Purchase Invoice Item", item.name,
                        {"igst_amount": amount, "taxable_value": money(item.net_amount)},
                        update_modified=False,
                    )
                frappe.db.set_value(
                    "GL Entry", tax_gl[0].name,
                    {"debit": igst, "debit_in_account_currency": igst},
                    update_modified=False,
                )
                frappe.db.set_value(
                    "GL Entry", round_gl[0].name,
                    {
                        "debit": max(rnd, Decimal("0.00")),
                        "debit_in_account_currency": max(rnd, Decimal("0.00")),
                        "credit": max(-rnd, Decimal("0.00")),
                        "credit_in_account_currency": max(-rnd, Decimal("0.00")),
                    },
                    update_modified=False,
                )
                frappe.clear_document_cache("Purchase Invoice", doc.name)
            after_doc = frappe.get_doc("Purchase Invoice", doc.name)
            after_gl = frappe.get_all(
                "GL Entry",
                filters={"voucher_type": "Purchase Invoice", "voucher_no": doc.name,
                         "is_cancelled": 0},
                fields=["debit", "credit"],
            )
            dr = sum((money(r.debit) for r in after_gl), Decimal("0.00"))
            cr = sum((money(r.credit) for r in after_gl), Decimal("0.00"))
            item_sum = sum((money(i.igst_amount) for i in after_doc.items), Decimal("0.00"))
            passed = (
                money(after_doc.total_taxes_and_charges) == igst
                and item_sum == igst
                and money(after_doc.rounding_adjustment) == rnd
                and money(after_doc.outstanding_amount) == money(source["expected_outstanding"])
                and dr == cr
                and money(party_gl[0].credit) == party
            ) if args.confirm else True
            results.append({
                "name": doc.name,
                "before": before,
                "target_igst": f"{igst:.2f}",
                "target_rounding": f"{rnd:.2f}",
                "after_tax": f"{money(after_doc.total_taxes_and_charges):.2f}",
                "after_item_igst": f"{item_sum:.2f}",
                "after_round": f"{money(after_doc.rounding_adjustment):.2f}",
                "gl_dr": f"{dr:.2f}",
                "gl_cr": f"{cr:.2f}",
                "pass": passed,
            })
        if args.confirm:
            if not all(row["pass"] for row in results):
                frappe.db.rollback()
            else:
                frappe.db.commit()
        else:
            frappe.db.rollback()
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()
        os.chdir(original)

    report = {
        "site": args.site,
        "mode": "applied" if args.confirm else "plan",
        "backup": str(backup),
        "pass": all(row["pass"] for row in results),
        "documents": results,
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
