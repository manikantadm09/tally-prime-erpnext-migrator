"""Live Tally vs production (erp.spaceki.com) end-to-end measurement.

Read-only. Uses the DEV extract staging plus live Tally (TALLY_URL) and
points the ERPNext REST client at production without rewriting env files.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter
from pathlib import Path

from t2e.config import get_config, set_environment
from t2e.erpnext_client import ERPNextClient
from t2e.staging import Staging
from t2e.tally_client import TallyClient

from tools.live_tally_dev_audit import (
    fy_windows,
    live_ledgers,
    live_vouchers,
)
from t2e.pl_check import tally_pl_report


PRODUCTION_URL = "https://erp.spaceki.com"


def point_config_at_production() -> None:
    set_environment("DEV")
    cfg = get_config()
    env = dict(cfg._erp_env())
    env["ERPNEXT_URL"] = PRODUCTION_URL
    cfg._env_erp = env
    if PRODUCTION_URL not in cfg.erp_url:
        raise SystemExit(f"failed to point config at production: {cfg.erp_url}")


def _words(value: str | None) -> str:
    return " ".join((value or "").split())


def main() -> None:
    os.environ.setdefault("TALLY_URL", "http://127.0.0.1:9001")
    point_config_at_production()
    cfg = get_config()
    print(f"ERPNext: {cfg.erp_url}")
    print(f"Tally:   {cfg.tally['url']} company={cfg.tally['company']}")

    client = TallyClient()
    store = Staging()
    erp = ERPNextClient(dry_run=True)
    field = cfg.idempotency_field
    company = cfg.erpnext["company"]
    from_d = str(cfg.tally.get("from_date", "20200928"))
    today = dt.date.today().strftime("%Y%m%d")
    to_d = max(str(cfg.tally.get("to_date", "20260815")), today)

    ping = erp.get_list("Company", fields=["name"], filters=[["name", "=", company]], limit=1)
    if not ping:
        raise SystemExit("production API did not return the company")
    print(f"production company ok: {ping[0]['name']}")

    print("\n=== LIVE TALLY VOUCHER CENSUS ===", flush=True)
    live = live_vouchers(client, from_d, to_d)
    staged = {}
    for row in store.conn.execute(
        "SELECT guid, vtype, vnumber, vdate, source_state, load_status, payload FROM voucher"
    ):
        payload = json.loads(row["payload"] or "{}")
        staged[row["guid"]] = {
            "vtype": row["vtype"],
            "vnumber": row["vnumber"] or "",
            "vdate": row["vdate"] or "",
            "source_state": row["source_state"],
            "load_status": row["load_status"],
            "narration": payload.get("NARRATION") or payload.get("Narration") or "",
        }

    live_only = sorted(set(live) - set(staged))
    staged_only = sorted(set(staged) - set(live))
    date_mismatch, number_mismatch, type_mismatch, narration_mismatch = [], [], [], []
    for guid, lv in live.items():
        st = staged.get(guid)
        if not st:
            continue
        if lv["date"] and st["vdate"] and lv["date"] != st["vdate"]:
            date_mismatch.append({"guid": guid, "live": lv["date"], "staged": st["vdate"]})
        if (lv["vnumber"] or "") != (st["vnumber"] or ""):
            number_mismatch.append({"guid": guid, "live": lv["vnumber"], "staged": st["vnumber"]})
        if lv["vtype"] and st["vtype"] and lv["vtype"] != st["vtype"]:
            type_mismatch.append({"guid": guid, "live": lv["vtype"], "staged": st["vtype"]})
        ln, sn = _words(lv["narration"]), _words(st["narration"])
        if ln != sn:
            narration_mismatch.append({"guid": guid, "live": ln, "staged": sn})

    print(f"live_vouchers={len(live)} staged={len(staged)}")
    print(f"live_only={len(live_only)} staged_only={len(staged_only)}")
    print(
        f"date_mismatch={len(date_mismatch)} number_mismatch={len(number_mismatch)} "
        f"type_mismatch={len(type_mismatch)} narration_mismatch={len(narration_mismatch)}"
    )

    print("\n=== PRODUCTION DOCUMENTS VS LIVE TALLY ===", flush=True)
    remark_fields = {
        "Sales Invoice": "remarks",
        "Purchase Invoice": "remarks",
        "Journal Entry": "user_remark",
        "Payment Entry": "remarks",
    }
    erp_by_guid: dict[str, dict] = {}
    extra_erp = []
    missing_active = []
    narration_erp = []
    cancelled = {}
    no_guid = {}
    for dt_name, rfield in remark_fields.items():
        rows = erp.get_list(
            dt_name,
            fields=["name", field, rfield, "docstatus", "posting_date"],
            filters=[[field, "is", "set"], ["company", "=", company]],
            limit=0,
        )
        cancelled[dt_name] = len(erp.get_list(
            dt_name, fields=["name"],
            filters=[["company", "=", company], ["docstatus", "=", 2]],
            limit=0,
        ))
        no_guid[dt_name] = len(erp.get_list(
            dt_name, fields=["name"],
            filters=[[field, "is", "not set"], ["company", "=", company],
                     ["docstatus", "!=", 2]],
            limit=0,
        ))
        print(f"  {dt_name}: {len(rows)} guid-linked, cancelled={cancelled[dt_name]}, no_guid={no_guid[dt_name]}")
        for r in rows:
            guid = (r.get(field) or "").strip()
            if ":" in guid:
                extra_erp.append({"doctype": dt_name, "name": r["name"], "guid": guid, "docstatus": r.get("docstatus")})
                continue
            erp_by_guid[guid] = {
                "doctype": dt_name,
                "name": r["name"],
                "docstatus": r.get("docstatus"),
                "posting_date": str(r.get("posting_date") or ""),
                "remark": r.get(rfield) or "",
            }

    active_live = {
        g: v for g, v in live.items()
        if not v.get("cancelled") and not v.get("optional")
    }
    for guid, lv in active_live.items():
        rec = erp_by_guid.get(guid)
        if not rec or rec["docstatus"] != 1:
            missing_active.append({
                "guid": guid,
                "date": lv["date"],
                "vtype": lv["vtype"],
                "vnumber": lv["vnumber"],
            })
            continue
        if lv["date"] and rec["posting_date"] and lv["date"] != rec["posting_date"]:
            date_mismatch.append({"guid": guid, "live": lv["date"], "erp": rec["posting_date"]})
        src = _words(lv["narration"])
        tgt = _words(rec["remark"])
        if src and src != tgt:
            narration_erp.append({
                "doctype": rec["doctype"],
                "name": rec["name"],
                "guid": guid,
                "live": src,
                "erp": tgt,
                "live_len": len(src),
                "erp_len": len(tgt),
                "prefix80_match": src[:80] == tgt[:80],
            })

    print(f"live_active={len(active_live)} erp_plain_guids={len(erp_by_guid)}")
    print(f"missing_active_on_production={len(missing_active)}")
    print(f"derived_extra_erp_guids={len(extra_erp)}")
    print(f"narration_live_vs_production={len(narration_erp)}")

    print("\n=== LIVE TALLY LEDGERS ===", flush=True)
    live_led = live_ledgers(client)
    staged_led = {
        row["guid"]: row["name"]
        for row in store.conn.execute("SELECT guid, name FROM master WHERE kind='ledger'")
    }
    live_led_by_guid = {r["guid"]: r for r in live_led if r["guid"]}
    customers = erp.get_list(
        "Customer", fields=["name", "customer_name", field, "gstin", "disabled"],
        filters=[[field, "is", "set"]], limit=0,
    )
    suppliers = erp.get_list(
        "Supplier", fields=["name", "supplier_name", field, "gstin", "disabled"],
        filters=[[field, "is", "set"]], limit=0,
    )
    party_gstin_mismatch = []
    party_name_mismatch = []
    for party in customers + suppliers:
        guid = (party.get(field) or "").strip()
        src = live_led_by_guid.get(guid)
        if not src:
            continue
        pname = _words(party.get("customer_name") or party.get("supplier_name") or party.get("name"))
        if _words(src["name"]) != pname:
            party_name_mismatch.append({
                "guid": guid, "tally": src["name"], "erp": pname,
            })
        tgst = _words(src.get("gstin") or "").upper()
        egst = _words(party.get("gstin") or "").upper()
        if tgst != egst:
            party_gstin_mismatch.append({
                "guid": guid, "party": src["name"], "tally": tgst, "erp": egst,
            })

    print(f"live_ledgers={len(live_led)} staged_ledgers={len(staged_led)}")
    print(f"ledger_guid_live_only={len(set(live_led_by_guid) - set(staged_led))}")
    print(f"ledger_guid_staged_only={len(set(staged_led) - set(live_led_by_guid))}")
    print(f"customers_with_guid={len(customers)} suppliers_with_guid={len(suppliers)}")
    print(f"party_name_mismatch={len(party_name_mismatch)} party_gstin_mismatch={len(party_gstin_mismatch)}")

    print("\n=== LIVE TALLY NATIVE P&L ===", flush=True)
    latest_live = max((v["date"] for v in live.values() if v["date"]), default=to_d)
    try:
        latest_date = dt.date.fromisoformat(latest_live)
    except ValueError:
        latest_date = dt.date.today()
    pnl = []
    for label, start, end in fy_windows(latest_date):
        buckets = tally_pl_report(client, start, end)
        closing = abs(buckets.get("Less: Closing Stock", 0.0))
        sales = abs(buckets.get("Sales Accounts", 0.0))
        ind_inc = abs(buckets.get("Indirect Incomes", 0.0))
        purch = abs(buckets.get("Add: Purchase Accounts", 0.0))
        direct = abs(buckets.get("Direct Expenses", 0.0))
        indirect = abs(buckets.get("Indirect Expenses", 0.0))
        income = sales + ind_inc
        expense = purch + direct + indirect
        row = {
            "fy": label,
            "from": start,
            "to": end,
            "income": round(income, 2),
            "expense": round(expense, 2),
            "closing_stock": round(closing, 2),
            "net_incl_stock": round(income - expense + closing, 2),
            "net_excl_stock": round(income - expense, 2),
        }
        pnl.append(row)
        print(
            f"  {label} income={row['income']:,.2f} expense={row['expense']:,.2f} "
            f"stock={row['closing_stock']:,.2f} net_incl={row['net_incl_stock']:,.2f}",
            flush=True,
        )

    report = {
        "measured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "erp_url": cfg.erp_url,
        "tally_url": cfg.tally["url"],
        "company": company,
        "from_date": from_d,
        "to_date": to_d,
        "live_vouchers": len(live),
        "live_active": len(active_live),
        "live_cancelled": sum(1 for v in live.values() if v.get("cancelled")),
        "live_optional": sum(1 for v in live.values() if v.get("optional")),
        "staged_vouchers": len(staged),
        "live_only": [live[g] for g in live_only],
        "staged_only": [
            {"guid": g, **{k: staged[g][k] for k in ("vtype", "vnumber", "vdate", "source_state")}}
            for g in staged_only
        ],
        "date_mismatch": date_mismatch,
        "number_mismatch": number_mismatch,
        "type_mismatch": type_mismatch,
        "narration_mismatch_live_vs_staging": narration_mismatch,
        "missing_active_on_production": missing_active,
        "narration_live_vs_production": narration_erp,
        "derived_extra_erp_guids": extra_erp,
        "cancelled_docs": cancelled,
        "leftover_docs_without_guid": no_guid,
        "live_ledgers": len(live_led),
        "ledger_guid_live_only": sorted(set(live_led_by_guid) - set(staged_led)),
        "ledger_guid_staged_only": sorted(set(staged_led) - set(live_led_by_guid)),
        "party_name_mismatch": party_name_mismatch,
        "party_gstin_mismatch": party_gstin_mismatch,
        "pnl": pnl,
        "live_type_counts": dict(Counter(v["vtype"] for v in live.values())),
    }
    out = Path(cfg.staging_db).parent / "reports" / "live_tally_prd_e2e.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nREPORT {out}")
    store.close()


if __name__ == "__main__":
    main()
