from decimal import Decimal
import unittest

import json

from t2e.repair_fallback_invoices import (
    FallbackRepairError, _row_for_invoice_build, gl_signature,
)


class FallbackInvoiceRepairTests(unittest.TestCase):
    def test_gl_signature_combines_invoice_and_bridge(self):
        source = [[
            {"account": "Expense", "debit": 100, "credit": 0},
            {"account": "Advance", "party_type": "Supplier", "party": "A",
             "debit": 0, "credit": 100},
        ]]
        replacement = [[
            {"account": "Expense", "debit": 100, "credit": 0},
            {"account": "Creditors", "party_type": "Supplier", "party": "A",
             "debit": 0, "credit": 100},
        ], [
            {"account": "Creditors", "party_type": "Supplier", "party": "A",
             "debit": 100, "credit": 0},
            {"account": "Advance", "party_type": "Supplier", "party": "A",
             "debit": 0, "credit": 100},
        ]]
        self.assertEqual(gl_signature(source), gl_signature(replacement))
        self.assertEqual(
            gl_signature(source)[('Expense', '', '')],
            Decimal('100.00'),
        )

    def test_gl_signature_detects_party_difference(self):
        left = [[{"account": "Creditors", "party_type": "Supplier",
                  "party": "A", "debit": 0, "credit": 10}]]
        right = [[{"account": "Creditors", "party_type": "Supplier",
                   "party": "B", "debit": 0, "credit": 10}]]
        self.assertNotEqual(gl_signature(left), gl_signature(right))

    def test_invalid_source_gstin_requires_explicit_acknowledgement(self):
        row = {"payload": json.dumps({"PARTYGSTIN": "29AACF4867F1ZB"})}
        with self.assertRaises(FallbackRepairError):
            _row_for_invoice_build(row, False)

    def test_invalid_source_gstin_is_omitted_only_from_transient_copy(self):
        row = {"payload": json.dumps({
            "PARTYGSTIN": "29AACF4867F1ZB",
            "NARRATION": "Original narration",
        })}
        transient, invalid = _row_for_invoice_build(row, True)
        source_payload = json.loads(row["payload"])
        transient_payload = json.loads(transient["payload"])
        self.assertEqual(invalid, "29AACF4867F1ZB")
        self.assertEqual(source_payload["PARTYGSTIN"], "29AACF4867F1ZB")
        self.assertNotIn("PARTYGSTIN", transient_payload)
        self.assertIn("29AACF4867F1ZB", transient_payload["NARRATION"])


if __name__ == "__main__":
    unittest.main()
