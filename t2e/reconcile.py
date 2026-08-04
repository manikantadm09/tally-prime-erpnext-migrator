"""Reconciliation: prove that every Tally record reached ERPNext.

Produces a report comparing, per voucher type and master kind:
  * Tally count (staged)        -> what the source has
  * loaded / skipped / error    -> staging load outcomes
  * ERPNext count (live)        -> what actually exists in the target

Plus a per-record list of anything not loaded (with the error), so nothing is
silently dropped. The report is written to data/reports/reconciliation.* .
"""
from __future__ import annotations

import csv
import json
from datetime import datetime

from .config import get_config
from .erpnext_client import ERPNextClient
from .staging import Staging


def _count_migrated(erp: ERPNextClient, doctype: str, field: str,
                    company: str) -> int:
    rows = erp.get_list(
        doctype, fields=["name"],
        filters=[[field, "is", "set"], ["company", "=", company]], limit=0)
    return len(rows)


def build_report(erp: ERPNextClient, store: Staging) -> dict:
    cfg = get_config()
    company = cfg.erpnext["company"]
    field = cfg.idempotency_field
    counts = store.counts()

    # --- voucher summary by type ---
    vtypes = sorted({k.split(":", 1)[1] for k in counts if k.startswith("voucher:")})
    vsummary = []
    for vt in vtypes:
        c = counts.get(f"voucher:{vt}", {})
        vsummary.append({
            "voucher_type": vt,
            "tally": sum(c.values()),
            "loaded": c.get("loaded", 0),
            "skipped": c.get("skipped", 0),
            "error": c.get("error", 0),
            "pending": c.get("pending", 0),
        })

    # --- master summary by kind ---
    mkinds = sorted({k.split(":", 1)[1] for k in counts if k.startswith("master:")})
    msummary = []
    for mk in mkinds:
        c = counts.get(f"master:{mk}", {})
        msummary.append({
            "kind": mk,
            "tally": sum(c.values()),
            "loaded": c.get("loaded", 0),
            "skipped": c.get("skipped", 0),
            "error": c.get("error", 0),
            "pending": c.get("pending", 0),
        })

    # --- live ERPNext counts of migrated docs ---
    erp_migrated = {}
    for dt in ["Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry"]:
        try:
            erp_migrated[dt] = _count_migrated(
                erp, dt, field, company)
        except Exception as exc:  # noqa: BLE001
            erp_migrated[dt] = f"error: {exc}"

    # --- failures detail ---
    failures = []
    for table, type_col, name_col in (("master", "kind", "name"),
                                      ("voucher", "vtype", "vnumber")):
        for r in store.conn.execute(
            f"SELECT guid, error, {type_col} t, {name_col} nm "
            f"FROM {table} WHERE load_status='error'"
        ):
            failures.append({"table": table, "guid": r["guid"], "type": r["t"],
                             "name": r["nm"], "error": r["error"]})

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": company,
        "vouchers": vsummary,
        "masters": msummary,
        "erpnext_migrated_counts": erp_migrated,
        "failures": failures,
        "totals": {
            "tally_vouchers": sum(s["tally"] for s in vsummary),
            "loaded_vouchers": sum(s["loaded"] for s in vsummary),
            "error_vouchers": sum(s["error"] for s in vsummary),
            "tally_masters": sum(s["tally"] for s in msummary),
            "loaded_masters": sum(s["loaded"] + s["skipped"] for s in msummary),
        },
    }
    _write(report)
    return report


def _write(report: dict) -> None:
    rep_dir = get_config().staging_db.parent / "reports"
    (rep_dir / "reconciliation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    # flat CSV of failures for quick triage
    with (rep_dir / "failures.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["table", "type", "guid", "name", "error"])
        for f in report["failures"]:
            w.writerow([f["table"], f["type"], f["guid"], f.get("name"), f["error"]])


def print_summary(report: dict) -> None:
    print(f"\nReconciliation @ {report['generated_at']}  ({report['company']})")
    print("\nMASTERS  kind            tally  loaded  skipped  error  pending")
    for s in report["masters"]:
        print(f"  {s['kind']:<18}{s['tally']:>6}{s['loaded']:>8}"
              f"{s['skipped']:>9}{s['error']:>7}{s['pending']:>9}")
    print("\nVOUCHERS type            tally  loaded  skipped  error  pending")
    for s in report["vouchers"]:
        print(f"  {s['voucher_type']:<18}{s['tally']:>6}{s['loaded']:>8}"
              f"{s['skipped']:>9}{s['error']:>7}{s['pending']:>9}")
    t = report["totals"]
    print(f"\nTOTAL vouchers: tally={t['tally_vouchers']} loaded={t['loaded_vouchers']} "
          f"error={t['error_vouchers']}")
    print("ERPNext migrated doc counts (live):", report["erpnext_migrated_counts"])
    if report["failures"]:
        print(f"\n{len(report['failures'])} failures -> data/reports/failures.csv")
