"""Apply a reviewed plan that unlinks bill allocations contradicted by Tally.

ERPNext's standard ``Unreconcile Payment`` document is used for every named
payment/invoice pair.  This changes only payment-ledger/reference metadata; it
must not change active GL.  A fresh exact-allocation plan is generated after
this reset, so intentionally over-reset invoice amounts are put back only where
Tally's native bill report proves them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import urllib.parse
from typing import Any

from .config import get_config
from .erpnext_client import ERPNextClient
from .load_masters import fetch_company_defaults


PENNY = Decimal("0.01")
MAX_PLAN_AGE_SECONDS = 3600
SUPPORTED_PAYMENT_TYPES = {"Payment Entry", "Journal Entry"}


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


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


def linked_payments(
    erp: ERPNextClient, company: str, doctype: str, docname: str,
) -> list[dict]:
    params = urllib.parse.urlencode({
        "company": company,
        "doctype": doctype,
        "docname": docname,
    })
    path = (
        "/api/method/erpnext.accounts.doctype.unreconcile_payment."
        "unreconcile_payment.get_linked_payments_for_doc?" + params
    )
    return erp._request("GET", path).get("message") or []


def load_and_validate_plan(path: Path, company: str) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("mode") != "read-only" or not plan.get("safe_to_apply"):
        raise RuntimeError("plan is not a safe read-only bill-reset plan")
    if plan.get("company") != company:
        raise RuntimeError(
            f"plan company {plan.get('company')!r} does not match {company!r}")
    generated = datetime.fromisoformat(str(plan["generated_at"]))
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - generated).total_seconds()
    if age < -60 or age > MAX_PLAN_AGE_SECONDS:
        raise RuntimeError(f"plan is stale or future-dated (age={age:.0f}s); regenerate it")
    source = Path(plan["source_report"])
    if not source.exists():
        raise RuntimeError(f"plan source report is missing: {source}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != plan.get("source_report_sha256"):
        raise RuntimeError("invoice verification report changed after this plan was built")
    if not plan.get("selections"):
        raise RuntimeError("plan has no payment/invoice pairs")
    return plan


class TallyBillUnreconciler:
    def __init__(self, erp: ERPNextClient, plan_path: Path):
        self.erp = erp
        self.defaults = fetch_company_defaults(erp)
        self.plan_path = plan_path
        self.plan = load_and_validate_plan(plan_path, self.defaults.name)

    @staticmethod
    def _key(row: dict) -> tuple[str, str]:
        return str(row.get("reference_doctype") or ""), str(row.get("reference_name") or "")

    def _validate_all(self) -> None:
        by_invoice: dict[tuple[str, str], list[dict]] = {}
        for row in self.plan["selections"]:
            key = (row["against_voucher_type"], row["against_voucher_no"])
            if key not in by_invoice:
                by_invoice[key] = linked_payments(
                    self.erp, self.defaults.name, key[0], key[1])
            live = [
                item for item in by_invoice[key]
                if self._key(item) == (row["voucher_type"], row["voucher_no"])
            ]
            if len(live) != 1:
                raise RuntimeError(
                    f"allocation drift for {row['voucher_type']} {row['voucher_no']} -> "
                    f"{row['against_voucher_type']} {row['against_voucher_no']}")
            if money(live[0].get("allocated_amount")) != money(row.get("allocated_amount")):
                raise RuntimeError(
                    f"allocation amount drift for {row['voucher_no']} -> "
                    f"{row['against_voucher_no']}")

    def run(self) -> dict:
        self._validate_all()
        gl_before = _gl_signature(self.erp, self.defaults.name)
        results = []
        success = False
        try:
            for row in self.plan["selections"]:
                selection = {
                    "company": self.defaults.name,
                    "voucher_type": row["voucher_type"],
                    "voucher_no": row["voucher_no"],
                    "against_voucher_type": row["against_voucher_type"],
                    "against_voucher_no": row["against_voucher_no"],
                }
                if not self.erp.dry_run:
                    self.erp._request(
                        "POST",
                        "/api/method/erpnext.accounts.doctype.unreconcile_payment."
                        "unreconcile_payment.create_unreconcile_doc_for_selection",
                        json={"selections": json.dumps([selection])},
                    )
                results.append({**selection, "status": (
                    "validated" if self.erp.dry_run else "unreconciled")})
            success = True
        finally:
            gl_after = _gl_signature(self.erp, self.defaults.name)
            if gl_after != gl_before:
                success = False
                results.append({"status": "error", "error": "active GL changed"})
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "company": self.defaults.name,
                "mode": "dry-run" if self.erp.dry_run else "live",
                "plan": str(self.plan_path),
                "pairs": len(self.plan["selections"]),
                "gl_before": gl_before,
                "gl_after": gl_after,
                "pass": success and gl_before == gl_after,
                "results": results,
            }
            report = (
                get_config().staging_db.parent / "reports" /
                "tally_bill_unreconciliation.json"
            )
            report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            payload["report"] = str(report)
        if not payload["pass"]:
            raise RuntimeError("bill unreconciliation failed; inspect its report")
        return payload
