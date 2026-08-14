"""Plan GL-neutral allocations only to invoice deficits proven by Tally bills.

This is deliberately read-only. Unlike ``reconcile-payments`` it never gives
ERPNext the party's complete invoice list, so no unrelated invoice can be
selected by FIFO. The input is the fresh Tally-vs-ERPNext outstanding report.
"""
from __future__ import annotations

from collections import defaultdict
import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from t2e.config import get_config
from t2e.erpnext_client import ERPNextClient
from t2e.lines import parse_entries
from t2e.load_masters import fetch_company_defaults
from t2e.staging import Staging


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def norm(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def sign(value) -> int:
    value = money(value)
    return 1 if value > 0 else -1 if value < 0 else 0


def source_graph(vouchers: list[dict], party_types: dict[str, str]) -> tuple[dict, dict]:
    """Build Tally bill origins and source-proven settlement funding edges."""
    origins: dict[tuple[str, str], set[str]] = defaultdict(set)
    parsed_rows = []
    for voucher in vouchers:
        payload = json.loads(voucher["payload"])
        for entry in parse_entries(payload):
            party_key = norm(entry["ledger"])
            if party_key not in party_types:
                continue
            bills = entry["bills"]
            parsed_rows.append((voucher, party_key, bills))
            for bill in bills:
                # Tally uses both New Ref and Advance to originate a bill-wise
                # balance. A later Agst Ref can consume either kind.
                if bill["type"] in ("New Ref", "Advance"):
                    origins[(party_key, norm(bill["name"]))].add(voucher["guid"])

    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    event_guids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for voucher, party_key, bills in parsed_rows:
        against_refs = [bill for bill in bills if bill["type"] == "Agst Ref"]
        if not against_refs:
            continue
        # Sign alone cannot classify Tally Agst Ref rows. Some payment-origin
        # refs retain the same sign when consumed (for example STRONGLASS ref
        # 777). Instead connect every ref in one settlement event to the event
        # voucher and to every bill origin in that event; the live ERPNext
        # Payment Reconciliation list later filters this superset to documents
        # that are genuinely available payments, excluding invoice origins.
        keys = {(party_key, norm(bill["name"])) for bill in against_refs}
        for key in keys:
            adjacency[key].update(keys - {key})
            event_guids[key].add(voucher["guid"])

    funding: dict[tuple[str, str], set[str]] = defaultdict(set)
    visited: set[tuple[str, str]] = set()
    for start in set(adjacency) | set(origins):
        if start in visited:
            continue
        component = set()
        stack = [start]
        while stack:
            key = stack.pop()
            if key in component:
                continue
            component.add(key)
            stack.extend(adjacency.get(key, set()) - component)
        visited.update(component)
        candidates = set()
        for key in component:
            candidates.update(origins.get(key, set()))
            candidates.update(event_guids.get(key, set()))
        for key in component:
            funding[key].update(candidates)
    return origins, funding


def payment_key(row: dict) -> tuple[str, str]:
    return str(row.get("reference_type") or ""), str(row.get("reference_name") or "")


def distribute_reduction(group: dict, invoice_by_name: dict[str, dict]) -> list[dict]:
    """Allocate a connected group's required reduction across its ERP invoices.

    Reused Tally bill references make the individual invoice split unknowable;
    the native report proves only the connected-group total. We deterministically
    reduce oldest invoices first and explicitly flag these ambiguous groups.
    """
    remaining = money(group["difference"])
    targets = []
    docs = [invoice_by_name[name] for name in group["erp_documents"]
            if name in invoice_by_name]
    docs.sort(key=lambda row: (str(row.get("invoice_date") or ""),
                               str(row.get("invoice_number") or "")))
    for invoice in docs:
        if remaining <= 0:
            break
        outstanding = money(invoice.get("outstanding_amount"))
        amount = min(outstanding, remaining)
        if amount > 0:
            targets.append({"invoice": invoice, "amount": amount,
                            "group_refs": group["bill_refs"]})
            remaining -= amount
    if remaining:
        raise RuntimeError(
            f"cannot distribute {group['difference']} for {group['erp_documents']}; "
            f"remaining={remaining}")
    return targets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--safe-subset",
        action="store_true",
        help=(
            "exclude parties whose exact Tally target cannot be funded from "
            "current ERPNext payment rows; exclusions remain explicit in the plan"
        ),
    )
    args = parser.parse_args()
    cfg = get_config()
    report_dir = cfg.staging_db.parent / "reports"
    verification_path = report_dir / "invoice_outstanding_verification.json"
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    positive = [
        row for row in verification["differences"]
        if money(row["difference"]) > 0
        and row.get("classification", "requires_exact_allocation")
        == "requires_exact_allocation"
    ]
    excluded_return_groups = [
        row for row in verification["differences"]
        if row.get("classification")
        == "erpnext_return_reconciliation_turnover_exception"
    ]

    erp = ERPNextClient(dry_run=True)
    defaults = fetch_company_defaults(erp)
    live_party_names = {
        party_type: {
            norm(row["name"]): row["name"]
            for row in erp.get_list(party_type, fields=["name"], limit=0)
        }
        for party_type in ("Customer", "Supplier")
    }
    party_types = {
        party_key: party_type
        for party_type, names in live_party_names.items()
        for party_key in names
    }
    live_by_guid: dict[str, tuple[str, str]] = {}
    invoice_guid_by_name: dict[str, str] = {}
    for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
        for row in erp.get_list(
            doctype, fields=["name", cfg.idempotency_field], filters=[
                ["company", "=", defaults.name], ["docstatus", "=", 1],
                [cfg.idempotency_field, "is", "set"],
            ], limit=0):
            guid = str(row.get(cfg.idempotency_field) or "")
            live_by_guid[guid] = (doctype, row["name"])
            if doctype in ("Sales Invoice", "Purchase Invoice"):
                invoice_guid_by_name[row["name"]] = guid

    store = Staging()
    vouchers = [dict(row) for row in store.vouchers()]
    store.close()
    origin_graph, funding_graph = source_graph(vouchers, party_types)

    groups_by_party: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for group in positive:
        party_type = "Customer" if group["direction"] == "Receivable" else "Supplier"
        groups_by_party[(party_type, group["party"])].append(group)

    parties = []
    total_required = Decimal("0.00")
    total_planned = Decimal("0.00")
    total_residual = Decimal("0.00")
    for (party_type, party), groups in sorted(groups_by_party.items()):
        live_party = live_party_names[party_type].get(norm(party), party)
        account = defaults.receivable if party_type == "Customer" else defaults.payable
        base = {
            "doctype": "Payment Reconciliation", "company": defaults.name,
            "party_type": party_type, "party": live_party,
            "receivable_payable_account": account,
        }
        doc = erp.run_doc_method("get_unreconciled_entries", base)
        invoices = doc.get("invoices") or []
        invoice_by_name = {row["invoice_number"]: row for row in invoices}
        payments = doc.get("payments") or []
        payments_by_key = {payment_key(row): dict(row) for row in payments}
        remaining_by_key = {
            key: money(row.get("amount"))
            for key, row in payments_by_key.items()
        }

        targets = []
        errors = []
        for group in groups:
            try:
                targets.extend(distribute_reduction(group, invoice_by_name))
            except RuntimeError as exc:
                errors.append(str(exc))
        required = sum((target["amount"] for target in targets), Decimal("0.00"))

        # Feed one proven target invoice at a time and only source documents
        # connected to its Tally Agst Ref chain. This prevents both invoice FIFO
        # and payment-source FIFO from crossing unrelated bill references.
        target_meta = {}
        allocation = []
        provenance_residual = Decimal("0.00")
        pending_fallback = []
        for target in targets:
            invoice = dict(target["invoice"])
            invoice["outstanding_amount"] = float(target["amount"])
            invoice["amount"] = float(target["amount"])
            invoice_name = invoice["invoice_number"]
            candidate_guids = set()
            for bill_ref in target["group_refs"]:
                candidate_guids.update(funding_graph.get(
                    (norm(party), norm(bill_ref)), set()))
                # Some Tally invoices are entered directly "Agst Ref" to a
                # previously originated advance. In that case there is no later
                # settlement voucher: the advance origin itself funds the invoice.
                candidate_guids.update(origin_graph.get(
                    (norm(party), norm(bill_ref)), set()))
            invoice_guid = invoice_guid_by_name.get(invoice_name)
            if invoice_guid:
                candidate_guids.add(f"{invoice_guid}:party-control-bridge")
            candidate_keys = {
                live_by_guid[guid] for guid in candidate_guids
                if guid in live_by_guid
            }
            candidate_payments = []
            for key in sorted(candidate_keys):
                available = remaining_by_key.get(key, Decimal("0.00"))
                if available <= 0 or key not in payments_by_key:
                    continue
                payment = dict(payments_by_key[key])
                payment["amount"] = float(available)
                payment["unreconciled_amount"] = float(available)
                candidate_payments.append(payment)

            planned_doc = erp.run_doc_method(
                "allocate_entries", doc,
                args={"invoices": [invoice], "payments": candidate_payments},
            ) if candidate_payments else {"allocation": []}
            target_allocations = planned_doc.get("allocation") or []
            target_planned = sum(
                (money(row.get("allocated_amount")) for row in target_allocations),
                Decimal("0.00"))
            for row in target_allocations:
                key = payment_key(row)
                remaining_by_key[key] = (
                    remaining_by_key.get(key, Decimal("0.00"))
                    - money(row.get("allocated_amount")))
                row["tally_bill_refs"] = target["group_refs"]
                row["source_proven"] = key in candidate_keys
                allocation.append(row)
            target_residual = target["amount"] - target_planned
            provenance_residual += target_residual
            if target_residual > 0:
                pending_fallback.append((invoice, target, target_residual))
            target_meta[invoice_name] = {
                "starting_outstanding": f"{money(target['invoice'].get('outstanding_amount')):.2f}",
                "required": f"{target['amount']:.2f}",
                "planned": f"{target_planned:.2f}",
                "residual": f"{target_residual:.2f}",
                "tally_bill_refs": target["group_refs"],
                "candidate_source_guids": sorted(candidate_guids),
                "eligible_source_documents": [
                    {"doctype": key[0], "name": key[1]}
                    for key in sorted(candidate_keys)
                    if key in payments_by_key
                ],
            }

        # The exact Tally target remains authoritative even when old source
        # vouchers contain incomplete bill-origin metadata. Resolve only that
        # proven residual from other unallocated entries of the same party;
        # never expose another invoice as a target.
        fallback_total = Decimal("0.00")
        for invoice, target, target_residual in pending_fallback:
            fallback_payments = []
            for key, payment_row in sorted(payments_by_key.items()):
                available = remaining_by_key.get(key, Decimal("0.00"))
                if available <= 0:
                    continue
                payment = dict(payment_row)
                payment["amount"] = float(available)
                payment["unreconciled_amount"] = float(available)
                fallback_payments.append(payment)
            fallback_invoice = dict(invoice)
            fallback_invoice["outstanding_amount"] = float(target_residual)
            fallback_invoice["amount"] = float(target_residual)
            fallback_doc = erp.run_doc_method(
                "allocate_entries", doc,
                args={"invoices": [fallback_invoice],
                      "payments": fallback_payments},
            ) if fallback_payments else {"allocation": []}
            fallback_allocations = fallback_doc.get("allocation") or []
            fallback_planned = sum(
                (money(row.get("allocated_amount")) for row in fallback_allocations),
                Decimal("0.00"))
            for row in fallback_allocations:
                key = payment_key(row)
                remaining_by_key[key] = (
                    remaining_by_key.get(key, Decimal("0.00"))
                    - money(row.get("allocated_amount")))
                row["tally_bill_refs"] = target["group_refs"]
                row["source_proven"] = False
                row["fallback_reason"] = (
                    "Tally target proven; payment-origin bill metadata incomplete")
                allocation.append(row)
            fallback_total += fallback_planned
            meta = target_meta[invoice["invoice_number"]]
            revised_planned = money(meta["planned"]) + fallback_planned
            revised_residual = target["amount"] - revised_planned
            meta["planned"] = f"{revised_planned:.2f}"
            meta["residual"] = f"{revised_residual:.2f}"
            meta["fallback"] = f"{fallback_planned:.2f}"
        planned = sum((money(row.get("allocated_amount")) for row in allocation),
                      Decimal("0.00"))
        residual = required - planned
        if residual != provenance_residual - fallback_total:
            errors.append("internal residual mismatch")
        parties.append({
            "party_type": party_type,
            "source_party": party,
            "party": live_party,
            "groups": len(groups),
            "ambiguous_connected_groups": sum(
                len(group["erp_documents"]) > 1 for group in groups),
            "required": f"{required:.2f}",
            "planned": f"{planned:.2f}",
            "residual": f"{residual:.2f}",
            "source_proven": f"{planned - fallback_total:.2f}",
            "same_party_fallback": f"{fallback_total:.2f}",
            "eligible_payments": len(payments),
            "targets": target_meta,
            "allocations": allocation,
            "errors": errors,
        })

    # ERPNext represents return invoices as unreconciled payment rows, but
    # reconciling one against a normal invoice creates an extra same-control
    # Journal Entry. That preserves balances while inflating Trial Balance
    # turnover versus Tally. Exclude the complete target whenever the reviewed
    # plan chose an invoice/return source; the executor independently refuses
    # such a row as a second fail-closed guard.
    dynamically_excluded_returns = []
    actionable_parties = []
    for party_row in parties:
        return_targets = {
            str(row.get("invoice_number") or "")
            for row in party_row["allocations"]
            if str(row.get("reference_type") or "")
            in ("Sales Invoice", "Purchase Invoice")
        }
        for invoice_name in sorted(return_targets):
            meta = party_row["targets"].pop(invoice_name, None)
            if meta:
                dynamically_excluded_returns.append({
                    "party_type": party_row["party_type"],
                    "party": party_row["party"],
                    "invoice": invoice_name,
                    "amount": meta["required"],
                    "reason": "ERPNext return reconciliation creates artificial GL turnover",
                })
        party_row["allocations"] = [
            row for row in party_row["allocations"]
            if str(row.get("invoice_number") or "") not in return_targets
        ]
        required = sum(
            (money(meta["required"]) for meta in party_row["targets"].values()),
            Decimal("0.00"))
        planned = sum(
            (money(row.get("allocated_amount")) for row in party_row["allocations"]),
            Decimal("0.00"))
        fallback = sum(
            (money(row.get("allocated_amount")) for row in party_row["allocations"]
             if not row.get("source_proven")), Decimal("0.00"))
        party_row.update({
            "groups": len(party_row["targets"]),
            "required": f"{required:.2f}",
            "planned": f"{planned:.2f}",
            "residual": f"{required - planned:.2f}",
            "source_proven": f"{planned - fallback:.2f}",
            "same_party_fallback": f"{fallback:.2f}",
        })
        if party_row["targets"] or party_row["errors"]:
            actionable_parties.append(party_row)
    parties = actionable_parties
    excluded_residual_parties = []
    if args.safe_subset:
        retained = []
        for row in parties:
            if money(row["residual"]) or row["errors"]:
                excluded_residual_parties.append({
                    "party_type": row["party_type"],
                    "party": row["party"],
                    "required": row["required"],
                    "planned": row["planned"],
                    "residual": row["residual"],
                    "targets": sorted(row["targets"]),
                    "errors": row["errors"],
                    "reason": "insufficient current ERPNext payment availability",
                })
            else:
                retained.append(row)
        parties = retained
    total_required = sum((money(row["required"]) for row in parties), Decimal("0.00"))
    total_planned = sum((money(row["planned"]) for row in parties), Decimal("0.00"))
    total_residual = total_required - total_planned
    excluded_return_amount = (
        sum((money(row["difference"]) for row in excluded_return_groups), Decimal("0.00"))
        + sum((money(row["amount"]) for row in dynamically_excluded_returns), Decimal("0.00"))
    )

    payload = {
        "mode": "read-only",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "company": defaults.name,
        "source_report": str(verification_path),
        "source_report_sha256": hashlib.sha256(
            verification_path.read_bytes()).hexdigest(),
        "source_positive_difference_groups": len(positive) + len(excluded_return_groups),
        "positive_difference_groups": sum(len(row["targets"]) for row in parties),
        "excluded_return_reconciliation_groups": (
            len(excluded_return_groups) + len(dynamically_excluded_returns)),
        "excluded_return_reconciliation_amount": f"{excluded_return_amount:.2f}",
        "excluded_return_reconciliation_details": dynamically_excluded_returns,
        "safe_subset": args.safe_subset,
        "excluded_residual_parties": excluded_residual_parties,
        "parties": len(parties),
        "required": f"{total_required:.2f}",
        "planned": f"{total_planned:.2f}",
        "residual": f"{total_residual:.2f}",
        "safe_to_apply": total_residual == 0
                         and not any(row["errors"] for row in parties),
        "details": parties,
    }
    output = report_dir / "exact_bill_allocation_plan.json"
    output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items()
                      if key != "details"}, indent=2))
    print("PARTY RESIDUALS")
    for row in parties:
        if money(row["residual"]) or row["errors"]:
            print(f"  {row['party_type']} {row['party']}: required={row['required']} "
                  f"planned={row['planned']} residual={row['residual']} "
                  f"errors={len(row['errors'])}")
    print(f"REPORT {output}")


if __name__ == "__main__":
    main()
