"""Plan conversion of Tally Purchase vouchers routed as JEs into invoices."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from t2e.erpnext_client import ERPNextClient
from t2e.erpnext_client import ERPNextError
from t2e.load_invoices import InvoiceLoader
from t2e.load_masters import fetch_company_defaults
from t2e.mapping import LedgerResolver
from t2e.staging import Staging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("comparison")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    comparison = json.loads(Path(args.comparison).read_text(encoding="utf-8"))
    guids = {row["tally_guid"] for row in comparison["left_only_invoices"]}
    erp = ERPNextClient(dry_run=True)
    store = Staging()
    defaults = fetch_company_defaults(erp)
    loader = InvoiceLoader(
        erp, store, defaults, LedgerResolver(store, defaults))
    details = []
    try:
        for guid in sorted(guids):
            row = store.voucher_by_guid(guid)
            if row is None:
                raise RuntimeError(f"staging voucher absent: {guid}")
            invoice = erp.find_by_field(
                "Purchase Invoice", loader.field, guid, exclude_cancelled=True)
            journal = erp.find_by_field(
                "Journal Entry", loader.field, guid, exclude_cancelled=True)
            try:
                built = loader._build(row)
            except ERPNextError as exc:
                details.append({
                    "guid": guid, "source_type": row["vtype"],
                    "source_number": row["vnumber"], "buildable": False,
                    "reason": str(exc),
                    "current_invoice": invoice, "current_journal": journal,
                })
                continue
            if built is None:
                details.append({
                    "guid": guid, "source_type": row["vtype"],
                    "source_number": row["vnumber"], "buildable": False,
                    "reason": "source shape cannot be represented as an invoice",
                    "current_invoice": invoice, "current_journal": journal,
                })
                continue
            doc, party, doctype, billname, bridge = built
            details.append({
                "guid": guid,
                "source_type": row["vtype"],
                "source_number": row["vnumber"],
                "source_date": row["vdate"],
                "source_amount": f"{float(row['amount'] or 0):.2f}",
                "buildable": True,
                "planned_doctype": doctype,
                "party": party,
                "bill_reference": billname,
                "requires_control_bridge": bool(bridge),
                "current_invoice": invoice,
                "current_journal": journal,
            })
    finally:
        store.close()

    summary = {
        "total": len(details),
        "buildable_as_invoice": sum(row["buildable"] for row in details),
        "requires_control_bridge": sum(
            row.get("requires_control_bridge", False) for row in details),
        "current_journal": sum(bool(row["current_journal"]) for row in details),
        "current_invoice": sum(bool(row["current_invoice"]) for row in details),
        "unbuildable": sum(not row["buildable"] for row in details),
    }
    payload = {"mode": "read-only", "summary": summary, "details": details}
    output = Path(args.output)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summary, "report": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
