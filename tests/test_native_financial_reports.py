from pathlib import Path
import tempfile
import unittest

from tools.verify_native_financial_reports_api import (
    parse_balance_sheet,
    parse_profit_and_loss,
    parse_trial_balance,
)


class NativeFinancialReportParserTests(unittest.TestCase):
    def _xml(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_balance_sheet_signed_sides(self):
        path = self._xml("""<ENVELOPE>
        <BSNAME><DSPACCNAME><DSPDISPNAME>Liability</DSPDISPNAME></DSPACCNAME></BSNAME>
        <BSAMT><BSMAINAMT>10</BSMAINAMT></BSAMT>
        <BSNAME><DSPACCNAME><DSPDISPNAME>Asset</DSPDISPNAME></DSPACCNAME></BSNAME>
        <BSAMT><BSMAINAMT>-10</BSMAINAMT></BSAMT></ENVELOPE>""")
        parsed = parse_balance_sheet(path)
        self.assertEqual(parsed["liability_side_total"], "10.00")
        self.assertEqual(parsed["asset_side_total"], "10.00")

    def test_profit_loss_uses_main_totals_not_subtotals(self):
        path = self._xml("""<ENVELOPE>
        <DSPACCNAME><DSPDISPNAME>Income</DSPDISPNAME></DSPACCNAME>
        <PLAMT><PLSUBAMT>999</PLSUBAMT><BSMAINAMT>15</BSMAINAMT></PLAMT>
        <DSPACCNAME><DSPDISPNAME>Expense</DSPDISPNAME></DSPACCNAME>
        <PLAMT><BSMAINAMT>-9</BSMAINAMT></PLAMT></ENVELOPE>""")
        parsed = parse_profit_and_loss(path)
        self.assertEqual(parsed["income"], "15.00")
        self.assertEqual(parsed["expense"], "9.00")
        self.assertEqual(parsed["profit"], "6.00")

    def test_trial_balance_sums_both_sides(self):
        path = self._xml("""<ENVELOPE>
        <DSPACCNAME><DSPDISPNAME>A</DSPDISPNAME></DSPACCNAME>
        <DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA>-5</DSPCLDRAMTA></DSPCLDRAMT>
        <DSPCLCRAMT><DSPCLCRAMTA>2</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO>
        <DSPACCNAME><DSPDISPNAME>B</DSPDISPNAME></DSPACCNAME>
        <DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA>-3</DSPCLDRAMTA></DSPCLDRAMT>
        <DSPCLCRAMT><DSPCLCRAMTA>6</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO>
        </ENVELOPE>""")
        parsed = parse_trial_balance(path)
        self.assertEqual(parsed["closing_debit"], "8.00")
        self.assertEqual(parsed["closing_credit"], "8.00")


if __name__ == "__main__":
    unittest.main()
