"""Compare cancelled and recreated Period Closing Voucher GL account totals."""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json

from t2e.erpnext_client import ERPNextClient


CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


def signature(erp: ERPNextClient, name: str) -> dict:
    rows = erp.get_list(
        "GL Entry",
        fields=["account", "debit", "credit", "is_cancelled"],
        filters=[["voucher_type", "=", "Period Closing Voucher"],
                 ["voucher_no", "=", name]],
        limit=0,
    )
    accounts: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
    )
    for row in rows:
        account = row.get("account") or ""
        accounts[account]["debit"] += money(row.get("debit"))
        accounts[account]["credit"] += money(row.get("credit"))
    return {
        "rows": len(rows),
        "cancelled_values": sorted({int(row.get("is_cancelled") or 0) for row in rows}),
        "debit": sum((value["debit"] for value in accounts.values()), Decimal("0.00")),
        "credit": sum((value["credit"] for value in accounts.values()), Decimal("0.00")),
        "accounts": accounts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    args = parser.parse_args()
    erp = ERPNextClient(dry_run=True)
    old = signature(erp, args.old)
    new = signature(erp, args.new)
    differences = []
    for account in sorted(set(old["accounts"]) | set(new["accounts"])):
        left = old["accounts"].get(account, {"debit": Decimal("0"), "credit": Decimal("0")})
        right = new["accounts"].get(account, {"debit": Decimal("0"), "credit": Decimal("0")})
        if left != right:
            differences.append({
                "account": account,
                "old": left,
                "new": right,
                "debit_delta": right["debit"] - left["debit"],
                "credit_delta": right["credit"] - left["credit"],
            })
    result = {
        "old": {key: value for key, value in old.items() if key != "accounts"},
        "new": {key: value for key, value in new.items() if key != "accounts"},
        "differences": differences,
        "pass": not differences,
    }
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
