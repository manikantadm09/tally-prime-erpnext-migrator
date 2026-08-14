"""Backfill India Compliance GST tax-type metadata on migrated invoices.

This fixes the client-side GST Settings warning for historical Tally invoices
whose real, rate-specific tax ledgers are not the single default GST Settings
account set. It changes only ``gst_tax_type`` in invoice tax child rows.
Plan-only is the default; apply needs a fresh database backup.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def gst_kind(value: str) -> str | None:
    text = " ".join(str(value or "").split()).casefold()
    for kind in ("cgst", "sgst", "igst", "cess"):
        if kind in text:
            return kind
    return None


def discover_sites_path(site: str, cwd: Path | None = None) -> Path:
    cwd = (cwd or Path.cwd()).resolve()
    for candidate in (cwd / "sites", cwd):
        if (candidate / site / "site_config.json").is_file():
            return candidate
    raise RuntimeError(f"Cannot locate site {site!r} below {cwd}")


def manifest_rows(path: Path, company: str, expected_count: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("mode") != "read-only" or payload.get("company") != company:
        raise RuntimeError("manifest identity is not the requested read-only company audit")
    rows = [
        row for row in payload.get("details") or []
        if row.get("doctype") in ("Sales Invoice", "Purchase Invoice")
        and row.get("gst_breakup_classification") == "correct"
        and row.get("gst_tax_type_classification") == "missing"
        and row.get("gst_tax_type_rows")
    ]
    if len(rows) != expected_count:
        raise RuntimeError(
            f"manifest scope drift: expected={expected_count} actual={len(rows)}")
    if len({(row["doctype"], row["name"]) for row in rows}) != len(rows):
        raise RuntimeError("manifest contains duplicate invoice identities")
    return rows


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
        "doctype": doc.doctype,
        "name": doc.name,
        "guid": doc.tally_guid,
        "grand_total": f"{money(doc.grand_total):.2f}",
        "outstanding_amount": f"{money(doc.outstanding_amount):.2f}",
        "status": doc.status,
    }


def row_key(account_head: str, description: str, amount: Decimal, tax_type: str) -> tuple:
    return (account_head or "", description or "", f"{amount:.2f}", tax_type)


def tax_child_doctype(invoice_doctype: str) -> str:
    mapping = {
        "Sales Invoice": "Sales Taxes and Charges",
        "Purchase Invoice": "Purchase Taxes and Charges",
    }
    try:
        return mapping[invoice_doctype]
    except KeyError as exc:
        raise RuntimeError(f"unsupported invoice doctype: {invoice_doctype}") from exc


def plan_invoice(frappe, company: str, row: dict) -> dict:
    doc = frappe.get_doc(row["doctype"], row["name"])
    before = snapshot(doc)
    if doc.company != company or doc.docstatus != 1:
        raise RuntimeError(f"{doc.name}: company/docstatus drift")
    if doc.tally_guid != row.get("guid"):
        raise RuntimeError(f"{doc.name}: tally_guid drift")

    expected = Counter(
        row_key(
            item.get("account_head") or "",
            item.get("description") or "",
            money(item.get("amount")),
            item["expected_gst_tax_type"],
        )
        for item in row["gst_tax_type_rows"]
    )
    child_rows = []
    for tax in doc.taxes:
        tax_type = gst_kind(tax.description or tax.account_head)
        if not tax_type or not money(tax.tax_amount):
            continue
        if tax.gst_tax_type:
            raise RuntimeError(f"{doc.name}: GST tax type is no longer blank")
        key = row_key(tax.account_head, tax.description, money(tax.tax_amount), tax_type)
        child_rows.append({
            "name": tax.name,
            "expected_gst_tax_type": tax_type,
            "key": key,
        })
    actual = Counter(item["key"] for item in child_rows)
    if actual != expected:
        raise RuntimeError(f"{doc.name}: source/current tax-row drift")
    return {"doc": doc, "before": before, "tax_rows": child_rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--backup")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.company != args.confirm_company:
        raise SystemExit("confirmation does not exactly match company")
    manifest = Path(args.manifest).resolve()
    if not manifest.is_file():
        raise SystemExit(f"manifest does not exist: {manifest}")
    backup = Path(args.backup).resolve() if args.backup else None
    if args.confirm and (
        backup is None or not backup.is_file() or backup.stat().st_size == 0
    ):
        raise SystemExit("--confirm requires a non-empty --backup file")
    rows = manifest_rows(manifest, args.company, args.expected_count)

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
                child_doctype = tax_child_doctype(plan["doc"].doctype)
                for tax in plan["tax_rows"]:
                    frappe.db.set_value(
                        child_doctype, tax["name"], "gst_tax_type",
                        tax["expected_gst_tax_type"], update_modified=False)
            frappe.db.commit()

        documents = []
        for plan in plans:
            after_doc = frappe.get_doc(plan["doc"].doctype, plan["doc"].name)
            after = snapshot(after_doc)
            unchanged = {
                key: after[key] == plan["before"][key]
                for key in ("doctype", "name", "guid", "grand_total", "outstanding_amount", "status")
            }
            row_values = {tax.name: tax.gst_tax_type or "" for tax in after_doc.taxes}
            tax_types_ok = all(
                row_values.get(tax["name"]) == tax["expected_gst_tax_type"]
                for tax in plan["tax_rows"]
            ) if args.confirm else all(
                not row_values.get(tax["name"]) for tax in plan["tax_rows"]
            )
            documents.append({
                "name": after_doc.name,
                "guid": after_doc.tally_guid,
                "tax_rows": len(plan["tax_rows"]),
                "unchanged": unchanged,
                "tax_types_ok": tax_types_ok,
                "pass": all(unchanged.values()) and tax_types_ok,
            })
        gl_after = gl_signature(frappe, args.company)
        passed = (
            len(plans) == args.expected_count
            and all(row["pass"] for row in documents)
            and gl_after == gl_before
        )
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": not args.confirm,
            "backup": str(backup) if backup else None,
            "validated_invoices": len(plans),
            "validated_tax_rows": sum(len(plan["tax_rows"]) for plan in plans),
            "documents": documents,
            "active_gl_before": gl_before,
            "active_gl_after": gl_after,
            "pass": passed,
        }
        report = Path(args.report).resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key != "documents"}, indent=2))
        return 0 if passed else 1
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()
        os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
