from decimal import Decimal
from types import SimpleNamespace
import unittest

from tools.frappe_repair_invoice_paise_differences import (
    item_igst_amounts, source_turnover,
)


class InvoicePaiseRepairTests(unittest.TestCase):
    def test_multi_item_igst_uses_half_up_per_source_line(self):
        items = [
            SimpleNamespace(net_amount="2637991.71"),
            SimpleNamespace(net_amount="30437.20"),
            SimpleNamespace(net_amount="19023.25"),
            SimpleNamespace(net_amount="1521.86"),
        ]
        self.assertEqual(
            item_igst_amounts(items),
            [Decimal("474838.51"), Decimal("5478.70"),
             Decimal("3424.19"), Decimal("273.93")],
        )
        self.assertEqual(sum(item_igst_amounts(items)), Decimal("484015.33"))

    def test_negative_rounding_increases_source_turnover_above_party_total(self):
        source = {
            "net_total": Decimal("2688974.02"),
            "igst": Decimal("484015.33"),
            "rounding_adjustment": Decimal("-0.35"),
        }
        self.assertEqual(source_turnover(source), Decimal("3172989.35"))


if __name__ == "__main__":
    unittest.main()
