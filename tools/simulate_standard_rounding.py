"""Read-only, in-memory ERPNext calculation for PINV-26-01219.

Removes the explicit Tally ROUNDING OFF tax row and lets ERPNext calculate its
standard rounded total. ``run_doc_method`` returns the mutated in-memory doc;
it does not save or submit anything.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t2e.erpnext_client import ERPNextClient, ERPNextError  # noqa: E402
from t2e.lines import is_round_ledger  # noqa: E402


def full_doc(erp: ERPNextClient, doctype: str, name: str) -> dict:
    dt = urllib.parse.quote(doctype, safe="")
    nm = urllib.parse.quote(name, safe="")
    return erp._request("GET", f"/api/resource/{dt}/{nm}")["data"]


def summary(doc: dict) -> dict:
    return {
        "disable_rounded_total": doc.get("disable_rounded_total"),
        "net_total": doc.get("net_total"),
        "total_taxes_and_charges": doc.get("total_taxes_and_charges"),
        "grand_total": doc.get("grand_total"),
        "rounding_adjustment": doc.get("rounding_adjustment"),
        "rounded_total": doc.get("rounded_total"),
        "taxes": [
            {
                "charge_type": row.get("charge_type"),
                "account_head": row.get("account_head"),
                "description": row.get("description"),
                "tax_amount": row.get("tax_amount"),
                "total": row.get("total"),
            }
            for row in doc.get("taxes", [])
        ],
    }


def main() -> None:
    erp = ERPNextClient(dry_run=True)
    original = full_doc(erp, "Purchase Invoice", "PINV-26-01219")
    proposal = copy.deepcopy(original)
    proposal["name"] = "new-purchase-invoice-standard-rounding-audit"
    proposal["__islocal"] = 1
    proposal["__unsaved"] = 1
    proposal["disable_rounded_total"] = 0
    proposal["docstatus"] = 0
    for key in [
        "creation", "modified", "modified_by", "owner", "submitted_by",
        "amended_from", "status",
    ]:
        proposal.pop(key, None)
    proposal["taxes"] = [
        row for row in proposal.get("taxes", [])
        if not is_round_ledger(
            f"{row.get('account_head', '')} {row.get('description', '')}"
        )
    ]
    for table in ["items", "taxes", "payment_schedule", "advances"]:
        for row in proposal.get(table, []):
            row.pop("name", None)
            row.pop("creation", None)
            row.pop("modified", None)
            row.pop("modified_by", None)
            row.pop("owner", None)
            row["docstatus"] = 0
    accounts = erp.get_list(
        "Account",
        fields=[
            "name", "account_name", "parent_account", "root_type",
            "account_type", "is_group",
        ],
        filters=[
            ["company", "=", original["company"]],
            ["account_name", "like", "%Round%"],
        ],
    )
    company = full_doc(erp, "Company", original["company"])
    simulated = None
    simulation_error = None
    try:
        simulated = erp.run_doc_method("calculate_taxes_and_totals", proposal)
    except ERPNextError as exc:
        simulation_error = {
            "message": str(exc),
            "status": exc.status,
            "body": exc.body,
        }
    print(json.dumps({
        "document": original["name"],
        "original": summary(original),
        "simulated_standard_rounding": (
            summary(simulated) if simulated is not None else None
        ),
        "simulation_error": simulation_error,
        "company_round_off_account": company.get("round_off_account"),
        "round_named_accounts": accounts,
        "saved": False,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except ERPNextError as exc:
        print(f"ERPNext API error: {exc}", file=sys.stderr)
        if exc.body:
            print(exc.body, file=sys.stderr)
        raise SystemExit(2)
