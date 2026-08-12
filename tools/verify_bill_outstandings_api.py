"""Compare Tally native bill outstandings with ERPNext party entries read-only."""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.load_masters import fetch_company_defaults
from t2e.tally_client import sanitize_xml


PENNY = Decimal("0.01")


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def latest_native_report(raw_dir: Path, report_slug: str) -> Path:
    """Return the newest dated native report, failing closed if none exists."""
    candidates = sorted(raw_dir.glob(f"native_{report_slug}_*.xml"))
    if not candidates:
        raise FileNotFoundError(
            f"No native Tally report found for {report_slug!r} in {raw_dir}"
        )
    return candidates[-1]


def parse_tally_bills(path: Path) -> list[dict[str, Any]]:
    """Parse Tally's repeating BILLFIXED/BILLCL/BILLDUE blocks."""
    root = ET.fromstring(sanitize_xml(path.read_text(encoding="utf-8")))
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for element in list(root):
        if element.tag == "BILLFIXED":
            if current is not None:
                rows.append(current)
            current = {
                "bill_date": (element.findtext("BILLDATE") or "").strip(),
                "bill_ref": (element.findtext("BILLREF") or "").strip(),
                "party": (element.findtext("BILLPARTY") or "").strip(),
                "amount": Decimal("0.00"),
                "due_date": "",
                "overdue_days": 0,
            }
        elif current is not None and element.tag == "BILLCL":
            current["amount"] = money(element.text)
        elif current is not None and element.tag == "BILLDUE":
            current["due_date"] = (element.text or "").strip()
        elif current is not None and element.tag == "BILLOVERDUE":
            current["overdue_days"] = int((element.text or "0").strip() or 0)
    if current is not None:
        rows.append(current)
    return rows


def _base_doc(company: str, party_type: str, party: str, account: str) -> dict:
    return {
        "doctype": "Payment Reconciliation",
        "company": company,
        "party_type": party_type,
        "party": party,
        "receivable_payable_account": account,
    }


def norm(value: str) -> str:
    return " ".join((value or "").split()).lower()


def fetch_party_entries(
    erp: ERPNextClient,
    company: str,
    accounts: dict[str, str],
    parties: set[str],
) -> dict[tuple[str, str], dict[str, list[dict]]]:
    """Fetch both possible ERPNext roles for every Tally bill party."""
    entries: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for party_type in ("Customer", "Supplier"):
        live_names = {
            norm(row["name"]): row["name"]
            for row in erp.get_list(party_type, fields=["name"], limit=0)
        }
        for source_party in sorted(parties):
            live_party = live_names.get(norm(source_party))
            if not live_party:
                continue
            doc = erp.run_doc_method(
                "get_unreconciled_entries",
                _base_doc(
                    company, party_type, live_party, accounts[party_type]),
            )
            entries[(party_type, norm(source_party))] = {
                "invoices": doc.get("invoices") or [],
                "payments": doc.get("payments") or [],
            }
    return entries


