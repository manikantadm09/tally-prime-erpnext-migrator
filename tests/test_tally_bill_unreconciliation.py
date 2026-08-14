from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from t2e.tally_bill_unreconciliation import load_and_validate_plan


class TallyBillUnreconciliationTests(unittest.TestCase):
    def _files(self):
        temp = tempfile.TemporaryDirectory()
        source = Path(temp.name) / "source.json"
        source.write_text('{"fresh": true}', encoding="utf-8")
        plan = Path(temp.name) / "plan.json"
        payload = {
            "mode": "read-only",
            "safe_to_apply": True,
            "company": "Test Company",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_report": str(source),
            "source_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "selections": [{
                "voucher_type": "Payment Entry", "voucher_no": "PAY-1",
                "against_voucher_type": "Sales Invoice",
                "against_voucher_no": "SINV-1", "allocated_amount": "10.00",
            }],
        }
        plan.write_text(json.dumps(payload), encoding="utf-8")
        return temp, source, plan

    def test_plan_is_bound_to_source_report(self):
        temp, source, plan = self._files()
        self.addCleanup(temp.cleanup)
        self.assertEqual(
            load_and_validate_plan(plan, "Test Company")["selections"][0]["voucher_no"],
            "PAY-1",
        )
        source.write_text('{"fresh": false}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            load_and_validate_plan(plan, "Test Company")

    def test_unsafe_plan_is_refused(self):
        temp, _, plan = self._files()
        self.addCleanup(temp.cleanup)
        payload = json.loads(plan.read_text())
        payload["safe_to_apply"] = False
        plan.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "not a safe"):
            load_and_validate_plan(plan, "Test Company")


if __name__ == "__main__":
    unittest.main()
