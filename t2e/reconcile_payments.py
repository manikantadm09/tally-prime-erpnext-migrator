"""Payment Reconciliation: net a party's unallocated payments/advances against
its outstanding invoices, per party, FIFO.

WHY THIS EXISTS
In Tally the client routinely booked receipts/payments as advances or
on-account -- no bill link. After migration those become *unallocated* Payment
Entries sitting on the Debtors/Creditors control account, so every invoice stays
Unpaid and shows up overdue for years, even though the party's *net* balance is
already correct (the migrated GL ties to Tally -- that stays untouched).

WHAT THIS DOES
Drives ERPNext's own Payment Reconciliation controller through the same three
whitelisted methods the desk uses:

    get_unreconciled_entries()   -> lists the party's open invoices + payments
    allocate_entries(args)       -> FIFO-allocates payments across invoices
    reconcile()                  -> writes the allocation

It is GL-neutral: when allocated amounts match (same-currency INR migration, no
write-off) it only adds reference rows to the payments and reduces each invoice's
outstanding -- no P&L impact, the party's control-account balance does not move.
Genuine leftover advances stay unallocated, genuinely unpaid invoices stay
overdue; only the *false* overdues clear.

DRY-RUN (default): get_unreconciled_entries + allocate_entries are read-only, so
the planned allocation is computed and reported without touching ERPNext. Only
--confirm calls reconcile().

GST-on-advance: this driver sets no tax/withholding fields, so no advance-GST is
generated here. If your India Compliance settings auto-book GST on advances at
reconcile time, validate on one party first (``--party "<name>" --confirm``).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .mapping import CompanyDefaults

# (party doctype, CompanyDefaults attr holding its control account)
PARTY_SPECS = [
    ("Customer", "receivable"),
    ("Supplier", "payable"),
]


class PaymentReconciler:
    def __init__(self, erp: ERPNextClient, defaults: CompanyDefaults):
        self.erp = erp
        self.d = defaults
        self.results: list[dict] = []   # per-party outcomes for the report

    # ---- per party -------------------------------------------------------
    def _base_doc(self, party_type: str, account: str, party: str) -> dict:
        return {
            "doctype": "Payment Reconciliation",
            "company": self.d.name,
            "party_type": party_type,
            "party": party,
            "receivable_payable_account": account,
        }

    def _reconcile_in_refreshed_chunks(
        self, party_type: str, account: str, party: str
    ) -> float:
        """Reconcile one allocation at a time, refreshing between writes.

        ERPNext can reject a multi-row allocation when the same invoice is
        touched more than once in one request: a later row then carries the
        invoice's old ``modified`` timestamp.  A fresh controller document for
        every row is slower but preserves ERPNext's validation and avoids
        bypassing the optimistic-lock check.
        """
        reconciled = 0.0
        for _ in range(1000):
            doc = self._base_doc(party_type, account, party)
            doc = self.erp.run_doc_method("get_unreconciled_entries", doc)
            invoices = doc.get("invoices") or []
            payments = doc.get("payments") or []
            if not invoices or not payments:
                return round(reconciled, 2)
            doc = self.erp.run_doc_method(
                "allocate_entries", doc,
                args={"invoices": invoices, "payments": payments})
            allocation = doc.get("allocation") or []
            if not allocation:
                return round(reconciled, 2)
            first = allocation[0]
            amount = round(float(first.get("allocated_amount") or 0), 2)
            if amount <= 0:
                return round(reconciled, 2)
            doc["allocation"] = [first]
            self.erp.run_doc_method("reconcile", doc)
            reconciled = round(reconciled + amount, 2)
        raise RuntimeError(
            f"payment reconciliation exceeded 1000 refreshed chunks for {party}")

    def reconcile_party(self, party_type: str, account: str, party: str) -> dict | None:
        """Return an outcome dict, or None when there is nothing to net."""
        doc = self._base_doc(party_type, account, party)
        doc = self.erp.run_doc_method("get_unreconciled_entries", doc)
        invoices = doc.get("invoices") or []
        payments = doc.get("payments") or []
        if not invoices or not payments:
            return None

        # FIFO allocation is done server-side by the controller (iterates
        # payments over invoices in order); we just hand it the open rows.
        doc = self.erp.run_doc_method(
            "allocate_entries", doc,
            args={"invoices": invoices, "payments": payments})
        allocation = doc.get("allocation") or []
        allocated = round(sum(float(a.get("allocated_amount") or 0)
                              for a in allocation), 2)
        if allocated <= 0:
            return None

        outcome = {
            "party_type": party_type,
            "party": party,
            "allocated": allocated,
            "invoices": len({a.get("invoice_number") for a in allocation}),
            "payments": len({a.get("reference_name") for a in allocation}),
            "status": "planned",
        }
        if not self.erp.dry_run:
            try:
                self.erp.run_doc_method("reconcile", doc)
                outcome["status"] = "reconciled"
            except ERPNextError as exc:
                detail = exc.body or str(exc)
                if "modified after you pulled it" in detail:
                    try:
                        outcome["allocated"] = self._reconcile_in_refreshed_chunks(
                            party_type, account, party)
                        outcome["status"] = "reconciled"
                    except (ERPNextError, RuntimeError) as retry_exc:
                        outcome["status"] = "error"
                        retry_detail = getattr(retry_exc, "body", None) or str(retry_exc)
                        outcome["error"] = retry_detail[:2000]
                else:
                    outcome["status"] = "error"
                    outcome["error"] = detail[:2000]
        self.results.append(outcome)
        return outcome

    # ---- run -------------------------------------------------------------
    def run(self, only_party: str | None = None, limit: int = 0,
            progress=lambda *a: None) -> dict:
        stats = {"parties": 0, "planned": 0, "reconciled": 0,
                 "error": 0, "skipped": 0, "allocated": 0.0}
        n = 0
        stop = False
        for party_type, acct_attr in PARTY_SPECS:
            if stop:
                break
            account = getattr(self.d, acct_attr)
            if only_party:
                parties = [only_party] if self.erp.exists(party_type, only_party) else []
            else:
                parties = [r["name"] for r in
                           self.erp.get_list(party_type, fields=["name"], limit=0)]
            for party in parties:
                if limit and n >= limit:
                    stop = True
                    break
                n += 1
                stats["parties"] += 1
                try:
                    outcome = self.reconcile_party(party_type, account, party)
                except ERPNextError as exc:
                    stats["error"] += 1
                    detail = exc.body or str(exc)
                    self.results.append({"party_type": party_type, "party": party,
                                         "status": "error",
                                         "error": detail[:2000]})
                    outcome = None
                if outcome is None:
                    stats["skipped"] += 1
                else:
                    stats[outcome["status"]] = stats.get(outcome["status"], 0) + 1
                    stats["allocated"] = round(stats["allocated"] + outcome["allocated"], 2)
                if n % 20 == 0:
                    progress(n, stats)
        progress(n, stats)
        self._write_report(stats)
        return stats

    # ---- report ----------------------------------------------------------
    def _write_report(self, stats: dict) -> None:
        rep_dir = get_config().staging_db.parent / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "company": self.d.name,
            "mode": "dry-run" if self.erp.dry_run else "live",
            "stats": stats,
            "parties": self.results,
        }
        (rep_dir / "payment_reconciliation.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        with (rep_dir / "payment_reconciliation.csv").open(
                "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["party_type", "party", "status", "invoices",
                        "payments", "allocated", "error"])
            for r in self.results:
                w.writerow([r.get("party_type"), r.get("party"), r.get("status"),
                            r.get("invoices"), r.get("payments"),
                            r.get("allocated"), r.get("error", "")])
