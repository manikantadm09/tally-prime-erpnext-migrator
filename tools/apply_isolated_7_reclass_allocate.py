"""Atomic 7-JE party-control reclass + exact Agst Ref allocation.

Leaves ACC-JV-2026-07503 and ACC-JV-2026-08737 in the 142-exception set.
Does not apply the nine-JE document. All-or-nothing: any failure voids
bridges created in this window (unlink first if already allocated).
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient, ERPNextError
from t2e.exact_bill_reconciliation import _gl_signature, money
from t2e.load_masters import fetch_company_defaults
from t2e.load_period_closing import PeriodClosingLoader
from t2e.load_vouchers import _name_of
from t2e.repair_party_bridges import unreconcile_selections

ROOT = Path("/apps/frappe-bench/tallytoerpnext")
NINE = ROOT / "data/reports/agst_ref_safe_9_apply_plan.json"
BILLMAP = ROOT / "data/reports/party_control_reclass_9_plan.json"
EXCLUDE = {"ACC-JV-2026-07503", "ACC-JV-2026-08737"}
EXPECTED_AMOUNT = Decimal("1038244.00")
EXPECTED_COUNT = 7
PCV_FROM = "2023-03-31"


def _doc(erp, doctype, name):
    dt = urllib.parse.quote(doctype)
    nm = urllib.parse.quote(name, safe="")
    return erp._request("GET", f"/api/resource/{dt}/{nm}")["data"]


def _ple(erp, voucher_type, voucher_no, company):
    return erp.get_list(
        "Payment Ledger Entry",
        fields=["voucher_type", "voucher_no", "against_voucher_type",
                "against_voucher_no", "amount", "delinked", "account", "party"],
        filters=[["voucher_type", "=", voucher_type],
                 ["voucher_no", "=", voucher_no], ["delinked", "=", 0]],
        limit=0,
    )


def _gl_net(erp, company, account, party_type=None, party=None):
    filters = [["account", "=", account], ["is_cancelled", "=", 0],
               ["company", "=", company]]
    if party_type:
        filters += [["party_type", "=", party_type], ["party", "=", party]]
    rows = erp.get_list("GL Entry", fields=["debit", "credit"], filters=filters, limit=0)
    dr = sum((money(r["debit"]) for r in rows), Decimal("0"))
    cr = sum((money(r["credit"]) for r in rows), Decimal("0"))
    return dr, cr, dr - cr


def build_pairs():
    nine = json.loads(NINE.read_text())
    billmap = json.loads(BILLMAP.read_text())
    isolated = set(billmap["isolation"]["bill_map_isolated_jes"])
    pairs = []
    for p in nine["pairs"]:
        if p["settlement"] in EXCLUDE:
            continue
        if p["settlement"] not in isolated:
            raise RuntimeError(f"{p['settlement']} is not bill-map isolated")
        pairs.append(p)
    if len(pairs) != EXPECTED_COUNT:
        raise RuntimeError(f"expected {EXPECTED_COUNT} pairs, got {len(pairs)}")
    total = sum((money(p["agst_ref_amount"]) for p in pairs), Decimal("0"))
    if total != EXPECTED_AMOUNT:
        raise RuntimeError(f"amount {total} != {EXPECTED_AMOUNT}")
    return pairs


def preflight(erp, defaults, pairs):
    company = defaults.name
    creditors = defaults.payable
    failures = []
    live_pairs = []
    invoice_need = defaultdict(lambda: Decimal("0"))
    for p in pairs:
        invoice_need[p["invoice"]] += money(p["agst_ref_amount"])

    for p in pairs:
        je = _doc(erp, "Journal Entry", p["settlement"])
        pi = _doc(erp, "Purchase Invoice", p["invoice"])
        named = None
        for a in je.get("accounts") or []:
            acc = a.get("account") or ""
            if (acc.endswith(" - SDL") and money(a.get("debit")) > 0
                    and not a.get("party")
                    and not str(acc).casefold().startswith("bank charges")):
                named = a
        amount = money(p["agst_ref_amount"])
        checks = {
            "je_submitted": je.get("docstatus") == 1,
            "pi_submitted": pi.get("docstatus") == 1,
            "same_party": pi.get("supplier") == p["party"],
            "je_guid_match": je.get("tally_guid") == p["settlement_guid"],
            "pi_guid_match": pi.get("tally_guid") == p["invoice_guid"],
            "named_gl_equals_agst_ref": named is not None and money(named.get("debit")) == amount,
            "named_has_no_party": named is not None and not named.get("party"),
            "pi_credits_creditors": pi.get("credit_to") == creditors,
            "pi_outstanding_covers_this_row": money(pi.get("outstanding_amount")) >= amount,
            "no_existing_bridge": erp.find_by_field(
                "Journal Entry", get_config().idempotency_field,
                f"{p['settlement_guid']}:party-control-bridge",
                exclude_cancelled=True) is None,
            "source_je_not_on_creditors_party": True,
        }
        source_ple = [
            r for r in _ple(erp, "Journal Entry", p["settlement"], company)
            if str(r.get("against_voucher_no")) != p["settlement"]
        ]
        checks["source_je_has_no_invoice_ple"] = not source_ple
        if named and named.get("account") == creditors and named.get("party"):
            checks["source_je_not_on_creditors_party"] = False
        if money(pi.get("outstanding_amount")) < invoice_need[p["invoice"]]:
            checks["pi_outstanding_covers_window_sum"] = False
        else:
            checks["pi_outstanding_covers_window_sum"] = True
        failed = [k for k, v in checks.items() if not v]
        if failed:
            failures.append({"settlement": p["settlement"], "failed": failed})
        live_pairs.append({
            **p,
            "named_account": named.get("account") if named else None,
            "named_debit": f"{money(named.get('debit')):.2f}" if named else None,
            "pi_outstanding_live": f"{money(pi.get('outstanding_amount')):.2f}",
            "pi_status_live": pi.get("status"),
            "gate_checks": checks,
            "gate": "passed" if not failed else "failed",
        })
    return live_pairs, failures


def open_pcvs(erp, company):
    rows = erp.get_list(
        "Period Closing Voucher",
        fields=["name", "period_end_date", "fiscal_year", "docstatus",
                get_config().idempotency_field],
        filters=[["company", "=", company], ["docstatus", "=", 1],
                 ["period_end_date", ">=", PCV_FROM]],
        limit=0,
    )
    rows.sort(key=lambda r: r.get("period_end_date") or "", reverse=True)
    cancelled = []
    for row in rows:
        erp.cancel("Period Closing Voucher", row["name"])
        cancelled.append(row)
    return cancelled


def create_bridge(erp, defaults, field, pair):
    amount = money(pair["agst_ref_amount"])
    key = f"{pair['settlement_guid']}:party-control-bridge"
    doc = {
        "company": defaults.name,
        "posting_date": pair["tally_date"],
        "voucher_type": "Journal Entry",
        "title": f"Party-control reclass {pair['settlement']}",
        "user_remark": (
            f"Reclass {pair['named_account']} -> {defaults.payable} / "
            f"Supplier {pair['party']} for Tally Agst Ref {pair['bill_ref']} "
            f"on {pair['invoice']}. Bridge is the payment instrument; "
            f"do not allocate the original named-GL JE."
        )[:1000],
        field: key,
        "accounts": [
            {
                "account": defaults.payable,
                "party_type": "Supplier",
                "party": pair["party"],
                "debit_in_account_currency": float(amount),
                "cost_center": defaults.cost_center,
            },
            {
                "account": pair["named_account"],
                "credit_in_account_currency": float(amount),
                "cost_center": defaults.cost_center,
            },
        ],
    }
    res = erp.insert_and_submit("Journal Entry", doc)
    name = _name_of(res)
    if not name:
        name = erp.find_by_field("Journal Entry", field, key, exclude_cancelled=True)
    if not name:
        raise RuntimeError(f"bridge submit returned no name: {res}")
    return name, key


def allocate_bridge(erp, defaults, pair, bridge_name):
    amount = money(pair["agst_ref_amount"])
    base = {
        "doctype": "Payment Reconciliation",
        "company": defaults.name,
        "party_type": "Supplier",
        "party": pair["party"],
        "receivable_payable_account": defaults.payable,
    }
    doc = erp.run_doc_method("get_unreconciled_entries", base)
    invoices = {row["invoice_number"]: row for row in doc.get("invoices") or []}
    invoice = invoices.get(pair["invoice"])
    payments = [
        row for row in (doc.get("payments") or [])
        if str(row.get("reference_type")) == "Journal Entry"
        and str(row.get("reference_name")) == bridge_name
    ]
    if invoice is None:
        raise RuntimeError(f"PI {pair['invoice']} not unreconciled for {pair['party']}")
    if money(invoice.get("outstanding_amount")) < amount:
        raise RuntimeError(f"PI {pair['invoice']} outstanding too small")
    if not payments:
        raise RuntimeError(f"bridge {bridge_name} not an unreconciled Creditors payment")
    payment = payments[0]
    if money(payment.get("amount")) < amount:
        raise RuntimeError(f"bridge {bridge_name} available {payment.get('amount')}")
    invoice_arg = dict(invoice)
    invoice_arg["outstanding_amount"] = float(amount)
    invoice_arg["amount"] = float(amount)
    planned = erp.run_doc_method(
        "allocate_entries", doc,
        args={"invoices": [invoice_arg], "payments": [dict(payment)]},
    )
    rows = planned.get("allocation") or []
    if len(rows) != 1:
        raise RuntimeError(f"ERPNext returned {len(rows)} allocation rows")
    row = rows[0]
    if (str(row.get("invoice_number")) != pair["invoice"]
            or str(row.get("reference_name")) != bridge_name
            or money(row.get("allocated_amount")) != amount):
        raise RuntimeError(f"allocation mismatch: {row}")
    planned["allocation"] = [row]
    erp.run_doc_method("reconcile", planned)


def void_window(erp, company, created):
    errors = []
    for item in reversed(created):
        name = item.get("bridge")
        if not name:
            continue
        try:
            doc = _doc(erp, "Journal Entry", name)
            refs = [
                {
                    "reference_type": a.get("reference_type"),
                    "reference_name": a.get("reference_name"),
                }
                for a in (doc.get("accounts") or [])
                if a.get("reference_type") or a.get("reference_name")
            ]
            if refs:
                selections = unreconcile_selections(company, name, refs)
                erp._write(
                    "POST",
                    "/api/method/erpnext.accounts.doctype.unreconcile_payment."
                    "unreconcile_payment.create_unreconcile_doc_for_selection",
                    json={"selections": json.dumps(selections)},
                )
            if doc.get("docstatus") == 1:
                erp.cancel("Journal Entry", name)
            item["voided"] = True
        except Exception as exc:
            item["voided"] = False
            errors.append({"bridge": name, "error": str(exc)[:2000]})
    return errors


def verify(erp, defaults, created):
    company = defaults.name
    issues = []
    for item in created:
        bridge = item["bridge"]
        pair = item["pair"]
        amount = money(pair["agst_ref_amount"])
        je = _doc(erp, "Journal Entry", bridge)
        if je.get("docstatus") != 1:
            issues.append(f"{bridge} not submitted")
        dr = sum(money(a.get("debit")) for a in je.get("accounts") or [])
        cr = sum(money(a.get("credit")) for a in je.get("accounts") or [])
        if dr != cr or dr != amount:
            issues.append(f"{bridge} not balanced at {amount}")
        ple = [
            r for r in _ple(erp, "Journal Entry", bridge, company)
            if str(r.get("against_voucher_no")) == pair["invoice"]
        ]
        linked = sum((abs(money(r.get("amount"))) for r in ple), Decimal("0"))
        if linked != amount:
            issues.append(f"{bridge} PLE to {pair['invoice']} is {linked} not {amount}")
        source_ple = [
            r for r in _ple(erp, "Journal Entry", pair["settlement"], company)
            if str(r.get("against_voucher_no")) == pair["invoice"]
        ]
        if source_ple:
            issues.append(f"source {pair['settlement']} unexpectedly linked to PI")
        pi = _doc(erp, "Purchase Invoice", pair["invoice"])
        item["pi_outstanding_after"] = f"{money(pi.get('outstanding_amount')):.2f}"
        item["pi_status_after"] = pi.get("status")
        item["ple"] = [
            {"against": r.get("against_voucher_no"), "amount": f"{money(r.get('amount')):.2f}"}
            for r in ple
        ]
    gl = _gl_signature(erp, company)
    if gl["debit"] != gl["credit"]:
        issues.append(f"GL imbalance {gl}")
    return issues, gl


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--backup")
    args = parser.parse_args()
    pairs = build_pairs()
    erp_ro = ERPNextClient(dry_run=True)
    defaults = fetch_company_defaults(erp_ro)
    live_pairs, failures = preflight(erp_ro, defaults, pairs)
    gl_before = _gl_signature(erp_ro, defaults.name)
    plan = {
        "mode": "atomic-window",
        "writes": bool(args.confirm),
        "applied": False,
        "safe_to_apply": not failures,
        "company": defaults.name,
        "control_account": defaults.payable,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "does_not_apply_nine_je_document": True,
        "excluded_exception_jes": sorted(EXCLUDE),
        "exception_set": "142 invoice_already_cleared_without_this_link / conflict repair",
        "summary": {
            "pairs": len(live_pairs),
            "amount": f"{EXPECTED_AMOUNT:.2f}",
            "failed_preflight": len(failures),
        },
        "gate_failures": failures,
        "pairs": live_pairs,
        "gl_before": gl_before,
    }
    plan_path = ROOT / "data/reports/agst_ref_isolated_7_reclass_allocate_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    (ROOT / "agst_ref_isolated_7_reclass_allocate_plan.json").write_text(plan_path.read_text())
    if failures:
        print(json.dumps({"preflight": "failed", "failures": failures}, indent=2))
        return 1
    if not args.confirm:
        print(json.dumps({"preflight": "passed", "plan": str(plan_path),
                          "amount": f"{EXPECTED_AMOUNT:.2f}"}, indent=2))
        return 0
    if not args.backup:
        raise SystemExit("--confirm requires --backup")

    erp = ERPNextClient(dry_run=False)
    defaults = fetch_company_defaults(erp)
    live_pairs, failures = preflight(erp, defaults, pairs)
    if failures:
        report = {"applied": False, "aborted": True, "reason": "live preflight failed",
                  "failures": failures, "backup": args.backup}
        out = ROOT / "data/reports/agst_ref_isolated_7_apply_abort.json"
        out.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 1

    created = []
    pcv_cancelled = []
    result = {"applied": False, "backup": args.backup, "company": defaults.name}
    try:
        pcv_cancelled = open_pcvs(erp, defaults.name)
        result["pcv_cancelled"] = [r["name"] for r in pcv_cancelled]
        field = get_config().idempotency_field
        for pair in live_pairs:
            name, key = create_bridge(erp, defaults, field, pair)
            item = {"pair": pair, "bridge": name, "tally_guid": key, "allocated": False}
            created.append(item)
            allocate_bridge(erp, defaults, pair, name)
            item["allocated"] = True
        issues, gl_after = verify(erp, defaults, created)
        if issues:
            raise RuntimeError("post-write verify failed: " + "; ".join(issues))
        pc_stats, pc_results = PeriodClosingLoader(erp, defaults).run()
        result.update({
            "applied": True,
            "pass": True,
            "bridges": [
                {
                    "source_je": c["pair"]["settlement"],
                    "bridge": c["bridge"],
                    "invoice": c["pair"]["invoice"],
                    "amount": c["pair"]["agst_ref_amount"],
                    "party": c["pair"]["party"],
                    "bill_ref": c["pair"]["bill_ref"],
                    "allocated": c["allocated"],
                    "pi_outstanding_after": c.get("pi_outstanding_after"),
                    "pi_status_after": c.get("pi_status_after"),
                    "ple": c.get("ple"),
                }
                for c in created
            ],
            "gl_before": gl_before,
            "gl_after": gl_after,
            "gl_balanced": gl_after["debit"] == gl_after["credit"],
            "period_closing": {"stats": pc_stats, "results": pc_results},
        })
    except Exception as exc:
        void_errors = void_window(erp, defaults.name, created)
        try:
            pc_stats, pc_results = PeriodClosingLoader(erp, defaults).run()
        except Exception as pc_exc:
            pc_stats, pc_results = {"error": str(pc_exc)}, []
        result.update({
            "applied": False,
            "aborted": True,
            "pass": False,
            "error": str(exc)[:4000],
            "error_body": (getattr(exc, "body", None) or "")[:4000],
            "created_then_voided": created,
            "void_errors": void_errors,
            "period_closing_after_abort": {"stats": pc_stats, "results": pc_results},
        })
        out = ROOT / "data/reports/agst_ref_isolated_7_apply_abort.json"
        out.write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(json.dumps(result, indent=2, default=str)[:4000])
        return 1

    out = ROOT / "data/reports/agst_ref_isolated_7_apply_result.json"
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    (ROOT / "agst_ref_isolated_7_apply_result.json").write_text(out.read_text())
    print(json.dumps({k: result[k] for k in ("applied", "pass", "gl_balanced", "bridges")
                      if k in result}, indent=2, default=str)[:4000])
    print("WROTE", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
