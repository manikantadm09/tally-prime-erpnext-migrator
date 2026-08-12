from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
import unittest

from tools.frappe_repair_return_bill_references import (
    REPAIRS,
    _actual_reference_map,
    discover_sites_path,
    money,
    repair_total,
)


class ReturnBillReferenceRepairTests(unittest.TestCase):
    def test_manifest_is_internally_balanced(self):
        returns = set()
        targets = set()
        for repair in REPAIRS:
            self.assertNotIn(repair["return"], returns)
            returns.add(repair["return"])
            allocations = sum(
                (money(row["amount"]) for row in repair["allocations"]),
                Decimal("0.00"),
            )
            self.assertEqual(
                allocations, abs(money(repair["return_outstanding_before"])))
            for row in repair["allocations"]:
                self.assertNotIn(row["target"], targets)
                targets.add(row["target"])
                self.assertEqual(
                    money(row["target_before"]) - money(row["amount"]),
                    money(row["target_after"]),
                )

        self.assertEqual(len(returns), 5)
        self.assertEqual(len(targets), 6)
        self.assertEqual(repair_total(), Decimal("1742330.00"))

    def test_shared_return_is_split_to_exact_tally_amounts(self):
        shared = next(row for row in REPAIRS if row["return"] == "PRET-26-00010")
        self.assertEqual(
            [(row["target"], row["amount"]) for row in shared["allocations"]],
            [("PINV-26-01381", "124.00"),
             ("PINV-26-01398", "16107.00")],
        )

    def test_reference_map_aggregates_duplicate_rows(self):
        rows = [
            SimpleNamespace(
                against_voucher_type="Purchase Invoice",
                against_voucher_no="PINV-1",
                amount_in_account_currency=-10,
            ),
            SimpleNamespace(
                against_voucher_type="Purchase Invoice",
                against_voucher_no="PINV-1",
                amount_in_account_currency=-2.5,
            ),
        ]
        self.assertEqual(
            _actual_reference_map(rows),
            {("Purchase Invoice", "PINV-1"): Decimal("-12.50")},
        )

    def test_sites_path_discovery_accepts_bench_root_or_sites_dir(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site_dir = root / "sites" / "test.local"
            site_dir.mkdir(parents=True)
            (site_dir / "site_config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(discover_sites_path("test.local", root), root / "sites")
            self.assertEqual(
                discover_sites_path("test.local", root / "sites"), root / "sites")


if __name__ == "__main__":
    unittest.main()
