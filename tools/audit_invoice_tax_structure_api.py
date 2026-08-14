"""Compare staged Tally tax ledgers with live ERPNext invoice child rows."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import urllib.parse

from t2e.erpnext_client import ERPNextClient
from t2e.lines import is_tax_ledger, parse_entries
from t2e.load_invoices import _place_of_supply, _scalar
from t2e.staging import Staging


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def expected_taxes(row) -> Counter:
    payload = json.loads(row["payload"])
    sign = -1 if row["vtype"] in ("Credit Note", "Debit Note") else 1
    return Counter(
        (norm(entry["ledger"]), sign * money(entry["mag"]))
        for entry in parse_entries(payload)
        if is_tax_ledger(entry["ledger"])
    )


def actual_taxes(doc: dict) -> Counter:
    return Counter(
        (norm(row.get("description") or row.get("account_head")),
         money(row.get("tax_amount")))
        for row in doc.get("taxes") or []
        if money(row.get("tax_amount"))
    )


def tax_items(doc: dict, expected_names: set[str]) -> Counter:
    rows = Counter()
    for row in doc.get("items") or []:
        name = norm(row.get("item_name") or row.get("description"))
        if name in expected_names:
            amount = money(row.get("net_amount") or row.get("amount"))
            rows[(name, amount)] += 1
    return rows


def gst_kind(value: str) -> str | None:
    text = norm(value)
    for kind in ("cgst", "sgst", "igst", "cess"):
        if kind in text:
            return kind.upper()
    return None


def expected_gst_breakup(expected: Counter) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for (ledger, amount), count in expected.items():
        kind = gst_kind(ledger)
        if kind:
            totals[kind] = totals.get(kind, Decimal("0.00")) + amount * count
    return totals


def actual_gst_breakup(doc: dict) -> dict[str, Decimal]:
    return {
        kind: sum(
            (money(item.get(f"{kind.lower()}_amount")) for item in doc.get("items") or []),
            Decimal("0.00"),
        )
        for kind in ("CGST", "SGST", "IGST", "CESS")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--guid", action="append", default=[],
                        help="audit only this exact Tally GUID (repeatable)")
    args = parser.parse_args()
    erp = ERPNextClient(dry_run=True)
    store = Staging()
    by_guid = {}
    other_by_guid = {}
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        rows = erp.get_list(
            doctype,
            fields=["name", "tally_guid", "posting_date", "docstatus"],
            filters=[["company", "=", args.company], ["docstatus", "=", 1],
                     ["tally_guid", "is", "set"]],
            limit=0,
        )
        for row in rows:
            by_guid[row["tally_guid"]] = {**row, "doctype": doctype}
    for doctype in ("Journal Entry", "Payment Entry"):
        rows = erp.get_list(
            doctype,
            fields=["name", "tally_guid", "posting_date", "docstatus"],
            filters=[["company", "=", args.company], ["docstatus", "=", 1],
                     ["tally_guid", "is", "set"]],
            limit=0,
        )
        for row in rows:
            other_by_guid[row["tally_guid"]] = {**row, "doctype": doctype}

    details = []
    for source in store.vouchers():
        if source["vtype"] not in ("Sales", "Purchase", "Credit Note", "Debit Note"):
            continue
        if args.guid and source["guid"] not in set(args.guid):
            continue
        expected = expected_taxes(source)
        if not expected:
            continue
        target = by_guid.get(source["guid"])
        if not target:
            other = other_by_guid.get(source["guid"])
            details.append({
                "guid": source["guid"], "source_number": source["vnumber"],
                "source_type": source["vtype"], "source_date": source["vdate"],
                "party": source["party"],
                "classification": (
                    "non_invoice_target" if other else "missing_target"
                ),
                "doctype": other.get("doctype") if other else None,
                "name": other.get("name") if other else None,
            })
            continue
        path = "/api/resource/{}/{}".format(
            urllib.parse.quote(target["doctype"], safe=""),
            urllib.parse.quote(target["name"], safe=""),
        )
        doc = erp._request("GET", path)["data"]
        source_payload = json.loads(source["payload"])
        expected_place = _place_of_supply(
            _scalar(source_payload.get("PLACEOFSUPPLY"))
            or _scalar(source_payload.get("STATENAME")),
            _scalar(source_payload.get("PARTYGSTIN")).strip().upper(),
        )
        actual = actual_taxes(doc)
        expected_names = {name for name, _ in expected}
        items = tax_items(doc, expected_names)
        expected_breakup = expected_gst_breakup(expected)
        actual_breakup = actual_gst_breakup(doc)
        relevant_actual_breakup = {
            key: actual_breakup.get(key, Decimal("0.00"))
            for key in expected_breakup
        }
        if relevant_actual_breakup == expected_breakup:
            breakup_classification = "correct"
        elif not any(relevant_actual_breakup.values()):
            breakup_classification = "zero"
        else:
            breakup_classification = "mismatch"
        if actual == expected and not items:
            classification = "correct_tax_rows"
        elif items == expected and not actual:
            classification = "taxes_as_items"
        else:
            classification = "other_mismatch"
        details.append({
            "guid": source["guid"],
            "source_type": source["vtype"],
            "source_number": source["vnumber"],
            "source_date": source["vdate"],
            "party": source["party"],
            "doctype": target["doctype"],
            "name": target["name"],
            "supplier_bill_no": doc.get("bill_no"),
            "expected_place_of_supply": expected_place,
            "actual_place_of_supply": doc.get("place_of_supply") or "",
            "classification": classification,
            "gst_breakup_classification": breakup_classification,
            "expected_gst_breakup": {
                key: f"{value:.2f}" for key, value in expected_breakup.items()
            },
            "actual_gst_breakup": {
                key: f"{value:.2f}" for key, value in relevant_actual_breakup.items()
            },
            "expected_taxes": [
                {"ledger": name, "amount": f"{amount:.2f}", "count": count}
                for (name, amount), count in expected.items()
            ],
            "actual_taxes": [
                {"ledger": name, "amount": f"{amount:.2f}", "count": count}
                for (name, amount), count in actual.items()
            ],
            "tax_item_rows": [
                {"ledger": name, "amount": f"{amount:.2f}", "count": count}
                for (name, amount), count in items.items()
            ],
            "net_total": f"{money(doc.get('net_total')):.2f}",
            "total_taxes_and_charges": f"{money(doc.get('total_taxes_and_charges')):.2f}",
            "grand_total": f"{money(doc.get('grand_total')):.2f}",
            "status": doc.get("status"),
        })

    counts = Counter(row["classification"] for row in details)
    breakup_counts = Counter(
        row["gst_breakup_classification"] for row in details
        if row.get("gst_breakup_classification")
    )
    place_counts = Counter(
        "correct" if row.get("expected_place_of_supply") == row.get("actual_place_of_supply")
        else "mismatch"
        for row in details
        if row.get("expected_place_of_supply") is not None
    )
    payload = {
        "company": args.company,
        "mode": "read-only",
        "source_invoices_with_tax": len(details),
        "classifications": dict(sorted(counts.items())),
        "gst_breakup_classifications": dict(sorted(breakup_counts.items())),
        "place_of_supply_classifications": dict(sorted(place_counts.items())),
        "details": details,
    }
    output = Path(args.output).resolve()
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "details"}, indent=2))
    print(f"REPORT {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
