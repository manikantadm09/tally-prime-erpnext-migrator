from types import SimpleNamespace
import unittest

from tools.frappe_repair_uat_invoice_gst_display import plan_doc


class _Row(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def _item(**values):
    return _Row(**values)


def _tax(**values):
    return _Row(**values)


def _doc(items, taxes, grand_total=100, outstanding=100):
    return SimpleNamespace(
        items=items, taxes=taxes,
        grand_total=grand_total, outstanding_amount=outstanding,
    )


class UatInvoiceGstDisplayTests(unittest.TestCase):
    def test_duplicate_same_rate_cgst_rows_are_stamped(self):
        doc = _doc(
            [_item(name="i1", net_amount=25126.8, gst_treatment="Nil-Rated",
                   cgst_rate=0, cgst_amount=0, sgst_rate=0, sgst_amount=0)],
            [
                _tax(name="t1", description="CGST INPUT @ 9%",
                     account_head="CGST INPUT @ 9% - SDL", tax_amount=2261.41),
                _tax(name="t2", description="CGST INPUT @ 9%",
                     account_head="CGST INPUT @ 9% - SDL", tax_amount=2261.41),
            ],
            grand_total=29649.62, outstanding=29649.62,
        )
        plan = plan_doc(doc)
        self.assertEqual(plan["status"], "stamp")
        self.assertEqual(plan["kinds"], {"cgst": 9.0})
        self.assertEqual(plan["items"][0]["values"]["cgst_rate"], 9.0)
        self.assertEqual(plan["items"][0]["values"]["cgst_amount"], 4522.82)
        self.assertEqual(len(plan["taxes"]), 2)

    def test_unclaimed_gst_uses_implied_standard_slab(self):
        doc = _doc(
            [_item(name="i1", net_amount=17812.50, gst_treatment="Nil-Rated",
                   cgst_rate=0, cgst_amount=0, sgst_rate=0, sgst_amount=0)],
            [
                _tax(name="t1", description="Unclaimed CGST",
                     account_head="Unclaimed CGST - SDL", tax_amount=2493.75),
                _tax(name="t2", description="Unclaimed SGST",
                     account_head="Unclaimed SGST - SDL", tax_amount=2493.75),
            ],
            grand_total=22800, outstanding=22800,
        )
        plan = plan_doc(doc)
        self.assertEqual(plan["status"], "stamp")
        self.assertEqual(plan["kinds"], {"cgst": 14.0, "sgst": 14.0})
        self.assertEqual(plan["items"][0]["values"]["cgst_rate"], 14.0)

    def test_tax_as_item_igst_stamps_goods_only(self):
        doc = _doc(
            [
                _item(name="g1", item_name="GST PURCHASE", net_amount=16948.31,
                      gst_treatment="Non-GST", igst_rate=0, igst_amount=0,
                      expense_account="GST PURCHASE - SDL"),
                _item(name="t1", item_name="IGST INPUT @ 18 %", net_amount=3050.69,
                      gst_treatment="Non-GST", igst_rate=0, igst_amount=0,
                      expense_account="IGST INPUT @ 18 % - SDL"),
            ],
            [],
            grand_total=19999, outstanding=19999,
        )
        plan = plan_doc(doc)
        self.assertEqual(plan["status"], "stamp")
        self.assertEqual(plan["kinds"], {"igst": 18.0})
        self.assertEqual([row["name"] for row in plan["items"]], ["g1"])
        self.assertEqual(plan["items"][0]["values"]["igst_rate"], 18.0)
        self.assertEqual(plan["items"][0]["values"]["igst_amount"], 3050.69)
        self.assertEqual(plan["taxes"], [])

    def test_two_cgst_rates_still_skip(self):
        doc = _doc(
            [_item(name="i1", net_amount=1000, gst_treatment="Nil-Rated",
                   cgst_rate=0, cgst_amount=0)],
            [
                _tax(name="t1", description="CGST INPUT @ 9%",
                     account_head="CGST 9", tax_amount=90),
                _tax(name="t2", description="CGST INPUT @ 6%",
                     account_head="CGST 6", tax_amount=60),
            ],
        )
        self.assertEqual(plan_doc(doc)["reason"], "multi_rate_or_unparsed")

    def test_split_multi_rate_is_not_collapsed_to_implied_slab(self):
        doc = _doc(
            [
                _item(name="i1", net_amount=38.11, gst_treatment="Taxable",
                      cgst_rate=2.5, cgst_amount=0.95, sgst_rate=2.5, sgst_amount=0.95),
                _item(name="i2", net_amount=6220.33, gst_treatment="Taxable",
                      cgst_rate=9, cgst_amount=559.83, sgst_rate=9, sgst_amount=559.83),
            ],
            [
                _tax(name="t1", description="CGST INPUT @ 9%",
                     account_head="CGST 9", tax_amount=559.83),
                _tax(name="t2", description="SGST INPUT @ 9%",
                     account_head="SGST 9", tax_amount=559.83),
                _tax(name="t3", description="CGST INPUT @2.5%",
                     account_head="CGST 2.5", tax_amount=0.95),
                _tax(name="t4", description="SGST INPUT@2.5%",
                     account_head="SGST 2.5", tax_amount=0.95),
            ],
        )
        self.assertEqual(plan_doc(doc)["reason"], "multi_rate_or_unparsed")


if __name__ == "__main__":
    unittest.main()