def audit_kind(
    direction: str,
    primary_party_type: str,
    opposite_party_type: str,
    tally_rows: list[dict[str, Any]],
    entries: dict[tuple[str, str], dict[str, list[dict]]],
) -> tuple[dict, list[dict]]:
    tally_by_party: dict[str, list[dict]] = defaultdict(list)
    for row in tally_rows:
        tally_by_party[row["party"]].append(row)

    details = []
    for party, party_bills in sorted(tally_by_party.items()):
        primary = entries.get(
            (primary_party_type, norm(party)),
            {"invoices": [], "payments": []},
        )
        opposite = entries.get(
            (opposite_party_type, norm(party)),
            {"invoices": [], "payments": []},
        )
        # Receivable = Customer invoices + Supplier debit advances/payments.
        # Payable = Supplier invoices + Customer credit advances/payments.
        invoices = primary["invoices"]
        advances = opposite["payments"]
        tally_total = sum(
            (abs(money(row["amount"])) for row in party_bills),
            Decimal("0.00"),
        )
        erp_invoice_total = sum(
            (abs(money(row.get("outstanding_amount"))) for row in invoices),
            Decimal("0.00"),
        )
        erp_advance_total = sum(
            (abs(money(row.get("amount"))) for row in advances),
            Decimal("0.00"),
        )
        erp_directional_total = erp_invoice_total + erp_advance_total
        difference = erp_directional_total - tally_total
        details.append({
            "direction": direction,
            "party": party,
            "tally_bill_count": len(party_bills),
            "tally_outstanding": f"{tally_total:.2f}",
            "erp_primary_role": primary_party_type,
            "erp_invoice_entry_count": len(invoices),
            "erp_invoice_outstanding": f"{erp_invoice_total:.2f}",
            "erp_advance_role": opposite_party_type,
            "erp_advance_entry_count": len(advances),
            "erp_advance_outstanding": f"{erp_advance_total:.2f}",
            "erp_directional_outstanding": f"{erp_directional_total:.2f}",
            "outstanding_difference": f"{difference:.2f}",
            "matches_party_total": abs(difference) <= PENNY,
        })

    summary = {
        "direction": direction,
        "tally_bill_count": len(tally_rows),
        "tally_party_count": len(tally_by_party),
        "tally_outstanding": f"{sum((abs(money(r['amount'])) for r in tally_rows), Decimal('0.00')):.2f}",
        "erp_invoice_entry_count": sum(r["erp_invoice_entry_count"] for r in details),
        "erp_invoice_outstanding": f"{sum((money(r['erp_invoice_outstanding']) for r in details), Decimal('0.00')):.2f}",
        "erp_advance_entry_count": sum(r["erp_advance_entry_count"] for r in details),
        "erp_advance_outstanding": f"{sum((money(r['erp_advance_outstanding']) for r in details), Decimal('0.00')):.2f}",
        "erp_directional_outstanding": f"{sum((money(r['erp_directional_outstanding']) for r in details), Decimal('0.00')):.2f}",
        "matching_parties": sum(bool(r["matches_party_total"]) for r in details),
        "different_parties": sum(not r["matches_party_total"] for r in details),
    }
    return summary, details


def main() -> None:
    cfg = get_config()
    reports = cfg.staging_db.parent / "reports"
    raw = cfg.staging_db.parent / "raw"
    erp = ERPNextClient(dry_run=True)
    defaults = fetch_company_defaults(erp)
    specs = [
        (
            "Receivable", "Customer", "Supplier",
            latest_native_report(raw, "bills-receivable"),
        ),
        (
            "Payable", "Supplier", "Customer",
            latest_native_report(raw, "bills-payable"),
        ),
    ]
    parsed = {
        direction: parse_tally_bills(path)
        for direction, _, _, path in specs
    }
    all_parties = {
        row["party"] for rows in parsed.values() for row in rows
    }
    entries = fetch_party_entries(
        erp,
        defaults.name,
        {"Customer": defaults.receivable, "Supplier": defaults.payable},
        all_parties,
    )
    summaries = {}
    details: list[dict] = []
    for direction, primary_type, opposite_type, _ in specs:
        summary, rows = audit_kind(
            direction,
            primary_type,
            opposite_type,
            parsed[direction],
            entries,
        )
        summaries[direction] = summary
        details.extend(rows)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": defaults.name,
        "mode": "read-only",
        "summaries": summaries,
        "different_parties": [
            row for row in details if not row["matches_party_total"]
        ],
    }
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / "bill_outstanding_verification.json"
    csv_path = reports / "bill_outstanding_parties.csv"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(details[0]))
        writer.writeheader()
        writer.writerows(details)

    print(json.dumps(summaries, indent=2))
    print("TOP DIFFERENCES")
    for row in sorted(
        payload["different_parties"],
        key=lambda item: abs(money(item["outstanding_difference"])),
        reverse=True,
    )[:20]:
        print(
            f"  {row['direction']} {row['party']}: "
            f"Tally={row['tally_outstanding']} "
            f"ERP={row['erp_directional_outstanding']} "
            f"diff={row['outstanding_difference']}"
        )
    print(f"REPORT {json_path}")
    print(f"CSV {csv_path}")


if __name__ == "__main__":
    main()
