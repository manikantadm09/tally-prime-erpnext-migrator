"""Unlink false invoice references from party-control bridge JEs.

A bridge is a GL reclassification, not a receipt/payment. An invoice reference
on its party row makes ERPNext treat it as a settlement and incorrectly reduces
invoice outstanding. The repair uses ERPNext's supported ``Unreconcile Payment``
workflow. That workflow removes only reference metadata from the JE, GL Entry
and Payment Ledger Entry and recalculates invoice outstanding; it does not
cancel/repost historical accounting documents or change debit/credit values.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime

from .config import get_config
from .erpnext_client import ERPNextClient


def financial_signature(rows: list[dict]) -> list[tuple]:
    """Comparable GL identity; settlement-reference metadata is excluded."""
    signature = []
    for row in rows:
        signature.append((
            row.get("account"), row.get("party_type"), row.get("party"),
            round(float(row.get("debit") or 0), 2),
            round(float(row.get("credit") or 0), 2),
            row.get("cost_center"), row.get("project"),
        ))
    return sorted(signature, key=lambda value: tuple(str(item or "") for item in value))


def unreconcile_selections(company: str, voucher_name: str,
                           references: list[dict]) -> list[dict]:
    """Build the exact rows accepted by ERPNext's supported unlink API."""
    rows = []
    for reference in references:
        rows.append((
            {
                "company": company,
                "voucher_type": "Journal Entry",
                "voucher_no": voucher_name,
                "against_voucher_type": reference["reference_type"],
                "against_voucher_no": reference["reference_name"],
            }
        ))
    return rows


class PartyBridgeRepair:
    def __init__(self, erp: ERPNextClient, idempotency_field: str):
        self.erp = erp
        self.field = idempotency_field

    def _get_doc(self, name: str) -> dict:
        nm = urllib.parse.quote(str(name), safe="")
        return self.erp._request(
            "GET", f"/api/resource/Journal%20Entry/{nm}")["data"]

    def _gl_rows(self, name: str) -> list[dict]:
        return self.erp.get_list(
            "GL Entry",
            fields=["account", "party_type", "party", "debit", "credit",
                    "cost_center", "project", "against_voucher_type",
                    "against_voucher"],
            filters=[["voucher_type", "=", "Journal Entry"],
                     ["voucher_no", "=", name], ["is_cancelled", "=", 0]],
            limit=0,
        )

    def candidates(self, names: list[str] | None = None) -> list[dict]:
        filters = [
            ["company", "=", get_config().erpnext["company"]],
            ["docstatus", "=", 1],
            [self.field, "like", "%:party-control-bridge"],
        ]
        if names:
            filters.append(["name", "in", names])
        return self.erp.get_list(
            "Journal Entry",
            fields=["name", self.field, "posting_date", "total_debit",
                    "total_credit"],
            filters=filters,
            limit=0,
        )

    def preview(self, names: list[str] | None = None) -> list[dict]:
        result = []
        for candidate in self.candidates(names):
            doc = self._get_doc(candidate["name"])
            referenced = [
                {
                    "party_type": row.get("party_type"),
                    "party": row.get("party"),
                    "reference_type": row.get("reference_type"),
                    "reference_name": row.get("reference_name"),
                    "amount": round(float(
                        row.get("debit_in_account_currency")
                        or row.get("credit_in_account_currency") or 0), 2),
                }
                for row in doc.get("accounts") or []
                if row.get("reference_type") or row.get("reference_name")
            ]
            if referenced:
                result.append({**candidate, "references": referenced})
        return result

    def repair_one(self, candidate: dict) -> dict:
        name = candidate["name"]
        references = candidate["references"]
        before_gl = self._gl_rows(name)
        before_signature = financial_signature(before_gl)
        selections = unreconcile_selections(
            get_config().erpnext["company"], name, references)
        if self.erp.dry_run:
            return {"journal_entry": name, "status": "planned",
                    "references": len(selections)}

        self.erp._write(
            "POST",
            "/api/method/erpnext.accounts.doctype.unreconcile_payment."
            "unreconcile_payment.create_unreconcile_doc_for_selection",
            json={"selections": json.dumps(selections)},
        )

        after = self._get_doc(name)
        remaining = [row for row in after.get("accounts") or []
                     if row.get("reference_type") or row.get("reference_name")]
        if remaining:
            raise RuntimeError(f"{name}: invoice reference remains after unlink")
        after_gl = self._gl_rows(name)
        if financial_signature(after_gl) != before_signature:
            raise RuntimeError(f"{name}: debit/credit GL identity changed during unlink")
        false_against = [
            row for row in after_gl
            if any(row.get("against_voucher_type") == ref["reference_type"]
                   and row.get("against_voucher") == ref["reference_name"]
                   for ref in references)
        ]
        if false_against:
            raise RuntimeError(f"{name}: derived GL reference remains after unlink")
        return {"journal_entry": name, "status": "repaired",
                "references": len(selections)}

    def run(self, names: list[str] | None = None) -> dict:
        planned = self.preview(names)
        results = []
        for candidate in planned:
            try:
                results.append(self.repair_one(candidate))
            except Exception as exc:
                detail = getattr(exc, "body", None) or str(exc)
                results.append({"journal_entry": candidate["name"],
                                "status": "error", "error": detail[:4000]})
                break  # fail closed; never continue after a partial failure
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "dry-run" if self.erp.dry_run else "live",
            "candidate_count": len(planned),
            "candidates": planned,
            "results": results,
            "pass": len(results) == len(planned)
                    and all(row["status"] != "error" for row in results),
        }
        report = get_config().staging_db.parent / "reports" / "party_bridge_repair.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        payload["report"] = str(report)
        return payload
