"""Plan evidence-backed invoice allocations without changing ERPNext.

This workflow is intentionally separate from both source-exact ``Agst Ref``
loading and the legacy party-wide FIFO reconciler:

* explicit Tally ``Agst Ref`` rows belong to the normal voucher loader;
* this planner considers only still-unallocated Payment Entries;
* a payment and invoice must be a mutually unique exact-amount match for the
  same party, control account, and document direction;
* every proposed allocation records whether it would disagree with Tally's
  native Bills Receivable/Payable status.

The output is review evidence, never authorization to write.  There is no
``--confirm`` option in this module.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3

from t2e.lines import as_list


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def source_payment_evidence(staging_path: Path) -> dict[str, dict]:
    """Source metadata keyed by ERPNext Payment Entry name."""
    connection = sqlite3.connect(staging_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """select guid,vtype,vnumber,vdate,party,erp_doctype,erp_name,payload
                 from voucher
                where erp_doctype='Payment Entry' and erp_name is not null"""
        ).fetchall()
    finally:
        connection.close()

    output = {}
    for row in rows:
        payload = json.loads(row["payload"])
        raw_entries = as_list(payload.get("ALLLEDGERENTRIES.LIST")) \
            or as_list(payload.get("LEDGERENTRIES.LIST"))
        bill_types = set()
        bill_names = set()
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            if norm(entry.get("LEDGERNAME")) != norm(row["party"]):
                continue
            for bill in as_list(entry.get("BILLALLOCATIONS.LIST")):
                if not isinstance(bill, dict):
                    continue
                bill_type = str(bill.get("BILLTYPE") or "").strip()
                bill_name = str(bill.get("NAME") or "").strip()
                if bill_type:
                    bill_types.add(bill_type)
                if bill_name:
                    bill_names.add(bill_name)
        output[str(row["erp_name"])] = {
            "guid": str(row["guid"]),
            "vtype": str(row["vtype"]),
            "vnumber": str(row["vnumber"] or ""),
            "vdate": str(row["vdate"]),
            "party": str(row["party"] or ""),
            "bill_types": sorted(bill_types),
            "bill_names": sorted(bill_names),
            "narration": str(payload.get("NARRATION") or "").strip(),
        }
    return output


def tally_expected_by_guid(verification: dict | None) -> dict[str, Decimal]:
    """Return unambiguous invoice-level Tally expected outstandings.

    Connected groups containing several invoice GUIDs prove only a group total,
    so they are deliberately omitted rather than guessing an individual split.
    """
    output = {}
    for row in (verification or {}).get("details", []):
        guids = row.get("source_guids") or []
        if len(guids) == 1:
            output[str(guids[0])] = money(row.get("expected_erp_outstanding"))
    return output


def _settlement_words(narration: str) -> bool:
    words = norm(narration)
    return any(token in words for token in (
        "paid", "payment", "received", "receipt", "against", "advance",
        "dues", "settlement",
    ))


def build_plan(state: dict, source: dict[str, dict],
               verification: dict | None = None) -> dict:
    invoices = [
        row for row in state.get("invoices", [])
        if int(row.get("is_return") or 0) == 0
        and money(row.get("outstanding_amount")) > 0
    ]
    transactions = {
        (str(row.get("doctype")), str(row.get("name"))): row
        for row in state.get("transactions", [])
    }

    invoice_index: dict[tuple, list[dict]] = defaultdict(list)
    for invoice in invoices:
        expected_doctype = str(invoice.get("doctype"))
        key = (
            norm(invoice.get("party")), norm(invoice.get("account")),
            money(invoice.get("outstanding_amount")), expected_doctype,
        )
        invoice_index[key].append(invoice)

    payments = []
    seen_payment_rows = set()
    for row in state.get("payment_ledger", []):
        if row.get("voucher_type") != "Payment Entry":
            continue
        if (row.get("against_voucher_type"), row.get("against_voucher_no")) != (
                "Payment Entry", row.get("voucher_no")):
            continue
        identity = (str(row.get("voucher_no")), str(row.get("name")))
        if identity in seen_payment_rows:
            continue
        seen_payment_rows.add(identity)
        amount = abs(money(row.get("amount_in_account_currency")
                           or row.get("amount")))
        if amount <= 0:
            continue
        transaction = transactions.get(("Payment Entry", row.get("voucher_no")))
        evidence = source.get(str(row.get("voucher_no")))
        if not transaction or not evidence:
            continue
        party_type = str(transaction.get("party_type") or "")
        target_doctype = {
            "Customer": "Sales Invoice", "Supplier": "Purchase Invoice",
        }.get(party_type)
        expected_vtype = {"Customer": "Receipt", "Supplier": "Payment"}.get(
            party_type)
        if not target_doctype or evidence.get("vtype") != expected_vtype:
            continue
        if norm(evidence.get("party")) != norm(row.get("party")):
            continue
        # Explicit source allocations should have been handled by the normal
        # loader. Do not reinterpret them as evidence matches.
        if any(norm(value) == "agst ref" for value in evidence["bill_types"]):
            continue
        key = (
            norm(row.get("party")), norm(row.get("account")), amount,
            target_doctype,
        )
        payments.append({
            "row": row,
            "transaction": transaction,
            "source": evidence,
            "amount": amount,
            "candidate_invoices": invoice_index.get(key, []),
        })

    # Mutual uniqueness is essential. A unique invoice for one payment is not
    # enough when several equal payments all point to that same invoice.
    payment_candidates_by_invoice: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for payment in payments:
        if len(payment["candidate_invoices"]) == 1:
            invoice = payment["candidate_invoices"][0]
            payment_candidates_by_invoice[
                (invoice["doctype"], invoice["name"])
            ].append(payment)

    expected_by_guid = tally_expected_by_guid(verification)
    candidates = []
    rejected = Counter()
    for payment in payments:
        possible = payment["candidate_invoices"]
        if not possible:
            rejected["no_exact_open_invoice"] += 1
            continue
        if len(possible) != 1:
            rejected["multiple_equal_invoices"] += 1
            continue
        invoice = possible[0]
        inverse = payment_candidates_by_invoice[(invoice["doctype"], invoice["name"])]
        if len(inverse) != 1:
            rejected["multiple_equal_payments"] += 1
            continue

        payment_date = date.fromisoformat(str(payment["row"]["posting_date"]))
        invoice_date = date.fromisoformat(str(invoice["posting_date"]))
        signed_days = (payment_date - invoice_date).days
        absolute_days = abs(signed_days)
        narration_supports = _settlement_words(payment["source"]["narration"])
        source_types = payment["source"]["bill_types"]
        source_supports = bool({norm(value) for value in source_types} & {
            "advance", "on account", "new ref",
        })
        if absolute_days == 0 and (narration_supports or source_supports):
            confidence = "high"
            reason = "mutually unique exact amount, same party/account/date"
        elif absolute_days <= 7 and narration_supports:
            confidence = "high"
            reason = "mutually unique exact amount within 7 days with narration evidence"
        elif absolute_days <= 30:
            confidence = "review"
            reason = "mutually unique exact amount within 30 days"
        else:
            confidence = "manual"
            reason = "mutually unique exact amount but weak date proximity"

        tally_expected = expected_by_guid.get(str(invoice.get("tally_guid")))
        tally_status = (
            "unknown" if tally_expected is None
            else "would_disagree" if tally_expected > 0
            else "would_align"
        )
        would_disagree = tally_status == "would_disagree"
        candidates.append({
            "confidence": confidence,
            "reason": reason,
            "party_type": payment["transaction"]["party_type"],
            "party": payment["row"]["party"],
            "account": payment["row"]["account"],
            "invoice_doctype": invoice["doctype"],
            "invoice_name": invoice["name"],
            "invoice_guid": invoice.get("tally_guid"),
            "invoice_date": str(invoice["posting_date"]),
            "invoice_outstanding": f"{money(invoice['outstanding_amount']):.2f}",
            "payment_doctype": "Payment Entry",
            "payment_name": payment["row"]["voucher_no"],
            "payment_guid": payment["source"]["guid"],
            "payment_date": str(payment["row"]["posting_date"]),
            "unallocated_amount": f"{payment['amount']:.2f}",
            "days_payment_after_invoice": signed_days,
            "source_voucher_type": payment["source"]["vtype"],
            "source_voucher_number": payment["source"]["vnumber"],
            "source_bill_types": source_types,
            "source_bill_names": payment["source"]["bill_names"],
            "source_narration": payment["source"]["narration"],
            "tally_expected_outstanding": (
                f"{tally_expected:.2f}" if tally_expected is not None else None),
            "tally_bill_status_effect": tally_status,
            "would_disagree_with_tally_bill_status": would_disagree,
            "requires_explicit_policy_approval": True,
        })

    candidates.sort(key=lambda row: (
        {"high": 0, "review": 1, "manual": 2}[row["confidence"]],
        abs(row["days_payment_after_invoice"]), norm(row["party"]),
        row["invoice_name"],
    ))
    confidence_counts = Counter(row["confidence"] for row in candidates)
    tally_effect_counts = Counter(
        row["tally_bill_status_effect"] for row in candidates)
    confidence_amounts = {
        level: f"{sum((money(row['unallocated_amount']) for row in candidates
                       if row['confidence'] == level), Decimal('0.00')):.2f}"
        for level in ("high", "review", "manual")
    }
    return {
        "mode": "read-only",
        "site": state.get("site"),
        "company": state.get("company"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": "evidence-backed non-Tally allocation review",
        "safe_to_apply_automatically": False,
        "summary": {
            "open_invoices": len(invoices),
            "eligible_unallocated_payment_rows": len(payments),
            "mutually_unique_exact_candidates": len(candidates),
            "confidence_counts": {
                level: confidence_counts[level]
                for level in ("high", "review", "manual")
            },
            "confidence_amounts": confidence_amounts,
            "would_disagree_with_tally_bill_status": sum(
                row["would_disagree_with_tally_bill_status"] for row in candidates),
            "tally_bill_status_effect": {
                level: tally_effect_counts[level]
                for level in ("would_align", "would_disagree", "unknown")
            },
            "rejected": dict(sorted(rejected.items())),
        },
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", help="ERPNext payment-state export JSON")
    parser.add_argument("--staging", default="data/staging.sqlite")
    parser.add_argument("--verification", help="fresh Tally invoice-outstanding report")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    verification = (
        json.loads(Path(args.verification).read_text(encoding="utf-8"))
        if args.verification else None
    )
    payload = build_plan(
        state, source_payment_evidence(Path(args.staging)), verification)
    state_path = Path(args.state).resolve()
    staging_path = Path(args.staging).resolve()
    payload.update({
        "source_state": str(state_path),
        "source_state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
        "staging_path": str(staging_path),
        "verification_report": (
            str(Path(args.verification).resolve()) if args.verification else None),
        "verification_report_sha256": (
            hashlib.sha256(Path(args.verification).read_bytes()).hexdigest()
            if args.verification else None),
    })
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": payload["summary"], "report": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
