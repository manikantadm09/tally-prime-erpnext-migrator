"""One-off, PRD-safe reclassification for MAX ALUMINIUM only.

Tally parents MAX ALUMINIUM under "Advances to Vendors" (a Current Assets
group), but its ERPNext transactions defaulted to the generic "Creditors"
payable control account instead -- a pre-existing gap from the original
migration (see the historic "Advances to Vendors" account-group failure in
failures.csv). This mirrors the already-established, already-applied
reclassification pattern used for 12+ other vendors (t2e/repair_vendor_advance_control.py),
but as a single current-dated journal instead of rebuilding historical
Period Closing Vouchers -- repair_vendor_advance_control.py explicitly
refuses to run against production for exactly that reason.

Reads MAX ALUMINIUM's live net balance in Creditors, and posts ONE
balance-neutral Journal Entry moving that exact amount to
"Advances to Vendors - SDL". No other party, voucher, or historical period
is touched. Suppliers stay Suppliers; only the control account changes.

Usage:
    python -m tools.repair_max_aluminium_prd            # plan only
    python -m tools.repair_max_aluminium_prd --confirm   # execute
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient

PENNY = Decimal("0.01")
PARTY = "MAX ALUMINIUM"
CREDITORS = "Creditors - SDL"
CONTROL = "Advances to Vendors - SDL"
KEY = "vendor-advance-control-prd-max-aluminium-catchup"


def _money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    cfg = get_config()
    if "erp.spaceki.com" not in cfg.erp_url:
        raise SystemExit(f"refusing non-production target {cfg.erp_url}")

    erp = ERPNextClient(dry_run=not args.confirm)
    field = cfg.idempotency_field

    existing = erp.find_by_field("Journal Entry", field, KEY, exclude_cancelled=True)
    if existing:
        print(f"Already applied: {existing}. Nothing to do.")
        return

    rows = erp.get_list(
        "GL Entry",
        fields=["debit", "credit"],
        filters=[["account", "=", CREDITORS], ["party", "=", PARTY], ["is_cancelled", "=", 0]],
        limit=0,
    )
    net = sum((_money(r["debit"]) - _money(r["credit"]) for r in rows), Decimal("0"))
    print(f"{PARTY} current net in {CREDITORS}: {net}")

    if net == 0:
        print("Already zero. Nothing to do.")
        return

    def line(account: str, debit: Decimal, credit: Decimal) -> dict:
        row = {"account": account, "party_type": "Supplier", "party": PARTY}
        if debit > 0:
            row["debit_in_account_currency"] = float(debit)
        else:
            row["credit_in_account_currency"] = float(credit)
        return row

    amt = abs(net)
    if net > 0:
        accounts = [line(CONTROL, amt, Decimal("0")), line(CREDITORS, Decimal("0"), amt)]
    else:
        accounts = [line(CREDITORS, amt, Decimal("0")), line(CONTROL, Decimal("0"), amt)]

    je = {
        "company": cfg.erpnext["company"],
        "posting_date": cfg.tally["to_date"][:4] + "-" + cfg.tally["to_date"][4:6] + "-" + cfg.tally["to_date"][6:],
        "voucher_type": "Journal Entry",
        "title": f"Reclass {PARTY} Advances to Vendors (PRD catch-up)",
        "user_remark": (
            "Reclass Advances to Vendors debit from Creditors onto the "
            "Current Assets control for MAX ALUMINIUM only. Supplier "
            "unchanged; invoices stay on Creditors. Matches the pattern "
            "already applied for other vendors under this Tally group."
        ),
        "accounts": accounts,
        field: KEY,
    }
    print("Planned journal entry:")
    import json
    print(json.dumps(je, indent=2))

    if not args.confirm:
        print("\nDry run only. Re-run with --confirm to post.")
        return

    result = erp.insert_and_submit("Journal Entry", je)
    print("Created and submitted:", result.get("name") if isinstance(result, dict) else result)


if __name__ == "__main__":
    main()
