"""Apply one explicitly reviewed evidence-backed payment/invoice pair.

This executor never performs party-wide FIFO. It accepts one named Payment
Entry and one named invoice from a fresh evidence plan, re-reads ERPNext,
requires exact live amounts, asks ERPNext to calculate only that pair, and
proves that active GL totals did not change.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .config import get_config
from .erpnext_client import ERPNextClient
from .exact_bill_reconciliation import _gl_signature, money, payment_key
from .load_masters import fetch_company_defaults


MAX_PLAN_AGE_SECONDS = 3600


def load_plan(path: Path, company: str) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("mode") != "read-only":
        raise RuntimeError("evidence plan is not read-only")
    if plan.get("policy") != "evidence-backed non-Tally allocation review":
        raise RuntimeError("unexpected evidence-plan policy")
    if plan.get("safe_to_apply_automatically") is not False:
        raise RuntimeError("evidence plan must remain explicitly reviewed")
    if plan.get("company") != company:
        raise RuntimeError(
            f"plan company {plan.get('company')!r} does not match {company!r}")
    generated = datetime.fromisoformat(str(plan["generated_at"]))
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - generated).total_seconds()
    if age < -60 or age > MAX_PLAN_AGE_SECONDS:
        raise RuntimeError(f"evidence plan is stale or future-dated (age={age:.0f}s)")
    source = Path(str(plan.get("source_state") or ""))
    if not source.is_file():
        raise RuntimeError(f"plan source state is missing: {source}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != plan.get(
            "source_state_sha256"):
        raise RuntimeError("ERPNext source-state export changed after planning")
    verification = plan.get("verification_report")
    if verification:
        verification_path = Path(str(verification))
        if not verification_path.is_file():
            raise RuntimeError("Tally verification report is missing")
        if hashlib.sha256(verification_path.read_bytes()).hexdigest() != plan.get(
                "verification_report_sha256"):
            raise RuntimeError("Tally verification report changed after planning")
    return plan


class EvidencePaymentReconciler:
    def __init__(self, erp: ERPNextClient, plan_path: Path):
        self.erp = erp
        self.defaults = fetch_company_defaults(erp)
        self.plan_path = plan_path
        self.plan = load_plan(plan_path, self.defaults.name)

    def select(self, payment_name: str, invoice_name: str,
               acknowledge_weaker_evidence: bool = False) -> dict:
        selected = [
            row for row in self.plan.get("candidates", [])
            if row.get("payment_name") == payment_name
            and row.get("invoice_name") == invoice_name
        ]
        if len(selected) != 1:
            raise RuntimeError(
                f"expected one reviewed pair, found {len(selected)}")
        candidate = selected[0]
        confidence = candidate.get("confidence")
        if confidence != "high" and not acknowledge_weaker_evidence:
            raise RuntimeError(
                f"{confidence or 'unknown'}-confidence pair requires explicit "
                "weaker-evidence acknowledgement")
        if confidence not in {"high", "review", "manual"}:
            raise RuntimeError(f"unsupported evidence confidence: {confidence!r}")
        return candidate

    def _base_doc(self, candidate: dict) -> dict:
        expected_account = (
            self.defaults.receivable
            if candidate["party_type"] == "Customer"
            else self.defaults.payable
        )
        if candidate["account"] != expected_account:
            raise RuntimeError(
                f"party control account drift: plan={candidate['account']} "
                f"live-default={expected_account}")
        return {
            "doctype": "Payment Reconciliation",
            "company": self.defaults.name,
            "party_type": candidate["party_type"],
            "party": candidate["party"],
            "receivable_payable_account": expected_account,
        }

    def _fresh(self, candidate: dict) -> dict:
        return self.erp.run_doc_method(
            "get_unreconciled_entries", self._base_doc(candidate))

    @staticmethod
    def _find_payment(payments: list[dict], name: str) -> dict | None:
        rows = [row for row in payments if payment_key(row) == (
            "Payment Entry", name)]
        return rows[0] if len(rows) == 1 else None

    def _plan_live_pair(self, candidate: dict) -> tuple[dict, dict]:
        doc = self._fresh(candidate)
        invoices = [
            row for row in doc.get("invoices") or []
            if str(row.get("invoice_number")) == candidate["invoice_name"]
        ]
        payment = self._find_payment(
            doc.get("payments") or [], candidate["payment_name"])
        amount = money(candidate["unallocated_amount"])
        if len(invoices) != 1:
            raise RuntimeError("invoice is no longer uniquely unreconciled")
        invoice = invoices[0]
        if money(invoice.get("outstanding_amount")) != amount:
            raise RuntimeError(
                f"invoice outstanding drift: plan={amount} "
                f"live={money(invoice.get('outstanding_amount'))}")
        if payment is None or money(payment.get("amount")) != amount:
            raise RuntimeError("payment unallocated amount drift")
        invoice_arg = dict(invoice)
        invoice_arg["outstanding_amount"] = float(amount)
        invoice_arg["amount"] = float(amount)
        planned_doc = self.erp.run_doc_method(
            "allocate_entries", doc,
            args={"invoices": [invoice_arg], "payments": [dict(payment)]},
        )
        rows = planned_doc.get("allocation") or []
        if len(rows) != 1:
            raise RuntimeError(f"ERPNext returned {len(rows)} allocation rows")
        row = rows[0]
        if (
            str(row.get("invoice_number")) != candidate["invoice_name"]
            or payment_key(row) != ("Payment Entry", candidate["payment_name"])
            or money(row.get("allocated_amount")) != amount
        ):
            raise RuntimeError("ERPNext proposed a different payment/invoice pair")
        planned_doc["allocation"] = [row]
        return planned_doc, row

    def run(self, payment_name: str, invoice_name: str,
            acknowledge_tally_deviation: bool = False,
            acknowledge_weaker_evidence: bool = False) -> dict:
        candidate = self.select(
            payment_name, invoice_name,
            acknowledge_weaker_evidence=acknowledge_weaker_evidence)
        if (
            not self.erp.dry_run
            and candidate.get("tally_bill_status_effect") == "would_disagree"
            and not acknowledge_tally_deviation
        ):
            raise RuntimeError(
                "write refused: acknowledge the reviewed Tally bill-status deviation")

        gl_before = _gl_signature(self.erp, self.defaults.name)
        planned_doc, allocation = self._plan_live_pair(candidate)
        if not self.erp.dry_run:
            self.erp.run_doc_method("reconcile", planned_doc)
        gl_after = _gl_signature(self.erp, self.defaults.name)
        if gl_after != gl_before:
            raise RuntimeError("active GL changed during payment reconciliation")

        live_invoice = self.erp.get_list(
            candidate["invoice_doctype"],
            fields=["name", "status", "outstanding_amount"],
            filters=[["name", "=", invoice_name]], limit=1,
        )
        live_payment = self.erp.get_list(
            "Payment Entry", fields=["name", "unallocated_amount"],
            filters=[["name", "=", payment_name]], limit=1,
        )
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "company": self.defaults.name,
            "mode": "dry-run" if self.erp.dry_run else "live",
            "plan": str(self.plan_path),
            "candidate": candidate,
            "erpnext_allocation": allocation,
            "gl_before": gl_before,
            "gl_after": gl_after,
            "invoice_after": live_invoice[0] if live_invoice else None,
            "payment_after": live_payment[0] if live_payment else None,
            "pass": gl_before == gl_after,
        }
        report = (
            get_config().staging_db.parent / "reports"
            / "evidence_payment_reconciliation.json"
        )
        report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        payload["report"] = str(report)
        return payload
