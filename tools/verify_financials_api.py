"""Read-only source-to-target financial verification through public APIs.

This avoids requiring direct ERPNext database credentials.  It compares live
Tally ledger balances and staged source voucher values with migration-linked
ERPNext documents and their General Ledger entries.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.load_masters import fetch_company_defaults
from t2e.mapping import GroupTree, acc_name
from t2e.staging import Staging
from t2e.tally_client import TallyClient, sanitize_xml


PENNY = Decimal("0.01")
TOLERANCE = Decimal("0.01")
DOCTYPE_NAMES = [
    "Sales Invoice",
    "Purchase Invoice",
    "Journal Entry",
    "Payment Entry",
]


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        return Decimal("0.00")


def norm(value: str | None) -> str:
    return " ".join((value or "").split())


def tally_ledgers(
    client: TallyClient,
    tree: GroupTree,
    from_date: str,
    to_date: str,
    label: str,
) -> list[dict[str, Any]]:
    client.from_date, client.to_date = from_date, to_date
    root = client.export_collection(
        f"verify_ledgers_{label}",
        "Ledger",
        methods=["Name", "Parent", "OpeningBalance", "ClosingBalance"],
        dated=True,
        save_as=f"verify_ledgers_{label}",
    )
    rows = []
    for element in root.findall(".//LEDGER"):
        name = norm(element.get("NAME") or element.findtext("NAME"))
        if not name:
            continue
        parent = norm(element.findtext("PARENT"))
        closing_credit_positive = money(element.findtext("CLOSINGBALANCE"))
        rows.append(
            {
                "name": name,
                "parent": parent,
                "root_type": tree.root_type(parent),
                "opening_credit_positive": money(
                    element.findtext("OPENINGBALANCE")
                ),
                "closing_credit_positive": closing_credit_positive,
                # Common signed convention for comparison: Dr positive, Cr negative.
                "balance": -closing_credit_positive,
            }
        )
    return rows


def fiscal_periods(latest: date) -> list[tuple[str, date, date]]:
    periods = []
    for start_year in range(2021, latest.year + 1):
        start = date(start_year, 4, 1)
        end = date(start_year + 1, 3, 31)
        if start_year == 2021:
            start = date(2022, 1, 1)
        if start > latest:
            break
        end = min(end, latest)
        periods.append((f"FY{start_year}-{str(start_year + 1)[-2:]}", start, end))
        if end == latest:
            break
    return periods


def fetch_erp_data(erp: ERPNextClient, company: str) -> dict[str, Any]:
    accounts = erp.get_list(
        "Account",
        fields=[
            "name",
            "account_name",
            "root_type",
            "account_type",
            "is_group",
        ],
        filters=[["company", "=", company]],
        limit=0,
    )
    documents: dict[str, list[dict]] = {}
    document_by_name: dict[str, dict] = {}
    for doctype in DOCTYPE_NAMES:
        rows = erp.get_list(
            doctype,
            fields=["name", "tally_guid", "posting_date", "docstatus"],
            filters=[
                ["company", "=", company],
                ["tally_guid", "is", "set"],
                ["docstatus", "=", 1],
            ],
            limit=0,
        )
        documents[doctype] = rows
        for row in rows:
            document_by_name[row["name"]] = {
                **row,
                "doctype": doctype,
            }
    gl = erp.get_list(
        "GL Entry",
        fields=[
            "posting_date",
            "account",
            "party_type",
            "party",
            "voucher_type",
            "voucher_no",
            "debit",
            "credit",
            "is_opening",
        ],
        filters=[
            ["company", "=", company],
            ["is_cancelled", "=", 0],
        ],
        limit=0,
    )
    migrated_gl = [
        row for row in gl if row["voucher_no"] in document_by_name
    ]
    return {
        "accounts": accounts,
        "documents": documents,
        "document_by_name": document_by_name,
        "gl": migrated_gl,
    }


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _scalar(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("#text", ""))
    return str(value)


def _list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def source_postings(
    store: Staging,
    defaults,
    from_yyyymmdd: str,
    closing_stock_spec: dict,
    account_meta: dict[str, dict],
) -> list[dict[str, Any]]:
    """Reconstruct Tally's signed ledger postings from captured raw payloads.

    Tally XML amounts are credit-positive, debit-negative; this function emits
    the common comparison convention debit-positive, credit-negative.
    """
    canonical_accounts = {
        norm(account_name).lower(): account_name
        for account_name in account_meta
    }

    def canonical(account_name: str) -> str:
        return canonical_accounts.get(
            norm(account_name).lower(), account_name
        )

    mapping = {}
    for master in store.masters("ledger"):
        if not master["erp_name"]:
            continue
        if master["erp_doctype"] in ("Customer", "Supplier"):
            key = ("party", master["erp_doctype"], master["erp_name"])
            account = canonical(
                defaults.receivable
                if master["erp_doctype"] == "Customer"
                else defaults.payable
            )
        else:
            account = canonical(master["erp_name"])
            key = ("account", account)
        mapping[norm(master["name"]).lower()] = (key, account)

    postings = []
    opening_date = (
        datetime.strptime(from_yyyymmdd, "%Y%m%d").date() - timedelta(days=1)
    )
    captured_openings: dict[str, Decimal] = {}
    opening_path = get_config().staging_db.parent / "raw" / "opening_ledgers.xml"
    if opening_path.exists():
        root = ET.fromstring(
            sanitize_xml(opening_path.read_text(encoding="utf-8"))
        )
        for element in root.findall(".//LEDGER"):
            name = norm(element.get("NAME") or element.findtext("NAME"))
            opening = money(element.findtext("OPENINGBALANCE"))
            if name and opening:
                captured_openings[name.lower()] = opening

    for master in store.masters("ledger"):
        payload = json.loads(master["payload"])
        opening = captured_openings.get(
            norm(master["name"]).lower(),
            money(_scalar(payload.get("OPENINGBALANCE"))),
        )
        if not opening:
            continue
        resolved = mapping.get(norm(master["name"]).lower())
        if not resolved:
            continue
        key, account = resolved
        postings.append(
            {
                "date": opening_date,
                "key": key,
                "account": account,
                "ledger": master["name"],
                "balance": -opening,
                "source": "opening",
                "guid": master["guid"],
            }
        )

    for voucher in store.vouchers():
        payload = json.loads(voucher["payload"])
        raw = _list(payload.get("ALLLEDGERENTRIES.LIST")) or _list(
            payload.get("LEDGERENTRIES.LIST")
        )
        for line in raw:
            if not isinstance(line, dict):
                continue
            ledger = norm(_scalar(line.get("LEDGERNAME")))
            if not ledger:
                continue
            resolved = mapping.get(ledger.lower())
            if not resolved:
                continue
            key, account = resolved
            # Tally amount: negative debit, positive credit.
            balance = -money(_scalar(line.get("AMOUNT")))
            postings.append(
                {
                    "date": date.fromisoformat(voucher["vdate"]),
                    "key": key,
                    "account": account,
                    "ledger": ledger,
                    "balance": balance,
                    "source": voucher["vtype"],
                    "guid": voucher["guid"],
                }
            )

    asset = canonical(
        acc_name(closing_stock_spec["asset_account"], defaults.abbr)
    )
    opening_pl = canonical(
        acc_name(closing_stock_spec["opening_pl_name"], defaults.abbr)
    )
    closing_pl = canonical(
        acc_name(closing_stock_spec["closing_pl_name"], defaults.abbr)
    )
    previous = Decimal("0.00")
    for posting_date, value in sorted(
        closing_stock_spec.get("balances", {}).items()
    ):
        closing = money(value)
        if previous:
            postings.extend(
                [
                    {
                        "date": date.fromisoformat(posting_date),
                        "key": ("account", opening_pl),
                        "account": opening_pl,
                        "ledger": "Opening Stock",
                        "balance": previous,
                        "source": "tally_internal_closing_stock",
                        "guid": f"closing-stock-{posting_date}",
                    },
                    {
                        "date": date.fromisoformat(posting_date),
                        "key": ("account", asset),
                        "account": asset,
                        "ledger": closing_stock_spec["asset_account"],
                        "balance": -previous,
                        "source": "tally_internal_closing_stock",
                        "guid": f"closing-stock-{posting_date}",
                    },
                ]
            )
        postings.extend(
            [
                {
                    "date": date.fromisoformat(posting_date),
                    "key": ("account", asset),
                    "account": asset,
                    "ledger": closing_stock_spec["asset_account"],
                    "balance": closing,
                    "source": "tally_internal_closing_stock",
                    "guid": f"closing-stock-{posting_date}",
                },
                {
                    "date": date.fromisoformat(posting_date),
                    "key": ("account", closing_pl),
                    "account": closing_pl,
                    "ledger": "Closing Stock (P&L)",
                    "balance": -closing,
                    "source": "tally_internal_closing_stock",
                    "guid": f"closing-stock-{posting_date}",
                },
            ]
        )
        previous = closing
    return postings


def trial_balance_from_postings(
    postings: list[dict[str, Any]],
    target_accounts: dict[str, Decimal],
    target_parties: dict[tuple[str, str], Decimal],
    as_of: date,
) -> tuple[dict, list[dict]]:
    source: dict[tuple, Decimal] = defaultdict(lambda: Decimal("0.00"))
    names: dict[tuple, set[str]] = defaultdict(set)
    for posting in postings:
        if posting["date"] <= as_of:
            source[posting["key"]] += money(posting["balance"])
            names[posting["key"]].add(posting["ledger"])

    rows = []
    exact = 0
    for key, source_balance in source.items():
        if key[0] == "party":
            target = target_parties.get((key[1], key[2]), Decimal("0.00"))
            target_name = f"{key[1]}:{key[2]}"
        else:
            target = target_accounts.get(key[1], Decimal("0.00"))
            target_name = key[1]
        source_balance = money(source_balance)
        target = money(target)
        difference = target - source_balance
        matched = abs(difference) <= TOLERANCE
        exact += int(matched)
        rows.append(
            {
                "target": target_name,
                "source_ledgers": " | ".join(sorted(names[key])),
                "source_balance_dr_minus_cr": f"{source_balance:.2f}",
                "target_balance_dr_minus_cr": f"{target:.2f}",
                "difference": f"{difference:.2f}",
                "matches": matched,
            }
        )
    rows.sort(key=lambda row: abs(money(row["difference"])), reverse=True)

    source_account_balance: dict[str, Decimal] = defaultdict(
        lambda: Decimal("0.00")
    )
    for posting in postings:
        if posting["date"] <= as_of:
            source_account_balance[posting["account"]] += money(
                posting["balance"]
            )
    source_debit = sum(
        (max(value, Decimal("0.00")) for value in source_account_balance.values()),
        Decimal("0.00"),
    )
    source_credit = sum(
        (max(-value, Decimal("0.00")) for value in source_account_balance.values()),
        Decimal("0.00"),
    )
    target_debit = sum(
        (max(value, Decimal("0.00")) for value in target_accounts.values()),
        Decimal("0.00"),
    )
    target_credit = sum(
        (max(-value, Decimal("0.00")) for value in target_accounts.values()),
        Decimal("0.00"),
    )
    summary = {
        "mapped_targets": len(source),
        "exact_mapped_balance_matches": exact,
        "mapped_balance_differences": len(source) - exact,
        "tally_trial_balance_debit": f"{source_debit:.2f}",
        "tally_trial_balance_credit": f"{source_credit:.2f}",
        "tally_internal_difference": f"{source_debit - source_credit:.2f}",
        "erpnext_trial_balance_debit": f"{target_debit:.2f}",
        "erpnext_trial_balance_credit": f"{target_credit:.2f}",
        "erpnext_internal_difference": f"{target_debit - target_credit:.2f}",
        "trial_balance_debit_difference": f"{target_debit - source_debit:.2f}",
        "trial_balance_credit_difference": f"{target_credit - source_credit:.2f}",
    }
    return summary, rows


def fiscal_summary_from_postings(
    label: str,
    start: date,
    end: date,
    postings: list[dict[str, Any]],
    erp_gl: list[dict[str, Any]],
    account_meta: dict[str, dict],
) -> dict:
    source_roots = defaultdict(lambda: Decimal("0.00"))
    target_roots = defaultdict(lambda: Decimal("0.00"))
    for posting in postings:
        if start <= posting["date"] <= end:
            root_type = account_meta.get(posting["account"], {}).get(
                "root_type", "Unknown"
            )
            source_roots[root_type] += money(posting["balance"])
    for entry in erp_gl:
        posting_date = date.fromisoformat(str(entry["posting_date"]))
        if start <= posting_date <= end:
            root_type = account_meta.get(entry["account"], {}).get(
                "root_type", "Unknown"
            )
            target_roots[root_type] += money(entry.get("debit")) - money(
                entry.get("credit")
            )
    source_income = -source_roots["Income"]
    source_expense = source_roots["Expense"]
    target_income = -target_roots["Income"]
    target_expense = target_roots["Expense"]
    return {
        "period": label,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "tally_income": f"{source_income:.2f}",
        "erpnext_income": f"{target_income:.2f}",
        "income_difference": f"{target_income - source_income:.2f}",
        "tally_expense": f"{source_expense:.2f}",
        "erpnext_expense": f"{target_expense:.2f}",
        "expense_difference": f"{target_expense - source_expense:.2f}",
        "tally_net_profit": f"{source_income - source_expense:.2f}",
        "erpnext_net_profit": f"{target_income - target_expense:.2f}",
        "net_profit_difference": (
            f"{(target_income - target_expense) - (source_income - source_expense):.2f}"
        ),
        "root_balance_differences": {
            root: f"{target_roots[root] - source_roots[root]:.2f}"
            for root in ("Asset", "Liability", "Equity", "Income", "Expense")
        },
    }


def balance_sheet_from_postings(
    as_of: date,
    postings: list[dict[str, Any]],
    erp_gl: list[dict[str, Any]],
    account_meta: dict[str, dict],
) -> dict:
    source = defaultdict(lambda: Decimal("0.00"))
    target = defaultdict(lambda: Decimal("0.00"))
    for posting in postings:
        if posting["date"] <= as_of:
            root = account_meta.get(posting["account"], {}).get(
                "root_type", "Unknown"
            )
            source[root] += money(posting["balance"])
    for entry in erp_gl:
        if date.fromisoformat(str(entry["posting_date"])) <= as_of:
            root = account_meta.get(entry["account"], {}).get(
                "root_type", "Unknown"
            )
            target[root] += money(entry.get("debit")) - money(
                entry.get("credit")
            )
    rows = {}
    for root in ("Asset", "Liability", "Equity", "Income", "Expense", "Unknown"):
        rows[root] = {
            "tally_dr_minus_cr": f"{source[root]:.2f}",
            "erpnext_dr_minus_cr": f"{target[root]:.2f}",
            "difference": f"{target[root] - source[root]:.2f}",
        }
    source_profit = -source["Income"] - source["Expense"]
    target_profit = -target["Income"] - target["Expense"]
    return {
        "as_of": as_of.isoformat(),
        "root_balances": rows,
        "tally_assets": f"{source['Asset']:.2f}",
        "erpnext_assets": f"{target['Asset']:.2f}",
        "asset_difference": f"{target['Asset'] - source['Asset']:.2f}",
        "tally_liability_plus_equity": (
            f"{-(source['Liability'] + source['Equity']):.2f}"
        ),
        "erpnext_liability_plus_equity": (
            f"{-(target['Liability'] + target['Equity']):.2f}"
        ),
        "liability_plus_equity_difference": (
            f"{-(target['Liability'] + target['Equity']) + source['Liability'] + source['Equity']:.2f}"
        ),
        "tally_unclosed_net_profit": f"{source_profit:.2f}",
        "erpnext_unclosed_net_profit": f"{target_profit:.2f}",
        "net_profit_difference": f"{target_profit - source_profit:.2f}",
        "tally_all_roots_difference": f"{sum(source.values(), Decimal('0.00')):.2f}",
        "erpnext_all_roots_difference": f"{sum(target.values(), Decimal('0.00')):.2f}",
    }


def voucher_verification(
    store: Staging, gl: list[dict[str, Any]]
) -> tuple[dict, list[dict]]:
    by_voucher: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "rows": 0,
            "debit": Decimal("0.00"),
            "credit": Decimal("0.00"),
        }
    )
    for entry in gl:
        bucket = by_voucher[entry["voucher_no"]]
        bucket["rows"] += 1
        bucket["debit"] += money(entry.get("debit"))
        bucket["credit"] += money(entry.get("credit"))

    differences = []
    missing = 0
    unbalanced = 0
    exact = 0
    source_total = Decimal("0.00")
    target_total = Decimal("0.00")
    for row in store.vouchers():
        source = money(row["amount"])
        source_total += source
        bucket = by_voucher.get(row["erp_name"])
        if not bucket:
            missing += 1
            differences.append(
                {
                    "guid": row["guid"],
                    "type": row["vtype"],
                    "number": row["vnumber"],
                    "date": row["vdate"],
                    "erp_document": row["erp_name"],
                    "source_debit": f"{source:.2f}",
                    "target_debit": "0.00",
                    "target_credit": "0.00",
                    "difference": f"{-source:.2f}",
                    "reason": "missing_target_gl",
                }
            )
            continue
        debit = money(bucket["debit"])
        credit = money(bucket["credit"])
        target_total += debit
        if abs(debit - credit) > TOLERANCE:
            unbalanced += 1
        difference = debit - source
        if abs(difference) <= TOLERANCE:
            exact += 1
        else:
            differences.append(
                {
                    "guid": row["guid"],
                    "type": row["vtype"],
                    "number": row["vnumber"],
                    "date": row["vdate"],
                    "erp_document": row["erp_name"],
                    "source_debit": f"{source:.2f}",
                    "target_debit": f"{debit:.2f}",
                    "target_credit": f"{credit:.2f}",
                    "difference": f"{difference:.2f}",
                    "reason": "voucher_value_difference",
                }
            )
    summary = {
        "source_vouchers": len(store.vouchers()),
        "exact_amount_matches": exact,
        "differences": len(differences),
        "missing_target_gl": missing,
        "unbalanced_target_vouchers": unbalanced,
        "source_total_debit": f"{source_total:.2f}",
        "target_total_debit_for_source_vouchers": f"{target_total:.2f}",
        "difference": f"{target_total - source_total:.2f}",
    }
    return summary, differences


def _target_balances(
    gl: list[dict[str, Any]], as_of: date
) -> tuple[dict[str, Decimal], dict[tuple[str, str], Decimal]]:
    accounts: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    parties: dict[tuple[str, str], Decimal] = defaultdict(
        lambda: Decimal("0.00")
    )
    cutoff = as_of.isoformat()
    for entry in gl:
        if str(entry["posting_date"]) > cutoff:
            continue
        value = money(entry.get("debit")) - money(entry.get("credit"))
        accounts[entry["account"]] += value
        if entry.get("party_type") and entry.get("party"):
            parties[(entry["party_type"], entry["party"])] += value
    return accounts, parties


def trial_balance_verification(
    ledger_rows: list[dict[str, Any]],
    store: Staging,
    target_accounts: dict[str, Decimal],
    target_parties: dict[tuple[str, str], Decimal],
) -> tuple[dict, list[dict]]:
    master_by_name = {
        norm(row["name"]).lower(): row for row in store.masters("ledger")
    }
    mapped_source: dict[tuple, dict] = {}
    unmapped = []
    source_debit = Decimal("0.00")
    source_credit = Decimal("0.00")
    for ledger in ledger_rows:
        balance = money(ledger["balance"])
        source_debit += max(balance, Decimal("0.00"))
        source_credit += max(-balance, Decimal("0.00"))
        master = master_by_name.get(ledger["name"].lower())
        if not master or not master["erp_name"]:
            if abs(balance) > TOLERANCE:
                unmapped.append(
                    {
                        "ledger": ledger["name"],
                        "parent": ledger["parent"],
                        "balance": f"{balance:.2f}",
                    }
                )
            continue
        if master["erp_doctype"] in ("Customer", "Supplier"):
            key = ("party", master["erp_doctype"], master["erp_name"])
        else:
            key = ("account", master["erp_name"])
        bucket = mapped_source.setdefault(
            key,
            {
                "source_ledgers": [],
                "source_balance": Decimal("0.00"),
                "root_types": set(),
            },
        )
        bucket["source_ledgers"].append(ledger["name"])
        bucket["source_balance"] += balance
        bucket["root_types"].add(ledger["root_type"])

    rows = []
    exact = 0
    for key, source in mapped_source.items():
        if key[0] == "party":
            target = target_parties.get((key[1], key[2]), Decimal("0.00"))
            target_name = f"{key[1]}:{key[2]}"
        else:
            target = target_accounts.get(key[1], Decimal("0.00"))
            target_name = key[1]
        source_balance = money(source["source_balance"])
        target = money(target)
        difference = target - source_balance
        if abs(difference) <= TOLERANCE:
            exact += 1
        rows.append(
            {
                "target": target_name,
                "source_ledgers": " | ".join(source["source_ledgers"]),
                "root_types": " | ".join(sorted(source["root_types"])),
                "source_balance_dr_minus_cr": f"{source_balance:.2f}",
                "target_balance_dr_minus_cr": f"{target:.2f}",
                "difference": f"{difference:.2f}",
                "matches": abs(difference) <= TOLERANCE,
            }
        )

    rows.sort(key=lambda row: abs(money(row["difference"])), reverse=True)
    target_debit = sum(
        (max(money(value), Decimal("0.00")) for value in target_accounts.values()),
        Decimal("0.00"),
    )
    target_credit = sum(
        (max(-money(value), Decimal("0.00")) for value in target_accounts.values()),
        Decimal("0.00"),
    )
    summary = {
        "tally_ledgers": len(ledger_rows),
        "mapped_targets": len(mapped_source),
        "exact_mapped_balance_matches": exact,
        "mapped_balance_differences": len(mapped_source) - exact,
        "unmapped_nonzero_ledgers": len(unmapped),
        "tally_trial_balance_debit": f"{source_debit:.2f}",
        "tally_trial_balance_credit": f"{source_credit:.2f}",
        "tally_internal_difference": f"{source_debit - source_credit:.2f}",
        "erpnext_trial_balance_debit": f"{target_debit:.2f}",
        "erpnext_trial_balance_credit": f"{target_credit:.2f}",
        "erpnext_internal_difference": f"{target_debit - target_credit:.2f}",
        "trial_balance_debit_difference": f"{target_debit - source_debit:.2f}",
        "trial_balance_credit_difference": f"{target_credit - source_credit:.2f}",
        "unmapped": unmapped,
    }
    return summary, rows


def pnl_verification(
    label: str,
    start: date,
    end: date,
    tally_rows: list[dict[str, Any]],
    erp_gl: list[dict[str, Any]],
    account_meta: dict[str, dict],
    closing_stock: dict[str, float],
) -> dict:
    tally_income = Decimal("0.00")
    tally_expense = Decimal("0.00")
    for row in tally_rows:
        closing = money(row["closing_credit_positive"])
        if row["root_type"] == "Income":
            tally_income += closing
        elif row["root_type"] == "Expense":
            tally_expense += -closing

    erp_income = Decimal("0.00")
    erp_expense = Decimal("0.00")
    for entry in erp_gl:
        posting = date.fromisoformat(str(entry["posting_date"]))
        if not start <= posting <= end:
            continue
        meta = account_meta.get(entry["account"], {})
        movement = money(entry.get("credit")) - money(entry.get("debit"))
        if meta.get("root_type") == "Income":
            erp_income += movement
        elif meta.get("root_type") == "Expense":
            erp_expense += -movement

    stock_close = money(closing_stock.get(end.isoformat(), 0))
    previous_dates = sorted(
        d for d in closing_stock if d < end.isoformat()
    )
    stock_open = (
        money(closing_stock[previous_dates[-1]]) if previous_dates else Decimal("0.00")
    )
    # Stock adjustments are already included on the ERP side.  They are not
    # ordinary Income/Expense ledgers in Tally, so add them to source P&L here.
    tally_income_with_stock = tally_income + stock_close
    tally_expense_with_stock = tally_expense + stock_open
    return {
        "period": label,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "tally_income_including_stock": f"{tally_income_with_stock:.2f}",
        "erpnext_income": f"{erp_income:.2f}",
        "income_difference": f"{erp_income - tally_income_with_stock:.2f}",
        "tally_expense_including_stock": f"{tally_expense_with_stock:.2f}",
        "erpnext_expense": f"{erp_expense:.2f}",
        "expense_difference": f"{erp_expense - tally_expense_with_stock:.2f}",
        "tally_net_profit": f"{tally_income_with_stock - tally_expense_with_stock:.2f}",
        "erpnext_net_profit": f"{erp_income - erp_expense:.2f}",
        "net_profit_difference": (
            f"{(erp_income - erp_expense) - (tally_income_with_stock - tally_expense_with_stock):.2f}"
        ),
        "stock_open": f"{stock_open:.2f}",
        "stock_close": f"{stock_close:.2f}",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    cfg = get_config()
    company = cfg.erpnext["company"]
    reports = cfg.staging_db.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    store = Staging()
    erp = ERPNextClient()
    defaults = fetch_company_defaults(erp)

    latest_text = store.conn.execute(
        "SELECT MAX(vdate) FROM voucher"
    ).fetchone()[0]
    latest = date.fromisoformat(latest_text)
    books_from = str(cfg.tally.get("from_date", "20220101"))
    dates = [
        date(2022, 3, 31),
        date(2023, 3, 31),
        date(2024, 3, 31),
        date(2025, 3, 31),
        date(2026, 3, 31),
        latest,
    ]

    print("Fetching migration-linked ERPNext documents and GL entries ...")
    erp_data = fetch_erp_data(erp, company)
    account_meta = {row["name"]: row for row in erp_data["accounts"]}

    print("Comparing every source voucher with its target GL value ...")
    voucher_summary, voucher_differences = voucher_verification(
        store, erp_data["gl"]
    )
    print("Reconstructing Tally ledger postings from captured source XML ...")
    postings = source_postings(
        store,
        defaults,
        books_from,
        cfg.yaml["closing_stock"],
        account_meta,
    )

    snapshots = {}
    balance_sheets = []
    current_trial_rows = []
    for as_of in dates:
        print(f"Comparing Trial Balance at {as_of} ...")
        target_accounts, target_parties = _target_balances(
            erp_data["gl"], as_of
        )
        summary, detail = trial_balance_from_postings(
            postings, target_accounts, target_parties, as_of
        )
        snapshots[as_of.isoformat()] = summary
        balance_sheets.append(
            balance_sheet_from_postings(
                as_of, postings, erp_data["gl"], account_meta
            )
        )
        if as_of == latest:
            current_trial_rows = detail

    pnl = []
    for label, start, end in fiscal_periods(latest):
        print(f"Comparing P&L for {label} ...")
        pnl.append(
            fiscal_summary_from_postings(
                label,
                start,
                end,
                postings,
                erp_data["gl"],
                account_meta,
            )
        )

    current_accounts, current_parties = _target_balances(
        erp_data["gl"], latest
    )
    source_party = {"Customer": Decimal("0.00"), "Supplier": Decimal("0.00")}
    for posting in postings:
        if (
            posting["date"] <= latest
            and posting["key"][0] == "party"
            and posting["key"][1] in source_party
        ):
            source_party[posting["key"][1]] += money(posting["balance"])
    target_party = {
        party_type: sum(
            (
                balance
                for (kind, _), balance in current_parties.items()
                if kind == party_type
            ),
            Decimal("0.00"),
        )
        for party_type in source_party
    }
    party_controls = {
        party_type: {
            "tally_dr_minus_cr": f"{source_party[party_type]:.2f}",
            "erpnext_dr_minus_cr": f"{target_party[party_type]:.2f}",
            "difference": f"{target_party[party_type] - source_party[party_type]:.2f}",
        }
        for party_type in source_party
    }

    documents = {
        doctype: len(rows)
        for doctype, rows in erp_data["documents"].items()
    }
    checks = {
        "all_source_vouchers_have_exact_target_gl_value": (
            voucher_summary["differences"] == 0
            and voucher_summary["missing_target_gl"] == 0
            and voucher_summary["unbalanced_target_vouchers"] == 0
        ),
        "all_trial_balance_snapshots_match": all(
            summary["mapped_balance_differences"] == 0
            and money(summary["trial_balance_debit_difference"]) == 0
            and money(summary["trial_balance_credit_difference"]) == 0
            for summary in snapshots.values()
        ),
        "all_fiscal_profit_and_loss_totals_match": all(
            money(row["income_difference"]) == 0
            and money(row["expense_difference"]) == 0
            and money(row["net_profit_difference"]) == 0
            for row in pnl
        ),
        "all_balance_sheet_snapshots_match": all(
            money(row["asset_difference"]) == 0
            and money(row["liability_plus_equity_difference"]) == 0
            and money(row["net_profit_difference"]) == 0
            and money(row["tally_all_roots_difference"]) == 0
            and money(row["erpnext_all_roots_difference"]) == 0
            for row in balance_sheets
        ),
        "customer_and_supplier_controls_match": all(
            money(row["difference"]) == 0 for row in party_controls.values()
        ),
    }
    replay_status = "PASS" if all(checks.values()) else "FAIL"
    status = f"GL_REPLAY_{replay_status}"
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "verification_status": status,
        "native_report_equivalence": (
            "NOT TESTED: use diagnose_native_report_gap.py and native Tally "
            "report exports"
        ),
        "checks": checks,
        "scope": {
            "tally_company": cfg.tally["company"],
            "erpnext_company": company,
            "latest_source_voucher_date": latest.isoformat(),
            "comparison_basis": (
                "captured raw Tally XML voucher/opening postings plus audited "
                "Tally closing-stock values vs migration-linked ERPNext REST/GL"
            ),
        },
        "documents": documents,
        "source_posting_lines": len(postings),
        "voucher_values": voucher_summary,
        "trial_balance_snapshots": snapshots,
        "profit_and_loss": pnl,
        "balance_sheet_snapshots": balance_sheets,
        "party_controls": party_controls,
        "notes": [
            "Dr-minus-Cr is positive for debit balances and negative for credit balances.",
            "Historical balances are reconstructed from the exact Tally XML lines retained in staging; no ERPNext values are used to calculate the source side.",
            "A GL replay match does not prove Tally native Balance Sheet or Trial Balance equivalence; Tally applies special Profit & Loss A/c and optional-voucher rules.",
            "ERPNext party ledgers are compared through Customer/Supplier dimensions on control accounts.",
            "Year-end stock adjustments are included from the audited closing-stock values in config.yaml.",
            "Only ERPNext documents carrying tally_guid are included.",
        ],
    }
    dated = date.today().isoformat()
    report_path = reports / f"financial_verification_{dated}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_csv(reports / f"voucher_value_differences_{dated}.csv", voucher_differences)
    write_csv(reports / f"trial_balance_current_{dated}.csv", current_trial_rows)
    write_csv(reports / f"profit_and_loss_by_fy_{dated}.csv", pnl)
    summary_path = reports / f"financial_verification_summary_{dated}.md"
    summary_lines = [
        "# Tally to ERPNext Financial Verification",
        "",
        f"**Status: {status} (not a native-report equivalence result)**",
        "",
        f"- Tally company: {cfg.tally['company']}",
        f"- ERPNext company: {company}",
        f"- Latest source voucher: {latest.isoformat()}",
        f"- Source vouchers checked: {voucher_summary['source_vouchers']}",
        f"- Exact voucher-value matches: {voucher_summary['exact_amount_matches']}",
        f"- Source/target voucher debit total: INR {voucher_summary['source_total_debit']}",
        f"- Current mapped ledger/party rows checked: {len(current_trial_rows)}",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    summary_lines.extend(
        f"| {name.replace('_', ' ').title()} | {'PASS' if passed else 'FAIL'} |"
        for name, passed in checks.items()
    )
    summary_lines.extend(
        [
            "",
            "Detailed evidence:",
            "",
            f"- `{report_path.name}`",
            f"- `trial_balance_current_{dated}.csv`",
            f"- `profit_and_loss_by_fy_{dated}.csv`",
            f"- `voucher_value_differences_{dated}.csv`",
            "",
        ]
    )
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    print("\nVOUCHER VALUES")
    print(json.dumps(voucher_summary, indent=2))
    print("\nTRIAL BALANCE SNAPSHOTS")
    for snapshot, summary in snapshots.items():
        print(
            f"  {snapshot}: mapped differences="
            f"{summary['mapped_balance_differences']}, "
            f"Tally Dr/Cr={summary['tally_trial_balance_debit']}/"
            f"{summary['tally_trial_balance_credit']}, "
            f"ERP Dr/Cr={summary['erpnext_trial_balance_debit']}/"
            f"{summary['erpnext_trial_balance_credit']}"
        )
    print("\nPROFIT & LOSS")
    for row in pnl:
        print(
            f"  {row['period']}: Tally={row['tally_net_profit']} "
            f"ERPNext={row['erpnext_net_profit']} "
            f"diff={row['net_profit_difference']}"
        )
    print("\nPARTY CONTROLS")
    print(json.dumps(party_controls, indent=2))
    print(f"\nREPLAY STATUS {status}")
    print(f"\nREPORT {report_path}")
    print(f"SUMMARY {summary_path}")
    store.close()


if __name__ == "__main__":
    main()
