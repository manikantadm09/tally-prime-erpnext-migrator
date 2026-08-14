import unittest

from tools.audit_invoice_tax_structure_api import (
    gst_tax_type_classification,
    gst_tax_type_rows,
)


class InvoiceTaxAuditTests(unittest.TestCase):
    def test_blank_tax_types_are_reported_as_missing(self):
        rows = gst_tax_type_rows({"taxes": [
            {"account_head": "CGST INPUT @ 9% - SDL", "tax_amount": 360},
            {"account_head": "SGST INPUT @ 9% - SDL", "tax_amount": 360},
        ]})
        self.assertEqual([row["expected_gst_tax_type"] for row in rows], ["cgst", "sgst"])
        self.assertEqual(gst_tax_type_classification(rows), "missing")

    def test_matching_tax_types_are_correct(self):
        rows = gst_tax_type_rows({"taxes": [
            {"account_head": "IGST INPUT @ 18% - SDL", "tax_amount": 18,
             "gst_tax_type": "igst"},
        ]})
        self.assertEqual(gst_tax_type_classification(rows), "correct")


if __name__ == "__main__":
    unittest.main()
