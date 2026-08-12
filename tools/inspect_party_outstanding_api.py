"""Inspect ERPNext's unreconciled entries for one party (read-only)."""
from __future__ import annotations

import argparse
import json

from t2e.erpnext_client import ERPNextClient
from t2e.load_masters import fetch_company_defaults


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("party_type", choices=["Customer", "Supplier"])
    parser.add_argument("party")
    parser.add_argument("--allocate", action="store_true")
    args = parser.parse_args()

    erp = ERPNextClient(dry_run=True)
    defaults = fetch_company_defaults(erp)
    account = (
        defaults.receivable if args.party_type == "Customer"
        else defaults.payable
    )
    doc = erp.run_doc_method("get_unreconciled_entries", {
        "doctype": "Payment Reconciliation",
        "company": defaults.name,
        "party_type": args.party_type,
        "party": args.party,
        "receivable_payable_account": account,
    })
    if args.allocate:
        doc = erp.run_doc_method(
            "allocate_entries", doc,
            args={
                "invoices": doc.get("invoices") or [],
                "payments": doc.get("payments") or [],
            },
        )
    print(json.dumps({
        "party_type": args.party_type,
        "party": args.party,
        "invoices": doc.get("invoices") or [],
        "payments": doc.get("payments") or [],
        "allocation": doc.get("allocation") or [],
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
