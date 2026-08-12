"""Classify ERP invoice links as explicit Tally Agst Ref or non-source FIFO."""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from t2e.lines import parse_entries


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def bill_refs(payload: dict, bill_type: str) -> set[str]:
    return {
        norm(bill.get("name"))
        for entry in parse_entries(payload)
        for bill in entry.get("bills", [])
        if str(bill.get("type") or "").casefold() == bill_type.casefold()
        and norm(bill.get("name"))
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state")
    parser.add_argument("--staging", default="data/staging.sqlite")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))

    connection = sqlite3.connect(args.staging)
    connection.row_factory = sqlite3.Row
    try:
        source = {
            str(row["guid"]): {
                "party": norm(row["party"]),
                "vtype": row["vtype"],
                "vnumber": row["vnumber"],
                "new_refs": bill_refs(json.loads(row["payload"]), "New Ref"),
                "against_refs": bill_refs(json.loads(row["payload"]), "Agst Ref"),
            }
            for row in connection.execute(
                "select guid,party,vtype,vnumber,payload from voucher")
        }
    finally:
        connection.close()

    invoices = {
        (row["doctype"], row["name"]): row for row in state["invoices"]
    }
    transactions = {
        (row["doctype"], row["name"]): row
        for row in state.get("transactions", [])
    }
    details = []
    for ple in state["payment_ledger"]:
        target_key = (ple["against_voucher_type"], ple["against_voucher_no"])
        target = invoices.get(target_key)
        source_key = (ple["voucher_type"], ple["voucher_no"])
        if not target or source_key == target_key:
            continue
        transaction = invoices.get(source_key) or transactions.get(source_key) or {}
        source_guid = str(transaction.get("tally_guid") or "")
        source_row = source.get(source_guid)
        target_row = source.get(str(target["tally_guid"]))
        if not source_row:
            classification = "untagged_or_derived_erp_document"
            shared_refs = set()
        elif not target_row:
            classification = "target_absent_from_tally_staging"
            shared_refs = set()
        else:
            shared_refs = source_row["against_refs"] & target_row["new_refs"]
            same_party = source_row["party"] == norm(target["party"])
            classification = (
                "explicit_tally_agst_ref"
                if shared_refs and same_party
                else "not_supported_by_tally_agst_ref"
            )
        details.append({
            "classification": classification,
            "target_guid": target["tally_guid"],
            "target_name": target["name"],
            "target_party": target["party"],
            "source_guid": source_guid,
            "source_doctype": ple["voucher_type"],
            "source_name": ple["voucher_no"],
            "source_vtype": source_row["vtype"] if source_row else None,
            "source_number": source_row["vnumber"] if source_row else None,
            "source_against_refs": sorted(source_row["against_refs"]) if source_row else [],
            "target_new_refs": sorted(target_row["new_refs"]) if target_row else [],
            "shared_refs": sorted(shared_refs),
            "amount": f"{money(ple['amount_in_account_currency']):.2f}",
        })

    summary = defaultdict(lambda: {"rows": 0, "absolute_amount": Decimal("0.00")})
    for row in details:
        summary[row["classification"]]["rows"] += 1
        summary[row["classification"]]["absolute_amount"] += abs(money(row["amount"]))
    payload = {
        "site": state["site"],
        "summary": {
            key: {
                "rows": value["rows"],
                "absolute_amount": f"{value['absolute_amount']:.2f}",
            }
            for key, value in sorted(summary.items())
        },
        "details": details,
    }
    output = Path(args.output)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": payload["summary"], "report": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
