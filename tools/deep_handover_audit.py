"""Deep live-Tally vs frozen-dev handover audit. Read-only."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

from t2e.config import get_config, set_environment
from t2e.erpnext_client import ERPNextClient
from t2e.lines import parse_entries
from t2e.mapping import GroupTree, acc_name
from t2e.staging import Staging
from t2e.tally_client import TallyClient
from t2e.tally_export import _text
from tools.fetch_tally_native_report import main as _fetch_main  # noqa: F401
from tools.verify_bill_outstandings_api import money, parse_tally_bills
from xml.etree import ElementTree as ET


PENNY = Decimal("0.01")


def d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(s) -> str:
    return " ".join((s or "").split())


def fold(s) -> str:
    return norm(s).casefold()


def fetch_report(client: TallyClient, report: str, start: str, end: str, raw: Path) -> Path:
    names = {
        "bills-receivable": "Bills Receivable",
        "bills-payable": "Bills Payable",
    }
    envelope = (
        "<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        f"<REPORTNAME>{names[report]}</REPORTNAME>"
        "<STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{client.company}</SVCURRENTCOMPANY>"
        f'<SVFROMDATE TYPE="Date">{start}</SVFROMDATE>'
        f'<SVTODATE TYPE="Date">{end}</SVTODATE>'
        "</STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"
    )
    text = client._post(envelope)
    path = raw / f"native_{report}_{start}_{end}.xml"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> None:
    set_environment("DEV")
    cfg = get_config()
    if "dev.spaceki.com" not in cfg.erp_url:
        raise SystemExit(f"refusing {cfg.erp_url}")
    client = TallyClient()
    store = Staging()
    erp = ERPNextClient(dry_run=True)
    field = cfg.idempotency_field
    company = cfg.erpnext["company"]
    abbr = "SDL"
    raw = cfg.staging_db.parent / "raw"
    tree = GroupTree(store)
    report: dict = {"erp": cfg.erp_url, "tally": cfg.tally["url"]}

    print("=== LIVE TALLY MASTERS ===", flush=True)
    live_masters = {}
    for kind, ctype, methods in [
        ("ledger", "Ledger", ["Name", "Parent", "GUID", "PartyGSTIN", "GSTIN",
                              "LedgerPhone", "Email", "Address", "IsBillWiseOn"]),
        ("group", "Group", ["Name", "Parent", "GUID"]),
        ("stockitem", "StockItem", ["Name", "Parent", "GUID", "OpeningBalance", "OpeningValue"]),
        ("stockgroup", "StockGroup", ["Name", "Parent", "GUID"]),
        ("godown", "Godown", ["Name", "Parent", "GUID"]),
        ("costcentre", "CostCentre", ["Name", "Parent", "GUID"]),
        ("costcategory", "CostCategory", ["Name", "GUID"]),
        ("unit", "Unit", ["Name", "GUID"]),
        ("vouchertype", "VoucherType", ["Name", "GUID"]),
        ("taxunit", "TaxUnit", ["Name", "GSTRegNumber", "GUID"]),
    ]:
        root = client.export_collection(
            f"deep_{kind}", ctype, methods=methods, save_as=f"deep_{kind}")
        rows = []
        for el in root.findall(f".//{ctype.upper()}"):
            name = norm(el.get("NAME") or _text(el, "NAME"))
            if not name:
                continue
            rows.append({
                "name": name,
                "parent": norm(_text(el, "PARENT")),
                "guid": _text(el, "GUID") or (el.get("GUID") or ""),
                "gstin": _text(el, "PARTYGSTIN") or _text(el, "GSTIN") or _text(el, "GSTREGNUMBER"),
                "phone": _text(el, "LEDGERPHONE"),
                "email": _text(el, "EMAIL"),
                "address": _text(el, "ADDRESS"),
                "billwise": _text(el, "ISBILLWISEON"),
                "opening": _text(el, "OPENINGBALANCE") or _text(el, "OPENINGVALUE"),
            })
        live_masters[kind] = rows
        print(f"  live {kind}={len(rows)}", flush=True)

    staged_masters = defaultdict(list)
    for row in store.conn.execute("SELECT kind, guid, name, parent, load_status, erp_doctype, erp_name, payload FROM master"):
        staged_masters[row["kind"]].append(row)

    master_cmp = {}
    for kind, live_rows in live_masters.items():
        live_g = {r["guid"] for r in live_rows if r["guid"]}
        live_n = {fold(r["name"]) for r in live_rows}
        st = staged_masters.get(kind, [])
        st_g = {r["guid"] for r in st}
        st_n = {fold(r["name"]) for r in st}
        by_status = Counter(r["load_status"] for r in st)
        master_cmp[kind] = {
            "live": len(live_rows),
            "staged": len(st),
            "guid_live_only": len(live_g - st_g),
            "guid_staged_only": len(st_g - live_g),
            "name_live_only": sorted(live_n - st_n)[:20],
            "load_status": dict(by_status),
        }
    report["masters"] = master_cmp
    print(json.dumps(master_cmp, indent=2))

    print("=== LEDGER DESTINATIONS ===", flush=True)
    customers = {fold(r["name"]): r for r in erp.get_list(
        "Customer", fields=["name", "gstin", field, "disabled"], limit=0)}
    suppliers = {fold(r["name"]): r for r in erp.get_list(
        "Supplier", fields=["name", "gstin", field, "disabled"], limit=0)}
    accounts = {fold(r["account_name"]): r for r in erp.get_list(
        "Account", fields=["name", "account_name", "is_group", "disabled", field],
        filters=[["company", "=", company]], limit=0)}

    ledger_dest = Counter()
    gstin_miss = []
    gstin_ok = 0
    gstin_skip = []
    party_missing = []
    account_missing = []
    for row in staged_masters["ledger"]:
        payload = json.loads(row["payload"] or "{}")
        name = norm(row["name"])
        parent = row["parent"] or ""
        gstin = norm(payload.get("PARTYGSTIN") or payload.get("GSTIN") or "")
        kind = tree.party_kind(parent)
        key = fold(name)
        if kind == "Customer":
            dest = customers.get(key)
            ledger_dest["customer"] += 1
            if not dest:
                party_missing.append(("Customer", name))
            elif gstin and norm(dest.get("gstin") or "") != gstin:
                if dest.get("gstin"):
                    gstin_miss.append((name, gstin, dest.get("gstin"), "Customer"))
                else:
                    gstin_skip.append((name, gstin, "Customer"))
            elif gstin:
                gstin_ok += 1
        elif kind == "Supplier":
            dest = suppliers.get(key)
            ledger_dest["supplier"] += 1
            if not dest:
                party_missing.append(("Supplier", name))
            elif gstin and norm(dest.get("gstin") or "") != gstin:
                if dest.get("gstin"):
                    gstin_miss.append((name, gstin, dest.get("gstin"), "Supplier"))
                else:
                    gstin_skip.append((name, gstin, "Supplier"))
            elif gstin:
                gstin_ok += 1
        else:
            dest = accounts.get(key)
            ledger_dest["account"] += 1
            if not dest and row["load_status"] == "loaded":
                account_missing.append(name)
    report["ledger_destinations"] = dict(ledger_dest)
    report["party_missing"] = party_missing
    report["account_missing_loaded"] = account_missing
    report["gstin"] = {
        "matched": gstin_ok,
        "mismatch": [{"party": a, "tally": b, "erp": c, "kind": d} for a, b, c, d in gstin_miss],
        "tally_has_erp_blank": [{"party": a, "tally": b, "kind": c} for a, b, c in gstin_skip],
    }
    print("dest", dict(ledger_dest), "party_missing", len(party_missing),
          "gstin_ok", gstin_ok, "gstin_mismatch", len(gstin_miss),
          "gstin_blank", len(gstin_skip))

    print("=== VOUCHER LINES + INVENTORY + BILLS ===", flush=True)
    inv_vouchers = 0
    inv_lines = 0
    bill_new = 0
    bill_agst = 0
    bill_onacc = 0
    cost_alloc = 0
    line_count = 0
    for row in store.conn.execute(
        "SELECT guid, vtype, vnumber, vdate, source_state, load_status, payload FROM voucher"
    ):
        if row["source_state"] in ("cancelled", "optional"):
            continue
        payload = json.loads(row["payload"] or "{}")
        entries = parse_entries(payload)
        line_count += len(entries)
        for e in entries:
            for b in e["bills"]:
                t = (b.get("type") or "")
                if t == "New Ref":
                    bill_new += 1
                elif t == "Agst Ref":
                    bill_agst += 1
                elif t == "On Account":
                    bill_onacc += 1
        inv = payload.get("ALLINVENTORYENTRIES.LIST") or payload.get("INVENTORYENTRIES.LIST")
        if inv:
            inv_vouchers += 1
            inv_lines += len(inv if isinstance(inv, list) else [inv])
        # cost centre allocations
        raw_e = payload.get("ALLLEDGERENTRIES.LIST") or []
        if isinstance(raw_e, dict):
            raw_e = [raw_e]
        for le in raw_e:
            if isinstance(le, dict) and (le.get("CATEGORYALLOCATIONS.LIST") or le.get("COSTCENTREALLOCATIONS.LIST")):
                cost_alloc += 1

    report["voucher_anatomy"] = {
        "active_vouchers": store.conn.execute(
            "SELECT COUNT(*) FROM voucher WHERE source_state NOT IN ('cancelled','optional')"
        ).fetchone()[0],
        "ledger_lines": line_count,
        "inventory_vouchers": inv_vouchers,
        "inventory_lines": inv_lines,
        "bill_new_ref": bill_new,
        "bill_agst_ref": bill_agst,
        "bill_on_account": bill_onacc,
        "cost_centre_allocations": cost_alloc,
    }
    print(report["voucher_anatomy"])

    print("=== ERPNext GUID COVERAGE ===", flush=True)
    staged_guids = {r[0] for r in store.conn.execute(
        "SELECT guid FROM voucher WHERE source_state NOT IN ('cancelled','optional')")}
    coverage = {}
    extra_submitted = 0
    missing_submitted = list(staged_guids)
    present = set()
    leftover_cancelled = {}
    for dt in ("Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry"):
        rows = erp.get_list(
            dt, fields=["name", field, "docstatus"],
            filters=[["company", "=", company]], limit=0)
        leftover_cancelled[dt] = sum(1 for r in rows if r.get("docstatus") == 2)
        for r in rows:
            g = (r.get(field) or "").strip()
            if g in staged_guids and r.get("docstatus") == 1:
                present.add(g)
            elif g and g not in staged_guids and r.get("docstatus") == 1 and not g.startswith(
                    ("period-closing-", "closing-stock-", "vendor-advance-control-",
                     "opening-balances", "ledger-fidelity")):
                extra_submitted += 1
        coverage[dt] = {
            "total": len(rows),
            "submitted": sum(1 for r in rows if r.get("docstatus") == 1),
            "cancelled": leftover_cancelled[dt],
        }
    missing = sorted(staged_guids - present)
    report["guid_coverage"] = {
        "active_source": len(staged_guids),
        "submitted_present": len(present),
        "missing_submitted": len(missing),
        "missing_sample": missing[:15],
        "extra_submitted_non_repair": extra_submitted,
        "docs": coverage,
    }
    print(report["guid_coverage"])

    print("=== GUID BILL PHASE ===", flush=True)
    bill_path = cfg.staging_db.parent / "reports" / "guid_bill_references.json"
    if bill_path.exists():
        bills = json.loads(bill_path.read_text(encoding="utf-8"))
        report["guid_bills"] = bills.get("plan_summary") or bills.get("summary") or {
            k: bills.get(k) for k in ("applied", "skipped", "exceptions") if k in bills
        }
        if "plan_summary" in bills:
            report["guid_bills"] = bills["plan_summary"]
            report["guid_bills"]["applied_note"] = "see load log"
    print(report.get("guid_bills"))

    print("=== LIVE TALLY BILLS ===", flush=True)
    br = fetch_report(client, "bills-receivable", "20200928", "20260815", raw)
    bp = fetch_report(client, "bills-payable", "20200928", "20260815", raw)
    rec = parse_tally_bills(br)
    pay = parse_tally_bills(bp)
    report["tally_bills"] = {
        "receivable_rows": len(rec),
        "receivable_amount": str(sum((money(r["amount"]) for r in rec), Decimal("0"))),
        "payable_rows": len(pay),
        "payable_amount": str(sum((money(r["amount"]) for r in pay), Decimal("0"))),
    }
    print(report["tally_bills"])

    # ERP invoice outstanding
    si_out = erp.get_list(
        "Sales Invoice",
        fields=["name", "outstanding_amount", "status", "docstatus"],
        filters=[["company", "=", company], ["docstatus", "=", 1]], limit=0)
    pi_out = erp.get_list(
        "Purchase Invoice",
        fields=["name", "outstanding_amount", "status", "docstatus"],
        filters=[["company", "=", company], ["docstatus", "=", 1]], limit=0)
    si_os = sum(abs(float(r.get("outstanding_amount") or 0)) for r in si_out)
    pi_os = sum(abs(float(r.get("outstanding_amount") or 0)) for r in pi_out)
    report["erp_outstanding"] = {
        "sales_invoices": len(si_out),
        "sales_outstanding": round(si_os, 2),
        "sales_unpaid": sum(1 for r in si_out if abs(float(r.get("outstanding_amount") or 0)) > 0.01),
        "purchase_invoices": len(pi_out),
        "purchase_outstanding": round(pi_os, 2),
        "purchase_unpaid": sum(1 for r in pi_out if abs(float(r.get("outstanding_amount") or 0)) > 0.01),
    }
    print(report["erp_outstanding"])

    print("=== STOCK ITEMS / GODOWNS / COST ===", flush=True)
    items = erp.get_list("Item", fields=["name", "item_name", "disabled"], limit=0)
    warehouses = erp.get_list("Warehouse", fields=["name"],
                              filters=[["company", "=", company]], limit=0)
    report["stock_ops"] = {
        "tally_stock_items": len(live_masters["stockitem"]),
        "tally_stock_groups": len(live_masters["stockgroup"]),
        "tally_godowns": len(live_masters["godown"]),
        "tally_cost_centres": len(live_masters["costcentre"]),
        "tally_units": len(live_masters["unit"]),
        "erp_items": len(items),
        "erp_warehouses": len(warehouses),
        "stock_item_names_sample": [r["name"] for r in live_masters["stockitem"][:15]],
    }
    print(report["stock_ops"])

    print("=== PER-LEDGER CLOSING (non-party) ===", flush=True)
    client.from_date, client.to_date = "20200928", "20260815"
    root = client.export_collection(
        "deep_ledger_close", "Ledger",
        methods=["Name", "Parent", "ClosingBalance", "GUID"],
        dated=True, save_as="deep_ledger_close")
    # ERP GL by account_name and by party
    gl_rows = erp.get_list(
        "GL Entry",
        fields=["account", "party", "party_type", "debit", "credit"],
        filters=[["company", "=", company], ["is_cancelled", "=", 0],
                 ["posting_date", "<=", "2026-08-15"]],
        limit=0)
    acc_net = defaultdict(lambda: Decimal("0"))
    party_net = defaultdict(lambda: Decimal("0"))
    for g in gl_rows:
        acc = (g.get("account") or "").rsplit(" - ", 1)[0]
        acc_net[fold(acc)] += d(g.get("debit")) - d(g.get("credit"))
        if g.get("party"):
            party_net[(g.get("party_type"), fold(g.get("party")))] += (
                d(g.get("debit")) - d(g.get("credit")))

    ledger_diffs = []
    ledger_ok = 0
    skipped_party = 0
    for el in root.findall(".//LEDGER"):
        name = norm(el.get("NAME") or "")
        parent = norm(_text(el, "PARENT"))
        if not name:
            continue
        bal = d(_text(el, "CLOSINGBALANCE"))
        # Tally credit-positive
        rt = tree.root_type(parent)
        if rt == "Asset":
            tally_dr = -bal
        elif rt in ("Liability", "Equity"):
            tally_dr = -bal  # convert credit-positive to Dr-Cr
        elif rt == "Income":
            tally_dr = -bal
        elif rt == "Expense":
            tally_dr = -bal
        else:
            tally_dr = -bal
        kind = tree.party_kind(parent)
        if kind:
            skipped_party += 1
            erp_dr = party_net.get((kind, fold(name)), Decimal("0"))
            # party GL sign is Dr-Cr; Tally party also converted above
            if abs(erp_dr - tally_dr) > Decimal("1.00"):
                ledger_diffs.append({
                    "name": name, "kind": kind, "tally": f"{tally_dr:.2f}",
                    "erp": f"{erp_dr:.2f}", "diff": f"{(erp_dr - tally_dr):.2f}",
                })
            else:
                ledger_ok += 1
            continue
        erp_dr = acc_net.get(fold(name), Decimal("0"))
        if abs(erp_dr - tally_dr) > Decimal("1.00"):
            ledger_diffs.append({
                "name": name, "kind": "Account", "tally": f"{tally_dr:.2f}",
                "erp": f"{erp_dr:.2f}", "diff": f"{(erp_dr - tally_dr):.2f}",
            })
        else:
            ledger_ok += 1

    ledger_diffs.sort(key=lambda r: abs(Decimal(r["diff"])), reverse=True)
    report["ledger_closings"] = {
        "compared_ok": ledger_ok,
        "diff_ge_1": len(ledger_diffs),
        "top": ledger_diffs[:40],
    }
    print("ledger_ok", ledger_ok, "diffs", len(ledger_diffs))
    for row in ledger_diffs[:15]:
        print(" ", row)

    out = cfg.staging_db.parent / "reports" / "deep_handover_audit.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("REPORT", out)
    store.close()


if __name__ == "__main__":
    main()
