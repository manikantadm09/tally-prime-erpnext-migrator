"""One Current Assets control for Advances to Vendors — parties stay Suppliers.

Tally prints the Advances to Vendors *group* under Current Assets. ERPNext can
show that same total on a single leaf ``Advances to Vendors - <abbr>`` without
creating CONNECTING DOTS / Sri Lakshmi as accounts.

Invoices stay on Creditors. Only Creditors+Supplier debit/credit nets for
ledgers under Tally's Advances to Vendors group are reclassed:

    Dr  Advances to Vendors - SDL   party=Sri Lakshmi …
    Cr  Creditors - SDL             party=Sri Lakshmi …

31 Mar (closed FY) and 15 Aug (open FY 2026-27 delta) are separate journals.
FY 2026-27 is not period-closed. Never FIFO. Never named vendor asset leaves.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .load_period_closing import PeriodClosingLoader
from .load_vouchers import _name_of
from .mapping import CompanyDefaults, GroupTree, acc_name, _fold, _norm
from .staging import Staging

PENNY = Decimal("0.01")
YEAR_ENDS = ("2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31")
AUG = "2026-08-15"
KEY_PREFIX = "vendor-advance-control-"
MAR = "2026-03-31"
ADVANCE_GROUP = "Advances to Vendors"


def _control_key(date: str) -> str:
    return f"{KEY_PREFIX}{date}"


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def advance_vendor_parties(store: Staging, tree: GroupTree) -> list[str]:
    """Tally ledgers under Advances to Vendors — keep as Supplier names."""
    names = []
    seen = set()
    for row in store.masters("ledger"):
        group = row["parent"] or ""
        chain = [group] + tree.ancestry(group)
        if not any(_fold(g) == _fold(ADVANCE_GROUP) for g in chain):
            continue
        party = _norm(row["erp_name"] or row["name"])
        key = _fold(party)
        if key in seen:
            continue
        seen.add(key)
        names.append(party)
    return sorted(names, key=str.casefold)


class VendorAdvanceControlRepair:
    def __init__(self, erp: ERPNextClient, store: Staging,
                 defaults: CompanyDefaults):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.cfg = get_config()
        self.field = self.cfg.idempotency_field
        self.tree = GroupTree(store)
        self.control = acc_name(ADVANCE_GROUP, defaults.abbr)
        self.creditors = defaults.payable

    def _party_nets(self, as_of: str, parties: list[str]) -> dict[str, Decimal]:
        if not parties:
            return {}
        canonical = {_fold(name): name for name in parties}
        rows = self.erp.get_list(
            "GL Entry",
            fields=["party", "debit", "credit", "voucher_type", "voucher_no",
                    "posting_date"],
            filters=[["company", "=", self.d.name],
                     ["account", "=", self.creditors],
                     ["party_type", "=", "Supplier"],
                     ["party", "in", parties],
                     ["is_cancelled", "=", 0],
                     ["posting_date", "<=", as_of]],
            limit=0,
        )
        existing = self.erp.get_list(
            "Journal Entry",
            fields=["name", self.field],
            filters=[[self.field, "like", "vendor-advance-control-%"],
                     ["docstatus", "!=", 2]],
            limit=0,
        )
        skip_vouchers = {row["name"] for row in existing}
        nets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for row in rows:
            if row.get("voucher_type") == "Period Closing Voucher":
                continue
            if row.get("voucher_no") in skip_vouchers:
                continue
            party = canonical.get(_fold(row.get("party") or ""))
            if not party:
                continue
            nets[party] += _money(row.get("debit")) - _money(row.get("credit"))
        return dict(nets)

    def _ensure_control_account(self) -> str:
        """Single leaf Advances to Vendors - abbr under Current Assets.

        The CoA already has this name as an empty *group*. Convert it to a leaf
        so the client sees one CA line, not 18 vendor-named accounts. Payable
        account_type lets Supplier sit on every GL line.
        """
        parent = acc_name("Current Assets", self.d.abbr)
        rows = self.erp.get_list(
            "Account",
            fields=["name", "is_group", "account_type", "root_type",
                    "parent_account"],
            filters=[["name", "=", self.control]],
            limit=1,
        )
        if rows:
            row = rows[0]
            children = self.erp.get_list(
                "Account", fields=["name"],
                filters=[["parent_account", "=", self.control]], limit=0)
            if children:
                raise ERPNextError(
                    f"{self.control} has child accounts { [c['name'] for c in children] }; "
                    "refusing to post or convert (would recreate named leaves)")
            values: dict[str, Any] = {}
            if row.get("is_group"):
                values["is_group"] = 0
            if row.get("account_type") != "Payable":
                values["account_type"] = "Payable"
            if values and not self.erp.dry_run:
                self.erp.update("Account", self.control, values)
            return self.control
        if not self.erp.dry_run:
            self.erp.insert("Account", {
                "account_name": ADVANCE_GROUP,
                "parent_account": parent,
                "company": self.d.name,
                "is_group": 0,
                "root_type": "Asset",
                "account_type": "Payable",
            })
        return self.control

    def _line(self, account: str, party: str, debit: Decimal,
              credit: Decimal) -> dict:
        row = {
            "account": account,
            "party_type": "Supplier",
            "party": party,
        }
        if debit > 0:
            row["debit_in_account_currency"] = float(debit)
        else:
            row["credit_in_account_currency"] = float(credit)
        return row

    def _build_je(self, date: str, key: str,
                  nets: dict[str, Decimal], title: str) -> dict | None:
        accounts = []
        for party in sorted(nets, key=str.casefold):
            net = nets[party]
            if not net:
                continue
            if net > 0:
                accounts.append(self._line(self.control, party, net, Decimal("0")))
                accounts.append(self._line(self.creditors, party, Decimal("0"), net))
            else:
                amt = abs(net)
                accounts.append(self._line(self.creditors, party, amt, Decimal("0")))
                accounts.append(self._line(self.control, party, Decimal("0"), amt))
        if not accounts:
            return None
        return {
            "company": self.d.name,
            "posting_date": date,
            "voucher_type": "Journal Entry",
            "title": title,
            "user_remark": (
                "Reclass Advances to Vendors debit from Creditors onto one "
                "Current Assets control. Suppliers unchanged; invoices stay "
                "on Creditors. Not named vendor asset leaves."),
            "accounts": accounts,
            self.field: key,
        }

    def plan(self, *, rebuild_year_ends: bool = False) -> dict[str, Any]:
        parties = advance_vendor_parties(self.store, self.tree)
        snapshots = {date: self._party_nets(date, parties) for date in YEAR_ENDS}
        snapshots[AUG] = self._party_nets(AUG, parties)
        windows = []
        previous: dict[str, Decimal] = {}
        first_needed = None
        for date in (*YEAR_ENDS, AUG):
            nets = snapshots[date]
            delta = {
                party: _money(nets.get(party, 0)) - _money(previous.get(party, 0))
                for party in set(nets) | set(previous)
            }
            delta = {k: v for k, v in delta.items() if v}
            total = sum(delta.values(), Decimal("0"))
            key = _control_key(date)
            existing = self.erp.find_by_field(
                "Journal Entry", self.field, key, exclude_cancelled=True)
            action = "create"
            if existing and not rebuild_year_ends:
                action = "skip"
            elif rebuild_year_ends and existing:
                action = "replace"
            if action != "skip" and first_needed is None and date in YEAR_ENDS:
                first_needed = date
            windows.append({
                "date": date, "key": key, "total": f"{total:.2f}",
                "closing": f"{sum(nets.values(), Decimal('0')):.2f}",
                "existing": existing, "action": action,
                "by_party": {k: f"{v:.2f}" for k, v in sorted(delta.items())},
            })
            previous = nets
        return {
            "company": self.d.name,
            "control_account": self.control,
            "creditors": self.creditors,
            "parties": parties,
            "rebuild_year_ends": rebuild_year_ends,
            "windows": windows,
            "reopen_period_end_from": first_needed,
        }

    def _cancel_control_jes(self) -> list[str]:
        rows = self.erp.get_list(
            "Journal Entry",
            fields=["name", self.field],
            filters=[[self.field, "like", f"{KEY_PREFIX}%"],
                     ["docstatus", "=", 1]],
            limit=0)
        cancelled = []
        for row in rows:
            if not self.erp.dry_run:
                self.erp.cancel("Journal Entry", row["name"])
            cancelled.append(row["name"])
        return cancelled

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
            if key == "period-closing-2026-2027":
                raise ERPNextError(
                    f"refusing to reopen {row['name']}; FY 2026-27 stays open")
            if not self.erp.dry_run:
                self.erp.cancel("Period Closing Voucher", row["name"])
            cancelled.append(row["name"])
        return cancelled

    def run(self, *, reopen_period_closings: bool = False,
            rebuild_year_ends: bool = False) -> dict[str, Any]:
        if self.cfg.env_name != "UAT":
            raise ERPNextError(
                "vendor-advance-control repair is UAT-only "
                f"(env={self.cfg.env_name})")
        plan = self.plan(rebuild_year_ends=rebuild_year_ends)
        report: dict[str, Any] = {
            "plan_only": self.erp.dry_run,
            "company": plan["company"],
            "control_account": plan["control_account"],
            "rebuild_year_ends": rebuild_year_ends,
            "windows": plan["windows"],
            "cancelled_jes": [],
            "cancelled_pcvs": [],
            "journals": [],
            "period_closing": None,
            "fy_2026_27_left_open": True,
        }
        if self.erp.dry_run:
            return report

        self._ensure_control_account()
        if rebuild_year_ends:
            if not reopen_period_closings:
                raise ERPNextError(
                    "year-end reclass needs --reopen-period-closings "
                    "(FY 2026-27 stays open)")
            from_end = plan["reopen_period_end_from"] or YEAR_ENDS[0]
            report["cancelled_pcvs"] = self._reopen_pcvs(from_end)
            report["cancelled_jes"] = self._cancel_control_jes()
            plan = self.plan(rebuild_year_ends=True)
            report["windows"] = plan["windows"]

        need_reopen = (not rebuild_year_ends) and any(
            w["action"] in ("create", "replace") and w["date"] in YEAR_ENDS
            for w in plan["windows"]
        )
        if need_reopen:
            if not reopen_period_closings:
                raise ERPNextError(
                    "year-end reclass needs --reopen-period-closings "
                    "(FY 2026-27 stays open)")
            from_end = plan["reopen_period_end_from"] or YEAR_ENDS[0]
            report["cancelled_pcvs"] = self._reopen_pcvs(from_end)

        last_closed = YEAR_ENDS[-1]
        for window in plan["windows"]:
            if window["action"] == "skip":
                report["journals"].append({
                    "date": window["date"], "status": "skipped",
                    "name": window["existing"], "key": window["key"],
                })
                continue
            nets = {k: _money(v) for k, v in window["by_party"].items()}
            je = self._build_je(
                window["date"], window["key"], nets,
                f"Advances to Vendors control {window['date']}")
            if not je:
                report["journals"].append({
                    "date": window["date"], "status": "empty",
                    "key": window["key"],
                })
            else:
                res = self.erp.insert_and_submit("Journal Entry", je)
                report["journals"].append({
                    "date": window["date"], "status": "created",
                    "name": _name_of(res), "key": window["key"],
                    "lines": len(je["accounts"]),
                })
            if (window["date"] == last_closed and report["cancelled_pcvs"]):
                stats, results = PeriodClosingLoader(self.erp, self.d).run()
                report["period_closing"] = {"stats": stats, "results": results}
        return report
