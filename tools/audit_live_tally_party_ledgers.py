"""Read-only live Tally audit of ledger parent and bill-wise configuration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests

from t2e.tally_client import sanitize_xml


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:9000")
    parser.add_argument("--company", required=True)
    parser.add_argument("--parties", required=True, help="JSON audit containing invoices")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    audit = json.loads(Path(args.parties).read_text(encoding="utf-8"))
    wanted = {norm(row["party"]): row["party"] for row in audit["invoices"]}
    envelope = (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>Collection</TYPE><ID>T2EPartyLedgerAudit</ID></HEADER><BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{escape(args.company)}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>"
        "<TDL><TDLMESSAGE><COLLECTION NAME=\"T2EPartyLedgerAudit\" "
        "ISMODIFY=\"No\" ISFIXED=\"No\"><TYPE>Ledger</TYPE>"
        "<NATIVEMETHOD>Name</NATIVEMETHOD><NATIVEMETHOD>Parent</NATIVEMETHOD>"
        "<NATIVEMETHOD>IsBillWiseOn</NATIVEMETHOD>"
        "<NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>"
        "<NATIVEMETHOD>ClosingBalance</NATIVEMETHOD>"
        "</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    response = requests.post(
        args.url,
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        timeout=180,
    )
    response.raise_for_status()
    root = ET.fromstring(sanitize_xml(response.text))
    rows = []
    for element in root.findall(".//LEDGER"):
        name = (element.get("NAME") or element.findtext("NAME") or "").strip()
        if norm(name) not in wanted:
            continue
        rows.append({
            "name": name,
            "parent": (element.findtext("PARENT") or "").strip(),
            "is_bill_wise_on": (element.findtext("ISBILLWISEON") or "").strip(),
            "opening_balance": (element.findtext("OPENINGBALANCE") or "").strip(),
            "closing_balance": (element.findtext("CLOSINGBALANCE") or "").strip(),
        })
    found = {norm(row["name"]) for row in rows}
    payload = {
        "mode": "read-only-live-tally",
        "url": args.url,
        "company": args.company,
        "requested": len(wanted),
        "found": len(rows),
        "missing": [name for key, name in wanted.items() if key not in found],
        "ledgers": sorted(rows, key=lambda row: norm(row["name"])),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "company": args.company,
        "requested": payload["requested"],
        "found": payload["found"],
        "missing": len(payload["missing"]),
        "bill_wise": {
            value: sum(norm(row["is_bill_wise_on"]) == norm(value) for row in rows)
            for value in ("Yes", "No")
        },
        "report": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
