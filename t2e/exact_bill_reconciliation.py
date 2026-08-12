"""Apply a reviewed, Tally-reference-driven Payment Reconciliation plan.

The planner proves the invoice reductions from Tally bill references.  This
module deliberately does not calculate FIFO allocations: it consumes only the
named invoice/payment pairs in that plan, refreshes ERPNext before every write,
and stops on any outstanding drift.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .load_masters import fetch_company_defaults


PENNY = Decimal("0.01")
MAX_PLAN_AGE_SECONDS = 3600


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def payment_key(row: dict) -> tuple[str, str]:
    return str(row.get("reference_type") or ""), str(row.get("reference_name") or "")


def _gl_signature(erp: ERPNextClient, company: str) -> dict[str, str | int]:
    rows = erp.get_list(
        "GL Entry", fields=["debit", "credit"],
        filters=[["company", "=", company], ["is_cancelled", "=", 0]], limit=0,
    )
    return {
        "rows": len(rows),
        "debit": f"{sum((money(row.get('debit')) for row in rows), Decimal('0.00')):.2f}",
        "credit": f"{sum((money(row.get('credit')) for row in rows), Decimal('0.00')):.2f}",
    }


def load_and_validate_plan(path: Path, company: str) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("mode") != "read-only" or not plan.get("safe_to_apply"):
        raise RuntimeError("plan is not a safe read-only exact-allocation plan")
    if money(plan.get("residual")) != 0:
        raise RuntimeError(f"plan has residual {plan.get('residual')}")
    if plan.get("company") != company:
        raise RuntimeError(
            f"plan company {plan.get('company')!r} does not match {company!r}")
    generated = datetime.fromisoformat(str(plan["generated_at"]))
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - generated).total_seconds()
    if age < -60 or age > MAX_PLAN_AGE_SECONDS:
        raise RuntimeError(f"plan is stale or future-dated (age={age:.0f}s); regenerate it")
    source_path = Path(plan["source_report"])
    if not source_path.exists():
        raise RuntimeError(f"plan source report is missing: {source_path}")
    actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if actual_hash != plan.get("source_report_sha256"):
        raise RuntimeError("invoice verification report changed after this plan was built")
    return plan


class ExactBillReconciler:
    def __init__(self, erp: ERPNextClient, plan_path: Path):
        self.erp = erp
        self.defaults = fetch_company_defaults(erp)
        self.plan_path = plan_path
        self.plan = load_and_validate_plan(plan_path, self.defaults.name)
        self.results: list[dict] = []

    def _base_doc(self, party_row: dict) -> dict:
        account = (
            self.defaults.receivable
            if party_row["party_type"] == "Customer"
            else self.defaults.payable
        )
        return {
            "doctype": "Payment Reconciliation",
            "company": self.defaults.name,
            "party_type": party_row["party_type"],
            "party": party_row["party"],
            "receivable_payable_account": account,
        }

    def _fresh(self, party_row: dict) -> dict:
        return self.erp.run_doc_method(
            "get_unreconciled_entries", self._base_doc(party_row))

    @staticmethod
    def _find_payment(payments: list[dict], allocation: dict) -> dict | None:
        key = payment_key(allocation)
        candidates = [row for row in payments if payment_key(row) == key]
        reference_row = str(allocation.get("reference_row") or "")
        if reference_row:
            exact = [row for row in candidates
                     if str(row.get("reference_row") or "") == reference_row]
            if exact:
                return exact[0]
        return candidates[0] if candidates else None

    def _validate_party_start(self, party_row: dict, doc: dict) -> None:
        invoices = {row["invoice_number"]: row for row in doc.get("invoices") or []}
        payments = doc.get("payments") or []
        required_by_payment: dict[tuple[str, str], Decimal] = defaultdict(
            lambda: Decimal("0.00"))
        planned_by_invoice: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        for allocation in party_row["allocations"]:
            amount = money(allocation.get("allocated_amount"))
            if amount <= 0:
                raise RuntimeError("plan contains a non-positive allocation")
            invoice_name = str(allocation["invoice_number"])
            if invoice_name not in invoices:
                raise RuntimeError(f"invoice is no longer unreconciled: {invoice_name}")
            if self._find_payment(payments, allocation) is None:
                raise RuntimeError(
                    f"payment is no longer unreconciled: {payment_key(allocation)}")
            required_by_payment[payment_key(allocation)] += amount
            planned_by_invoice[invoice_name] += amount

        for invoice_name, meta in party_row["targets"].items():
            current = invoices.get(invoice_name)
            if current is None:
                raise RuntimeError(f"planned invoice is absent: {invoice_name}")
            live = money(current.get("outstanding_amount"))
            expected = money(meta.get("starting_outstanding"))
            if live != expected:
                raise RuntimeError(
                    f"invoice drift for {invoice_name}: plan={expected} live={live}")
            if planned_by_invoice[invoice_name] != money(meta.get("planned")):
                raise RuntimeError(f"allocation sum mismatch for {invoice_name}")

        for key, required in required_by_payment.items():
            sample = next(a for a in party_row["allocations"] if payment_key(a) == key)
            current = self._find_payment(payments, sample)
            available = money(current.get("amount")) if current else Decimal("0.00")
            if available < required:
                raise RuntimeError(
                    f"payment drift for {key}: required={required} live={available}")

    def _apply_allocation(self, party_row: dict, allocation: dict) -> Decimal:
        if str(allocation.get("reference_type")) not in ("Payment Entry", "Journal Entry"):
            raise RuntimeError(
                "exact reconciliation refuses invoice/return sources because ERPNext "
                "would create artificial GL turnover")
        doc = self._fresh(party_row)
        invoices = {row["invoice_number"]: row for row in doc.get("invoices") or []}
        invoice_name = str(allocation["invoice_number"])
        invoice = invoices.get(invoice_name)
        payment = self._find_payment(doc.get("payments") or [], allocation)
        amount = money(allocation.get("allocated_amount"))
        if invoice is None or money(invoice.get("outstanding_amount")) < amount:
            raise RuntimeError(f"invoice drift before write: {invoice_name}")
        if payment is None or money(payment.get("amount")) < amount:
            raise RuntimeError(f"payment drift before write: {payment_key(allocation)}")

        invoice_arg = dict(invoice)
        invoice_arg["outstanding_amount"] = float(amount)
        invoice_arg["amount"] = float(amount)
        # Keep the payment row's full live availability. ERPNext validates this
        # against the database during reconcile(); only the target invoice is
        # capped so allocate_entries chooses the reviewed partial amount.
        payment_arg = dict(payment)
        planned_doc = self.erp.run_doc_method(
            "allocate_entries", doc,
            args={"invoices": [invoice_arg], "payments": [payment_arg]},
        )
        rows = planned_doc.get("allocation") or []
        if len(rows) != 1:
            raise RuntimeError(f"ERPNext returned {len(rows)} allocation rows")
        row = rows[0]
        if (
            str(row.get("invoice_number")) != invoice_name
            or payment_key(row) != payment_key(allocation)
            or money(row.get("allocated_amount")) != amount
        ):
            raise RuntimeError("ERPNext allocation differs from the reviewed exact pair")
        if not self.erp.dry_run:
            planned_doc["allocation"] = [row]
            self.erp.run_doc_method("reconcile", planned_doc)
        return amount

    def run(self, only_party: str | None = None) -> dict:
        selected = [row for row in self.plan["details"]
                    if not only_party or norm(row["party"]) == norm(only_party)]
        if only_party and not selected:
            raise RuntimeError(f"party is not present in the plan: {only_party}")
        if not selected:
            raise RuntimeError("plan has no selected parties")

        gl_before = _gl_signature(self.erp, self.defaults.name)
        total = Decimal("0.00")
        success = False
        try:
            for party_row in selected:
                if party_row.get("errors") or money(party_row.get("residual")) != 0:
                    raise RuntimeError(f"unsafe party plan: {party_row['party']}")
                self._validate_party_start(party_row, self._fresh(party_row))
                allocated = Decimal("0.00")
                try:
                    for allocation in party_row["allocations"]:
                        amount = self._apply_allocation(party_row, allocation)
                        allocated += amount
                        total += amount
                except (ERPNextError, RuntimeError) as exc:
                    detail = getattr(exc, "body", None) or str(exc)
                    self.results.append({
                        "party_type": party_row["party_type"],
                        "party": party_row["party"],
                        "status": "error",
                        "allocations": len(party_row["allocations"]),
                        "allocated": f"{allocated:.2f}",
                        "error": detail[:2000],
                    })
                    raise
                status = "validated" if self.erp.dry_run else "reconciled"
                self.results.append({
                    "party_type": party_row["party_type"],
                    "party": party_row["party"],
                    "status": status,
                    "allocations": len(party_row["allocations"]),
                    "allocated": f"{allocated:.2f}",
                })
            success = True
        finally:
            gl_after = _gl_signature(self.erp, self.defaults.name)
            if gl_after != gl_before:
                success = False
                self.results.append({"status": "error", "error": "active GL changed"})
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "company": self.defaults.name,
                "mode": "dry-run" if self.erp.dry_run else "live",
                "plan": str(self.plan_path),
                "selected_parties": len(selected),
                "allocated": f"{total:.2f}",
                "gl_before": gl_before,
                "gl_after": gl_after,
                "pass": success and gl_after == gl_before,
                "results": self.results,
            }
            report = get_config().staging_db.parent / "reports" / "exact_bill_reconciliation.json"
            report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            payload["report"] = str(report)
        if not payload["pass"]:
            raise RuntimeError("exact reconciliation failed; inspect its report")
        return payload
