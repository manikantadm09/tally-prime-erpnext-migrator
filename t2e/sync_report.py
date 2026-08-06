"""Read-only source delta report for repeat Tally extractions."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .staging import Staging

REPORT_FIELDS = (
    "source_state", "guid", "vtype", "vnumber", "vdate", "party", "amount",
    "alter_id", "source_present", "load_status", "erp_doctype", "erp_name",
)


def build_report(store: Staging) -> dict[str, Any]:
    rows = [dict(row) for row in store.source_delta_rows()]
    for row in rows:
        row["source_present"] = bool(row["source_present"])
    states = Counter(row["source_state"] for row in rows)
    # A source record excluded/cancelled before it ever reached ERPNext has no
    # target document to repair. It remains audit evidence but does not block.
    requires_decision = sum(
        1 for row in rows
        if row["source_state"] == "changed"
        or (row["source_state"] in ("missing", "cancelled")
            and row["erp_doctype"] and row["erp_name"])
    )
    return {
        "summary": {
            "new_count": states.get("new", 0),
            "optional_count": states.get("optional", 0),
            "requires_decision": requires_decision,
            "by_source_state": dict(sorted(states.items())),
            "safe_to_load_new": requires_decision == 0,
        },
        "vouchers": rows,
    }


def write_report(report: dict[str, Any], json_path: str | Path) -> tuple[Path, Path]:
    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path = json_path.with_suffix(".csv")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report["vouchers"])
    return json_path, csv_path


def print_summary(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("  source delta:", summary["new_count"], "new,",
          summary["requires_decision"], "requiring a decision")
    for state, count in summary["by_source_state"].items():
        print(f"    {state}: {count}")
    if summary["safe_to_load_new"]:
        print("  safe action: load pending new vouchers only (still use --confirm).")
    else:
        print("  action required: do not overwrite/cancel changed source vouchers automatically.")
