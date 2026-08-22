"""Cancel/delete ALL accounting transactions for one ERPNext company.

Batched so progress is visible and memory stays small. For remigration only.

  python -u tools/purge_company_transactions.py --dry-run
  python -u tools/purge_company_transactions.py --confirm \\
      --company "Sanrad Medical Systems Private Limited" \\
      --confirm-company "Sanrad Medical Systems Private Limited"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient, ERPNextError

TXN_DOCTYPES = [
    "Period Closing Voucher",
    "Payment Entry",
    "Sales Invoice",
    "Purchase Invoice",
    "Journal Entry",
]
PAGE = 50


def _log(msg: str) -> None:
    print(msg, flush=True)


def purge_doctype(erp: ERPNextClient, doctype: str, company: str) -> dict[str, int]:
    stats = {"cancelled": 0, "deleted": 0, "left_cancelled": 0, "errors": 0, "passes": 0}
    while True:
        rows = erp.get_list(
            doctype,
            fields=["name", "docstatus"],
            filters=[["company", "=", company], ["docstatus", "!=", 2]],
            limit=PAGE,
        )
        if not rows:
            break
        stats["passes"] += 1
        _log(f"  {doctype}: batch {stats['passes']} size={len(rows)}")
        for row in rows:
            name = row["name"]
            status = int(row.get("docstatus") or 0)
            try:
                if status == 1:
                    erp.cancel(doctype, name)
                    stats["cancelled"] += 1
                    status = 2
                if status == 0:
                    erp.delete(doctype, name)
                    stats["deleted"] += 1
            except ERPNextError as exc:
                stats["errors"] += 1
                if stats["errors"] <= 30:
                    _log(f"    ! {doctype} {name}: {str(exc)[:160]}")
        _log(
            f"    totals cancelled={stats['cancelled']} deleted={stats['deleted']} "
            f"errors={stats['errors']}"
        )
    # Count remaining cancelled shells (inactive; REST often cannot delete)
    left = erp.get_list(
        doctype, fields=["name"],
        filters=[["company", "=", company], ["docstatus", "=", 2]],
        limit=0,
    )
    stats["left_cancelled"] = len(left)
    _log(f"  {doctype} done; cancelled shells remaining={stats['left_cancelled']}")
    return stats


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--company", default=None)
    p.add_argument("--confirm-company", default=None)
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cfg = get_config()
    company = args.company or cfg.erpnext["company"]
    if args.confirm:
        if args.confirm_company != company:
            raise SystemExit(
                "--confirm-company must exactly match --company / config company")
        if args.dry_run:
            raise SystemExit("Pass only one of --dry-run or --confirm")

    dry = not args.confirm
    erp = ERPNextClient(dry_run=dry)
    mode = "DRY-RUN" if dry else "CONFIRM WRITE"
    _log(f"=== purge company transactions | {mode} | {company} ===")
    if dry:
        for doctype in TXN_DOCTYPES:
            active = erp.get_count(
                doctype,
                filters=[["company", "=", company], ["docstatus", "!=", 2]],
            )
            cancelled = erp.get_count(
                doctype,
                filters=[["company", "=", company], ["docstatus", "=", 2]],
            )
            _log(f"  {doctype}: active/draft={active} cancelled={cancelled}")
        _log("Re-run with --confirm --company NAME --confirm-company NAME")
        return 0

    result = {}
    for doctype in TXN_DOCTYPES:
        result[doctype] = purge_doctype(erp, doctype, company)
    _log(f"done: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
