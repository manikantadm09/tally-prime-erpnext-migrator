"""Trace a source Tally ledger/target ERPNext account delta per voucher."""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.lines import parse_entries
from t2e.staging import Staging


CENT = Decimal("0.01")
DOCTYPES = ("Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ledger", required=True)
    parser.add_argument("--target-account", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    cfg = get_config()
    company = cfg.erpnext["company"]
    erp = ERPNextClient(dry_run=True)
    store = Staging()
    documents_by_source_guid = defaultdict(list)
    for doctype in DOCTYPES:
        rows = erp.get_list(
            doctype,
            fields=["name", "tally_guid", "posting_date", "docstatus"],
            filters=[["company", "=", company], ["tally_guid", "is", "set"],
                     ["docstatus", "=", 1]],
            limit=0,
        )
        for row in rows:
            guid = str(row.get("tally_guid") or "")
            source_guid = guid.split(":", 1)[0]
            documents_by_source_guid[source_guid].append({
                **row, "doctype": doctype, "derived_guid": guid,
            })
    target_by_voucher = defaultdict(lambda: Decimal("0.00"))
    target_rows = erp.get_list(
        "GL Entry",
        fields=["voucher_type", "voucher_no", "posting_date", "debit", "credit"],
        filters=[["company", "=", company], ["is_cancelled", "=", 0],
                 ["account", "=", args.target_account]],
        limit=0,
    )
    for row in target_rows:
        target_by_voucher[row["voucher_no"]] += (
            money(row.get("debit")) - money(row.get("credit"))
        )

    details = []
    source_total = Decimal("0.00")
    target_total = Decimal("0.00")
    for row in store.vouchers():
        payload = json.loads(row["payload"])
        source = sum(
            (
                money(entry["mag"]) if entry["debit"] else -money(entry["mag"])
                for entry in parse_entries(payload)
                if norm(entry["ledger"]) == norm(args.source_ledger)
            ),
            Decimal("0.00"),
        )
        documents = documents_by_source_guid.get(str(row["guid"]), [])
        primary = next(
            (doc for doc in documents if doc["derived_guid"] == str(row["guid"])),
            None,
        )
        target = sum(
            (target_by_voucher.get(doc["name"], Decimal("0.00"))
             for doc in documents),
            Decimal("0.00"),
        )
        source_total += source
        target_total += target
        if source != target:
            details.append({
                "guid": row["guid"],
                "source_type": row["vtype"],
                "source_number": row["vnumber"],
                "source_date": row["vdate"],
                "target_doctype": primary.get("doctype") if primary else None,
                "target_name": primary.get("name") if primary else None,
                "derived_documents": [
                    {
                        "doctype": doc["doctype"], "name": doc["name"],
                        "tally_guid": doc["derived_guid"],
                        "target_dr_minus_cr": f"{target_by_voucher.get(doc['name'], Decimal('0.00')):.2f}",
                    }
                    for doc in documents
                    if target_by_voucher.get(doc["name"], Decimal("0.00"))
                ],
                "source_dr_minus_cr": f"{source:.2f}",
                "target_dr_minus_cr": f"{target:.2f}",
                "difference": f"{target - source:.2f}",
            })
    details.sort(key=lambda row: abs(money(row["difference"])), reverse=True)
    report = {
        "company": company,
        "source_ledger": args.source_ledger,
        "target_account": args.target_account,
        "source_total": f"{source_total:.2f}",
        "target_total": f"{target_total:.2f}",
        "difference": f"{target_total - source_total:.2f}",
        "difference_count": len(details),
        "details": details,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
