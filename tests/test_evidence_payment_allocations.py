import unittest

from tools.plan_evidence_payment_allocations import build_plan


def invoice(name="PINV-1", amount="4720.00", party="Supplier A",
            posting_date="2026-05-05", guid="INV-GUID"):
    return {
        "doctype": "Purchase Invoice", "name": name, "tally_guid": guid,
        "posting_date": posting_date, "outstanding_amount": amount,
        "is_return": 0, "party": party, "account": "Creditors - X",
    }


def state(invoices=None, payments=None, payment_ledger=None):
    return {
        "site": "test.local", "company": "Test Company",
        "invoices": invoices or [], "transactions": payments or [],
        "payment_ledger": payment_ledger or [],
    }


def payment(name="PAY-1", party="Supplier A"):
    return {
        "doctype": "Payment Entry", "name": name, "party_type": "Supplier",
        "party": party,
    }


def ple(name="PAY-1", amount="-4720.00", party="Supplier A",
        posting_date="2026-05-05"):
    return {
        "name": f"PLE-{name}", "account": "Creditors - X",
        "party": party, "voucher_type": "Payment Entry", "voucher_no": name,
        "against_voucher_type": "Payment Entry", "against_voucher_no": name,
        "posting_date": posting_date, "amount": amount,
        "amount_in_account_currency": amount,
    }


def source(name="PAY-1", bill_types=None, narration="TOWARDS DUES PAID"):
    return {name: {
        "guid": "PAY-GUID", "vtype": "Payment", "vnumber": "149",
        "vdate": "2026-05-05", "party": "Supplier A",
        "bill_types": bill_types or ["Advance"], "bill_names": ["149"],
        "narration": narration,
    }}


class EvidencePaymentAllocationTests(unittest.TestCase):
    def test_same_day_mutually_unique_advance_is_high_confidence(self):
        plan = build_plan(
            state([invoice()], [payment()], [ple()]), source(),
            {"details": [{"source_guids": ["INV-GUID"],
                           "expected_erp_outstanding": "4720.00"}]},
        )
        self.assertEqual(plan["summary"]["mutually_unique_exact_candidates"], 1)
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["confidence"], "high")
        self.assertEqual(candidate["tally_bill_status_effect"], "would_disagree")
        self.assertTrue(candidate["would_disagree_with_tally_bill_status"])
        self.assertFalse(plan["safe_to_apply_automatically"])

    def test_explicit_agst_ref_is_excluded(self):
        plan = build_plan(
            state([invoice()], [payment()], [ple()]),
            source(bill_types=["Agst Ref"]),
        )
        self.assertEqual(plan["candidates"], [])

    def test_multiple_equal_invoices_fail_closed(self):
        plan = build_plan(
            state(
                [invoice("PINV-1", guid="I1"), invoice("PINV-2", guid="I2")],
                [payment()], [ple()],
            ),
            source(),
        )
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["summary"]["rejected"]["multiple_equal_invoices"], 1)

    def test_multiple_equal_payments_fail_closed(self):
        plan = build_plan(
            state(
                [invoice()], [payment("PAY-1"), payment("PAY-2")],
                [ple("PAY-1"), ple("PAY-2")],
            ),
            {
                **source("PAY-1"),
                **source("PAY-2"),
            },
        )
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["summary"]["rejected"]["multiple_equal_payments"], 2)

    def test_account_mismatch_does_not_match(self):
        row = ple()
        row["account"] = "Other Creditors - X"
        plan = build_plan(state([invoice()], [payment()], [row]), source())
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["summary"]["rejected"]["no_exact_open_invoice"], 1)


if __name__ == "__main__":
    unittest.main()
