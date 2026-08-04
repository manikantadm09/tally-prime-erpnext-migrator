"""Backfill Tally GST identity metadata onto an already migrated ERPNext site.

This is intentionally separate from the financial reload: it only creates the
missing GST metadata fields and updates Company, party, Address, and submitted
invoice metadata.  GL Entries and accounting values are not touched.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t2e.config import get_config  # noqa: E402
from t2e.erpnext_client import ERPNextClient  # noqa: E402
from t2e.load_invoices import _place_of_supply, _scalar  # noqa: E402
from t2e.load_masters import (  # noqa: E402
    ensure_company_address,
    ensure_idempotency_field,
)
from t2e.staging import Staging  # noqa: E402


INVOICE_DOCTYPE = {
    "Sales": "Sales Invoice",
    "Credit Note": "Sales Invoice",
    "Purchase": "Purchase Invoice",
    "Debit Note": "Purchase Invoice",
}
_thread_state = threading.local()


def client() -> ERPNextClient:
    if not hasattr(_thread_state, "erp"):
        _thread_state.erp = ERPNextClient(dry_run=False)
    return _thread_state.erp


def invoice_values(vtype: str, payload: dict, company_gstin: str) -> dict:
    party_gstin = _scalar(payload.get("PARTYGSTIN"))
    state = (
        _scalar(payload.get("PLACEOFSUPPLY"))
        or _scalar(payload.get("STATENAME"))
    )
    values = {
        "company_gstin": company_gstin,
        "place_of_supply": _place_of_supply(state, party_gstin),
    }
    if vtype in ("Purchase", "Debit Note"):
        values["supplier_gstin"] = party_gstin
    else:
        values["billing_address_gstin"] = party_gstin
    return {key: value for key, value in values.items() if value}


def update_invoice(task: tuple[str, str, dict]) -> tuple[str, str, str]:
    doctype, name, values = task
    try:
        client().update(doctype, name, values)
        return doctype, name, ""
    except Exception as exc:
        return doctype, name, str(exc)[:1000]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="apply the metadata backfill (otherwise print a plan only)",
    )
    args = parser.parse_args()
    cfg = get_config()
    company = cfg.erpnext["company"]
    company_gstin = str(cfg.erpnext.get("company_gstin") or "").strip()
    store = Staging()
    counters = Counter()
    errors = []

    vouchers = [
        row for row in store.vouchers()
        if row["vtype"] in INVOICE_DOCTYPE
        and row["erp_name"]
        and row["load_status"] == "loaded"
    ]
    counters["invoice_documents_planned"] = len(vouchers)
    counters["party_roles_planned"] = len(list(store.party_roles()))
    if not args.confirm:
        print(json.dumps(dict(counters), indent=2))
        print("DRY RUN: pass --confirm to apply")
        store.close()
        return 0

    erp = ERPNextClient(dry_run=False)
    ensure_idempotency_field(erp)
    counters["gst_fields_ensured"] = 1
    ensure_company_address(erp, company, dry_run=False)
    company_values = {}
    if company_gstin:
        if erp.has_field("Company", "gstin"):
            company_values["gstin"] = company_gstin
        if erp.has_field("Company", "tax_id"):
            company_values["tax_id"] = company_gstin
    if company_values:
        erp.update("Company", company, company_values)
        counters["company_updated"] = 1

    master_payload = {
        row["guid"]: json.loads(row["payload"])
        for row in store.masters("ledger")
    }
    seen_parties = set()
    for role in store.party_roles():
        key = (role["party_type"], role["party"])
        if key in seen_parties:
            continue
        seen_parties.add(key)
        payload = master_payload.get(role["ledger_guid"], {})
        gstin = _scalar(payload.get("PARTYGSTIN"))
        if not gstin:
            counters["party_without_gstin"] += 1
            continue
        values = {}
        if erp.has_field(role["party_type"], "gstin"):
            values["gstin"] = gstin
        if erp.has_field(role["party_type"], "tax_id"):
            values["tax_id"] = gstin
        try:
            if values:
                erp.update(role["party_type"], role["party"], values)
                counters["party_updated"] += 1
            address = erp.find_by_field(
                "Address", cfg.idempotency_field,
                f"{role['ledger_guid']}:Address",
            )
            if address and erp.has_field("Address", "gstin"):
                erp.update("Address", address, {"gstin": gstin})
                counters["party_address_updated"] += 1
        except Exception as exc:
            errors.append({
                "doctype": role["party_type"],
                "name": role["party"],
                "error": str(exc)[:1000],
            })

    tasks = []
    for row in vouchers:
        payload = json.loads(row["payload"])
        tasks.append((
            INVOICE_DOCTYPE[row["vtype"]],
            row["erp_name"],
            invoice_values(row["vtype"], payload, company_gstin),
        ))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(update_invoice, task) for task in tasks]
        for future in as_completed(futures):
            doctype, name, error = future.result()
            if error:
                errors.append({
                    "doctype": doctype, "name": name, "error": error,
                })
            else:
                counters["invoice_updated"] += 1

    result = {
        "generated_on": date.today().isoformat(),
        "company": company,
        "company_gstin": company_gstin,
        "counts": dict(sorted(counters.items())),
        "errors": errors,
    }
    path = (
        cfg.staging_db.parent
        / "reports"
        / f"gst_metadata_backfill_{date.today().isoformat()}.json"
    )
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({
        "counts": result["counts"],
        "errors": len(errors),
        "report": str(path),
    }, indent=2))
    store.close()
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
