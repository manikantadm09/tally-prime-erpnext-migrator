"""Repair five migrated return-invoice bill references without GL turnover.

ERPNext's Payment Reconciliation creates a same-control-account Journal Entry
when a Sales/Purchase return is used to settle a normal invoice.  That is a
valid ERPNext workflow, but it adds artificial debit/credit turnover which is
not present in Tally.  For this migration we instead replace each return's
self-referencing Payment Ledger Entry with accounting-neutral references to
the exact Tally bills.

Run with the target bench Python after taking a fresh database backup.  The
command is plan-only unless ``--confirm`` is supplied.  It is bound to the
UAT documents, parties, amounts and before/after outstandings on
dev-site.local and refuses partial or unexpected state.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path


COMPANY = "Spaceki Designs LLP"
PENNY = Decimal("0.01")

# Return doctype/name -> exact source and target state proved from Tally
# voucher bill allocations and UAT (dev-site.local) Payment Ledger.
# SHAILENDRA SRET-26-00001 is a New Ref credit note paid by receipts, not an
# Agst Ref against the sales invoice, so it is intentionally absent.
REPAIRS = (
    {
        "doctype": "Sales Invoice",
        "return": "SRET-26-00009",
        "party_type": "Customer",
        "party": "MAHENDRA HOMES PRIVATE LIMITED",
        "account": "Debtors - SDL",
        "return_outstanding_before": "-1242677.00",
        "allocations": (
            {"target": "SINV-26-00161", "amount": "1242677.00",
             "target_before": "3498912.00", "target_after": "2256235.00"},
        ),
    },
    {
        "doctype": "Sales Invoice",
        "return": "SRET-26-00008",
        "party_type": "Customer",
        "party": "MAHENDRA HOMES PRIVATE LIMITED",
        "account": "Debtors - SDL",
        "return_outstanding_before": "-390630.00",
        "allocations": (
            {"target": "SINV-26-00162", "amount": "390630.00",
             "target_before": "2865433.00", "target_after": "2474803.00"},
        ),
    },
    {
        "doctype": "Purchase Invoice",
        "return": "PRET-26-00017",
        "party_type": "Supplier",
        "party": "P.C SAMPATH & CO",
        "account": "Creditors - SDL",
        "return_outstanding_before": "-16567.00",
        "allocations": (
            {"target": "PINV-26-02728", "amount": "16567.00",
             "target_before": "21841.00", "target_after": "5274.00"},
        ),
    },
    {
        "doctype": "Purchase Invoice",
        "return": "PRET-26-00018",
        "party_type": "Supplier",
        "party": "THE LIGHT PLACE",
        "account": "Creditors - SDL",
        "return_outstanding_before": "-16231.00",
        "allocations": (
            {"target": "PINV-26-02750", "amount": "16231.00",
             "target_before": "169583.00", "target_after": "153352.00"},
        ),
    },
    {
        "doctype": "Purchase Invoice",
        "return": "PRET-26-00019",
        "party_type": "Supplier",
        "party": "VIJAYALAXMI TIMBER DEPOT",
        "account": "Creditors - SDL",
        "return_outstanding_before": "-76225.00",
        "allocations": (
            {"target": "PINV-26-02976", "amount": "76225.00",
             "target_before": "333395.00", "target_after": "257170.00"},
        ),
    },
)

PLE_FIELDS = (
    "posting_date", "company", "account_type", "account", "party_type",
    "party", "due_date", "voucher_detail_no", "cost_center", "project",
    "finance_book", "voucher_type", "voucher_no", "against_voucher_type",
    "against_voucher_no", "amount", "account_currency",
    "amount_in_account_currency", "remarks",
)


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY)


def repair_total() -> Decimal:
    return sum(
        (money(allocation["amount"])
         for repair in REPAIRS for allocation in repair["allocations"]),
        Decimal("0.00"),
    )


def active_gl_signature(frappe, company: str) -> dict:
    row = frappe.db.sql(
        """select count(*) as row_count, coalesce(sum(debit), 0) as debit,
                  coalesce(sum(credit), 0) as credit
             from `tabGL Entry`
            where company=%s and is_cancelled=0""",
        company, as_dict=True,
    )[0]
    return {
        "rows": int(row.row_count),
        "debit": f"{money(row.debit):.2f}",
        "credit": f"{money(row.credit):.2f}",
    }


def active_ple_signature(frappe, company: str) -> dict:
    row = frappe.db.sql(
        """select count(*) as row_count, coalesce(sum(amount), 0) as amount,
                  coalesce(sum(amount_in_account_currency), 0) as account_amount
             from `tabPayment Ledger Entry`
            where company=%s and delinked=0""",
        company, as_dict=True,
    )[0]
    return {
        "rows": int(row.row_count),
        "amount": f"{money(row.amount):.2f}",
        "account_amount": f"{money(row.account_amount):.2f}",
    }


def _party_field(doctype: str) -> str:
    return "customer" if doctype == "Sales Invoice" else "supplier"


def _account_field(doctype: str) -> str:
    return "debit_to" if doctype == "Sales Invoice" else "credit_to"


def _active_source_rows(frappe, repair: dict) -> list:
    return frappe.get_all(
        "Payment Ledger Entry",
        filters={
            "voucher_type": repair["doctype"],
            "voucher_no": repair["return"],
            "delinked": 0,
        },
        fields=["name", *PLE_FIELDS],
        order_by="creation asc",
        limit_page_length=0,
    )


def _expected_replacement_map(repair: dict) -> dict[tuple[str, str], Decimal]:
    return {
        (repair["doctype"], allocation["target"]): -money(allocation["amount"])
        for allocation in repair["allocations"]
    }


def _actual_reference_map(rows: list) -> dict[tuple[str, str], Decimal]:
    result: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        key = (str(row.against_voucher_type), str(row.against_voucher_no))
        result[key] = result.get(key, Decimal("0.00")) + money(
            row.amount_in_account_currency)
    return result


def _validate_invoice(frappe, repair: dict, name: str, *, is_return: bool):
    doc = frappe.get_doc(repair["doctype"], name)
    if doc.company != COMPANY or doc.docstatus != 1:
        raise RuntimeError(f"{repair['doctype']} {name}: company/status mismatch")
    if bool(doc.is_return) != is_return:
        raise RuntimeError(f"{repair['doctype']} {name}: return flag mismatch")
    if doc.get(_party_field(repair["doctype"])) != repair["party"]:
        raise RuntimeError(f"{repair['doctype']} {name}: party mismatch")
    if doc.get(_account_field(repair["doctype"])) != repair["account"]:
        raise RuntimeError(f"{repair['doctype']} {name}: party account mismatch")
    return doc


def validate_state(frappe) -> tuple[list[dict], bool]:
    """Validate all documents and return (plan, already_applied)."""
    plan = []
    applied_count = 0
    pending_count = 0
    for repair in REPAIRS:
        return_doc = _validate_invoice(
            frappe, repair, repair["return"], is_return=True)
        rows = _active_source_rows(frappe, repair)
        expected_map = _expected_replacement_map(repair)
        actual_map = _actual_reference_map(rows)
        self_key = (repair["doctype"], repair["return"])
        expected_total = sum(
            (money(a["amount"]) for a in repair["allocations"]),
            Decimal("0.00"),
        )

        is_pending = (
            len(rows) == 1
            and set(actual_map) == {self_key}
            and actual_map[self_key] == -expected_total
            and money(return_doc.outstanding_amount)
            == money(repair["return_outstanding_before"])
        )
        is_applied = (
            actual_map == expected_map
            and money(return_doc.outstanding_amount) == Decimal("0.00")
        )
        if not is_pending and not is_applied:
            raise RuntimeError(
                f"{repair['return']}: unexpected/partial Payment Ledger state: "
                f"{actual_map}, outstanding={return_doc.outstanding_amount}")

        targets = []
        for allocation in repair["allocations"]:
            target = _validate_invoice(
                frappe, repair, allocation["target"], is_return=False)
            expected_outstanding = (
                allocation["target_after"] if is_applied
                else allocation["target_before"]
            )
            if money(target.outstanding_amount) != money(expected_outstanding):
                raise RuntimeError(
                    f"{allocation['target']}: outstanding drift; expected "
                    f"{expected_outstanding}, got {target.outstanding_amount}")
            targets.append({
                "invoice": allocation["target"],
                "allocation": f"{money(allocation['amount']):.2f}",
                "before": allocation["target_before"],
                "after": allocation["target_after"],
            })

        if is_applied:
            applied_count += 1
        else:
            pending_count += 1
        plan.append({
            "return": repair["return"],
            "doctype": repair["doctype"],
            "party": repair["party"],
            "state": "already_applied" if is_applied else "pending",
            "source_payment_ledger_rows": [row.name for row in rows],
            "targets": targets,
        })

    if applied_count and pending_count:
        raise RuntimeError("mixed applied/pending state; refusing a partial repair")
    return plan, applied_count == len(REPAIRS)


def apply_repair(frappe) -> list[str]:
    """Replace self references atomically. Caller validates and commits."""
    from erpnext.accounts.utils import update_voucher_outstanding

    created = []
    for repair in REPAIRS:
        source_rows = _active_source_rows(frappe, repair)
        if len(source_rows) != 1:
            raise RuntimeError(f"{repair['return']}: source row changed during apply")
        source = source_rows[0]

        # Preserve the immutable source row as audit evidence and delink it;
        # replacement rows carry the same total value and voucher identity.
        frappe.db.set_value(
            "Payment Ledger Entry", source.name, "delinked", 1,
            update_modified=True,
        )
        for allocation in repair["allocations"]:
            amount = money(allocation["amount"])
            values = {field: source.get(field) for field in PLE_FIELDS}
            values.update({
                "doctype": "Payment Ledger Entry",
                "against_voucher_type": repair["doctype"],
                "against_voucher_no": allocation["target"],
                "amount": -amount,
                "amount_in_account_currency": -amount,
                "delinked": 0,
            })
            replacement = frappe.get_doc(values)
            replacement.flags.ignore_permissions = 1
            replacement.flags.from_repost = 1
            replacement.flags.update_outstanding = "No"
            replacement.submit()
            created.append(replacement.name)

        affected = [repair["return"]] + [
            allocation["target"] for allocation in repair["allocations"]]
        for name in affected:
            update_voucher_outstanding(
                repair["doctype"], name, repair["account"],
                repair["party_type"], repair["party"],
            )
    return created


def discover_sites_path(site: str, cwd: Path | None = None) -> Path:
    """Find the bench sites directory for a standalone bench-Python run."""
    cwd = (cwd or Path.cwd()).resolve()
    candidates = (cwd / "sites", cwd)
    for candidate in candidates:
        if (candidate / site / "site_config.json").is_file():
            return candidate
    raise RuntimeError(
        f"Cannot locate {site}/site_config.json below {cwd}; run from the "
        "bench root or its sites directory")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--confirm-company", required=True)
    parser.add_argument("--backup", help="Existing fresh database backup path")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()

    if args.company != COMPANY or args.confirm_company != COMPANY:
        raise SystemExit(f"Both company arguments must exactly equal {COMPANY!r}")
    backup = Path(args.backup).resolve() if args.backup else None
    report_path = Path(args.report).resolve() if args.report else None
    if args.confirm and (
        backup is None or not backup.is_file() or backup.stat().st_size == 0
    ):
        raise SystemExit("--confirm requires a non-empty --backup file")

    import frappe

    already_connected = bool(
        getattr(frappe.local, "initialised", False)
        and getattr(frappe.local, "db", None))
    original_cwd = Path.cwd()
    if not already_connected:
        sites_path = discover_sites_path(args.site, original_cwd)
        # Frappe's logger constructs ``<site>/logs`` relative to cwd.
        os.chdir(sites_path)
        frappe.init(site=args.site, sites_path=str(sites_path), force=True)
        frappe.connect()
    elif frappe.local.site != args.site:
        raise RuntimeError(
            f"Connected site {frappe.local.site!r} is not {args.site!r}")

    try:
        if not frappe.db.exists("Company", COMPANY):
            raise RuntimeError(f"Company does not exist: {COMPANY}")
        gl_before = active_gl_signature(frappe, COMPANY)
        ple_before = active_ple_signature(frappe, COMPANY)
        plan, already_applied = validate_state(frappe)
        created = []

        if args.confirm and not already_applied:
            created = apply_repair(frappe)
            final_plan, final_applied = validate_state(frappe)
            if not final_applied:
                raise RuntimeError("post-repair validation did not reach applied state")
            plan = final_plan

        gl_after = active_gl_signature(frappe, COMPANY)
        ple_after = active_ple_signature(frappe, COMPANY)
        expected_row_delta = 0
        if args.confirm and not already_applied:
            expected_row_delta = sum(
                len(repair["allocations"]) for repair in REPAIRS
            ) - len(REPAIRS)
        gl_unchanged = gl_before == gl_after
        ple_value_unchanged = (
            ple_before["amount"] == ple_after["amount"]
            and ple_before["account_amount"] == ple_after["account_amount"]
        )
        ple_rows_expected = (
            ple_after["rows"] == ple_before["rows"] + expected_row_delta)
        passed = gl_unchanged and ple_value_unchanged and ple_rows_expected
        if not passed:
            raise RuntimeError(
                "accounting-neutral invariants failed; transaction will roll back")

        if args.confirm:
            frappe.db.commit()
        else:
            frappe.db.rollback()
        result = {
            "site": args.site,
            "company": COMPANY,
            "plan_only": not args.confirm,
            "already_applied": already_applied,
            "repair_total": f"{repair_total():.2f}",
            "backup": str(backup) if backup else None,
            "plan": plan,
            "created_payment_ledger_rows": created,
            "active_gl_before": gl_before,
            "active_gl_after": gl_after,
            "active_payment_ledger_before": ple_before,
            "active_payment_ledger_after": ple_after,
            "pass": passed,
        }
        text = json.dumps(result, indent=2, default=str)
        print(text, flush=True)
        if report_path:
            report_path.write_text(text + "\n", encoding="utf-8")
        return 0
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        if not already_connected:
            frappe.destroy()
            os.chdir(original_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
