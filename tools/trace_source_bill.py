"""Trace every staged Tally allocation for one party + bill reference."""
from __future__ import annotations

import argparse
import json

from t2e.lines import parse_entries
from t2e.staging import Staging
from tools.verify_bill_outstandings_api import norm


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("party")
    parser.add_argument("bill_ref")
    args = parser.parse_args()
    party_key = norm(args.party)
    ref_key = norm(args.bill_ref)
    store = Staging()
    rows = []
    for voucher in store.vouchers():
        for entry in parse_entries(json.loads(voucher["payload"])):
            if norm(entry["ledger"]) != party_key:
                continue
            bills = [bill for bill in entry["bills"] if norm(bill["name"]) == ref_key]
            if bills:
                rows.append({
                    "guid": voucher["guid"],
                    "vtype": voucher["vtype"],
                    "vnumber": voucher["vnumber"],
                    "vdate": voucher["vdate"],
                    "erp_doctype": voucher["erp_doctype"],
                    "erp_name": voucher["erp_name"],
                    "ledger_debit": entry["debit"],
                    "ledger_amount": entry["mag"],
                    "bills": bills,
                    "all_bills_on_party_line": entry["bills"],
                })
    store.close()
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
