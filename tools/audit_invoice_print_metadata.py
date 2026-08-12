"""Read-only audit of fields that affect invoice display and printing."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import json
from pathlib import Path
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t2e.erpnext_client import ERPNextClient, ERPNextError  # noqa: E402
from t2e.config import get_config  # noqa: E402

REPORT_DATE = date.today().isoformat()
INVOICE_TYPES = ("Sales Invoice", "Purchase Invoice")


def fetch_doc(doctype: str, name: str) -> dict:
    # A session is not shared across worker threads.
    erp = ERPNextClient(dry_run=True)
    dt = urllib.parse.quote(doctype, safe="")
    nm = urllib.parse.quote(name, safe="")
    return erp._request("GET", f"/api/resource/{dt}/{nm}")["data"]


def blank(value) -> bool:
    return value is None or str(value).strip() == ""


def main() -> int:
    erp = ERPNextClient(dry_run=True)
    company = get_config().erpnext["company"]
    targets = []
    for doctype in INVOICE_TYPES:
        targets.extend(
            (doctype, row["name"])
            for row in erp.get_list(
                doctype,
                fields=["name"],
                filters=[
                    ["company", "=", company],
                    ["docstatus", "=", 1],
                    ["tally_guid", "is", "set"],
                ],
                limit=0,
            )
        )
    docs = []
    errors = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(fetch_doc, doctype, name): (doctype, name)
            for doctype, name in targets
        }
        for future in as_completed(future_map):
            target = future_map[future]
            try:
                docs.append(future.result())
            except Exception as exc:
                errors.append({"doctype": target[0], "name": target[1],
                               "error": str(exc)})

    counts = Counter()
    examples: dict[str, list[dict]] = {}

    def add_example(key: str, doc: dict, extra: dict | None = None):
        bucket = examples.setdefault(key, [])
        if len(bucket) < 12:
            bucket.append({
                "doctype": doc.get("doctype"),
                "name": doc.get("name"),
                "posting_date": doc.get("posting_date"),
                **(extra or {}),
            })

    for doc in docs:
        dt = doc["doctype"]
        counts["documents_fetched"] += 1
        counts[f"{dt}_fetched"] += 1
        if blank(doc.get("company_gstin")):
            counts["blank_company_gstin"] += 1
        else:
            counts["nonblank_company_gstin"] += 1
        if blank(doc.get("place_of_supply")):
            counts["blank_place_of_supply"] += 1
        else:
            counts["nonblank_place_of_supply"] += 1
        if blank(doc.get("tax_category")):
            counts["blank_tax_category"] += 1
        if dt == "Purchase Invoice":
            if blank(doc.get("supplier_gstin")):
                counts["purchase_blank_supplier_gstin"] += 1
            else:
                counts["purchase_nonblank_supplier_gstin"] += 1
            if blank(doc.get("supplier_address")):
                counts["purchase_blank_supplier_address_link"] += 1
            if blank(doc.get("address_display")):
                counts["purchase_blank_supplier_address_display"] += 1
        else:
            if blank(doc.get("billing_address_gstin")):
                counts["sales_blank_billing_address_gstin"] += 1
            if blank(doc.get("customer_address")):
                counts["sales_blank_customer_address_link"] += 1

        items = doc.get("items", [])
        counts["item_rows"] += len(items)
        if items and all(
            row.get("item_code") == "Tally Migration Item" for row in items
        ):
            counts["documents_only_generic_migration_item"] += 1
        if items and all(float(row.get("qty") or 0) in (1.0, -1.0) for row in items):
            counts["documents_all_item_qty_one_or_minus_one"] += 1
        blank_descriptions = sum(blank(row.get("description")) for row in items)
        counts["item_rows_blank_description"] += blank_descriptions
        blank_hsn = sum(blank(row.get("gst_hsn_code")) for row in items)
        counts["item_rows_blank_hsn"] += blank_hsn

        taxes = doc.get("taxes", [])
        counts["tax_charge_rows"] += len(taxes)
        counts["tax_charge_rows_actual"] += sum(
            row.get("charge_type") == "Actual" for row in taxes
        )
        counts["tax_charge_rows_rate_based"] += sum(
            row.get("charge_type") != "Actual" for row in taxes
        )
        if taxes and all(row.get("charge_type") == "Actual" for row in taxes):
            counts["documents_all_tax_rows_actual"] += 1

        if blank(doc.get("remarks")):
            counts["blank_remarks"] += 1
        if blank(doc.get("terms")):
            counts["blank_terms"] += 1
        if doc.get("due_date") == doc.get("posting_date"):
            counts["due_date_equals_posting_date"] += 1

        if (
            blank(doc.get("company_gstin"))
            or (
                dt == "Purchase Invoice"
                and blank(doc.get("supplier_gstin"))
            )
        ):
            add_example(
                "gst_identity_missing",
                doc,
                {
                    "company_gstin": doc.get("company_gstin"),
                    "party_gstin": (
                        doc.get("supplier_gstin")
                        if dt == "Purchase Invoice"
                        else doc.get("billing_address_gstin")
                    ),
                    "place_of_supply": doc.get("place_of_supply"),
                },
            )

    result = {
        "generated_on": REPORT_DATE,
        "scope": {
            "erp_url": ERPNextClient(dry_run=True).base,
            "expected_documents": len(targets),
            "fetched_documents": len(docs),
        },
        "counts": dict(sorted(counts.items())),
        "examples": examples,
        "errors": errors,
    }
    report_dir = ROOT / "data" / "reports"
    json_path = report_dir / f"invoice_print_metadata_audit_{REPORT_DATE}.json"
    md_path = report_dir / f"invoice_print_metadata_audit_{REPORT_DATE}.md"
    json_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# Invoice print metadata audit",
        "",
        f"Generated: {REPORT_DATE}",
        "",
        "Read-only full-document audit of migrated Sales and Purchase Invoices.",
        "",
        "## Counts",
        "",
    ]
    lines.extend(f"- `{k}`: {v}" for k, v in sorted(counts.items()))
    lines += ["", f"API errors: {len(errors)}", ""]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "counts": result["counts"],
        "errors": len(errors),
        "json_report": str(json_path),
        "markdown_report": str(md_path),
    }, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ERPNextError as exc:
        print(f"ERPNext API error: {exc}", file=sys.stderr)
        if exc.body:
            print(exc.body, file=sys.stderr)
        raise SystemExit(2)
