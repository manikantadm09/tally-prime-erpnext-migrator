"""Fetch one native Tally financial report as XML for read-only diagnosis."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from pathlib import Path
from xml.etree import ElementTree as ET

from t2e.config import get_config
from t2e.tally_client import TallyClient, sanitize_xml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "report",
        choices=[
            "balance-sheet",
            "profit-loss",
            "trial-balance",
            "ledger-balances",
            "bills-receivable",
            "bills-payable",
        ],
    )
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    args = parser.parse_args()
    names = {
        "balance-sheet": "Balance Sheet",
        "profit-loss": "Profit and Loss",
        "trial-balance": "Trial Balance",
        "bills-receivable": "Bills Receivable",
        "bills-payable": "Bills Payable",
    }
    client = TallyClient()
    if args.report == "ledger-balances":
        client.from_date, client.to_date = args.from_date, args.to_date
        root = client.export_collection(
            f"native_ledger_balances_{args.from_date}_{args.to_date}",
            "Ledger",
            methods=["Name", "Parent", "OpeningBalance", "ClosingBalance"],
            dated=True,
            save_as=f"native_ledger_balances_{args.from_date}_{args.to_date}",
        )
        rows = []
        for element in root.findall(".//LEDGER"):
            rows.append(
                {
                    "name": (element.get("NAME") or "").strip(),
                    "parent": (element.findtext("PARENT") or "").strip(),
                    "opening_balance": (
                        element.findtext("OPENINGBALANCE") or ""
                    ).strip(),
                    "closing_balance": (
                        element.findtext("CLOSINGBALANCE") or ""
                    ).strip(),
                }
            )
        path = (
            get_config().staging_db.parent
            / "reports"
            / f"native_ledger_balances_{args.from_date}_{args.to_date}.csv"
        )
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"LEDGERS {len(rows)}")
        print(f"FILE {path}")
        return
    envelope = (
        "<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>"
        "<BODY><EXPORTDATA><REQUESTDESC>"
        f"<REPORTNAME>{names[args.report]}</REPORTNAME>"
        "<STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{client.company}</SVCURRENTCOMPANY>"
        f'<SVFROMDATE TYPE="Date">{args.from_date}</SVFROMDATE>'
        f'<SVTODATE TYPE="Date">{args.to_date}</SVTODATE>'
        "</STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"
    )
    raw = client._post(envelope)
    root = ET.fromstring(sanitize_xml(raw))
    path = (
        get_config().staging_db.parent
        / "raw"
        / f"native_{args.report}_{args.from_date}_{args.to_date}.xml"
    )
    path.write_text(raw, encoding="utf-8")
    tags = Counter(element.tag for element in root.iter())
    amount_elements = []
    for element in root.iter():
        if (
            element.text
            and element.text.strip()
            and any(word in element.tag for word in ("AMT", "BALANCE", "TOTAL"))
        ):
            amount_elements.append((element.tag, element.text.strip()))
    print(f"REPORT {names[args.report]}")
    print(f"BYTES {len(raw)}")
    print(f"TOP_TAGS {tags.most_common(30)}")
    print(f"AMOUNTS {amount_elements[-80:]}")
    print(f"FILE {path}")


if __name__ == "__main__":
    main()
