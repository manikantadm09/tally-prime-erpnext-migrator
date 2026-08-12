"""Verify migrated invoice outstanding amounts against Tally native bills."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import json

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.lines import parse_entries
from t2e.staging import Staging
from tools.verify_bill_outstandings_api import (
    latest_native_report,
    money,
    norm,
    parse_tally_bills,
)
from tools.plan_exact_bill_allocations_api import source_graph


DIRECTION = {"Sales": "Receivable", "Purchase": "Payable"}
RETURN_DIRECTION = {"Credit Note": "Receivable", "Debit Note": "Payable"}


def classify_difference(difference: Decimal, has_return_source: bool) -> str:
    if abs(difference) <= Decimal("0.01"):
        return "matches"
    if difference < 0:
        return "source_bill_vs_gl_exception"
    if has_return_source:
        return "erpnext_return_reconciliation_turnover_exception"
    return "requires_exact_allocation"


def source_invoice_bill_data(row) -> tuple[list[str], Decimal, Decimal]:
    payload = json.loads(row["payload"])
    entries = parse_entries(payload)
    party = norm(row["party"] or "")
    party_lines = [entry for entry in entries if norm(entry["ledger"]) == party]
    candidates = party_lines or [entry for entry in entries if entry["bills"]]
    refs = []
    signed_reference_total = Decimal("0.00")
    for entry in candidates:
        for bill in entry["bills"]:
            if bill["type"] in ("New Ref", "Agst Ref"):
                ref = str(bill["name"] or "").strip()
                if ref and norm(ref) not in {norm(value) for value in refs}:
                    refs.append(ref)
                signed_reference_total += money(bill["signed_amount"])
    party_total = sum(
        (money(entry["mag"]) for entry in candidates), Decimal("0.00"))
    refs = refs or [str(row["vnumber"] or row["guid"][:20]).strip()]
    return refs, abs(signed_reference_total), party_total


def main() -> None:
    cfg = get_config()
    raw = cfg.staging_db.parent / "raw"
    reports = cfg.staging_db.parent / "reports"
    native = {
        "Receivable": parse_tally_bills(
            latest_native_report(raw, "bills-receivable")),
        "Payable": parse_tally_bills(
            latest_native_report(raw, "bills-payable")),
    }
    native_by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for direction, rows in native.items():
        for row in rows:
            native_by_key[(direction, norm(row["party"]), norm(row["bill_ref"]))].append(row)

    erp = ERPNextClient(dry_run=True)
    company = cfg.erpnext["company"]
    live_by_guid = {}
    for doctype in ("Sales Invoice", "Purchase Invoice"):
        rows = erp.get_list(
            doctype,
            fields=[
                "name", "tally_guid", "status", "outstanding_amount",
                "posting_date", "due_date", "docstatus",
            ],
            filters=[
                ["company", "=", company],
                ["docstatus", "=", 1],
                ["tally_guid", "is", "set"],
            ],
            limit=0,
        )
        for row in rows:
            live_by_guid[str(row["tally_guid"])] = {**row, "doctype": doctype}

    store = Staging()
    records = []
    return_ref_keys: set[tuple[str, str, str]] = set()
    vouchers = [dict(row) for row in store.vouchers()]
    return_guids = {
        str(row["guid"]) for row in vouchers
        if row["vtype"] in RETURN_DIRECTION
    }
    party_keys = {norm(row["party"] or ""): "Party" for row in vouchers}
    return_origins, return_funding = source_graph(vouchers, party_keys)
    for row in vouchers:
        return_direction = RETURN_DIRECTION.get(row["vtype"])
        if return_direction:
            refs, _, _ = source_invoice_bill_data(row)
            return_ref_keys.update(
                (return_direction, norm(row["party"] or ""), norm(ref))
                for ref in refs
            )
        target = live_by_guid.get(str(row["guid"]))
        if not target:
            continue
        direction = DIRECTION.get(row["vtype"])
        if not direction:
            continue
        bill_refs, new_ref_total, party_total = source_invoice_bill_data(row)
        records.append({
            "direction": direction,
            "party": row["party"],
            "bill_refs": bill_refs,
            "source_guid": row["guid"],
            "source_number": row["vnumber"],
            "erp_document": target["name"],
            "erp_status": target["status"],
            "erp_outstanding": abs(money(target.get("outstanding_amount"))),
            "source_party_total": party_total,
            "source_new_ref_total": new_ref_total,
            "source_unreferenced": max(
                party_total - new_ref_total, Decimal("0.00")),
        })
    store.close()

    # Tally can split one invoice across several New Ref values, and several
    # invoices can reuse the same bill reference. Build connected components
    # per party/direction so each native bill balance is counted exactly once.
    by_party: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        by_party[(record["direction"], norm(record["party"] or ""))].append(record)

    grouped = []
    for (direction, party_key), party_records in by_party.items():
        components: list[dict] = []
        for record in party_records:
            record_refs = {norm(ref) for ref in record["bill_refs"]}
            touching = [
                component for component in components
                if component["ref_keys"] & record_refs
            ]
            if not touching:
                components.append({
                    "direction": direction,
                    "party": record["party"],
                    "ref_keys": set(record_refs),
                    "bill_refs": list(record["bill_refs"]),
                    "records": [record],
                })
                continue
            base = touching[0]
            base["ref_keys"].update(record_refs)
            base["records"].append(record)
            for ref in record["bill_refs"]:
                if norm(ref) not in {norm(value) for value in base["bill_refs"]}:
                    base["bill_refs"].append(ref)
            for extra in touching[1:]:
                base["ref_keys"].update(extra["ref_keys"])
                base["records"].extend(extra["records"])
                for ref in extra["bill_refs"]:
                    if norm(ref) not in {norm(value) for value in base["bill_refs"]}:
                        base["bill_refs"].append(ref)
                components.remove(extra)
        grouped.extend(components)

    details = []
    for group in grouped:
        tally_matches = []
        for ref_key in group["ref_keys"]:
            tally_matches.extend(native_by_key.get(
                (group["direction"], norm(group["party"]), ref_key), []))
        tally_open = sum(
            (abs(money(item["amount"])) for item in tally_matches),
            Decimal("0.00"),
        )
        erp_open = sum(
            (record["erp_outstanding"] for record in group["records"]),
            Decimal("0.00"),
        )
        unreferenced = sum(
            (record["source_unreferenced"] for record in group["records"]),
            Decimal("0.00"),
        )
        expected_open = unreferenced + tally_open
        difference = erp_open - expected_open
        has_return_source = any(
            (group["direction"], norm(group["party"]), ref_key)
            in return_ref_keys for ref_key in group["ref_keys"])
        if not has_return_source:
            candidate_return_guids = set()
            for ref_key in group["ref_keys"]:
                graph_key = (norm(group["party"]), ref_key)
                candidate_return_guids.update(return_origins.get(graph_key, set()))
                candidate_return_guids.update(return_funding.get(graph_key, set()))
            has_return_source = bool(candidate_return_guids & return_guids)
        classification = classify_difference(difference, has_return_source)
        details.append({
            "direction": group["direction"],
            "party": group["party"],
            "bill_refs": group["bill_refs"],
            "source_guids": [r["source_guid"] for r in group["records"]],
            "source_numbers": [r["source_number"] for r in group["records"]],
            "erp_documents": [r["erp_document"] for r in group["records"]],
            "erp_statuses": [r["erp_status"] for r in group["records"]],
            "invoice_count": len(group["records"]),
            "tally_native_match_count": len(tally_matches),
            "tally_outstanding": f"{tally_open:.2f}",
            "source_unreferenced": f"{unreferenced:.2f}",
            "expected_erp_outstanding": f"{expected_open:.2f}",
            "erp_outstanding": f"{erp_open:.2f}",
            "difference": f"{difference:.2f}",
            "matches": abs(difference) <= Decimal("0.01"),
            "classification": classification,
        })

    differences = [row for row in details if not row["matches"]]
    ambiguous = [row for row in details if row["tally_native_match_count"] > 1]
    summary = {
        "migrated_invoices": sum(row["invoice_count"] for row in details),
        "tally_bill_groups_for_migrated_invoices": len(details),
        "matching_bill_groups": len(details) - len(differences),
        "different_bill_groups": len(differences),
        "ambiguous_native_bill_keys": len(ambiguous),
        "repeated_source_bill_keys": sum(r["invoice_count"] > 1 for r in details),
        "erp_open_bill_keys": sum(money(r["erp_outstanding"]) > 0 for r in details),
        "expected_open_bill_groups": sum(money(r["expected_erp_outstanding"]) > 0 for r in details),
        "erp_outstanding": f"{sum((money(r['erp_outstanding']) for r in details), Decimal('0.00')):.2f}",
        "expected_erp_outstanding": f"{sum((money(r['expected_erp_outstanding']) for r in details), Decimal('0.00')):.2f}",
        "classifications": {
            classification: {
                "groups": sum(r["classification"] == classification for r in details),
                "difference": f"{sum((money(r['difference']) for r in details if r['classification'] == classification), Decimal('0.00')):.2f}",
            }
            for classification in (
                "matches", "requires_exact_allocation",
                "erpnext_return_reconciliation_turnover_exception",
                "source_bill_vs_gl_exception",
            )
        },
    }
    payload = {"summary": summary, "differences": differences, "ambiguous": ambiguous}
    report = reports / "invoice_outstanding_verification.json"
    report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print("TOP DIFFERENCES")
    for item in sorted(
        differences,
        key=lambda value: abs(money(value["difference"])),
        reverse=True,
    )[:30]:
        print(
            f"  {','.join(item['erp_documents'])} {item['party']} "
            f"refs={','.join(item['bill_refs'])} expected={item['expected_erp_outstanding']} "
            f"ERP={item['erp_outstanding']} diff={item['difference']}"
        )
    print(f"REPORT {report}")


if __name__ == "__main__":
    main()
