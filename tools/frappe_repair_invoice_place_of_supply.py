"""Repair source-proven invoice place-of-supply metadata without changing GL.

The input is the read-only report written by audit_invoice_tax_structure_api.py.
Plan-only is the default. Apply mode requires a fresh database backup and
refuses any live document, amount, status, GUID, or current-value drift.
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


def manifest_rows(path: Path, company: str, expected_count: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("mode") != "read-only" or payload.get("company") != company:
        raise RuntimeError("manifest identity is not the requested read-only company audit")
    rows = [
        row for row in payload.get("details") or []
        if row.get("doctype") in ("Sales Invoice", "Purchase Invoice")
        and row.get("expected_place_of_supply")
        and row.get("expected_place_of_supply") != row.get("actual_place_of_supply")
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
        "place_of_supply": doc.get("place_of_supply") or "",
        "grand_total": f"{money(doc.grand_total):.2f}",
        "outstanding_amount": f"{money(doc.outstanding_amount):.2f}",
        "status": doc.status,
    }


def plan_invoice(frappe, company: str, row: dict) -> dict:
    doc = frappe.get_doc(row["doctype"], row["name"])
    before = snapshot(doc)
    if doc.company != company or doc.docstatus != 1:
        raise RuntimeError(f"{doc.name}: company/docstatus drift")
    if doc.tally_guid != row.get("guid"):
        raise RuntimeError(f"{doc.name}: tally_guid drift")
    if before["place_of_supply"] != (row.get("actual_place_of_supply") or ""):
        raise RuntimeError(f"{doc.name}: current place_of_supply drift")
    return {
        "doc": doc,
        "before": before,
        "expected_place_of_supply": row["expected_place_of_supply"],
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
                doc = plan["doc"]
                frappe.db.set_value(
                    doc.doctype,
                    doc.name,
                    "place_of_supply",
                    plan["expected_place_of_supply"],
                    update_modified=False,
                )
            frappe.db.commit()

        documents = []
        for plan in plans:
            after_doc = frappe.get_doc(plan["doc"].doctype, plan["doc"].name)
            after = snapshot(after_doc)
            unchanged = {
                key: after[key] == plan["before"][key]
                for key in ("doctype", "name", "guid", "grand_total", "outstanding_amount", "status")
            }
            documents.append({
                "before": plan["before"],
                "after": after,
                "expected_place_of_supply": plan["expected_place_of_supply"],
                "unchanged": unchanged,
                "pass": (
                    (not args.confirm or after["place_of_supply"] == plan["expected_place_of_supply"])
                    and all(unchanged.values())
                ),
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
            "validated_count": len(plans),
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
