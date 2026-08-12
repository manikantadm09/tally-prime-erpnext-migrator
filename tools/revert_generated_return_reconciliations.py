"""Cancel/delete ERPNext JEs generated when Payment Reconciliation nets returns.

These JEs are balance-neutral but add artificial debit/credit turnover that is
absent from Tally.  The command is dry-run by default and validates a very
narrow document shape before allowing deletion.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json

from t2e.erpnext_client import ERPNextClient, ERPNextError


def amount(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def validate(erp: ERPNextClient, name: str) -> dict:
    doc = erp._request("GET", f"/api/resource/Journal%20Entry/{name}")["data"]
    docstatus = int(doc.get("docstatus") or 0)
    if docstatus not in (1, 2) or doc.get("tally_guid"):
        raise RuntimeError(f"{name}: not an untagged submitted/cancelled Journal Entry")
    gl = erp.get_list(
        "GL Entry",
        fields=["account", "party_type", "party", "debit", "credit",
                "against_voucher_type", "against_voucher"],
        filters=[["voucher_type", "=", "Journal Entry"],
                 ["voucher_no", "=", name],
                 ["is_cancelled", "=", 0 if docstatus == 1 else 1]],
        limit=0,
    )
    expected_rows = 2 if docstatus == 1 else 4
    if len(gl) != expected_rows:
        raise RuntimeError(
            f"{name}: expected {expected_rows} {'active' if docstatus == 1 else 'inactive'} "
            f"GL rows, found {len(gl)}")
    signature = {(r.get("account"), r.get("party_type"), r.get("party")) for r in gl}
    debit = sum((amount(r.get("debit")) for r in gl), Decimal("0.00"))
    credit = sum((amount(r.get("credit")) for r in gl), Decimal("0.00"))
    refs = {str(r.get("against_voucher") or "") for r in gl}
    normal = any(ref.startswith(("SINV-", "PINV-")) for ref in refs)
    returned = any(ref.startswith(("SRET-", "PRET-")) for ref in refs)
    if len(signature) != 1 or debit <= 0 or debit != credit or not (normal and returned):
        raise RuntimeError(f"{name}: GL shape is not a balanced return reconciliation")
    document_amount = amount(doc.get("total_debit"))
    if document_amount <= 0 or (docstatus == 1 and document_amount != debit):
        raise RuntimeError(f"{name}: Journal Entry total does not match GL")
    return {
        "name": name, "docstatus": docstatus,
        "amount": f"{document_amount:.2f}", "references": sorted(refs),
        "account_party": list(next(iter(signature))),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", action="append", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    erp = ERPNextClient(dry_run=not args.confirm)
    rows = [validate(erp, name) for name in args.name]
    if args.confirm:
        for row in rows:
            if row["docstatus"] == 1:
                erp.cancel("Journal Entry", row["name"])
            active = erp.get_list(
                "GL Entry", fields=["name"],
                filters=[["voucher_type", "=", "Journal Entry"],
                         ["voucher_no", "=", row["name"]],
                         ["is_cancelled", "=", 0]], limit=0)
            if active:
                raise RuntimeError(f"{row['name']}: active GL remains after cancel")
            try:
                erp.delete("Journal Entry", row["name"])
                row["status"] = (
                    "cancelled-and-deleted" if not erp.exists("Journal Entry", row["name"])
                    else "cancelled-delete-not-completed")
            except ERPNextError as exc:
                # Immutable-ledger sites commonly retain the cancelled parent;
                # accounting correctness comes from cancellation. A separately
                # backed-up server purge can remove this inactive shell later.
                row["status"] = "cancelled-retained"
                row["delete_error"] = (exc.body or str(exc))[:500]
    else:
        for row in rows:
            row["status"] = "validated-dry-run"
    print(json.dumps({
        "mode": "live" if args.confirm else "dry-run",
        "count": len(rows),
        "total": f"{sum((amount(r['amount']) for r in rows), Decimal('0.00')):.2f}",
        "rows": rows,
    }, indent=2))


if __name__ == "__main__":
    main()
