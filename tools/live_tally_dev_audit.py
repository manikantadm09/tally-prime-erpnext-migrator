"""Live Tally vs frozen-dev completeness audit.

Compares the open Tally company (via TALLY_URL) to:
  1. this extract's staging.sqlite
  2. submitted documents on the configured ERPNext site (must be DEV)

Does not write to ERPNext or Tally.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path

from t2e.config import get_config, set_environment
from t2e.erpnext_client import ERPNextClient
from t2e.pl_check import tally_pl_report
from t2e.staging import Staging
from t2e.tally_client import TallyClient
from t2e.tally_export import _text


def _month_windows(from_d: str, to_d: str) -> list[tuple[str, str]]:
    start = dt.datetime.strptime(from_d, "%Y%m%d").date()
    end = dt.datetime.strptime(to_d, "%Y%m%d").date()
    windows = []
    cur = start.replace(day=1)
    while cur <= end:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1)
        win_from = max(cur, start).strftime("%Y%m%d")
        win_to = min(nxt - dt.timedelta(days=1), end).strftime("%Y%m%d")
        windows.append((win_from, win_to))
        cur = nxt
    return windows


def _parse_tally_date(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def live_vouchers(client: TallyClient, from_d: str, to_d: str) -> dict[str, dict]:
    """One dated collection. TallyPrime often ignores SVTODATE and returns the
    full suffix from SVFROMDATE, which is what we want for a live census."""
    client.from_date, client.to_date = from_d, to_d
    print(f"  requesting live Tally vouchers {from_d}-{to_d} ...", flush=True)
    root = client.export_collection(
        "live_vouchers",
        "Voucher",
        methods=["GUID", "Date", "VoucherTypeName", "VoucherNumber", "Narration",
                 "IsCancelled", "IsDeleted", "IsOptional"],
        dated=True,
        save_as="live_vouchers",
    )
    rows = root.findall(".//VOUCHER")
    print(f"  live Tally raw voucher elements: {len(rows)}", flush=True)
    out: dict[str, dict] = {}
    for el in rows:
        guid = _text(el, "GUID") or (el.get("GUID") or "").strip()
        if not guid:
            continue
        cancelled = (_text(el, "ISCANCELLED").lower() == "yes"
                     or _text(el, "ISDELETED").lower() == "yes")
        optional = _text(el, "ISOPTIONAL").lower() == "yes"
        out[guid] = {
            "guid": guid,
            "date": _parse_tally_date(_text(el, "DATE") or _text(el, "Date")),
            "vtype": _text(el, "VOUCHERTYPENAME") or _text(el, "VoucherTypeName"),
            "vnumber": _text(el, "VOUCHERNUMBER") or _text(el, "VoucherNumber"),
            "narration": _text(el, "NARRATION") or _text(el, "Narration"),
            "cancelled": cancelled,
            "optional": optional,
        }
    return out


def live_ledgers(client: TallyClient) -> list[dict]:
    root = client.export_collection(
        "live_ledgers",
        "Ledger",
        methods=["Name", "Parent", "GUID", "PartyGSTIN", "GSTIN"],
        dated=False,
        save_as="live_ledgers",
    )
    rows = []
    for el in root.findall(".//LEDGER"):
        name = (el.get("NAME") or _text(el, "NAME") or "").strip()
        if not name:
            continue
        rows.append({
            "name": " ".join(name.split()),
            "parent": (_text(el, "PARENT") or "").strip(),
            "guid": _text(el, "GUID") or (el.get("GUID") or "").strip(),
            "gstin": _text(el, "PARTYGSTIN") or _text(el, "GSTIN"),
        })
    return rows


def fy_windows(latest: dt.date) -> list[tuple[str, str, str]]:
    periods = []
    for start_year in range(2020, latest.year + 1):
        start = dt.date(start_year, 4, 1)
        end = dt.date(start_year + 1, 3, 31)
        if start_year == 2020:
            start = dt.date(2020, 9, 28)
        if start > latest:
            break
        end = min(end, latest)
        label = f"FY{start_year}-{str(start_year + 1)[-2:]}"
        periods.append((label, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
    return periods


def main() -> None:
    set_environment("DEV")
    cfg = get_config()
    if "dev.spaceki.com" not in cfg.erp_url:
        raise SystemExit(f"refusing: ERP URL is {cfg.erp_url}, expected frozen-dev")
    print(f"ERPNext: {cfg.erp_url}")
    print(f"Tally:   {cfg.tally['url']} company={cfg.tally['company']}")

    client = TallyClient()
    store = Staging()
    erp = ERPNextClient(dry_run=True)
    field = cfg.idempotency_field
    company = cfg.erpnext["company"]
    from_d = str(cfg.tally.get("from_date", "20200928"))
    to_d = str(cfg.tally.get("to_date", "20260815"))

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
            "narration": (payload.get("NARRATION") or payload.get("Narration") or ""),
        }

    live_only = sorted(set(live) - set(staged))
    staged_only = sorted(set(staged) - set(live))
    date_mismatch = []
    number_mismatch = []
    type_mismatch = []
    narration_mismatch = []
    for guid, lv in live.items():
        st = staged.get(guid)
        if not st:
            continue
        if lv["date"] and st["vdate"] and lv["date"] != st["vdate"]:
            date_mismatch.append((guid, lv["date"], st["vdate"]))
        if (lv["vnumber"] or "") != (st["vnumber"] or ""):
            number_mismatch.append((guid, lv["vnumber"], st["vnumber"]))
        if lv["vtype"] and st["vtype"] and lv["vtype"] != st["vtype"]:
            type_mismatch.append((guid, lv["vtype"], st["vtype"]))
        ln = " ".join((lv["narration"] or "").split())
        sn = " ".join((st["narration"] or "").split())
        if ln != sn:
            narration_mismatch.append((guid, ln, sn))

    print(f"live_vouchers={len(live)} staged={len(staged)}")
    print(f"live_only_missing_from_extract={len(live_only)}")
    print(f"staged_only_missing_from_live={len(staged_only)}")
    print(f"date_mismatch={len(date_mismatch)} number_mismatch={len(number_mismatch)}")
    print(f"type_mismatch={len(type_mismatch)} narration_mismatch={len(narration_mismatch)}")
    for guid in live_only[:15]:
        print("  LIVE_ONLY", live[guid])
    for guid in staged_only[:15]:
        print("  STAGED_ONLY", guid, staged[guid]["vtype"], staged[guid]["vdate"],
              staged[guid]["source_state"])
    for row in narration_mismatch[:10]:
        print("  NARRATION", row[0], "LIVE=", row[1][:80], "STAGED=", row[2][:80])

    print("\n=== LIVE TALLY LEDGERS ===", flush=True)
    live_led = live_ledgers(client)
    staged_led = {
        row["guid"]: row["name"]
        for row in store.conn.execute(
            "SELECT guid, name FROM master WHERE kind='ledger'"
        )
    }
    live_led_by_guid = {r["guid"]: r["name"] for r in live_led if r["guid"]}
    live_led_names = {r["name"] for r in live_led}
    staged_led_names = set(staged_led.values())
    print(f"live_ledgers={len(live_led)} staged_ledgers={len(staged_led)}")
    print(f"ledger_guid_live_only={len(set(live_led_by_guid) - set(staged_led))}")
    print(f"ledger_guid_staged_only={len(set(staged_led) - set(live_led_by_guid))}")
    print(f"ledger_name_live_only={sorted(live_led_names - staged_led_names)[:20]}")
    print(f"ledger_name_staged_only={sorted(staged_led_names - live_led_names)[:20]}")

    print("\n=== ERPNext NARRATION / REMARKS ===", flush=True)
    remark_fields = {
        "Sales Invoice": "remarks",
        "Purchase Invoice": "remarks",
        "Journal Entry": "user_remark",
        "Payment Entry": "remarks",
    }
    word_miss = []
    extra_live_guids = []
    for dt_name, rfield in remark_fields.items():
        rows = erp.get_list(
            dt_name,
            fields=["name", field, rfield, "docstatus"],
            filters=[[field, "is", "set"], ["company", "=", company]],
            limit=0,
        )
        for r in rows:
            guid = (r.get(field) or "").strip()
            if not guid:
                continue
            if guid not in staged:
                extra_live_guids.append((dt_name, r["name"], guid, r.get("docstatus")))
                continue
            if r.get("docstatus") != 1:
                continue
            src = " ".join((staged[guid]["narration"] or "").split())[:1000]
            tgt = " ".join((r.get(rfield) or "").split())
            if src and src != tgt:
                word_miss.append((dt_name, r["name"], guid, src[:80], tgt[:80]))
        print(f"  {dt_name}: fetched {len(rows)}")
    leftover_no_guid = {}
    leftover_cancelled = {}
    for dt_name in remark_fields:
        leftover_no_guid[dt_name] = len(erp.get_list(
            dt_name,
            fields=["name"],
            filters=[[field, "is", "not set"], ["company", "=", company],
                     ["docstatus", "!=", 2]],
            limit=0,
        ))
        leftover_cancelled[dt_name] = len(erp.get_list(
            dt_name,
            fields=["name"],
            filters=[["company", "=", company], ["docstatus", "=", 2]],
            limit=0,
        ))
    print(f"narration_word_mismatches={len(word_miss)}")
    print(f"extra_erp_guids_not_in_this_extract={len({g for _,_,g,_ in extra_live_guids})}")
    print(f"leftover_docs_without_guid={leftover_no_guid}")
    print(f"cancelled_docs={leftover_cancelled}")
    for row in word_miss[:12]:
        print("  WORD", row)

    print("\n=== LIVE TALLY NATIVE P&L ===", flush=True)
    latest_live = max((v["date"] for v in live.values() if v["date"]), default=to_d)
    try:
        latest_date = dt.date.fromisoformat(latest_live)
    except ValueError:
        latest_date = dt.datetime.strptime(to_d, "%Y%m%d").date()
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
        net_incl = income - expense + closing
        net_excl = income - expense
        row = {
            "fy": label,
            "from": start,
            "to": end,
            "income": round(income, 2),
            "expense": round(expense, 2),
            "closing_stock": round(closing, 2),
            "net_incl_stock": round(net_incl, 2),
            "net_excl_stock": round(net_excl, 2),
        }
        pnl.append(row)
        print(
            f"  {label} {start}-{end} income={row['income']:,.2f} "
            f"expense={row['expense']:,.2f} stock={row['closing_stock']:,.2f} "
            f"net_excl={row['net_excl_stock']:,.2f} net_incl={row['net_incl_stock']:,.2f}",
            flush=True,
        )

    report = {
        "erp_url": cfg.erp_url,
        "tally_url": cfg.tally["url"],
        "live_vouchers": len(live),
        "staged_vouchers": len(staged),
        "live_only": live_only,
        "staged_only": [
            {
                "guid": g,
                "vtype": staged[g]["vtype"],
                "vdate": staged[g]["vdate"],
                "source_state": staged[g]["source_state"],
            }
            for g in staged_only
        ],
        "date_mismatch": date_mismatch,
        "number_mismatch": number_mismatch,
        "type_mismatch": type_mismatch,
        "narration_mismatch_live_vs_staging": [
            {"guid": g, "live": a, "staged": b} for g, a, b in narration_mismatch
        ],
        "narration_word_mismatches_erp": [
            {"doctype": a, "name": b, "guid": c, "src": d, "tgt": e}
            for a, b, c, d, e in word_miss
        ],
        "extra_erp_guids": extra_live_guids,
        "live_ledgers": len(live_led),
        "pnl": pnl,
        "leftover_docs_without_guid": leftover_no_guid,
        "cancelled_docs": leftover_cancelled,
        "live_type_counts": dict(Counter(v["vtype"] for v in live.values())),
        "live_active": sum(1 for v in live.values() if not v.get("cancelled") and not v.get("optional")),
        "live_cancelled": sum(1 for v in live.values() if v.get("cancelled")),
        "live_optional": sum(1 for v in live.values() if v.get("optional")),
    }
    out = Path(cfg.staging_db).parent / "reports" / "live_tally_dev_audit.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nREPORT {out}")
    store.close()


if __name__ == "__main__":
    main()
