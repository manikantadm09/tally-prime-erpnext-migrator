"""Create ERPNext Period Closing Vouchers for migrated fiscal years."""
from __future__ import annotations

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .load_vouchers import _name_of
from .mapping import CompanyDefaults, acc_name


class PeriodClosingLoader:
    def __init__(self, erp: ERPNextClient, defaults: CompanyDefaults):
        self.erp = erp
        self.d = defaults
        cfg = get_config()
        self.field = cfg.idempotency_field
        self.years = list(cfg.yaml["period_closing"]["fiscal_years"])
        retained = cfg.erpnext.get(
            "retained_earnings_account", "Reserves and Surplus")
        self.closing_account = acc_name(retained, defaults.abbr)

    @staticmethod
    def dates(fiscal_year: str) -> tuple[str, str]:
        start_year, end_year = map(int, fiscal_year.split("-", 1))
        return f"{start_year}-04-01", f"{end_year}-03-31"

    def run(self):
        if not self.erp.exists("Account", self.closing_account):
            raise ERPNextError(
                f"Retained earnings account missing: {self.closing_account}")
        if not self.erp.dry_run:
            self.erp.ensure_custom_field(
                "Period Closing Voucher", self.field, "Tally GUID")
        stats = {"created": 0, "skipped": 0, "error": 0}
        results = []
        for fiscal_year in self.years:
            start, end = self.dates(fiscal_year)
            key = f"period-closing-{fiscal_year}"
            existing = self.erp.find_by_field(
                "Period Closing Voucher", self.field, key,
                exclude_cancelled=True)
            if existing:
                stats["skipped"] += 1
                results.append((fiscal_year, "skipped", existing))
                continue
            doc = {
                "company": self.d.name,
                "fiscal_year": fiscal_year,
                "transaction_date": end,
                "period_start_date": start,
                "period_end_date": end,
                "closing_account_head": self.closing_account,
                "remarks": (
                    f"Tally migration fiscal-year close {fiscal_year}"),
                self.field: key,
            }
            try:
                res = self.erp.submit_doc("Period Closing Voucher", doc)
                name = _name_of(res) or (
                    "(dry-run)" if self.erp.dry_run else None)
                stats["created"] += 1
                results.append((fiscal_year, "created", name))
            except ERPNextError as exc:
                stats["error"] += 1
                results.append(
                    (fiscal_year, "error", str(exc)[:500]))
                # Later FY closes depend on earlier closes. Stop rather than
                # producing a partial sequence whose accumulated profit is wrong.
                break
        return stats, results
