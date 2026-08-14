import json
from pathlib import Path
import tempfile
import unittest

from tools.frappe_repair_invoice_place_of_supply import (
    discover_sites_path,
    manifest_rows,
)


class InvoicePlaceOfSupplyRepairTests(unittest.TestCase):
    def test_manifest_selects_only_exact_company_mismatches(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "audit.json"
            path.write_text(json.dumps({
                "mode": "read-only",
                "company": "Company A",
                "details": [
                    {"doctype": "Purchase Invoice", "name": "PI-1", "guid": "g1",
                     "expected_place_of_supply": "24-Gujarat",
                     "actual_place_of_supply": "29-Karnataka"},
                    {"doctype": "Purchase Invoice", "name": "PI-2", "guid": "g2",
                     "expected_place_of_supply": "29-Karnataka",
                     "actual_place_of_supply": "29-Karnataka"},
                ],
            }), encoding="utf-8")
            rows = manifest_rows(path, "Company A", 1)
            self.assertEqual([row["name"] for row in rows], ["PI-1"])

    def test_sites_path_discovery_accepts_bench_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            site = root / "sites" / "example.local"
            site.mkdir(parents=True)
            (site / "site_config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(discover_sites_path("example.local", root), root / "sites")


if __name__ == "__main__":
    unittest.main()
