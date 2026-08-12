"""Score two ERP invoice-state exports against live Tally bill expectations."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("verification")
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    verification = json.loads(Path(args.verification).read_text(encoding="utf-8"))
    left = json.loads(Path(args.left).read_text(encoding="utf-8"))
    right = json.loads(Path(args.right).read_text(encoding="utf-8"))
    left_by_guid = {row["tally_guid"]: row for row in left["invoices"]}
    right_by_guid = {row["tally_guid"]: row for row in right["invoices"]}

    rows = []
    for group in verification["details"]:
        guids = group["source_guids"]
        if not all(guid in left_by_guid and guid in right_by_guid for guid in guids):
            continue
        expected = money(group["expected_erp_outstanding"])
        left_open = sum(
            (abs(money(left_by_guid[guid]["outstanding_amount"])) for guid in guids),
            Decimal("0.00"),
        )
        right_open = sum(
            (abs(money(right_by_guid[guid]["outstanding_amount"])) for guid in guids),
            Decimal("0.00"),
        )
        rows.append({
            "party": group["party"],
            "direction": group["direction"],
            "bill_refs": group["bill_refs"],
            "guids": guids,
            "left_documents": [left_by_guid[guid]["name"] for guid in guids],
            "right_documents": [right_by_guid[guid]["name"] for guid in guids],
            "tally_expected": f"{expected:.2f}",
            "left_outstanding": f"{left_open:.2f}",
            "right_outstanding": f"{right_open:.2f}",
            "left_difference": f"{left_open - expected:.2f}",
            "right_difference": f"{right_open - expected:.2f}",
            "left_matches": abs(left_open - expected) <= Decimal("0.01"),
            "right_matches": abs(right_open - expected) <= Decimal("0.01"),
            "left_false_paid": left_open == 0 and expected > 0,
            "right_false_paid": right_open == 0 and expected > 0,
        })

    def score(side: str) -> dict:
        difference_key = f"{side}_difference"
        return {
            "groups": len(rows),
            "matching_groups": sum(row[f"{side}_matches"] for row in rows),
            "different_groups": sum(not row[f"{side}_matches"] for row in rows),
            "false_paid_groups": sum(row[f"{side}_false_paid"] for row in rows),
            "false_paid_tally_outstanding": f"{sum((money(row['tally_expected']) for row in rows if row[f'{side}_false_paid']), Decimal('0.00')):.2f}",
            "absolute_difference": f"{sum((abs(money(row[difference_key])) for row in rows), Decimal('0.00')):.2f}",
            "total_outstanding": f"{sum((money(row[f'{side}_outstanding']) for row in rows), Decimal('0.00')):.2f}",
        }

    payload = {
        "tally_source": "live native Bills Receivable/Payable reports",
        "left": left["site"],
        "right": right["site"],
        "tally_expected_total": f"{sum((money(row['tally_expected']) for row in rows), Decimal('0.00')):.2f}",
        "scores": {"left": score("left"), "right": score("right")},
        "details": sorted(
            rows,
            key=lambda row: abs(money(row["left_difference"])),
            reverse=True,
        ),
    }
    output = Path(args.output)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "tally_expected_total": payload["tally_expected_total"],
        "scores": payload["scores"],
        "largest_left_mismatches": payload["details"][:10],
        "report": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
