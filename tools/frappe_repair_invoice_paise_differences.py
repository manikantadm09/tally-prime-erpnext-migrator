"""Correct four exact IGST/rounding paise differences from Tally evidence.

Plan-only by default.  Apply mode requires a fresh database backup and an
explicit manifest.  The tool validates the live invoice/GL/PLE shape before
changing invoice metadata, item-wise GST, tax rows, active GL, and the invoice's
own Payment Ledger row as one transaction.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


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


def snapshot(doc) -> dict:
    return {
        "name": doc.name,
        "guid": doc.tally_guid,
        "net_total": f"{money(doc.net_total):.2f}",
        "tax_total": f"{money(doc.total_taxes_and_charges):.2f}",
        "grand_total": f"{money(doc.grand_total):.2f}",
        "rounded_total": f"{money(doc.rounded_total):.2f}",
        "rounding_adjustment": f"{money(doc.rounding_adjustment):.2f}",
        "outstanding_amount": f"{money(doc.outstanding_amount):.2f}",
        "status": doc.status,
    }


def load_manifest(path: Path, company: str, expected_count: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("company") != company:
        raise RuntimeError("manifest company mismatch")
    rows = payload.get("documents") or []
    if len(rows) != expected_count or len({row["guid"] for row in rows}) != len(rows):
        raise RuntimeError("manifest count or GUID uniqueness mismatch")
    for row in rows:
        row["net_total"] = money(row["net_total"])
        row["igst"] = money(row["igst"])
        row["rounding_adjustment"] = money(row["rounding_adjustment"])
        row["party_total"] = money(row["party_total"])
        row["expected_outstanding"] = money(row["expected_outstanding"])
        if row["net_total"] + row["igst"] + row["rounding_adjustment"] != row["party_total"]:
            raise RuntimeError(f"{row['name']}: source manifest is not balanced")
    return rows


def one(rows: list, label: str, name: str):
    if len(rows) != 1:
        raise RuntimeError(f"{name}: expected one {label}, found {len(rows)}")
    return rows[0]


def item_igst_amounts(items: list, rate: Decimal = Decimal("18")) -> list[Decimal]:
    """Calculate Tally-style per-line GST using commercial half-up rounding."""
    return [money(money(item.net_amount) * rate / Decimal("100")) for item in items]


def source_turnover(source: dict) -> Decimal:
    """Gross debit/credit turnover for a purchase including signed round-off."""
    return (
        source["net_total"] + source["igst"]
        + max(source["rounding_adjustment"], Decimal("0.00"))
    )


def plan_invoice(frappe, company: str, source: dict) -> dict:
    doc = frappe.get_doc("Purchase Invoice", source["name"])
    if doc.company != company or doc.docstatus != 1 or doc.tally_guid != source["guid"]:
        raise RuntimeError(f"{doc.name}: company/docstatus/GUID drift")
    if money(doc.net_total) != source["net_total"] or not doc.items:
        raise RuntimeError(f"{doc.name}: net total or item shape drift")
    if (sum((money(item.net_amount) for item in doc.items), Decimal("0.00"))
            != source["net_total"]
            or any(not item.gst_hsn_code for item in doc.items)):
        raise RuntimeError(f"{doc.name}: item totals/HSN drift")
    expected_item_igst = item_igst_amounts(doc.items)
    if sum(expected_item_igst, Decimal("0.00")) != source["igst"]:
        raise RuntimeError(
            f"{doc.name}: per-item half-up IGST does not equal Tally tax total")
    tax = one(
        [row for row in doc.taxes if "IGST" in (row.description or row.account_head).upper()],
        "IGST tax row", doc.name,
    )
    if abs(money(tax.tax_amount) - source["igst"]) > Decimal("0.05"):
        raise RuntimeError(f"{doc.name}: IGST drift is outside guarded paise range")

    gl = frappe.get_all(
        "GL Entry",
        filters={"voucher_type": "Purchase Invoice", "voucher_no": doc.name,
                 "is_cancelled": 0},
        fields=["name", "account", "party_type", "party", "debit", "credit",
                "debit_in_account_currency", "credit_in_account_currency"],
    )
    party_gl = one([row for row in gl if row.party_type == "Supplier"], "party GL row", doc.name)
    tax_gl = one([row for row in gl if row.account == tax.account_head], "IGST GL row", doc.name)
    round_gl = [row for row in gl if "ROUND" in row.account.upper()]
    if money(party_gl.credit) != money(doc.rounded_total or doc.grand_total):
        raise RuntimeError(f"{doc.name}: party GL/header drift")
    if money(tax_gl.debit) != money(tax.tax_amount):
        raise RuntimeError(f"{doc.name}: IGST GL/tax-row drift")
    if source["rounding_adjustment"]:
        round_gl = [one(round_gl, "rounding GL row", doc.name)]
    elif round_gl:
        raise RuntimeError(f"{doc.name}: unexpected rounding GL row")

    ple = frappe.get_all(
        "Payment Ledger Entry",
        filters={"against_voucher_type": "Purchase Invoice",
                 "against_voucher_no": doc.name, "delinked": 0,
                 "voucher_type": "Purchase Invoice", "voucher_no": doc.name},
        fields=["name", "amount", "amount_in_account_currency"],
    )
    invoice_ple = one(ple, "invoice Payment Ledger row", doc.name)
    if money(invoice_ple.amount) != money(party_gl.credit):
        raise RuntimeError(f"{doc.name}: invoice PLE/party GL drift")
    payment_total = -sum(
        (money(row.amount) for row in frappe.get_all(
            "Payment Ledger Entry",
            filters={"against_voucher_type": "Purchase Invoice",
                     "against_voucher_no": doc.name, "delinked": 0,
                     "voucher_type": ["!=", "Purchase Invoice"]},
            fields=["amount"],
        )),
        Decimal("0.00"),
    )
    if max(source["party_total"] - payment_total, Decimal("0.00")) != source["expected_outstanding"]:
        raise RuntimeError(f"{doc.name}: source outstanding/payment evidence drift")
    return {
        "doc": doc,
        "source": source,
        "before": snapshot(doc),
        "items": list(zip(doc.items, expected_item_igst)),
        "tax": tax,
        "party_gl": party_gl,
        "tax_gl": tax_gl,
        "round_gl": round_gl[0] if round_gl else None,
        "invoice_ple": invoice_ple,
        "before_turnover": sum(
            (money(row.debit) for row in gl), Decimal("0.00")),
    }


def set_money(frappe, doctype: str, name: str, fields: list[str], value: Decimal) -> None:
    frappe.db.set_value(
        doctype, name, {field: value for field in fields}, update_modified=False)


def apply_invoice(frappe, plan: dict) -> None:
    doc, source = plan["doc"], plan["source"]
    grand = source["net_total"] + source["igst"]
    rounded = source["party_total"] if source["rounding_adjustment"] else Decimal("0.00")
    status = "Paid" if source["expected_outstanding"] == 0 else "Overdue"
    frappe.db.set_value(
        "Purchase Invoice", doc.name,
        {
            "total_taxes_and_charges": source["igst"],
            "base_total_taxes_and_charges": source["igst"],
            "taxes_and_charges_added": source["igst"],
            "base_taxes_and_charges_added": source["igst"],
            "grand_total": grand,
            "base_grand_total": grand,
            "rounded_total": rounded,
            "base_rounded_total": rounded,
            "rounding_adjustment": source["rounding_adjustment"],
            "base_rounding_adjustment": source["rounding_adjustment"],
            "outstanding_amount": source["expected_outstanding"],
            "status": status,
        },
        update_modified=True,
    )
    set_money(
        frappe, "Purchase Taxes and Charges", plan["tax"].name,
        ["tax_amount", "tax_amount_after_discount_amount", "base_tax_amount",
         "base_tax_amount_after_discount_amount"], source["igst"])
    set_money(
        frappe, "Purchase Taxes and Charges", plan["tax"].name,
        ["total", "base_total"], grand)
    for item, igst in plan["items"]:
        frappe.db.set_value(
            "Purchase Invoice Item", item.name,
            {"igst_rate": 18, "igst_amount": igst,
             "taxable_value": money(item.net_amount), "gst_treatment": "Taxable",
             "item_tax_rate": json.dumps({plan["tax"].account_head: 18.0})},
            update_modified=False,
        )
    set_money(
        frappe, "GL Entry", plan["tax_gl"].name,
        ["debit", "debit_in_account_currency"], source["igst"])
    set_money(
        frappe, "GL Entry", plan["party_gl"].name,
        ["credit", "credit_in_account_currency"], source["party_total"])
    if plan["round_gl"]:
        adjustment = source["rounding_adjustment"]
        frappe.db.set_value(
            "GL Entry", plan["round_gl"].name,
            {
                "debit": max(adjustment, Decimal("0.00")),
                "debit_in_account_currency": max(adjustment, Decimal("0.00")),
                "credit": max(-adjustment, Decimal("0.00")),
                "credit_in_account_currency": max(-adjustment, Decimal("0.00")),
            },
            update_modified=False,
        )
    set_money(
        frappe, "Payment Ledger Entry", plan["invoice_ple"].name,
        ["amount", "amount_in_account_currency"], source["party_total"])
    frappe.clear_document_cache("Purchase Invoice", doc.name)


def verify_invoice(frappe, plan: dict) -> dict:
    doc, source = frappe.get_doc("Purchase Invoice", plan["doc"].name), plan["source"]
    gl = frappe.get_all(
        "GL Entry",
        filters={"voucher_type": "Purchase Invoice", "voucher_no": doc.name,
                 "is_cancelled": 0},
        fields=["account", "party_type", "debit", "credit"],
    )
    debit = sum((money(row.debit) for row in gl), Decimal("0.00"))
    credit = sum((money(row.credit) for row in gl), Decimal("0.00"))
    after = snapshot(doc)
    passed = (
        money(doc.total_taxes_and_charges) == source["igst"]
        and money(doc.grand_total) == source["net_total"] + source["igst"]
        and money(doc.rounding_adjustment) == source["rounding_adjustment"]
        and money(doc.outstanding_amount) == source["expected_outstanding"]
        and sum((money(item.igst_amount) for item in doc.items), Decimal("0.00"))
            == source["igst"]
        and debit == credit == source_turnover(source)
    )
    return {
        "name": doc.name,
        "guid": doc.tally_guid,
        "before": plan["before"],
        "after": after,
        "active_gl_debit": f"{debit:.2f}",
        "active_gl_credit": f"{credit:.2f}",
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
    rows = load_manifest(manifest, args.company, args.expected_count)

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        gl_before = gl_signature(frappe, args.company)
        plans = [plan_invoice(frappe, args.company, row) for row in rows]
        if args.confirm:
            for plan in plans:
                apply_invoice(frappe, plan)
            frappe.db.commit()
        documents = [verify_invoice(frappe, plan) for plan in plans] if args.confirm else []
        gl_after = gl_signature(frappe, args.company)
        expected_turnover_delta = sum(
            (plan["before_turnover"] - source_turnover(plan["source"])
             for plan in plans),
            Decimal("0.00"),
        )
        passed = (
            len(plans) == args.expected_count
            and (not args.confirm or all(row["pass"] for row in documents))
            and (not args.confirm or (
                money(gl_before["debit"]) - money(gl_after["debit"])
                == money(gl_before["credit"]) - money(gl_after["credit"])
                == expected_turnover_delta
            ))
        )
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": not args.confirm,
            "backup": str(backup) if backup else None,
            "validated_count": len(plans),
            "documents": documents,
            "expected_turnover_reduction": f"{expected_turnover_delta:.2f}",
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
