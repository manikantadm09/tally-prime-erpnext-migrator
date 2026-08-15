"""Post year-end closing-stock adjustments as Journal Entries.

Background: in Tally the auditor keys a closing balance straight onto the
"Closing Stock" ledger (group Stock-in-Hand) at each financial year-end -- there
is no voucher behind it. Tally's Stock-in-Hand behaviour then reflects that one
figure in BOTH the Balance Sheet (as a Current Asset) and the Profit & Loss (as
"Less: Closing Stock"), auto-generating the double-entry. A GL/voucher-level
migration never sees it, so ERPNext's P&L understates profit by the closing
stock every year.

This loader reproduces it with one Journal Entry per FY end, using GROSS lines
that mirror Tally's P&L presentation (separate Opening Stock and Closing Stock):

    Dr  Opening Stock (P&L, Expense)   = prior year's close   [omitted year 1]
    Cr  Closing Stock (asset)          = prior year's close
    Dr  Closing Stock (asset)          = this year's close
    Cr  Closing Stock (P&L, Income)    = this year's close

Net P&L impact each year = close - open = the change in inventory; the asset
balance climbs to each year's keyed value. Idempotent via the ``tally_guid``
custom field (key ``closing-stock-<yyyy-mm-dd>``), so re-runs skip.
"""
from __future__ import annotations

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .load_vouchers import _name_of
from .mapping import CompanyDefaults, acc_name


class ClosingStockLoader:
    def __init__(self, erp: ERPNextClient, defaults: CompanyDefaults):
        self.erp = erp
        self.d = defaults
        cfg = get_config()
        self.field = cfg.idempotency_field
        self.spec = cfg.yaml["closing_stock"]
        self.asset = acc_name(self.spec["asset_account"], self.d.abbr)
        self.opening_pl = acc_name(self.spec["opening_pl_name"], self.d.abbr)
        self.closing_pl = acc_name(self.spec["closing_pl_name"], self.d.abbr)

    # ---- account setup ---------------------------------------------------
    def _ensure_account(self, account_name: str, root_type: str) -> str:
        full = acc_name(account_name, self.d.abbr)
        if self.erp.exists("Account", full):
            return full
        parent = self.d.root_by_type.get(root_type) or self.d.root_by_type["Asset"]
        self.erp.insert("Account", {
            "account_name": account_name,
            "parent_account": parent,
            "company": self.d.name,
            "is_group": 0,
            "root_type": root_type,
        })
        return full

    def ensure_accounts(self) -> None:
        # The Stock-in-Hand asset ledger is created by the master load; only make
        # it here as a safety net (root_type Asset) if somehow absent.
        self._ensure_account(self.spec["asset_account"], "Asset")
        self._ensure_account(self.spec["opening_pl_name"], "Expense")
        self._ensure_account(self.spec["closing_pl_name"], "Income")
        if not self.erp.dry_run:
            # Guarantee the idempotency field exists on Journal Entry.
            try:
                self.erp.ensure_custom_field("Journal Entry", self.field, "Tally GUID")
            except ERPNextError:
                pass

    # ---- journal entry ---------------------------------------------------
    def _build_je(self, date: str, opening: float, closing: float, key: str) -> dict:
        cc = self.d.cost_center
        accounts: list[dict] = []
        if opening > 0:
            accounts.append({"account": self.opening_pl, "cost_center": cc,
                             "debit_in_account_currency": round(opening, 2)})
            accounts.append({"account": self.asset,
                             "credit_in_account_currency": round(opening, 2)})
        accounts.append({"account": self.asset,
                         "debit_in_account_currency": round(closing, 2)})
        accounts.append({"account": self.closing_pl, "cost_center": cc,
                         "credit_in_account_currency": round(closing, 2)})
        return {
            "company": self.d.name,
            "posting_date": date,
            "voucher_type": "Journal Entry",
            "title": f"Closing Stock {date}",
            "user_remark": ("Year-end closing stock (Tally Stock-in-Hand keyed "
                            "balance; opening=prior FY close)"),
            "accounts": accounts,
            self.field: key,
        }

    # ---- run -------------------------------------------------------------
    def run(self):
        self.ensure_accounts()
        balances = sorted((d, float(v)) for d, v in self.spec["balances"].items())
        stats = {"created": 0, "skipped": 0, "error": 0}
        results = []
        prev = 0.0
        for date, closing in balances:
            key = f"closing-stock-{date}"
            existing = self.erp.find_by_field(
                "Journal Entry", self.field, key, exclude_cancelled=True)
            if existing:
                stats["skipped"] += 1
                results.append((date, prev, closing, "skipped", existing))
                prev = closing
                continue
            je = self._build_je(date, prev, closing, key)
            try:
                res = self.erp.insert_and_submit("Journal Entry", je)
                name = _name_of(res) or ("(dry-run)" if self.erp.dry_run else None)
                stats["created"] += 1
                results.append((date, prev, closing, "created", name))
            except ERPNextError as exc:
                stats["error"] += 1
                results.append((date, prev, closing, "error", str(exc)[:300]))
            prev = closing
        return stats, results
