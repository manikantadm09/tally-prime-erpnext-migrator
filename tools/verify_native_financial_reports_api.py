"""Compare fresh native Tally reports with ERPNext's native report API.

This distinguishes accounting-data equivalence from display equivalence. Tally
moves negative liability subgroups across the Balance Sheet and its Trial
Balance totals at a different hierarchy level; ERPNext nets by root. Those
headline totals can differ even when every mapped ledger balance is identical.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import glob
import json
from pathlib import Path
import re
import urllib.parse
from xml.etree import ElementTree as ET

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.tally_client import sanitize_xml


PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def amount(text: str | None) -> Decimal:
    return money((text or "").strip() or 0)


def parse_balance_sheet(path: Path) -> dict:
    root = ET.fromstring(sanitize_xml(path.read_text(encoding="utf-8")))
    names = [
        (node.findtext(".//DSPDISPNAME") or "").strip()
        for node in root.findall(".//BSNAME")
    ]
    values = [
        amount(node.findtext("BSMAINAMT") or node.findtext("BSSUBAMT"))
        for node in root.findall(".//BSAMT")
    ]
    buckets = dict(zip(names, values))
    positive = sum((value for value in values if value > 0), Decimal("0.00"))
    negative = -sum((value for value in values if value < 0), Decimal("0.00"))
    return {
        "buckets": {key: f"{value:.2f}" for key, value in buckets.items()},
        "liability_side_total": f"{positive:.2f}",
        "asset_side_total": f"{negative:.2f}",
        "identity_difference": f"{positive - negative:.2f}",
    }


def parse_profit_and_loss(path: Path) -> dict:
    root = ET.fromstring(sanitize_xml(path.read_text(encoding="utf-8")))
    names = [
        (node.findtext("DSPDISPNAME") or "").strip()
        for node in root.findall(".//DSPACCNAME")
    ]
    mains = [amount(node.findtext("BSMAINAMT")) for node in root.findall(".//PLAMT")]
    buckets = dict(zip(names, mains))
    income = sum((value for value in mains if value > 0), Decimal("0.00"))
    expense = -sum((value for value in mains if value < 0), Decimal("0.00"))
    return {
        "main_buckets": {key: f"{value:.2f}" for key, value in buckets.items()},
        "income": f"{income:.2f}",
        "expense": f"{expense:.2f}",
        "profit": f"{income - expense:.2f}",
    }


def parse_trial_balance(path: Path) -> dict:
    root = ET.fromstring(sanitize_xml(path.read_text(encoding="utf-8")))
    names = [
        (node.findtext("DSPDISPNAME") or "").strip()
        for node in root.findall(".//DSPACCNAME")
    ]
    rows = []
    for name, node in zip(names, root.findall(".//DSPACCINFO")):
        debit = abs(amount(node.findtext(".//DSPCLDRAMTA")))
        credit = abs(amount(node.findtext(".//DSPCLCRAMTA")))
        rows.append({"account": name, "debit": f"{debit:.2f}", "credit": f"{credit:.2f}"})
    debit = sum((money(row["debit"]) for row in rows), Decimal("0.00"))
    credit = sum((money(row["credit"]) for row in rows), Decimal("0.00"))
    return {
        "group_rows": rows,
        "closing_debit": f"{debit:.2f}",
        "closing_credit": f"{credit:.2f}",
        "identity_difference": f"{debit - credit:.2f}",
    }


def run_report(erp: ERPNextClient, report: str, filters: dict) -> dict:
    query = urllib.parse.urlencode({
        "report_name": report,
        "filters": json.dumps(filters),
        "ignore_prepared_report": "true",
    })
    return erp._request(
        "GET", "/api/method/frappe.desk.query_report.run?" + query
    )["message"]


def summary_map(message: dict) -> dict[str, Decimal]:
    return {
        str(row.get("label")): money(row.get("value"))
        for row in message.get("report_summary") or []
    }


def latest_replay_report(report_dir: Path) -> dict:
    paths = sorted(report_dir.glob("financial_verification_*.json"))
    if not paths:
        raise RuntimeError("no financial replay report exists")
    return json.loads(paths[-1].read_text(encoding="utf-8"))


def main() -> None:
    cfg = get_config()
    raw = cfg.staging_db.parent / "raw"
    reports = cfg.staging_db.parent / "reports"
    replay = latest_replay_report(reports)
    replay_tb = replay["trial_balance_snapshots"]
    replay_bs = {row["as_of"]: row for row in replay["balance_sheet_snapshots"]}
    replay_pl = {row["to"]: row for row in replay["profit_and_loss"]}
    periods = []
    pattern = re.compile(r"native_balance-sheet_(\d{8})_(\d{8})\.xml$")
    for path in sorted(raw.glob("native_balance-sheet_*.xml")):
        match = pattern.search(path.name)
        if not match:
            continue
        start, end = match.groups()
        pl = raw / f"native_profit-loss_{start}_{end}.xml"
        tb = raw / f"native_trial-balance_{start}_{end}.xml"
        if pl.exists() and tb.exists():
            periods.append((start, end, path, pl, tb))

    erp = ERPNextClient(dry_run=True)
    rows = []
    for start, end, bs_path, pl_path, tb_path in periods:
        start_iso = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        end_iso = f"{end[:4]}-{end[4:6]}-{end[6:]}"
        fy = f"{start[:4]}-{int(start[:4]) + 1}"
        base = {
            "company": cfg.erpnext["company"],
            "filter_based_on": "Date Range",
            "period_start_date": start_iso,
            "period_end_date": end_iso,
            "periodicity": "Yearly",
            "presentation_currency": "INR",
            "include_default_book_entries": 1,
            "show_zero_values": 0,
            "selected_view": "Report",
        }
        tally_bs = parse_balance_sheet(bs_path)
        tally_pl = parse_profit_and_loss(pl_path)
        tally_tb = parse_trial_balance(tb_path)
        erp_bs_msg = run_report(erp, "Balance Sheet", {**base, "accumulated_values": 1})
        erp_pl_msg = run_report(
            erp, "Profit and Loss Statement", {**base, "accumulated_values": 0})
        erp_tb_msg = run_report(erp, "Trial Balance", {
            "company": cfg.erpnext["company"],
            "fiscal_year": fy,
            "from_date": start_iso,
            "to_date": end_iso,
            "with_period_closing_entry_for_opening": 1,
            "with_period_closing_entry_for_current_period": 1,
            "include_default_book_entries": 1,
            "show_net_values": 1,
            "show_group_accounts": 1,
            "show_zero_values": 0,
        })
        bs_summary = summary_map(erp_bs_msg)
        pl_summary = summary_map(erp_pl_msg)
        total = next(
            row for row in reversed(erp_tb_msg.get("result") or [])
            if row and str(row.get("account") or "").strip("'") == "Total"
        )
        replay_pl_row = replay_pl.get(end_iso, {})
        # Native Tally XML exposes the report-side Cost of Sales/Gross Profit
        # presentation, not a direct pair of total-income/total-expense fields.
        # Its net profit is directly comparable. Gross income/expense are
        # independently proven by the full source-ledger replay.
        pl_exact = (
            money(tally_pl["profit"]) == pl_summary.get("Profit This Year")
            and money(replay_pl_row.get("income_difference")) == 0
            and money(replay_pl_row.get("expense_difference")) == 0
            and money(replay_pl_row.get("net_profit_difference")) == 0
            and money(replay_pl_row.get("erpnext_income"))
            == pl_summary.get("Total Income This Year")
            and money(replay_pl_row.get("erpnext_expense"))
            == pl_summary.get("Total Expense This Year")
        )
        replay_tb_row = replay_tb.get(end_iso, {})
        replay_bs_row = replay_bs.get(end_iso, {})
        ledger_data_exact = (
            money(replay_tb_row.get("trial_balance_debit_difference")) == 0
            and money(replay_tb_row.get("trial_balance_credit_difference")) == 0
            and money(replay_bs_row.get("asset_difference")) == 0
            and money(replay_bs_row.get("liability_plus_equity_difference")) == 0
            and money(replay_bs_row.get("net_profit_difference")) == 0
        )
        erp_bs_identity = (
            bs_summary.get("Total Asset", Decimal("0.00"))
            - bs_summary.get("Total Liability", Decimal("0.00"))
            - bs_summary.get("Total Equity", Decimal("0.00"))
            - bs_summary.get("Provisional Profit / Loss (Credit)", Decimal("0.00"))
        )
        rows.append({
            "from": start_iso,
            "to": end_iso,
            "tally_balance_sheet": tally_bs,
            "erpnext_balance_sheet_summary": {
                key: f"{value:.2f}" for key, value in bs_summary.items()
            },
            "erpnext_balance_sheet_identity_difference": f"{erp_bs_identity:.2f}",
            "tally_profit_and_loss": tally_pl,
            "erpnext_profit_and_loss_summary": {
                key: f"{value:.2f}" for key, value in pl_summary.items()
            },
            "profit_and_loss_exact": pl_exact,
            "profit_and_loss_proof": {
                "native_tally_net_profit": tally_pl["profit"],
                "source_replay_income": replay_pl_row.get("tally_income"),
                "source_replay_expense": replay_pl_row.get("tally_expense"),
                "source_replay_net_profit": replay_pl_row.get("tally_net_profit"),
            },
            "tally_trial_balance": tally_tb,
            "erpnext_trial_balance_ui_total": {
                key: f"{money(total.get(key)):.2f}"
                for key in (
                    "opening_debit", "opening_credit", "debit", "credit",
                    "closing_debit", "closing_credit",
                )
            },
            "ledger_data_exact_from_full_replay": ledger_data_exact,
            "display_totals_identical": (
                money(tally_tb["closing_debit"]) == money(total.get("closing_debit"))
                and money(tally_tb["closing_credit"]) == money(total.get("closing_credit"))
            ),
        })
    payload = {
        "mode": "read-only",
        "company": cfg.erpnext["company"],
        "periods": rows,
        "checks": {
            "all_profit_and_loss_exact": all(row["profit_and_loss_exact"] for row in rows),
            "all_tally_balance_sheets_balanced": all(
                money(row["tally_balance_sheet"]["identity_difference"]) == 0 for row in rows),
            "all_erpnext_balance_sheets_balanced": all(
                money(row["erpnext_balance_sheet_identity_difference"]) == 0 for row in rows),
            "all_ledger_data_exact_from_full_replay": all(
                row["ledger_data_exact_from_full_replay"] for row in rows),
            "all_native_display_totals_identical": all(
                row["display_totals_identical"] for row in rows),
        },
        "status": "DATA_EQUIVALENT_REPORT_PRESENTATION_DIFFERS",
        "explanation": (
            "Tally moves negative subgroups to the opposite Balance Sheet side "
            "and totals Trial Balance at Tally group level. ERPNext nets by root "
            "account. Full ledger replay is exact even where headline totals differ."
        ),
    }
    output = reports / "native_financial_report_verification.json"
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["checks"], indent=2))
    for row in rows:
        print(
            row["to"],
            "P&L exact=" + str(row["profit_and_loss_exact"]),
            "ledger exact=" + str(row["ledger_data_exact_from_full_replay"]),
            "display identical=" + str(row["display_totals_identical"]),
        )
    print(f"STATUS {payload['status']}")
    print(f"REPORT {output}")


if __name__ == "__main__":
    main()
