from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from t2e.evidence_payment_reconciliation import (
    EvidencePaymentReconciler, load_plan,
)


class EvidencePaymentReconciliationTests(unittest.TestCase):
    def test_plan_requires_unchanged_source_export(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "state.json"
            source.write_text("{}", encoding="utf-8")
            plan = Path(directory) / "plan.json"
            plan.write_text(json.dumps({
                "mode": "read-only",
                "policy": "evidence-backed non-Tally allocation review",
                "safe_to_apply_automatically": False,
                "company": "Test Company",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_state": str(source),
                "source_state_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "candidates": [],
            }), encoding="utf-8")
            load_plan(plan, "Test Company")
            source.write_text('{"changed": true}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after planning"):
                load_plan(plan, "Test Company")

    def test_selection_requires_exact_high_confidence_pair(self):
        reconciler = EvidencePaymentReconciler.__new__(EvidencePaymentReconciler)
        reconciler.plan = {"candidates": [{
            "payment_name": "PAY-1", "invoice_name": "PINV-1",
            "confidence": "high",
        }]}
        self.assertEqual(
            reconciler.select("PAY-1", "PINV-1")["confidence"], "high")
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            reconciler.select("PAY-X", "PINV-1")

    def test_weaker_pair_requires_explicit_acknowledgement(self):
        reconciler = EvidencePaymentReconciler.__new__(EvidencePaymentReconciler)
        reconciler.plan = {"candidates": [{
            "payment_name": "PAY-2", "invoice_name": "PINV-2",
            "confidence": "review",
        }, {
            "payment_name": "PAY-3", "invoice_name": "PINV-3",
            "confidence": "manual",
        }]}
        with self.assertRaisesRegex(RuntimeError, "weaker-evidence"):
            reconciler.select("PAY-2", "PINV-2")
        self.assertEqual(
            reconciler.select(
                "PAY-2", "PINV-2", acknowledge_weaker_evidence=True
            )["confidence"],
            "review",
        )
        self.assertEqual(
            reconciler.select(
                "PAY-3", "PINV-3", acknowledge_weaker_evidence=True
            )["confidence"],
            "manual",
        )

    def test_live_pair_is_narrowed_to_one_payment_and_invoice(self):
        class FakeERP:
            def run_doc_method(self, method, doc, args=None):
                if method == "get_unreconciled_entries":
                    return {**doc, "invoices": [{
                        "invoice_number": "PINV-1", "outstanding_amount": 4720,
                    }], "payments": [{
                        "reference_type": "Payment Entry",
                        "reference_name": "PAY-1", "amount": 4720,
                    }]}
                if method == "allocate_entries":
                    self.args = args
                    return {**doc, "allocation": [{
                        "invoice_number": "PINV-1",
                        "reference_type": "Payment Entry",
                        "reference_name": "PAY-1", "allocated_amount": 4720,
                    }]}
                raise AssertionError(method)

        reconciler = EvidencePaymentReconciler.__new__(EvidencePaymentReconciler)
        reconciler.erp = FakeERP()
        reconciler.defaults = SimpleNamespace(
            name="Test Company", payable="Creditors - X", receivable="Debtors - X")
        candidate = {
            "party_type": "Supplier", "party": "Supplier A",
            "account": "Creditors - X", "invoice_name": "PINV-1",
            "payment_name": "PAY-1", "unallocated_amount": "4720.00",
        }
        _, allocation = reconciler._plan_live_pair(candidate)
        self.assertEqual(allocation["allocated_amount"], 4720)
        self.assertEqual(len(reconciler.erp.args["invoices"]), 1)
        self.assertEqual(len(reconciler.erp.args["payments"]), 1)


if __name__ == "__main__":
    unittest.main()
