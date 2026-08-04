"""Read-only preflight of the corrected migration plan."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t2e.config import get_config  # noqa: E402
from t2e.erpnext_client import ERPNextClient  # noqa: E402
from t2e.lines import is_round_ledger  # noqa: E402
from t2e.load_invoices import InvoiceLoader  # noqa: E402
from t2e.load_masters import fetch_company_defaults  # noqa: E402
from t2e.mapping import LedgerResolver, acc_name  # noqa: E402
from t2e.staging import Staging  # noqa: E402


def main() -> int:
    cfg = get_config()
    erp = ERPNextClient(dry_run=True)
    defaults = fetch_company_defaults(erp)
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "staging.sqlite"
        shutil.copy2(ROOT / "data" / "staging.sqlite", db_path)
        store = Staging(db_path)

        retained = acc_name(
            cfg.erpnext.get(
                "retained_earnings_account", "Reserves and Surplus"),
            defaults.abbr)
        store.conn.execute(
            """UPDATE master SET erp_doctype='Account',erp_name=?
                WHERE kind='ledger' AND lower(trim(name))=
                      'profit & loss a/c'""",
            (retained,))
        for row in store.masters("ledger"):
            if is_round_ledger(row["name"]):
                store.conn.execute(
                    """UPDATE master SET erp_doctype='Account',erp_name=?
                        WHERE guid=?""",
                    (defaults.round_off, row["guid"]))

        master_by_name = {
            " ".join(row["name"].split()): row
            for row in store.masters("ledger")
        }
        for voucher in store.vouchers():
            party = " ".join((voucher["party"] or "").split())
            master = master_by_name.get(party)
            if not master:
                continue
            if voucher["vtype"] in ("Sales", "Credit Note"):
                role = "Customer"
            elif voucher["vtype"] in ("Purchase", "Debit Note"):
                role = "Supplier"
            else:
                continue
            store.add_party_role(master["guid"], master["name"], role, party)
        store.conn.commit()

        resolver = LedgerResolver(store, defaults)
        loader = InvoiceLoader(erp, store, defaults, resolver)
        counts = Counter()
        errors = []
        flagged_path = (
            ROOT / "data" / "reports"
            / "tally_flagged_vouchers_2026-07-27.json")
        audited_optional = set()
        if flagged_path.exists():
            flagged = json.loads(flagged_path.read_text(encoding="utf-8"))
            audited_optional = {
                row["guid"] for row in flagged.get("vouchers", [])
                if str(row.get("is_optional", "")).lower() == "yes"
            }
        invoice_types = {"Sales", "Credit Note", "Purchase", "Debit Note"}
        for row in store.vouchers():
            payload = json.loads(row["payload"])
            if (
                str(payload.get("ISOPTIONAL", "")).lower() == "yes"
                or row["guid"] in audited_optional
            ):
                counts["optional_vouchers_excluded"] += 1
                continue
            counts["source_vouchers_after_optional_filter"] += 1
            if row["vtype"] not in invoice_types:
                continue
            counts["invoice_type_vouchers"] += 1
            built = loader._build(row)
            if built is None:
                counts["invoice_fallbacks"] += 1
                errors.append({
                    "guid": row["guid"], "vtype": row["vtype"],
                    "vnumber": row["vnumber"], "party": row["party"],
                    "reason": "invoice builder fallback",
                })
                continue
            doc, _, doctype, billname, bridge = built
            counts["planned_invoices"] += 1
            if bridge:
                counts["planned_party_control_bridges"] += 1
            if doc.get("disable_rounded_total") == 0:
                counts["planned_standard_rounded_total"] += 1
            if doc.get("_tally_manual_rounding"):
                counts["planned_manual_rounded_total"] += 1
            if doc.get("remarks"):
                counts["planned_invoice_narrations"] += 1
            if doctype == "Purchase Invoice":
                if doc.get("bill_no") == billname:
                    counts["purchase_exact_supplier_reference"] += 1
                else:
                    counts["purchase_duplicate_reference_discriminated"] += 1
            if any(
                is_round_ledger(
                    f"{tax.get('account_head', '')} "
                    f"{tax.get('description', '')}")
                for tax in doc.get("taxes", [])
            ):
                errors.append({
                    "guid": row["guid"],
                    "reason": "rounding leaked into tax table",
                })
        store.close()

    result = {
        "counts": dict(sorted(counts.items())),
        "errors": errors[:100],
        "retained_earnings_account": retained,
        "canonical_round_off_account": defaults.round_off,
        "pass": not errors and counts["invoice_fallbacks"] == 0,
    }
    path = ROOT / "data" / "reports" / "corrected_migration_preflight.json"
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({**result, "report": str(path)}, indent=2))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
