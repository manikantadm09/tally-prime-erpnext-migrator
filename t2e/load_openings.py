"""Post Tally ledger opening balances as one balanced opening Journal Entry.

The voucher migration replays only dated vouchers, so any balance a ledger
carried *before* the migration window (Tally's OPENINGBALANCE, i.e. the opening
balance sheet) is never posted. For most ledgers that opening is zero, but a few
capital / bank / provision ledgers -- and the brought-forward Profit & Loss A/c
-- carry one, which is why those accounts miss Tally by exactly their opening.

This loader reads every ledger's OPENINGBALANCE from Tally, converts Tally's
credit-positive sign to Dr/Cr, and posts a single opening Journal Entry dated the
day before the migration's from_date. Tally's openings net to zero (a balanced
opening balance sheet), so the entry needs no plug. Idempotent via the
``tally_guid`` custom field (key ``opening-balances``).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .load_vouchers import _name_of
from .mapping import CompanyDefaults, LedgerResolver
from .staging import Staging
from .tally_client import TallyClient

ROUND_TOL = 1.0  # residual up to this (Rs) is plugged to round-off; above -> error


def _f(s) -> float:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return 0.0


class OpeningsLoader:
    def __init__(self, erp: ERPNextClient, store: Staging, defaults: CompanyDefaults):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.r = LedgerResolver(store, defaults)
        cfg = get_config()
        self.field = cfg.idempotency_field
        self.from_date = str(cfg.tally.get("from_date", "20220101"))
        self.unresolved: list[str] = []

    def _opening_date(self) -> str:
        d = datetime.strptime(self.from_date, "%Y%m%d").date() - timedelta(days=1)
        return d.isoformat()

    def _fetch_openings(self) -> list[tuple[str, float]]:
        """[(ledger_name, opening_credit_positive)] for nonzero openings."""
        client = TallyClient()
        client.from_date, client.to_date = self.from_date, self.from_date
        root = client.export_collection(
            "opening_ledgers", "Ledger",
            methods=["Name", "Parent", "OpeningBalance"], dated=True,
            save_as="opening_ledgers")
        out = []
        for el in root.findall(".//LEDGER"):
            name = " ".join((el.get("NAME") or "").split())
            if not name:
                continue
            op = _f(el.findtext("OPENINGBALANCE"))
            if abs(op) >= 0.005:
                out.append((name, op))
        return out

    def _build_je(self, openings: list[tuple[str, float]], key: str) -> tuple[dict, list]:
        cc = self.d.cost_center
        accounts, preview = [], []
        total_dr = total_cr = 0.0
        for name, op in openings:
            res = self.r.get(name)
            if res is None:
                self.unresolved.append(name)
                res_account, party_type, party = self.d.suspense, None, None
            else:
                res_account, party_type, party = res.account, res.party_type, res.party
            # Tally OPENINGBALANCE: credit positive, debit negative.
            row = {"account": res_account, "cost_center": cc}
            if party:
                row["party_type"], row["party"] = party_type, party
            if op > 0:
                row["credit_in_account_currency"] = round(op, 2)
                total_cr += round(op, 2)
            else:
                row["debit_in_account_currency"] = round(-op, 2)
                total_dr += round(-op, 2)
            accounts.append(row)
            preview.append((name, res_account, party or "", op))

        diff = round(total_dr - total_cr, 2)
        if abs(diff) > ROUND_TOL:
            raise ERPNextError(
                f"Opening balances do not net to zero (Dr {total_dr} / Cr {total_cr}, "
                f"diff {diff}); refusing to plug more than Rs {ROUND_TOL}.")
        if abs(diff) > 1e-9:
            plug = {"account": self.d.round_off, "cost_center": cc}
            if diff > 0:
                plug["credit_in_account_currency"] = abs(diff)
            else:
                plug["debit_in_account_currency"] = abs(diff)
            accounts.append(plug)

        doc = {
            "company": self.d.name,
            "posting_date": self._opening_date(),
            "voucher_type": "Opening Entry",
            "is_opening": "Yes",
            "title": "Tally Opening Balances",
            "user_remark": "Tally ledger opening balances (pre-migration-window "
                           "opening balance sheet)",
            "accounts": accounts,
            self.field: key,
        }
        return doc, preview

    def run(self):
        try:
            self.erp.ensure_custom_field("Journal Entry", self.field, "Tally GUID")
        except ERPNextError:
            pass
        key = "opening-balances"
        openings = self._fetch_openings()
        existing = self.erp.find_by_field("Journal Entry", self.field, key)
        doc, preview = self._build_je(openings, key)
        if existing:
            return {"created": 0, "skipped": 1, "lines": len(openings)}, preview, existing
        res = self.erp.submit_doc("Journal Entry", doc)
        name = _name_of(res) or ("(dry-run)" if self.erp.dry_run else None)
        return {"created": 1, "skipped": 0, "lines": len(openings)}, preview, name
