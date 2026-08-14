"""Three-way, read-only audit of local invoice fallbacks and bill links.

The audit treats Tally staging as source evidence, the local ERPNext site as a
candidate high-fidelity representation, and production as the target to repair.
It deliberately does not claim that a local FIFO allocation is Tally truth.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3

from t2e.lines import parse_entries
from tools.verify_bill_outstandings_api import parse_tally_bills


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def source_rows(path: Path) -> dict[str, dict]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select guid,vtype,vnumber,vdate,party,amount,payload "
            "from voucher where source_present=1"
        ).fetchall()
    finally:
        connection.close()
    output = {}
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["entries"] = parse_entries(item["payload"])
        item["narration"] = str(item["payload"].get("NARRATION") or "")
        item["bill_types"] = sorted({
            str(bill.get("type") or "")
            for entry in item["entries"] for bill in entry.get("bills", [])
            if bill.get("type")
        })
        item["against_refs"] = sorted({
            norm(bill.get("name"))
            for entry in item["entries"] for bill in entry.get("bills", [])
            if norm(bill.get("type")) == "agst ref" and norm(bill.get("name"))
        })
        item["new_refs"] = sorted({
            norm(bill.get("name"))
            for entry in item["entries"] for bill in entry.get("bills", [])
            if norm(bill.get("type")) == "new ref" and norm(bill.get("name"))
        })
        party_key = norm(item["party"])
        party_entries = [e for e in item["entries"] if norm(e["ledger"]) == party_key]
        item["party_amount"] = sum(
            (money(e["mag"]) for e in party_entries), Decimal("0.00"))
        output[str(item["guid"])] = item
    return output


def evidence_index(payload: dict) -> dict[tuple[str, str], dict]:
    output = {}
    payment_refs = defaultdict(list)
    for row in payload.get("payment_references", []):
        payment_refs[row["parent"]].append(row)
    journal_accounts = defaultdict(list)
    for row in payload.get("journal_accounts", []):
        journal_accounts[row["parent"]].append(row)
    for row in payload.get("payment_entries", []):
        item = dict(row)
        item["references"] = payment_refs[row["name"]]
        output[("Payment Entry", row["name"])] = item
    for row in payload.get("journal_entries", []):
        item = dict(row)
        item["accounts"] = journal_accounts[row["name"]]
        output[("Journal Entry", row["name"])] = item
    return output


def source_for_transaction(transaction: dict, sources: dict[str, dict]):
    raw_guid = str(transaction.get("tally_guid") or "")
    if raw_guid in sources:
        return raw_guid, sources[raw_guid], "direct_tally_voucher"
    if ":" in raw_guid:
        base = raw_guid.split(":", 1)[0]
        if base in sources:
            return base, sources[base], "derived_from_tally_voucher"
    return raw_guid, None, "untagged_or_generated_local_document"


def iso_days(left, right):
    try:
        return (date.fromisoformat(str(left)) - date.fromisoformat(str(right))).days
    except (TypeError, ValueError):
        return None


def source_invoice_refs(source: dict) -> tuple[list[str], Decimal]:
    party_key = norm(source.get("party"))
    party_entries = [
        entry for entry in source.get("entries", [])
        if norm(entry.get("ledger")) == party_key
    ]
    candidates = party_entries or [
        entry for entry in source.get("entries", []) if entry.get("bills")
    ]
    refs = []
    referenced = Decimal("0.00")
    for entry in candidates:
        for bill in entry.get("bills", []):
            if str(bill.get("type") or "") in ("New Ref", "Agst Ref"):
                ref = str(bill.get("name") or "").strip()
                if ref and norm(ref) not in {norm(value) for value in refs}:
                    refs.append(ref)
                referenced += money(bill.get("signed_amount"))
    party_total = sum(
        (money(entry["mag"]) for entry in candidates), Decimal("0.00"))
    if not refs:
        refs = [str(source.get("vnumber") or source.get("guid", "")[:20]).strip()]
    return refs, max(party_total - abs(referenced), Decimal("0.00"))


def tally_outstanding_groups(local: dict, sources: dict[str, dict], payable_xml: Path):
    native = defaultdict(list)
    for row in parse_tally_bills(payable_xml):
        native[(norm(row["party"]), norm(row["bill_ref"]))].append(row)
    records = []
    for invoice in local.get("invoices", []):
        if invoice.get("doctype") != "Purchase Invoice" or int(invoice.get("is_return") or 0):
            continue
        source = sources.get(str(invoice.get("tally_guid")))
        if not source or source.get("vtype") != "Purchase":
            continue
        refs, unreferenced = source_invoice_refs(source)
        records.append({
            "guid": str(invoice["tally_guid"]),
            "party": invoice["party"],
            "refs": refs,
            "ref_keys": {norm(ref) for ref in refs},
            "unreferenced": unreferenced,
            "erp_open": abs(money(invoice.get("outstanding_amount"))),
        })
    by_party = defaultdict(list)
    for record in records:
        by_party[norm(record["party"])].append(record)
    result = {}
    for party_key, party_records in by_party.items():
        components = []
        for record in party_records:
            touching = [c for c in components if c["ref_keys"] & record["ref_keys"]]
            if not touching:
                components.append({"ref_keys": set(record["ref_keys"]), "records": [record]})
                continue
            base = touching[0]
            base["ref_keys"].update(record["ref_keys"])
            base["records"].append(record)
            for extra in touching[1:]:
                base["ref_keys"].update(extra["ref_keys"])
                base["records"].extend(extra["records"])
                components.remove(extra)
        for component in components:
            matches = []
            for ref_key in component["ref_keys"]:
                matches.extend(native.get((party_key, ref_key), []))
            expected = sum(
                (abs(money(row["amount"])) for row in matches), Decimal("0.00"))
            expected += sum(
                (row["unreferenced"] for row in component["records"]),
                Decimal("0.00"),
            )
            actual = sum(
                (row["erp_open"] for row in component["records"]),
                Decimal("0.00"),
            )
            summary = {
                "group_invoice_count": len(component["records"]),
                "native_match_count": len(matches),
                "tally_expected_outstanding": f"{expected:.2f}",
                "local_outstanding": f"{actual:.2f}",
                "difference": f"{actual - expected:.2f}",
                "matches_tally_native_bills": abs(actual - expected) <= PENNY,
            }
            for row in component["records"]:
                result[row["guid"]] = summary
    return result


def classify(row: dict) -> tuple[str, str]:
    if row["transaction_tally_guid"].endswith(":party-control-bridge"):
        return (
            "legacy_party_control_bridge_not_payment",
            "GL reclassification was linked to the invoice by the old loader; it is not a payment",
        )
    if row["source_kind"] != "direct_tally_voucher":
        return (
            "generated_local_settlement_requires_trace",
            "local link comes from a derived/untagged document, not a direct Tally voucher",
        )
    if not row["same_party"]:
        return "reject_party_mismatch", "source and invoice parties differ"
    if row["shared_bill_refs"]:
        return "explicit_tally_agst_ref", "Tally source and invoice share an explicit bill reference"
    if row["exact_whole_payment"] and row["days_payment_after_invoice"] == 0:
        return "strong_exact_same_day", "same party, mutually exact whole payment on invoice date"
    if row["exact_whole_payment"] and row["days_payment_after_invoice"] is not None:
        days = row["days_payment_after_invoice"]
        if 0 <= days <= 7:
            return "strong_exact_within_7_days", "same party and exact whole payment within 7 days"
        if 0 <= days <= 30:
            return "review_exact_within_30_days", "same party and exact whole payment within 30 days"
    if row["allocation_equals_source_party_amount"]:
        return "review_exact_payment_partial_invoice", "whole payment applied to a larger/partly settled invoice"
    return "fifo_or_partial_requires_manual_proof", "allocation is partial/FIFO and has no explicit Tally bill reference"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-state", required=True)
    parser.add_argument("--local-evidence", required=True)
    parser.add_argument("--production-state", required=True)
    parser.add_argument("--staging", required=True)
    parser.add_argument("--native-payable", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    local = json.loads(Path(args.local_state).read_text(encoding="utf-8"))
    local_evidence = json.loads(Path(args.local_evidence).read_text(encoding="utf-8"))
    production = json.loads(Path(args.production_state).read_text(encoding="utf-8"))
    sources = source_rows(Path(args.staging))
    tally_groups = tally_outstanding_groups(
        local, sources, Path(args.native_payable))

    local_invoices = {(r["doctype"], r["name"]): r for r in local["invoices"]}
    local_by_guid = {str(r["tally_guid"]): r for r in local["invoices"]}
    production_guids = {str(r["tally_guid"]) for r in production["invoices"]}
    fallback_guids = set(local_by_guid) - production_guids
    transactions = evidence_index(local_evidence)

    links = []
    for ple in local.get("payment_ledger", []):
        target_key = (ple["against_voucher_type"], ple["against_voucher_no"])
        target = local_invoices.get(target_key)
        if not target or str(target["tally_guid"]) not in fallback_guids:
            continue
        source_key = (ple["voucher_type"], ple["voucher_no"])
        if source_key == target_key:
            continue
        transaction = transactions.get(source_key, {})
        source_guid, source, source_kind = source_for_transaction(transaction, sources)
        target_source = sources.get(str(target["tally_guid"]))
        shared = sorted(
            set(source.get("against_refs", []) if source else [])
            & set(target_source.get("new_refs", []) if target_source else [])
        )
        allocation = abs(money(ple.get("amount_in_account_currency")))
        transaction_parties = {
            norm(value) for value in [transaction.get("party")]
            if norm(value)
        }
        for account in transaction.get("accounts", []):
            if norm(account.get("party")):
                transaction_parties.add(norm(account["party"]))
        target_party_key = norm(target.get("party"))
        source_party_entries = [
            entry for entry in (source.get("entries", []) if source else [])
            if norm(entry.get("ledger")) == target_party_key
        ]
        source_party_amount = sum(
            (money(entry["mag"]) for entry in source_party_entries),
            Decimal("0.00"),
        )
        if not source_party_amount and ple["voucher_type"] == "Payment Entry":
            source_party_amount = max(
                money(transaction.get("paid_amount")),
                money(transaction.get("received_amount")),
            )
        if not source_party_amount and ple["voucher_type"] == "Journal Entry":
            source_party_amount = sum((
                money(account.get("debit_in_account_currency"))
                + money(account.get("credit_in_account_currency"))
                for account in transaction.get("accounts", [])
                if norm(account.get("party")) == target_party_key
            ), Decimal("0.00"))
        invoice_total = abs(money(target.get("grand_total")))
        row = {
            "invoice_guid": str(target["tally_guid"]),
            "invoice_name": target["name"],
            "invoice_party": target["party"],
            "invoice_date": target["posting_date"],
            "invoice_total": f"{invoice_total:.2f}",
            "local_status": target["status"],
            "local_outstanding": f"{money(target['outstanding_amount']):.2f}",
            "source_kind": source_kind,
            "source_guid": source_guid,
            "transaction_tally_guid": str(transaction.get("tally_guid") or ""),
            "source_doctype": ple["voucher_type"],
            "source_name": ple["voucher_no"],
            "source_vtype": source.get("vtype") if source else None,
            "source_number": source.get("vnumber") if source else None,
            "source_date": source.get("vdate") if source else transaction.get("posting_date"),
            "source_party": source.get("party") if source else transaction.get("party"),
            "source_party_amount": f"{source_party_amount:.2f}",
            "allocation_amount": f"{allocation:.2f}",
            "source_narration": source.get("narration") if source else (
                transaction.get("remarks") or transaction.get("user_remark")
                or transaction.get("remark") or ""),
            "source_bill_types": source.get("bill_types", []) if source else [],
            "source_against_refs": source.get("against_refs", []) if source else [],
            "invoice_new_refs": target_source.get("new_refs", []) if target_source else [],
            "shared_bill_refs": shared,
            "transaction_parties": sorted(transaction_parties),
            "same_party": (
                target_party_key in transaction_parties
                or (source is not None and target_party_key == norm(source.get("party")))
                or bool(source_party_entries)
            ),
            "days_payment_after_invoice": iso_days(
                source.get("vdate") if source else transaction.get("posting_date"),
                target.get("posting_date"),
            ),
            "allocation_equals_source_party_amount": allocation == source_party_amount,
            "exact_whole_payment": (
                allocation == source_party_amount == invoice_total
            ),
        }
        row["classification"], row["reason"] = classify(row)
        links.append(row)

    links.sort(key=lambda row: (row["invoice_date"], row["invoice_name"], row["source_name"]))
    classification = Counter(row["classification"] for row in links)
    amount_by_class = defaultdict(lambda: Decimal("0.00"))
    for row in links:
        amount_by_class[row["classification"]] += money(row["allocation_amount"])

    invoices = []
    for guid in sorted(fallback_guids):
        inv = local_by_guid[guid]
        src = sources.get(guid, {})
        invoice_links = [row for row in links if row["invoice_guid"] == guid]
        invoices.append({
            "guid": guid,
            "local_invoice": inv["name"],
            "party": inv["party"],
            "date": inv["posting_date"],
            "source_number": src.get("vnumber"),
            "source_value": f"{money(src.get('amount')):.2f}",
            "local_total": f"{money(inv.get('grand_total')):.2f}",
            "local_status": inv["status"],
            "local_outstanding": f"{money(inv.get('outstanding_amount')):.2f}",
            "link_count": len(invoice_links),
            "explicit_link_count": sum(
                row["classification"] == "explicit_tally_agst_ref"
                for row in invoice_links),
            "linked_amount": f"{sum((money(row['allocation_amount']) for row in invoice_links), Decimal('0.00')):.2f}",
            "link_classifications": dict(Counter(
                row["classification"] for row in invoice_links)),
            "tally_native_bill_group": tally_groups.get(guid),
        })

    payload = {
        "mode": "read-only",
        "source": "Tally staging plus fresh local and production ERPNext exports",
        "summary": {
            "fallback_invoices": len(fallback_guids),
            "local_cross_document_links": len(links),
            "invoices_with_links": sum(row["link_count"] > 0 for row in invoices),
            "invoices_without_links": sum(row["link_count"] == 0 for row in invoices),
            "invoice_groups_matching_tally_native_bills": sum(
                bool((row.get("tally_native_bill_group") or {}).get(
                    "matches_tally_native_bills")) for row in invoices),
            "invoice_groups_differing_from_tally_native_bills": sum(
                row.get("tally_native_bill_group") is not None
                and not row["tally_native_bill_group"]["matches_tally_native_bills"]
                for row in invoices),
            "classifications": dict(sorted(classification.items())),
            "amounts_by_classification": {
                key: f"{value:.2f}" for key, value in sorted(amount_by_class.items())
            },
        },
        "invoices": invoices,
        "links": links,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": payload["summary"], "report": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
