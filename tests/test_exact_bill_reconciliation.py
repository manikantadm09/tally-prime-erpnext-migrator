from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from t2e.exact_bill_reconciliation import (
    ExactBillReconciler,
    load_and_validate_plan,
)
from tools.verify_invoice_outstandings_api import classify_difference


class ExactBillReconciliationTests(unittest.TestCase):
    def test_outstanding_difference_classifications(self):
        from decimal import Decimal
        self.assertEqual(classify_difference(Decimal("10"), False),
                         "requires_exact_allocation")
        self.assertEqual(classify_difference(Decimal("10"), True),
                         "erpnext_return_reconciliation_turnover_exception")
        self.assertEqual(classify_difference(Decimal("-10"), False),
                         "source_bill_vs_gl_exception")

    def _plan_files(self):
        temp = tempfile.TemporaryDirectory()
        source = Path(temp.name) / "source.json"
        source.write_text('{"fresh": true}', encoding="utf-8")
        plan = Path(temp.name) / "plan.json"
        payload = {
            "mode": "read-only",
            "safe_to_apply": True,
            "residual": "0.00",
            "company": "Test Company",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_report": str(source),
            "source_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "details": [],
        }
        plan.write_text(json.dumps(payload), encoding="utf-8")
        return temp, source, plan

    def test_plan_is_bound_to_unchanged_source_report(self):
        temp, source, plan = self._plan_files()
        self.addCleanup(temp.cleanup)
        self.assertEqual(
            load_and_validate_plan(plan, "Test Company")["residual"], "0.00")
        source.write_text('{"fresh": false}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            load_and_validate_plan(plan, "Test Company")

    def test_party_start_fails_closed_on_invoice_drift(self):
        reconciler = ExactBillReconciler.__new__(ExactBillReconciler)
        party = {
            "targets": {
                "PINV-1": {
                    "starting_outstanding": "100.00",
                    "planned": "60.00",
                }
            },
            "allocations": [{
                "invoice_number": "PINV-1",
                "reference_type": "Payment Entry",
                "reference_name": "PAY-1",
                "allocated_amount": 60,
            }],
        }
        doc = {
            "invoices": [{"invoice_number": "PINV-1", "outstanding_amount": 99}],
            "payments": [{
                "reference_type": "Payment Entry", "reference_name": "PAY-1",
                "amount": 60,
            }],
        }
        with self.assertRaisesRegex(RuntimeError, "invoice drift"):
            reconciler._validate_party_start(party, doc)

    def test_dry_run_allocates_only_reviewed_invoice_payment_pair(self):
        class FakeERP:
            dry_run = True

            def __init__(self):
                self.calls = []

            def run_doc_method(self, method, doc, args=None):
                self.calls.append((method, args))
                if method == "get_unreconciled_entries":
                    return {
                        **doc,
                        "invoices": [{
                            "invoice_number": "PINV-1", "outstanding_amount": 100,
                            "amount": 100,
                        }],
                        "payments": [{
                            "reference_type": "Payment Entry",
                            "reference_name": "PAY-1", "amount": 80,
                            "unreconciled_amount": 80,
                        }],
                    }
                if method == "allocate_entries":
                    invoice = args["invoices"][0]
                    payment = args["payments"][0]
                    return {
                        **doc,
                        "allocation": [{
                            "invoice_number": invoice["invoice_number"],
                            "reference_type": payment["reference_type"],
                            "reference_name": payment["reference_name"],
                            "allocated_amount": invoice["amount"],
                        }],
                    }
                raise AssertionError(f"unexpected method {method}")

        reconciler = ExactBillReconciler.__new__(ExactBillReconciler)
        reconciler.erp = FakeERP()
        reconciler.defaults = SimpleNamespace(
            name="Test Company", receivable="Debtors", payable="Creditors")
        party = {"party_type": "Supplier", "party": "Supplier A"}
        allocation = {
            "invoice_number": "PINV-1", "reference_type": "Payment Entry",
            "reference_name": "PAY-1", "allocated_amount": 60,
        }
        self.assertEqual(reconciler._apply_allocation(party, allocation), 60)
        self.assertEqual([call[0] for call in reconciler.erp.calls],
                         ["get_unreconciled_entries", "allocate_entries"])
        allocate_args = reconciler.erp.calls[1][1]
        self.assertEqual(allocate_args["invoices"][0]["amount"], 60.0)
        self.assertEqual(allocate_args["payments"][0]["amount"], 80)

    def test_return_invoice_source_is_refused(self):
        reconciler = ExactBillReconciler.__new__(ExactBillReconciler)
        with self.assertRaisesRegex(RuntimeError, "artificial GL turnover"):
            reconciler._apply_allocation(
                {"party_type": "Supplier", "party": "Supplier A"},
                {"reference_type": "Purchase Invoice", "reference_name": "PRET-1"},
            )

    def test_planner_keeps_only_payment_and_journal_sources(self):
        from tools.plan_exact_bill_allocations_api import is_settlement_source
        self.assertTrue(is_settlement_source(("Payment Entry", "PAY-1")))
        self.assertTrue(is_settlement_source(("Journal Entry", "JV-1")))
        self.assertFalse(is_settlement_source(("Purchase Invoice", "PRET-1")))
        self.assertFalse(is_settlement_source(("Sales Invoice", "SRET-1")))


if __name__ == "__main__":
    unittest.main()
