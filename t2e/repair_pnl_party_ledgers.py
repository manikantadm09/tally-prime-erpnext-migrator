"""Restore Income/Expense Tally ledgers that were loaded as Customer/Supplier.

Bill-wise ON an Indirect Expense ledger made the classifier create a party, so
the cost sat on Debtors and never hit Desk P&L. This repair:

1. Creates the missing named GL leaves
2. Rewrites staging so they are Accounts, not parties
3. Posts one same-FY Journal Entry per ledger moving Debtors/Creditors+party
   onto the expense/income account
4. Optionally reopens and recreates migration Period Closing Vouchers

Idempotent via ``tally_guid`` key ``pnl-party-reclass-<norm>-<fy>``.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .load_masters import MasterLoader, fetch_company_defaults
from .load_period_closing import PeriodClosingLoader
from .load_vouchers import _name_of
from .mapping import CompanyDefaults, GroupTree, acc_name, _norm
from .staging import Staging

PENNY = Decimal("0.01")
KEY_PREFIX = "pnl-party-reclass-"
_PNL_ROOT_TYPES = {"Income", "Expense"}


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def fiscal_year(iso_date: str) -> str:
    year, month, _ = iso_date.split("-")
    start = int(year) if int(month) >= 4 else int(year) - 1
    return f"{start}-{start + 1}"


def fy_dates(fiscal_year_name: str) -> tuple[str, str]:
    start_year, end_year = map(int, fiscal_year_name.split("-", 1))
    return f"{start_year}-04-01", f"{end_year}-03-31"


def pnl_party_candidates(store: Staging, tree: GroupTree) -> list[dict]:
    """Staged ledgers loaded as a party that Tally keeps as named GL leaves.

    Covers Income/Expense (P&L) and non-party-group assets/liabilities such as
    deposits. Ledgers under Sundry/Advances party groups are left as parties.
    """
    out = []
    party_names = {
        _norm(row["ledger_name"]) for row in store.party_roles()
    }
    for row in store.masters("ledger"):
        group = row["parent"] or ""
        if tree.party_kind(group):
            continue
        name = _norm(row["name"])
        is_party = row["erp_doctype"] in ("Customer", "Supplier") or name in party_names
        if not is_party:
            continue
        out.append({
            "guid": row["guid"],
            "name": row["name"],
            "norm": name,
            "parent": group,
            "root_type": tree.root_type(group),
            "erp_doctype": row["erp_doctype"],
        })
    return out


def _party_gl(erp: ERPNextClient, company: str, party: str) -> list[dict]:
    rows = erp.get_list(
        "GL Entry",
        fields=["name", "posting_date", "account", "party_type", "party",
                "debit", "credit", "voucher_type", "voucher_no"],
        filters=[["company", "=", company], ["party", "=", party],
                 ["is_cancelled", "=", 0]],
        limit=0,
    )
    return [
        row for row in rows
        if row.get("voucher_type") != "Period Closing Voucher"
    ]


def _net_by_fy(rows: list[dict]) -> dict[str, dict]:
    buckets: dict[str, dict] = {}
    for row in rows:
        fy = fiscal_year(str(row["posting_date"])[:10])
        bucket = buckets.setdefault(fy, {
            "debit": Decimal("0"), "credit": Decimal("0"),
            "last_date": str(row["posting_date"])[:10],
            "party_type": row.get("party_type") or "Customer",
            "control": row.get("account") or "",
            "rows": 0,
        })
        bucket["debit"] += _money(row.get("debit"))
        bucket["credit"] += _money(row.get("credit"))
        bucket["rows"] += 1
        if str(row["posting_date"])[:10] > bucket["last_date"]:
            bucket["last_date"] = str(row["posting_date"])[:10]
        if row.get("party_type"):
            bucket["party_type"] = row["party_type"]
        if row.get("account"):
            bucket["control"] = row["account"]
    return buckets


class PnlPartyLedgerRepair:
    def __init__(self, erp: ERPNextClient, store: Staging,
                 defaults: CompanyDefaults):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.cfg = get_config()
        self.field = self.cfg.idempotency_field
        self.tree = GroupTree(store)

    def plan(self) -> dict[str, Any]:
        candidates = pnl_party_candidates(self.store, self.tree)
        items = []
        for cand in candidates:
            account = acc_name(cand["name"], self.d.abbr)
            gl = _party_gl(self.erp, self.d.name, cand["name"])
            years = []
            for fy, bucket in sorted(_net_by_fy(gl).items()):
                net = (bucket["debit"] - bucket["credit"]).quantize(PENNY)
                if not net:
                    continue
                start, end = fy_dates(fy)
                posting_date = end if end <= bucket["last_date"] else bucket["last_date"]
                # Closed FYs post on 31 Mar so PCV picks them up; the open FY
                # posts on the last source date (do not invent a future 31 Mar).
                if fy in self.cfg.yaml["period_closing"]["fiscal_years"]:
                    posting_date = end
                key = f"{KEY_PREFIX}{_norm(cand['name']).casefold()}-{fy}"
                existing = self.erp.find_by_field(
                    "Journal Entry", self.field, key, exclude_cancelled=True)
                years.append({
                    "fiscal_year": fy,
                    "posting_date": posting_date,
                    "debit": f"{bucket['debit']:.2f}",
                    "credit": f"{bucket['credit']:.2f}",
                    "net": f"{net:.2f}",
                    "party_type": bucket["party_type"],
                    "control": bucket["control"],
                    "rows": bucket["rows"],
                    "key": key,
                    "existing": existing,
                    "action": "skip" if existing else "create",
                })
            items.append({
                **cand,
                "account": account,
                "account_exists": self.erp.exists("Account", account),
                "years": years,
            })
        reopen_from = None
        for item in items:
            for year in item["years"]:
                if year["action"] != "create":
                    continue
                if year["fiscal_year"] in self.cfg.yaml["period_closing"]["fiscal_years"]:
                    _start, end = fy_dates(year["fiscal_year"])
                    if reopen_from is None or end < reopen_from:
                        reopen_from = end
        return {
            "company": self.d.name,
            "candidates": items,
            "reopen_period_end_from": reopen_from,
            "create": sum(
                1 for item in items for year in item["years"]
                if year["action"] == "create"),
            "skip": sum(
                1 for item in items for year in item["years"]
                if year["action"] == "skip"),
        }

    def _ensure_accounts(self, candidates: list[dict]) -> int:
        loader = MasterLoader(self.erp, self.store, self.d)
        loader.load_account_groups()
        names = {cand["name"] for cand in candidates}
        created = loader.load_ledger_accounts(names=names)
        if not self.erp.dry_run:
            self.store.delete_party_roles_for(names)
            self.store.conn.commit()
        return created

    def _build_je(self, item: dict, year: dict) -> dict:
        net = _money(year["net"])
        expense = item["account"]
        control = year["control"] or (
            self.d.receivable if year["party_type"] == "Customer"
            else self.d.payable)
        cc = self.d.cost_center
        if net > 0:
            # Expense was posted as a debit on the party control.
            accounts = [
                {"account": expense, "cost_center": cc,
                 "debit_in_account_currency": float(net)},
                {"account": control, "party_type": year["party_type"],
                 "party": item["name"],
                 "credit_in_account_currency": float(net)},
            ]
        else:
            amount = abs(net)
            accounts = [
                {"account": control, "party_type": year["party_type"],
                 "party": item["name"],
                 "debit_in_account_currency": float(amount)},
                {"account": expense, "cost_center": cc,
                 "credit_in_account_currency": float(amount)},
            ]
        return {
            "company": self.d.name,
            "posting_date": year["posting_date"],
            "voucher_type": "Journal Entry",
            "title": f"P&L party reclass {item['name']} {year['fiscal_year']}",
            "user_remark": (
                f"Reclass {item['name']} from {year['party_type']} control "
                f"to {item['root_type']} account (Tally parent {item['parent']})"),
            "accounts": accounts,
            self.field: year["key"],
        }

    def _reopen_pcvs(self, from_end: str) -> list[str]:
        expected = {
            f"period-closing-{year}"
            for year in self.cfg.yaml["period_closing"]["fiscal_years"]
        }
        rows = self.erp.get_list(
            "Period Closing Voucher",
            fields=["name", "period_end_date", self.field],
            filters=[["company", "=", self.d.name], ["docstatus", "=", 1],
                     ["period_end_date", ">=", from_end]],
            limit=0)
        cancelled = []
        rows.sort(key=lambda row: row.get("period_end_date") or "", reverse=True)
        for row in rows:
            key = row.get(self.field)
            if key not in expected:
                raise ERPNextError(
                    f"refusing non-migration closing {row['name']} ({key!r})")
            if not self.erp.dry_run:
                self.erp.cancel("Period Closing Voucher", row["name"])
            cancelled.append(row["name"])
        return cancelled

    def run(self, *, reopen_period_closings: bool = False) -> dict[str, Any]:
        plan = self.plan()
        report: dict[str, Any] = {
            "plan_only": self.erp.dry_run,
            "company": plan["company"],
            "reopen_period_end_from": plan["reopen_period_end_from"],
            "accounts_created": 0,
            "cancelled_pcvs": [],
            "journals": [],
            "period_closing": None,
        }
        if self.erp.dry_run:
            report["candidates"] = plan["candidates"]
            report["create"] = plan["create"]
            report["skip"] = plan["skip"]
            return report

        if plan["reopen_period_end_from"]:
            if not reopen_period_closings:
                raise ERPNextError(
                    "closed fiscal years need --reopen-period-closings "
                    f"(from {plan['reopen_period_end_from']})")
            report["cancelled_pcvs"] = self._reopen_pcvs(
                plan["reopen_period_end_from"])

        report["accounts_created"] = self._ensure_accounts(plan["candidates"])
        for item in plan["candidates"]:
            if not self.erp.exists("Account", item["account"]):
                raise ERPNextError(f"account missing after load: {item['account']}")
            for year in item["years"]:
                if year["action"] == "skip":
                    report["journals"].append({
                        "ledger": item["name"], "fiscal_year": year["fiscal_year"],
                        "status": "skipped", "name": year["existing"],
                    })
                    continue
                je = self._build_je(item, year)
                try:
                    res = self.erp.insert_and_submit("Journal Entry", je)
                    report["journals"].append({
                        "ledger": item["name"], "fiscal_year": year["fiscal_year"],
                        "status": "created", "name": _name_of(res),
                        "net": year["net"], "posting_date": year["posting_date"],
                    })
                except ERPNextError as exc:
                    report["journals"].append({
                        "ledger": item["name"], "fiscal_year": year["fiscal_year"],
                        "status": "error", "error": str(exc)[:500],
                    })
                    raise

        if reopen_period_closings:
            stats, results = PeriodClosingLoader(self.erp, self.d).run()
            report["period_closing"] = {"stats": stats, "results": results}
        return report
