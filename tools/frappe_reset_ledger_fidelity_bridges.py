"""Guarded reset of explicitly named stale ledger-fidelity bridge JEs.

Plan-only by default. Apply requires a backup and an exact manifest. The old
bridges are cancelled and physically removed so the canonical loader can
recreate one clean bridge per source GUID from current invoice/Tally evidence.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path


CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


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


def load_manifest(path: Path, company: str, expected_count: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("company") != company:
        raise RuntimeError("manifest company mismatch")
    rows = payload.get("documents") or []
    if len(rows) != expected_count:
        raise RuntimeError("manifest count mismatch")
    if len({row["name"] for row in rows}) != len(rows):
        raise RuntimeError("manifest names are not unique")
    if len({row["tally_guid"] for row in rows}) != len(rows):
        raise RuntimeError("manifest GUIDs are not unique")
    for row in rows:
        row["total_debit"] = money(row["total_debit"])
        if not row["tally_guid"].endswith(":ledger-fidelity-bridge"):
            raise RuntimeError(f"unsafe non-fidelity tag: {row['tally_guid']}")
    return rows


def plan_document(frappe, company: str, source: dict) -> dict:
    doc = frappe.get_doc("Journal Entry", source["name"])
    actual = {
        "company": doc.company,
        "docstatus": doc.docstatus,
        "tally_guid": doc.tally_guid,
        "posting_date": str(doc.posting_date),
        "total_debit": f"{money(doc.total_debit):.2f}",
        "total_credit": f"{money(doc.total_credit):.2f}",
        "user_remark": str(doc.user_remark or ""),
    }
    expected = {
        "company": company,
        "docstatus": 1,
        "tally_guid": source["tally_guid"],
        "posting_date": source["posting_date"],
        "total_debit": f"{source['total_debit']:.2f}",
        "total_credit": f"{source['total_debit']:.2f}",
    }
    compared = {key: actual[key] for key in expected}
    if compared != expected:
        raise RuntimeError(
            f"{doc.name}: guarded identity/amount drift; "
            f"expected={expected} actual={actual}")
    rows = frappe.get_all(
        "GL Entry",
        filters={"voucher_type": "Journal Entry", "voucher_no": doc.name,
                 "is_cancelled": 0},
        fields=["debit", "credit"],
    )
    debit = sum((money(row.debit) for row in rows), Decimal("0.00"))
    credit = sum((money(row.credit) for row in rows), Decimal("0.00"))
    if not rows or debit != credit or debit != source["total_debit"]:
        raise RuntimeError(f"{doc.name}: active GL signature drift")
    return {"doc": doc, "active_gl_rows": len(rows), "turnover": debit}


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
        raise SystemExit("company confirmation mismatch")
    manifest = Path(args.manifest).resolve()
    backup = Path(args.backup).resolve() if args.backup else None
    if args.confirm and (
        backup is None or not backup.is_file() or backup.stat().st_size == 0
    ):
        raise SystemExit("--confirm requires a non-empty --backup")
    sources = load_manifest(manifest, args.company, args.expected_count)

    import frappe

    original_cwd = Path.cwd()
    sites_path = discover_sites_path(args.site, original_cwd)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        before = gl_signature(frappe, args.company)
        plans = [plan_document(frappe, args.company, row) for row in sources]
        turnover = sum((plan["turnover"] for plan in plans), Decimal("0.00"))
        removed = 0
        if args.confirm:
            for plan in plans:
                doc = plan["doc"]
                doc.flags.ignore_permissions = True
                doc.cancel()
                for doctype in (
                    "GL Entry", "Payment Ledger Entry", "Stock Ledger Entry"
                ):
                    frappe.db.delete(
                        doctype,
                        {"voucher_type": "Journal Entry", "voucher_no": doc.name},
                    )
                frappe.delete_doc(
                    "Journal Entry", doc.name, force=True, for_reload=True,
                    ignore_permissions=True, ignore_missing=False,
                    delete_permanently=True,
                )
                removed += 1
            frappe.db.commit()
        after = gl_signature(frappe, args.company)
        remaining = sum(
            int(bool(frappe.db.exists("Journal Entry", row["name"])))
            for row in sources
        )
        expected_after = {
            "debit": f"{money(before['debit']) - turnover:.2f}",
            "credit": f"{money(before['credit']) - turnover:.2f}",
        }
        passed = (
            len(plans) == args.expected_count
            and (not args.confirm or (
                removed == args.expected_count
                and remaining == 0
                and after["debit"] == expected_after["debit"]
                and after["credit"] == expected_after["credit"]
            ))
        )
        result = {
            "site": args.site,
            "company": args.company,
            "plan_only": not args.confirm,
            "backup": str(backup) if backup else None,
            "validated_count": len(plans),
            "turnover": f"{turnover:.2f}",
            "documents": [
                {
                    "name": plan["doc"].name,
                    "tally_guid": plan["doc"].tally_guid,
                    "active_gl_rows": plan["active_gl_rows"],
                    "turnover": f"{plan['turnover']:.2f}",
                }
                for plan in plans
            ],
            "removed": removed,
            "remaining": remaining,
            "active_gl_before": before,
            "active_gl_after": after,
            "expected_active_gl_after": expected_after,
            "pass": passed,
        }
        output = json.dumps(result, indent=2)
        shown = result if not args.summary_only else {
            key: value for key, value in result.items() if key != "documents"
        }
        print(json.dumps(shown, indent=2), flush=True)
        if args.report:
            Path(args.report).resolve().write_text(output + "\n", encoding="utf-8")
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
