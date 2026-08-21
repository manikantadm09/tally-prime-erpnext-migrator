"""Restore Tally tax-ledger names onto invoice tax-row descriptions.

Changes description text when the tax amount already matches Tally.
Does not change tax_amount, account_head, or GL.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path

from t2e.lines import is_tax_ledger, parse_entries
from t2e.staging import Staging


ALLOWED_SITES = ("dev.spaceki.com", "erp.spaceki.com")
PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def discover_sites_path(site: str, cwd: Path) -> Path:
    for candidate in (cwd / "sites", cwd):
        if (candidate / site / "site_config.json").is_file():
            return candidate
    raise RuntimeError(f"Cannot locate site {site!r} below {cwd}")


def expected_tax_names(payload: str, vtype: str) -> dict[tuple[str, str], str]:
    """(norm_name, amount) -> original Tally ledger name."""
    sign = -1 if vtype in ("Credit Note", "Debit Note") else 1
    out: dict[tuple[str, str], str] = {}
    for entry in parse_entries(json.loads(payload)):
        if not is_tax_ledger(entry["ledger"]):
            continue
        amount = f"{sign * money(entry['mag']):.2f}"
        out[(norm(entry["ledger"]), amount)] = entry["ledger"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.site not in ALLOWED_SITES:
        raise SystemExit(f"bound to {ALLOWED_SITES}, not {args.site}")

    import frappe

    original = Path.cwd()
    sites_path = discover_sites_path(args.site, original)
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    store = Staging()
    planned = []
    try:
        if not frappe.db.exists("Company", args.company):
            raise RuntimeError(args.company)
        for row in store.conn.execute(
            "SELECT guid, vtype, payload FROM voucher "
            "WHERE vtype IN ('Sales','Purchase','Credit Note','Debit Note')"
        ):
            expected = expected_tax_names(row["payload"], row["vtype"])
            if not expected:
                continue
            doctype = (
                "Sales Invoice"
                if row["vtype"] in ("Sales", "Credit Note")
                else "Purchase Invoice"
            )
            name = frappe.db.get_value(
                doctype, {"tally_guid": row["guid"], "docstatus": 1}, "name")
            if not name:
                continue
            doc = frappe.get_doc(doctype, name)
            if doc.company != args.company:
                continue
            child = "Sales Taxes and Charges" if doctype == "Sales Invoice" else (
                "Purchase Taxes and Charges")
            for tax in doc.taxes or []:
                amount = f"{money(tax.tax_amount):.2f}"
                current = tax.description or tax.account_head or ""
                tally_name = expected.get((norm(current), amount))
                if not tally_name:
                    # amount-matched IGST alias: description collapsed to "IGST"
                    for (nname, namt), original_name in expected.items():
                        if namt == amount and nname.startswith("igst") and norm(current) == "igst":
                            tally_name = original_name
                            break
                if not tally_name or tally_name == current:
                    continue
                planned.append({
                    "doctype": doctype,
                    "name": name,
                    "child": child,
                    "tax_row": tax.name,
                    "from": current,
                    "to": tally_name,
                    "amount": amount,
                })
                if args.confirm:
                    frappe.db.set_value(
                        child, tax.name, "description", tally_name,
                        update_modified=False)
        if args.confirm:
            frappe.db.commit()
    finally:
        store.close()
        frappe.destroy()
        os.chdir(original)

    report = {
        "site": args.site,
        "company": args.company,
        "mode": "applied" if args.confirm else "plan",
        "rows": len(planned),
        "details": planned,
    }
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: report[k] for k in ("site", "mode", "rows")}, indent=2))
    print(f"REPORT {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
