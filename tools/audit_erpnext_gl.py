"""Audit GL entries belonging to documents created by this migration."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
import json

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient


def main() -> None:
    erp = ERPNextClient()
    company = get_config().erpnext["company"]
    doctypes = [
        "Sales Invoice",
        "Purchase Invoice",
        "Journal Entry",
        "Payment Entry",
    ]
    documents = {
        doctype: erp.get_list(
            doctype,
            fields=["name"],
            filters=[
                ["tally_guid", "is", "set"],
                ["company", "=", company],
            ],
            limit=0,
        )
        for doctype in doctypes
    }
    migrated_names = {
        row["name"]
        for rows in documents.values()
        for row in rows
    }
    gl_entries = erp.get_list(
        "GL Entry",
        fields=["voucher_type", "voucher_no", "debit", "credit"],
        filters=[
            ["company", "=", company],
            ["is_cancelled", "=", 0],
        ],
        limit=0,
    )
    migrated_gl = [
        row for row in gl_entries if row["voucher_no"] in migrated_names
    ]

    totals: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"rows": 0, "debit": Decimal("0"), "credit": Decimal("0")}
    )
    for row in migrated_gl:
        bucket = totals[row["voucher_type"]]
        bucket["rows"] += 1
        bucket["debit"] += Decimal(str(row.get("debit") or 0))
        bucket["credit"] += Decimal(str(row.get("credit") or 0))

    print("MIGRATED DOCUMENTS")
    for doctype, rows in documents.items():
        print(f"  {doctype:<18} {len(rows):>6}")
    print("\nGENERAL LEDGER")
    for voucher_type, values in sorted(totals.items()):
        difference = values["debit"] - values["credit"]
        print(
            f"  {voucher_type:<18} rows={values['rows']:>6} "
            f"debit={values['debit']:.2f} credit={values['credit']:.2f} "
            f"difference={difference:.2f}"
        )
    debit = sum((v["debit"] for v in totals.values()), Decimal("0"))
    credit = sum((v["credit"] for v in totals.values()), Decimal("0"))
    print(
        f"\nTOTAL rows={len(migrated_gl)} debit={debit:.2f} "
        f"credit={credit:.2f} difference={debit - credit:.2f}"
    )
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": company,
        "migrated_documents": {
            doctype: len(rows) for doctype, rows in documents.items()
        },
        "general_ledger": {
            voucher_type: {
                "rows": values["rows"],
                "debit": f"{values['debit']:.2f}",
                "credit": f"{values['credit']:.2f}",
                "difference": f"{values['debit'] - values['credit']:.2f}",
            }
            for voucher_type, values in sorted(totals.items())
        },
        "totals": {
            "rows": len(migrated_gl),
            "debit": f"{debit:.2f}",
            "credit": f"{credit:.2f}",
            "difference": f"{debit - credit:.2f}",
            "balanced": debit == credit,
        },
    }
    report_path = (
        get_config().staging_db.parent
        / "reports"
        / f"erpnext_full_audit_{date.today().isoformat()}.json"
    )
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"REPORT {report_path}")


if __name__ == "__main__":
    main()
