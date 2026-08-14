import json
from pathlib import Path
import tempfile
import unittest

from tools.frappe_repair_invoice_gst_tax_types import (
    manifest_rows,
    row_key,
    tax_child_doctype,
)


class InvoiceGstTaxTypeRepairTests(unittest.TestCase):
    def test_manifest_accepts_only_source_proven_missing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "audit.json"
            path.write_text(json.dumps({
                "mode": "read-only", "company": "Company A", "details": [
                    {"doctype": "Sales Invoice", "name": "SI-1", "guid": "g1",
                     "gst_breakup_classification": "correct",
                     "gst_tax_type_classification": "missing",
                     "gst_tax_type_rows": [{"expected_gst_tax_type": "cgst"}]},
                    {"doctype": "Sales Invoice", "name": "SI-2", "guid": "g2",
                     "gst_breakup_classification": "mismatch",
                     "gst_tax_type_classification": "missing",
                     "gst_tax_type_rows": [{"expected_gst_tax_type": "cgst"}]},
                ],
            }), encoding="utf-8")
            rows = manifest_rows(path, "Company A", 1)
            self.assertEqual([row["name"] for row in rows], ["SI-1"])

    def test_row_key_preserves_paise_and_type(self):
        self.assertEqual(
            row_key("CGST - X", "CGST", 1, "cgst"),
            ("CGST - X", "CGST", "1.00", "cgst"),
        )

    def test_tax_child_doctype_uses_erpnext_child_table_names(self):
        self.assertEqual(tax_child_doctype("Sales Invoice"), "Sales Taxes and Charges")
        self.assertEqual(tax_child_doctype("Purchase Invoice"), "Purchase Taxes and Charges")


if __name__ == "__main__":
    unittest.main()
