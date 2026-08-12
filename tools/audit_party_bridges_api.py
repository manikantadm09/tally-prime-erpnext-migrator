"""Read-only audit of invoice party-control bridge Journal Entries."""
from __future__ import annotations

import json
import urllib.parse

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient


def main() -> None:
    cfg = get_config()
    erp = ERPNextClient(dry_run=True)
    rows = erp.get_list(
        "Journal Entry",
        fields=["name", "tally_guid", "posting_date", "docstatus"],
        filters=[
            ["company", "=", cfg.erpnext["company"]],
            ["docstatus", "=", 1],
            ["tally_guid", "like", "%:party-control-bridge"],
        ],
        limit=0,
    )
    result = []
    for row in rows:
        name = urllib.parse.quote(row["name"], safe="")
        doc = erp._request("GET", f"/api/resource/Journal%20Entry/{name}")["data"]
        references = [
            {
                "account": account.get("account"),
                "party_type": account.get("party_type"),
                "party": account.get("party"),
                "reference_type": account.get("reference_type"),
                "reference_name": account.get("reference_name"),
                "debit": account.get("debit_in_account_currency"),
                "credit": account.get("credit_in_account_currency"),
            }
            for account in doc.get("accounts") or []
        ]
        result.append({**row, "accounts": references})
    print(json.dumps({"count": len(result), "bridges": result}, indent=2))


if __name__ == "__main__":
    main()
