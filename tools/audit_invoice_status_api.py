"""Read-only audit of migrated ERPNext invoice status and outstanding value."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import json

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def summarize(rows: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    statuses = Counter(str(row.get("status") or "") for row in rows)
    open_rows = [row for row in rows if money(row.get("outstanding_amount")) > 0]
    return_rows = [row for row in rows if money(row.get("outstanding_amount")) < 0]
    overdue = [
        row for row in open_rows
        if str(row.get("status") or "") == "Overdue"
        or (
            row.get("due_date")
            and date.fromisoformat(str(row["due_date"])) < today
            and str(row.get("status") or "") not in ("Paid", "Return")
        )
    ]
    return {
        "submitted_migrated": len(rows),
        "status_counts": dict(sorted(statuses.items())),
        "open_count": len(open_rows),
        "open_outstanding": f"{sum((money(r.get('outstanding_amount')) for r in open_rows), Decimal('0.00')):.2f}",
        "overdue_count": len(overdue),
        "overdue_outstanding": f"{sum((money(r.get('outstanding_amount')) for r in overdue), Decimal('0.00')):.2f}",
        "open_return_count": len(return_rows),
        "open_return_credit": f"{abs(sum((money(r.get('outstanding_amount')) for r in return_rows), Decimal('0.00'))):.2f}",
    }


def main() -> None:
    cfg = get_config()
    erp = ERPNextClient(dry_run=True)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": cfg.erpnext["company"],
        "mode": "read-only",
        "invoices": {},
    }
    fields = [
        "name", "posting_date", "due_date", "status", "grand_total",
        "outstanding_amount", "tally_guid",
    ]
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        rows = erp.get_list(
            doctype,
            fields=fields,
            filters=[
                ["company", "=", cfg.erpnext["company"]],
                ["docstatus", "=", 1],
                ["tally_guid", "is", "set"],
            ],
            limit=0,
        )
        result["invoices"][doctype] = summarize(rows)

    report = cfg.staging_db.parent / "reports" / "invoice_status_audit.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"REPORT {report}")


if __name__ == "__main__":
    main()
