"""Compare two Tally voucher staging snapshots without changing either."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from t2e.config import get_config
from tools.verify_financials_api import money, norm


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def scalar(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return scalar(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("#text", ""))
    return str(value)


def voucher_lines(payload_text: str) -> dict[str, Decimal]:
    payload = json.loads(payload_text)
    raw = as_list(payload.get("ALLLEDGERENTRIES.LIST")) or as_list(
        payload.get("LEDGERENTRIES.LIST")
    )
    lines = defaultdict(lambda: Decimal("0.00"))
    for row in raw:
        if not isinstance(row, dict):
            continue
        ledger = norm(scalar(row.get("LEDGERNAME")))
        if ledger:
            # Common comparison sign: debit positive, credit negative.
            lines[ledger] += -money(scalar(row.get("AMOUNT")))
    return dict(lines)


def load(path: Path, from_date: str, to_date: str) -> dict[str, dict]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT guid,vtype,vnumber,vdate,party,amount,payload
               FROM voucher WHERE vdate BETWEEN ? AND ?""",
            (from_date, to_date),
        )
        return {
            row["guid"]: {
                **dict(row),
                "lines": voucher_lines(row["payload"]),
            }
            for row in rows
        }
    finally:
        connection.close()


def compact(row: dict) -> dict:
    payload = json.loads(row["payload"])
    return {
        "guid": row["guid"],
        "date": row["vdate"],
        "type": row["vtype"],
        "number": row["vnumber"],
        "party": row["party"],
        "amount": f"{money(row['amount']):.2f}",
        "master_id": scalar(payload.get("MASTERID")),
        "alter_id": scalar(payload.get("ALTERID")),
        "is_optional": scalar(payload.get("ISOPTIONAL")),
        "is_postdated": scalar(payload.get("ISPOSTDATED")),
        "effective_date": scalar(payload.get("EFFECTIVEDATE")),
        "lines_dr_minus_cr": {
            ledger: f"{value:.2f}"
            for ledger, value in sorted(row["lines"].items())
        },
    }


def signature(row: dict):
    return (
        row["vdate"],
        row["vtype"],
        row["vnumber"],
        tuple(sorted((ledger, f"{value:.2f}") for ledger, value in row["lines"].items())),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("original", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--from-date", default="2025-04-01")
    parser.add_argument("--to-date", default="2026-03-31")
    args = parser.parse_args()
    original = load(args.original, args.from_date, args.to_date)
    current = load(args.current, args.from_date, args.to_date)
    original_ids, current_ids = set(original), set(current)
    removed = sorted(original_ids - current_ids)
    added = sorted(current_ids - original_ids)
    changed = sorted(
        guid
        for guid in original_ids & current_ids
        if signature(original[guid]) != signature(current[guid])
    )

    original_ledgers = defaultdict(lambda: Decimal("0.00"))
    current_ledgers = defaultdict(lambda: Decimal("0.00"))
    for row in original.values():
        for ledger, value in row["lines"].items():
            original_ledgers[ledger] += value
    for row in current.values():
        for ledger, value in row["lines"].items():
            current_ledgers[ledger] += value
    ledger_differences = []
    for ledger in sorted(set(original_ledgers) | set(current_ledgers)):
        difference = current_ledgers[ledger] - original_ledgers[ledger]
        if abs(difference) >= Decimal("0.01"):
            ledger_differences.append(
                {
                    "ledger": ledger,
                    "migrated_snapshot_dr_minus_cr": (
                        f"{original_ledgers[ledger]:.2f}"
                    ),
                    "current_tally_dr_minus_cr": (
                        f"{current_ledgers[ledger]:.2f}"
                    ),
                    "current_minus_migrated": f"{difference:.2f}",
                }
            )
    ledger_differences.sort(
        key=lambda row: abs(money(row["current_minus_migrated"])),
        reverse=True,
    )
    report = {
        "generated_at": date.today().isoformat(),
        "period": {"from": args.from_date, "to": args.to_date},
        "counts": {
            "migrated_snapshot": len(original),
            "current_tally": len(current),
            "common_guid": len(original_ids & current_ids),
            "removed_or_cancelled_from_current": len(removed),
            "added_in_current": len(added),
            "changed_common_guid": len(changed),
        },
        "removed_or_cancelled": [compact(original[guid]) for guid in removed],
        "added": [compact(current[guid]) for guid in added],
        "changed": [
            {
                "guid": guid,
                "migrated_snapshot": compact(original[guid]),
                "current_tally": compact(current[guid]),
            }
            for guid in changed
        ],
        "flagged_current_vouchers": [
            compact(row)
            for row in current.values()
            if scalar(json.loads(row["payload"]).get("ISOPTIONAL")) == "Yes"
            or scalar(json.loads(row["payload"]).get("ISPOSTDATED")) == "Yes"
        ],
        "ledger_movement_differences": ledger_differences,
    }
    output = (
        get_config().staging_db.parent
        / "reports"
        / f"tally_snapshot_comparison_{date.today().isoformat()}.json"
    )
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"REPORT {output}")


if __name__ == "__main__":
    main()
