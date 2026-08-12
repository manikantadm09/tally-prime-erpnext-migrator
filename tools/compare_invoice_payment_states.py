"""Compare two read-only invoice/Payment Ledger state exports by Tally GUID."""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sqlite3


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY)


def indexes(payload: dict) -> tuple[dict, dict, dict]:
    invoices = {
        (row["doctype"], row["name"]): row for row in payload["invoices"]
    }
    transactions = {
        (row["doctype"], row["name"]): row
        for row in payload.get("transactions", [])
    }
    by_guid = {row["tally_guid"]: row for row in payload["invoices"]}
    return invoices, transactions, by_guid


def allocation_map(payload: dict) -> tuple[dict, list[dict]]:
    invoices, transactions, _ = indexes(payload)
    grouped: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0.00"))
    rows = []
    for ple in payload["payment_ledger"]:
        target_key = (ple["against_voucher_type"], ple["against_voucher_no"])
        target = invoices.get(target_key)
        if not target:
            continue
        source_key = (ple["voucher_type"], ple["voucher_no"])
        if source_key == target_key:
            continue
        source = invoices.get(source_key) or transactions.get(source_key) or {}
        source_guid = str(source.get("tally_guid") or "")
        source_identity = source_guid or f"{ple['voucher_type']}:{ple['voucher_no']}"
        amount = money(ple["amount_in_account_currency"])
        grouped[(target["tally_guid"], source_identity)] += amount
        rows.append({
            "target_guid": target["tally_guid"],
            "target_doctype": target["doctype"],
            "target_name": target["name"],
            "target_party": target["party"],
            "source_guid": source_guid,
            "source_doctype": ple["voucher_type"],
            "source_name": ple["voucher_no"],
            "amount": f"{amount:.2f}",
        })
    return dict(grouped), rows


def source_kind(source_identity: str, real_guids: set[str]) -> str:
    if source_identity in real_guids:
        return "direct_tally_voucher"
    if ":" in source_identity and source_identity.split(":", 1)[0] in real_guids:
        return "derived_from_tally_voucher"
    if source_identity.startswith(("Payment Entry:", "Journal Entry:")):
        return "untagged_erp_document"
    return "unknown_guid"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--staging", default="data/staging.sqlite")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    left = json.loads(Path(args.left).read_text(encoding="utf-8"))
    right = json.loads(Path(args.right).read_text(encoding="utf-8"))
    _, _, left_guid = indexes(left)
    _, _, right_guid = indexes(right)
    left_alloc, left_rows = allocation_map(left)
    right_alloc, right_rows = allocation_map(right)

    connection = sqlite3.connect(args.staging)
    try:
        real_guids = {
            str(row[0]) for row in connection.execute("select guid from voucher")
        }
    finally:
        connection.close()

    common = set(left_guid) & set(right_guid)
    invoice_differences = []
    for guid in common:
        left_open = money(left_guid[guid]["outstanding_amount"])
        right_open = money(right_guid[guid]["outstanding_amount"])
        if left_open != right_open:
            invoice_differences.append({
                "guid": guid,
                "left_name": left_guid[guid]["name"],
                "right_name": right_guid[guid]["name"],
                "doctype": left_guid[guid]["doctype"],
                "party": left_guid[guid]["party"],
                "left_outstanding": f"{left_open:.2f}",
                "right_outstanding": f"{right_open:.2f}",
                "difference": f"{right_open - left_open:.2f}",
            })

    allocation_keys = set(left_alloc) | set(right_alloc)
    allocation_differences = []
    for target_guid, source_identity in allocation_keys:
        left_amount = left_alloc.get((target_guid, source_identity), Decimal("0.00"))
        right_amount = right_alloc.get((target_guid, source_identity), Decimal("0.00"))
        if left_amount != right_amount:
            allocation_differences.append({
                "target_guid": target_guid,
                "source_identity": source_identity,
                "source_kind": source_kind(source_identity, real_guids),
                "left_amount": f"{left_amount:.2f}",
                "right_amount": f"{right_amount:.2f}",
                "difference": f"{right_amount - left_amount:.2f}",
            })

    def allocation_summary(rows: list[dict]) -> dict:
        kinds: dict[str, dict] = defaultdict(
            lambda: {"rows": 0, "absolute_amount": Decimal("0.00")})
        for row in rows:
            identity = row["source_guid"] or (
                f"{row['source_doctype']}:{row['source_name']}")
            kind = source_kind(identity, real_guids)
            kinds[kind]["rows"] += 1
            kinds[kind]["absolute_amount"] += abs(money(row["amount"]))
        return {
            key: {
                "rows": value["rows"],
                "absolute_amount": f"{value['absolute_amount']:.2f}",
            }
            for key, value in sorted(kinds.items())
        }

    payload = {
        "left": left["site"],
        "right": right["site"],
        "invoice_documents": {
            "left": len(left["invoices"]),
            "right": len(right["invoices"]),
            "common_guids": len(common),
            "left_only_guids": len(set(left_guid) - set(right_guid)),
            "right_only_guids": len(set(right_guid) - set(left_guid)),
            "outstanding_differences": len(invoice_differences),
            "left_total_outstanding": f"{sum((money(r['outstanding_amount']) for r in left['invoices']), Decimal('0.00')):.2f}",
            "right_total_outstanding": f"{sum((money(r['outstanding_amount']) for r in right['invoices']), Decimal('0.00')):.2f}",
        },
        "allocation_sources": {
            "left": allocation_summary(left_rows),
            "right": allocation_summary(right_rows),
            "different_target_source_pairs": len(allocation_differences),
        },
        "left_only_invoices": [
            left_guid[guid] for guid in sorted(set(left_guid) - set(right_guid))
        ],
        "right_only_invoices": [
            right_guid[guid] for guid in sorted(set(right_guid) - set(left_guid))
        ],
        "invoice_differences": sorted(
            invoice_differences,
            key=lambda row: abs(money(row["difference"])),
            reverse=True,
        ),
        "allocation_differences": sorted(
            allocation_differences,
            key=lambda row: abs(money(row["difference"])),
            reverse=True,
        ),
    }
    output = Path(args.output)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "invoice_documents": payload["invoice_documents"],
        "allocation_sources": payload["allocation_sources"],
        "report": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
