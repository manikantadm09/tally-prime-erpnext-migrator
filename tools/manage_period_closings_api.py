"""Plan or open migration-created Period Closing Vouchers for a repair window.

Plan-only by default. Apply requires an explicit backup path and exact company
confirmation. Only submitted, migration-tagged PCVs ending on/after the given
date are in scope, and they are cancelled newest-first.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient


def money(value) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"


def gl_signature(erp: ERPNextClient, name: str) -> dict:
    rows = erp.get_list(
        "GL Entry", fields=["account", "debit", "credit"],
        filters=[["voucher_type", "=", "Period Closing Voucher"],
                 ["voucher_no", "=", name], ["is_cancelled", "=", 0]],
        limit=0)
    return {
        "rows": len(rows),
        "debit": money(sum(float(row.get("debit") or 0) for row in rows)),
        "credit": money(sum(float(row.get("credit") or 0) for row in rows)),
        "accounts": sorted({row.get("account") for row in rows}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--from-date", required=True, help="yyyy-mm-dd")
    parser.add_argument("--backup")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.company != args.confirm_company:
        raise SystemExit("company confirmation mismatch")
    if args.confirm and not args.backup:
        raise SystemExit("--confirm requires --backup")

    cfg = get_config()
    erp = ERPNextClient(dry_run=not args.confirm)
    rows = erp.get_list(
        "Period Closing Voucher",
        fields=["name", "company", "period_start_date", "period_end_date",
                "fiscal_year", "docstatus", cfg.idempotency_field],
        filters=[["company", "=", args.company], ["docstatus", "=", 1],
                 ["period_end_date", ">=", args.from_date]],
        limit=0)
    rows.sort(key=lambda row: row.get("period_end_date") or "", reverse=True)
    expected = {
        f"period-closing-{year}"
        for year in cfg.yaml["period_closing"]["fiscal_years"]
    }
    for row in rows:
        key = row.get(cfg.idempotency_field)
        if key not in expected:
            raise RuntimeError(
                f"refusing non-migration closing {row['name']} ({key!r})")
        row["gl"] = gl_signature(erp, row["name"])
    if args.confirm:
        for row in rows:
            erp.cancel("Period Closing Voucher", row["name"])
        remaining = erp.get_list(
            "Period Closing Voucher", fields=["name"],
            filters=[["company", "=", args.company], ["docstatus", "=", 1],
                     ["period_end_date", ">=", args.from_date]], limit=0)
        if remaining:
            raise RuntimeError(f"active target closings remain: {remaining}")
    report = {
        "company": args.company,
        "plan_only": not args.confirm,
        "from_date": args.from_date,
        "backup": args.backup,
        "closings": rows,
        "count": len(rows),
        "pass": True,
    }
    output = Path(args.output).resolve()
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
