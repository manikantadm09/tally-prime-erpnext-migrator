"""Command-line entry point for the Tally -> ERPNext migration.

    python -m t2e extract                 # pull Tally -> staging (read-only)
    python -m t2e load-masters [--confirm]
    python -m t2e load-vouchers [--confirm] [--type Payment] [--limit N]
    python -m t2e wipe [--confirm] [--with-masters]
    python -m t2e reconcile
    python -m t2e run-all [--confirm]     # wipe -> masters -> vouchers -> reconcile

All write operations are DRY-RUN unless --confirm is passed.
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import get_config
from .erpnext_client import ERPNextClient
from .staging import Staging
from .tally_client import TallyClient


def _banner(dry_run: bool) -> None:
    cfg = get_config()
    mode = "DRY-RUN (no writes)" if dry_run else "LIVE (writing to ERPNext)"
    print(f"=== Tally -> ERPNext migration | env: {cfg.env_name} "
          f"({cfg.erp_url}) | mode: {mode} ===")


def cmd_extract(args) -> int:
    from . import tally_export as tx
    c, s = TallyClient(), Staging()
    print("Extracting masters from Tally...")
    print("  masters:", tx.extract_masters(c, s))

    def prog(f, t, n):
        if n:
            print(f"  vouchers {f[:6]}: {n}")
    print("Extracting vouchers (month-chunked)...")
    full_history = getattr(args, "full_history", False)
    checkpoint = s.get_checkpoint()
    if full_history:
        print("  --full-history: ignoring checkpoint, scanning full configured history")
    elif checkpoint:
        print(f"  checkpoint: {checkpoint} (re-scanning lookback window forward)")
    else:
        print("  no checkpoint yet: scanning full configured history")
    voucher_counts = tx.extract_vouchers(c, s, progress=prog, full_history=full_history)
    print("  vouchers:", voucher_counts)
    from .sync_report import build_report, print_summary, write_report
    report = build_report(s)
    print_summary(report)
    json_path, csv_path = write_report(
        report, get_config().staging_db.parent / "reports" / "source_delta.json")
    print(f"  -> delta report: {json_path}, {csv_path}")
    range_to = voucher_counts.get("_range_to")
    if range_to and report["summary"]["safe_to_load_new"]:
        s.set_checkpoint(range_to)
        print(f"  checkpoint advanced to {range_to}")
    elif range_to:
        print(f"  checkpoint NOT advanced: {report['summary']['requires_decision']} "
              "record(s) need a decision first")
    s.close()
    return 0


def cmd_approve_change(args) -> int:
    """Human-approved repair for one changed/missing/cancelled voucher GUID."""
    from .approve_change import ApprovalError, approve
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    if erp.dry_run:
        print("  (dry-run: shows what would happen; pass --confirm to execute)")
    try:
        result = approve(
            erp, s, args.guid, get_config().idempotency_field,
            acknowledge_closed_period=args.acknowledge_closed_period)
        print(f"  GUID {result['guid']}: {result['source_state']} "
              f"{result['vtype']} {result['vdate']} #{result['vnumber']} "
              f"({result['erp_doctype']} {result['erp_name']})")
        print(f"  -> {result['action']}")
        rc = 0
    except ApprovalError as exc:
        print(f"  ! refused: {exc}")
        rc = 1
    s.close()
    return rc


def cmd_repair_fallback_invoice(args) -> int:
    """Replace one or more fallback JEs only after exact GL equivalence."""
    from .repair_fallback_invoices import FallbackRepairError, repair_one
    erp, store = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    results = []
    failed = False
    def json_safe(value):
        if isinstance(value, dict):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        return value
    for guid in args.guid:
        try:
            result = repair_one(
                erp, store, guid, get_config().idempotency_field,
                phase=args.phase,
                neutralize_target_gst=args.neutralize_target_gst,
                acknowledge_invalid_source_gstin=
                    args.acknowledge_invalid_source_gstin)
            results.append(result)
            print(json.dumps(json_safe(result), indent=2, default=str))
        except (FallbackRepairError, Exception) as exc:
            # Keep processing an explicitly supplied batch. Each record is
            # independently fail-closed and reports its own evidence.
            failed = True
            print(f"  ! {guid}: {exc}", file=sys.stderr)
            if args.debug:
                import traceback
                traceback.print_exc()
    store.close()
    return 1 if failed else 0


def cmd_sync_report(args) -> int:
    """Generate a read-only source change report from the staging database."""
    from .sync_report import build_report, print_summary, write_report
    s = Staging()
    report = build_report(s)
    print_summary(report)
    output = args.output or str(
        get_config().staging_db.parent / "reports" / "source_delta.json")
    json_path, csv_path = write_report(report, output)
    print(f"  -> delta report: {json_path}, {csv_path}")
    s.close()
    return 0


def _masters(erp: ERPNextClient, s: Staging):
    from .load_masters import (MasterLoader, ensure_company_address,
                               ensure_idempotency_field, fetch_company_defaults)
    if not erp.dry_run:
        ensure_idempotency_field(erp)
        print("  company GST address:",
              ensure_company_address(erp, get_config().erpnext["company"], dry_run=False))
    defaults = fetch_company_defaults(erp)
    ml = MasterLoader(erp, s, defaults)
    ml.ensure_suspense()
    print("  UOMs:", ml.load_uoms())
    print("  account groups:", ml.load_account_groups())
    print("  ledger accounts:", ml.load_ledger_accounts())
    nc, ns = ml.load_parties()
    print(f"  customers: {nc}  suppliers: {ns}")
    print("  party GST categories:", ml.sync_party_gst_categories())
    print("  cost centers:", ml.load_cost_centers())
    print("  items:", ml.load_items())
    print("  audit-only masters classified:",
          ml.classify_out_of_scope_masters())
    return defaults


def cmd_load_masters(args) -> int:
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    _masters(erp, s)
    s.close()
    return 0


def cmd_load_invoices(args) -> int:
    from .load_invoices import InvoiceLoader, ensure_generic_item
    from .load_masters import fetch_company_defaults
    from .mapping import LedgerResolver
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    defaults = fetch_company_defaults(erp)
    if not erp.dry_run:
        ensure_generic_item(erp)
    resolver = LedgerResolver(s, defaults)
    il = InvoiceLoader(erp, s, defaults, resolver)

    def prog(i, total, stats):
        print(f"  {i}/{total}  planned={stats.get('planned', 0)} "
              f"loaded={stats['loaded']} skipped={stats.get('skipped', 0)} "
              f"fallback={stats['fallback']} error={stats['error']}")
    print("  invoices:", il.run(vtype=args.type, limit=args.limit, progress=prog))
    print(f"  ({len(il.fallback)} vouchers fall back to Journal Entry)")
    s.close()
    return 0


def cmd_load_vouchers(args) -> int:
    from .load_masters import fetch_company_defaults
    from .load_vouchers import VoucherLoader
    from .mapping import LedgerResolver
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    defaults = fetch_company_defaults(erp)
    resolver = LedgerResolver(s, defaults)
    vl = VoucherLoader(erp, s, defaults, resolver)

    def prog(i, total, stats):
        print(f"  {i}/{total}  planned={stats.get('planned', 0)} "
              f"loaded={stats['loaded']} skipped={stats.get('skipped', 0)} "
              f"error={stats['error']}")
    stats = vl.run(vtype=args.type, limit=args.limit, progress=prog)
    print("  voucher load:", stats)
    if vl.unresolved:
        print(f"  ! {len(vl.unresolved)} unresolved ledgers (first 10):",
              list(vl.unresolved)[:10])
    s.close()
    return 0


def cmd_load_ledger_fidelity(args) -> int:
    """Reclassify target-substituted invoice GL back to exact Tally ledgers."""
    from .ledger_fidelity import LedgerFidelityLoader
    from .load_masters import fetch_company_defaults
    from .mapping import LedgerResolver
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    defaults = fetch_company_defaults(erp)
    loader = LedgerFidelityLoader(
        erp, s, defaults, LedgerResolver(s, defaults))

    def prog(i, total, stats):
        print(f"  {i}/{total} created={stats['created']} error={stats['error']}")
    stats = loader.run(progress=prog, only_guids=set(args.guid or []))
    print("  ledger fidelity:", stats)
    if erp.dry_run and stats["planned"]:
        print("  -> run before load-period-closing; review, then pass --confirm")
    s.close()
    return 1 if stats["error"] else 0


def _reset_staging_after_wipe(with_masters: bool) -> None:
    """Wiped ERPNext docs no longer exist, so mark their staged source rows
    pending again -- otherwise the loader would skip them on reload."""
    s = Staging()
    s.conn.execute("UPDATE voucher SET load_status='pending', erp_name=NULL, error=NULL")
    if with_masters:
        s.conn.execute("UPDATE master SET load_status='pending', erp_name=NULL, error=NULL")
        s.conn.execute("DELETE FROM party_role")
    s.conn.execute("DELETE FROM bill_ref")
    s.conn.commit()
    s.close()


def cmd_wipe(args) -> int:
    from .wipe import active_migrated_counts, wipe
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    if erp.dry_run:
        print("  (dry-run: would cancel+delete transactions; pass --confirm to execute)")
    print("  wiped:", wipe(erp, with_masters=args.with_masters))
    if not erp.dry_run:
        remaining = active_migrated_counts(erp)
        if any(remaining.values()):
            print("  ! staging NOT reset; active tagged documents remain:",
                  remaining)
            return 1
        _reset_staging_after_wipe(args.with_masters)
        print("  active tagged documents remaining: 0; staging reset")
    return 0


def cmd_wipe_db(args) -> int:
    from .db_wipe import db_wipe, reset_staging
    cfg = get_config()
    dry = not args.confirm
    if not dry and not args.all_company_transactions:
        raise SystemExit(
            "Refusing DB wipe: --confirm must be paired with "
            "--all-company-transactions. This deletes every transaction and "
            "ledger row for the configured company.")
    _banner(dry)
    print("  DB-level wipe of transactions for", cfg.erpnext["company"])
    res = db_wipe(cfg.erpnext["company"], dry_run=dry)
    for k, v in res.items():
        print(f"    {k}: {v}")
    if not dry:
        reset_staging()
        Staging().clear_bill_refs()
        print("  staging voucher status reset to pending; bill refs cleared")
    return 0


def cmd_load_bill_references(args) -> int:
    """GUID-backed Agst Ref after documents exist. Never FIFO."""
    from .guid_bill_references import GuidBillReferenceLoader, plan_guid_bill_references
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    plan = plan_guid_bill_references(s)
    print("  plan:", plan["summary"])
    if plan["exceptions"]:
        reasons = plan["summary"].get("exception_reasons") or {}
        print("  source exceptions (not invented):", reasons)
    stats = GuidBillReferenceLoader(erp, s).run(plan)
    print("  bill-reference load:", {
        k: stats[k] for k in
        ("applied", "skipped", "errors", "gl_unchanged", "pass")
    })
    print(f"  -> {stats['report']}")
    s.close()
    return 0 if stats.get("pass") else 1


def cmd_reconcile(args) -> int:
    from .reconcile import build_report, print_summary
    erp, s = ERPNextClient(dry_run=True), Staging()
    print_summary(build_report(erp, s))
    s.close()
    return 0


def cmd_reconcile_payments(args) -> int:
    from .load_masters import fetch_company_defaults
    from .reconcile_payments import PaymentReconciler
    if args.confirm and not args.acknowledge_non_tally_fifo:
        print("  ! refused: FIFO allocation does not reproduce Tally bill references. "
              "Use the exact-reference workflow for migration fidelity. If FIFO is "
              "an explicitly approved business choice, pass "
              "--acknowledge-non-tally-fifo.")
        return 2
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    if erp.dry_run:
        print("  (dry-run: computing planned FIFO allocations only; "
              "pass --confirm to reconcile)")
    pr = PaymentReconciler(erp, fetch_company_defaults(erp))

    def prog(n, stats):
        print(f"  {n} parties  planned={stats.get('planned', 0)} "
              f"reconciled={stats.get('reconciled', 0)} skipped={stats['skipped']} "
              f"error={stats['error']} allocated={stats['allocated']:,.2f}")
    stats = pr.run(only_party=args.party, limit=args.limit, progress=prog)
    print("  payment reconciliation:", stats)
    print("  -> report: data/reports/payment_reconciliation.{json,csv}")
    return 0


def cmd_repair_party_bridges(args) -> int:
    from .repair_party_bridges import PartyBridgeRepair
    cfg = get_config()
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    repair = PartyBridgeRepair(erp, cfg.idempotency_field)
    payload = repair.run(names=args.name or None)
    print(f"  party-control bridges: candidates={payload['candidate_count']} "
          f"mode={payload['mode']} pass={payload['pass']}")
    for row in payload["results"]:
        print(f"  {row['journal_entry']}: {row['status']} "
              f"references={row.get('references', 0)} {row.get('error', '')}")
    print(f"  -> report: {payload['report']}")
    return 0 if payload["pass"] else 1


def cmd_reconcile_exact_bills(args) -> int:
    from pathlib import Path
    from .exact_bill_reconciliation import ExactBillReconciler
    cfg = get_config()
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    plan = Path(args.plan) if args.plan else (
        cfg.staging_db.parent / "reports" / "exact_bill_allocation_plan.json")
    reconciler = ExactBillReconciler(erp, plan)
    payload = reconciler.run(only_party=args.party)
    print(f"  exact bill reconciliation: parties={payload['selected_parties']} "
          f"allocated={payload['allocated']} pass={payload['pass']}")
    for row in payload["results"]:
        print(f"  {row.get('party_type', '')} {row.get('party', '')}: "
              f"{row['status']} allocations={row.get('allocations', 0)} "
              f"amount={row.get('allocated', '0.00')}")
    print(f"  -> report: {payload['report']}")
    return 0


def cmd_unreconcile_tally_bill_mismatches(args) -> int:
    from pathlib import Path
    from .tally_bill_unreconciliation import TallyBillUnreconciler
    cfg = get_config()
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    plan = Path(args.plan) if args.plan else (
        cfg.staging_db.parent / "reports" /
        "tally_bill_unreconciliation_plan.json")
    payload = TallyBillUnreconciler(erp, plan).run()
    print(f"  Tally bill reset: pairs={payload['pairs']} "
          f"mode={payload['mode']} pass={payload['pass']}")
    print(f"  -> report: {payload['report']}")
    return 0


def cmd_reconcile_evidence_payment(args) -> int:
    from pathlib import Path
    from .evidence_payment_reconciliation import EvidencePaymentReconciler
    cfg = get_config()
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    plan = Path(args.plan) if args.plan else (
        cfg.staging_db.parent / "reports" / "evidence_payment_allocation_plan.json")
    reconciler = EvidencePaymentReconciler(erp, plan)
    payload = reconciler.run(
        args.payment, args.invoice,
        acknowledge_tally_deviation=args.acknowledge_tally_bill_deviation,
        acknowledge_weaker_evidence=args.acknowledge_weaker_evidence,
    )
    print(f"  evidence payment reconciliation: {args.payment} -> {args.invoice} "
          f"mode={payload['mode']} pass={payload['pass']}")
    print(f"  -> report: {payload['report']}")
    return 0


def cmd_load_closing_stock(args) -> int:
    from .load_closing_stock import ClosingStockLoader
    from .load_masters import fetch_company_defaults
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    loader = ClosingStockLoader(erp, fetch_company_defaults(erp))
    stats, results = loader.run()
    for date, opening, closing, status, name in results:
        print(f"  {date}  open={opening:>16,.2f} close={closing:>16,.2f} "
              f"delta={closing - opening:>16,.2f}  -> {status} {name or ''}")
    print("  closing-stock:", stats)
    return 0


def cmd_repair_pnl_party_ledgers(args) -> int:
    from .load_masters import fetch_company_defaults
    from .repair_pnl_party_ledgers import PnlPartyLedgerRepair
    erp, store = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    repair = PnlPartyLedgerRepair(erp, store, fetch_company_defaults(erp))
    try:
        report = repair.run(
            reopen_period_closings=args.reopen_period_closings)
    except Exception as exc:
        print(f"  ! {exc}", file=sys.stderr)
        store.close()
        return 1
    print(json.dumps(report, indent=2, default=str))
    store.close()
    return 1 if any(
        row.get("status") == "error" for row in report.get("journals") or []
    ) else 0


def cmd_repair_vendor_advance_control(args) -> int:
    from .load_masters import fetch_company_defaults
    from .repair_vendor_advance_control import VendorAdvanceControlRepair
    erp, store = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    repair = VendorAdvanceControlRepair(
        erp, store, fetch_company_defaults(erp))
    try:
        report = repair.run(
            reopen_period_closings=args.reopen_period_closings,
            rebuild_year_ends=args.rebuild_year_ends)
    except Exception as exc:
        print(f"  ! {exc}", file=sys.stderr)
        store.close()
        return 1
    print(json.dumps(report, indent=2, default=str))
    store.close()
    return 1 if any(
        row.get("status") == "error" for row in report.get("journals") or []
    ) else 0


def cmd_load_period_closing(args) -> int:
    from .load_masters import fetch_company_defaults
    from .load_period_closing import PeriodClosingLoader
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    stats, results = PeriodClosingLoader(
        erp, fetch_company_defaults(erp)).run()
    for fiscal_year, status, detail in results:
        print(f"  {fiscal_year}: {status} {detail or ''}")
    print("  period-closing:", stats)
    return 0


def cmd_pl_check(args) -> int:
    from . import pl_check
    cfg = get_config()
    pl_check.run(from_date=args.from_date or str(cfg.tally.get("from_date", "20000101")),
                 to_date=args.to_date or str(cfg.tally.get("to_date", "20990101")))
    return 0


def cmd_load_openings(args) -> int:
    from .load_masters import fetch_company_defaults
    from .load_openings import OpeningsLoader
    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)
    loader = OpeningsLoader(erp, s, fetch_company_defaults(erp))
    stats, preview, name = loader.run()
    for nm, acc, party, op in preview:
        side = f"Cr {op:,.2f}" if op > 0 else f"Dr {-op:,.2f}"
        print(f"  {nm[:34]:<36} -> {acc[:34]:<36} {('['+party+']') if party else '':<20} {side}")
    if loader.unresolved:
        print(f"  ! {len(loader.unresolved)} unresolved ledgers: {loader.unresolved}")
    print("  opening balances:", stats, "->", name)
    s.close()
    return 0


def cmd_ensure_fiscal_years(args) -> int:
    from .load_masters import ensure_fiscal_years
    cfg = get_config()
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    res = ensure_fiscal_years(
        erp,
        str(cfg.tally.get("from_date", "20220101")),
        str(cfg.tally.get("to_date", "20990101")),
        dry_run=erp.dry_run)
    for name, status in sorted(res.items()):
        print(f"  {name}: {status}")
    return 0


def cmd_bs_check(args) -> int:
    from . import bs_check
    cfg = get_config()
    bs_check.run(from_date=args.from_date or str(cfg.tally.get("from_date", "20000101")),
                 to_date=args.to_date or str(cfg.tally.get("to_date", "20990101")))
    return 0


def cmd_run_all(args) -> int:
    from .load_closing_stock import ClosingStockLoader
    from .load_invoices import InvoiceLoader, ensure_generic_item
    from .ledger_fidelity import LedgerFidelityLoader
    from .load_masters import ensure_fiscal_years
    from .load_openings import OpeningsLoader
    from .load_period_closing import PeriodClosingLoader
    from .load_vouchers import VoucherLoader
    from .mapping import LedgerResolver
    from .reconcile import build_report, print_summary

    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)

    print("\n[1/10] Safe wipe of migration-created records")
    if not erp.dry_run:
        from .wipe import wipe
        print("  wiped:", wipe(erp, with_masters=args.with_masters))
        _reset_staging_after_wipe(args.with_masters)

    print("\n[2/10] Ensure fiscal years and load masters")
    cfg = get_config()
    print("  fiscal years:", ensure_fiscal_years(
        erp,
        str(cfg.tally.get("from_date", "20220101")),
        str(cfg.tally.get("to_date", "20990101")),
        dry_run=erp.dry_run))
    defaults = _masters(erp, s)

    print("\n[3/10] Load source opening balances")
    opening_stats, _, opening_name = OpeningsLoader(
        erp, s, defaults).run()
    print("  opening balances:", opening_stats, "->", opening_name)

    print("\n[4/10] Load Sales/Purchase invoices")
    if not erp.dry_run:
        ensure_generic_item(erp)
    resolver = LedgerResolver(s, defaults)
    il = InvoiceLoader(erp, s, defaults, resolver)

    def iprog(i, total, st):
        print(f"  {i}/{total}  planned={st.get('planned', 0)} "
              f"loaded={st['loaded']} skipped={st.get('skipped', 0)} "
              f"fallback={st['fallback']} error={st['error']}")
    invoice_stats = il.run(progress=iprog)
    print("  invoices:", invoice_stats)
    if invoice_stats.get("cancelled_retries"):
        print("  ! cancelled retry shells remain on submitted replacements; "
              "purge them after verifying each GUID has a live invoice")

    print("\n[5/10] Load payments / journals with no Agst Ref")
    resolver = LedgerResolver(s, defaults)  # refresh after invoices/bill index
    vl = VoucherLoader(erp, s, defaults, resolver)

    def prog(i, total, stats):
        print(f"  {i}/{total}  planned={stats.get('planned', 0)} "
              f"loaded={stats['loaded']} skipped={stats.get('skipped', 0)} "
              f"error={stats['error']}")
    print("  voucher load:", vl.run(progress=prog))
    if vl.unresolved:
        print(f"  ! {len(vl.unresolved)} unresolved ledgers")

    print("\n[6/10] GUID-backed bill references (Agst Ref exact; never FIFO)")
    from .guid_bill_references import GuidBillReferenceLoader, plan_guid_bill_references
    bill_plan = plan_guid_bill_references(s)
    print("  bill-reference plan:", bill_plan["summary"])
    bill_stats = GuidBillReferenceLoader(erp, s).run(bill_plan)
    print("  bill-reference load:", {
        k: bill_stats[k] for k in
        ("applied", "skipped", "errors", "gl_unchanged", "pass")
        if k in bill_stats
    })
    print(f"  -> {bill_stats.get('report')}")

    print("\n[7/10] Load Tally closing-stock adjustments")
    closing_stats, closing_results = ClosingStockLoader(
        erp, defaults).run()
    for row in closing_results:
        print(" ", row)
    print("  closing-stock:", closing_stats)

    print("\n[8/10] Reclass target-substituted invoice accounts to Tally ledgers")
    fidelity_stats = LedgerFidelityLoader(
        erp, s, defaults, LedgerResolver(s, defaults)).run()
    print("  ledger fidelity:", fidelity_stats)

    print("\n[9/10] Close fiscal-year P&L to retained earnings")
    pc_stats, pc_results = PeriodClosingLoader(erp, defaults).run()
    for row in pc_results:
        print(" ", row)
    print("  period-closing:", pc_stats)

    print("\n[10/10] Reconcile")
    print_summary(build_report(erp, s))
    print("  FIFO reconcile-payments was not auto-applied and must not be used "
          "for Tally-fidelity. GUID Agst Ref is the bill-reference phase.")
    s.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="t2e", description="Tally -> ERPNext migration")
    p.add_argument("--env", choices=["prd", "dev", "uat", "PRD", "DEV", "UAT"],
                   default=None,
                   help="target environment (PRD, DEV, or UAT). "
                        "UAT is the clean remigration site. Never point UAT "
                        "at frozen-dev staging. Use: python -m t2e --env uat <command>")
    sub = p.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract")
    ex.add_argument(
        "--full-history", action="store_true",
        help="ignore the checkpoint and re-scan the full configured history "
             "(use before sign-off/cutover, not for routine updates)")
    ex.set_defaults(func=cmd_extract)

    sr = sub.add_parser(
        "sync-report",
        help="read-only delta report: new/changed/missing/cancelled Tally vouchers")
    sr.add_argument("--output", default=None, help="JSON output path (CSV is written beside it)")
    sr.set_defaults(func=cmd_sync_report)

    ac = sub.add_parser(
        "approve-change",
        help="human-approved repair for one changed/missing/cancelled voucher "
             "from sync-report")
    ac.add_argument("--guid", required=True, help="Tally GUID from sync-report")
    ac.add_argument("--confirm", action="store_true", help="execute the cancel")
    ac.add_argument(
        "--acknowledge-closed-period", action="store_true",
        help="required to proceed when the voucher's fiscal year already has "
             "a Period Closing Voucher")
    ac.set_defaults(func=cmd_approve_change)

    rf = sub.add_parser(
        "repair-fallback-invoice",
        help="replace invoice-shaped fallback JEs with GL-equivalent invoices")
    rf.add_argument("--guid", action="append", required=True,
                    help="exact Tally GUID (repeatable)")
    rf.add_argument("--confirm", action="store_true", help="execute writes")
    rf.add_argument(
        "--phase", choices=("full", "prepare", "finalize"), default="full",
        help="prepare keeps the source JE active for server-side GST metadata; "
             "finalize verifies it and then cancels the source JE")
    rf.add_argument("--debug", action="store_true",
                    help="print a traceback for a failed guarded repair")
    rf.add_argument(
        "--neutralize-target-gst", action="store_true",
        help="prepare with company-state POS to prevent target tax rewriting; "
             "requires server-side POS/GST metadata restoration before finalize")
    rf.add_argument(
        "--acknowledge-invalid-source-gstin", action="store_true",
        help="explicitly omit a checksum-invalid Tally GSTIN from ERPNext GSTIN "
             "fields while preserving the exact source value in invoice remarks")
    rf.set_defaults(func=cmd_repair_fallback_invoice)

    for name, func in [("load-masters", cmd_load_masters), ("wipe", cmd_wipe),
                       ("run-all", cmd_run_all)]:
        sp = sub.add_parser(name)
        sp.add_argument("--confirm", action="store_true", help="execute writes")
        sp.add_argument("--with-masters", action="store_true",
                        help="also wipe migration-created masters")
        sp.set_defaults(func=func)

    wdb = sub.add_parser(
        "wipe-db",
        help="DANGEROUS: delete all configured-company transactions via DB")
    wdb.add_argument("--confirm", action="store_true", help="execute deletes")
    wdb.add_argument(
        "--all-company-transactions", action="store_true",
        help="required acknowledgement: deletes non-migration transactions too")
    wdb.set_defaults(func=cmd_wipe_db)

    for nm, fn in [("load-vouchers", cmd_load_vouchers), ("load-invoices", cmd_load_invoices)]:
        lv = sub.add_parser(nm)
        lv.add_argument("--confirm", action="store_true")
        lv.add_argument("--type", default=None, help="only this Tally voucher type")
        lv.add_argument("--limit", type=int, default=0)
        lv.set_defaults(func=fn)

    br = sub.add_parser(
        "load-bill-references",
        help="GUID-backed Agst Ref after insert: exact named invoice, never FIFO")
    br.add_argument("--confirm", action="store_true", help="execute allocations")
    br.set_defaults(func=cmd_load_bill_references)

    sub.add_parser("reconcile").set_defaults(func=cmd_reconcile)

    rp = sub.add_parser("reconcile-payments",
                        help="net unallocated payments/advances against outstanding "
                             "invoices (FIFO, per party)")
    rp.add_argument("--confirm", action="store_true", help="execute reconciliation")
    rp.add_argument("--party", default=None, help="only this customer/supplier")
    rp.add_argument("--limit", type=int, default=0, help="max parties to process")
    rp.add_argument(
        "--acknowledge-non-tally-fifo", action="store_true",
        help="required with --confirm: acknowledges FIFO may change bill-wise "
             "aging and will not reproduce Tally bill references")
    rp.set_defaults(func=cmd_reconcile_payments)

    pb = sub.add_parser(
        "repair-party-bridges",
        help="replace party-control bridge JEs without invoice references")
    pb.add_argument("--confirm", action="store_true", help="execute replacements")
    pb.add_argument("--name", action="append", default=[],
                    help="repair only this Journal Entry (repeatable; use for pilot)")
    pb.set_defaults(func=cmd_repair_party_bridges)

    eb = sub.add_parser(
        "reconcile-exact-bills",
        help="apply a reviewed Tally-reference exact bill allocation plan")
    eb.add_argument("--confirm", action="store_true", help="execute reconciliation")
    eb.add_argument("--party", default=None,
                    help="only this exact customer/supplier (recommended pilot)")
    eb.add_argument("--plan", default=None,
                    help="plan JSON (default: data/reports/exact_bill_allocation_plan.json)")
    eb.set_defaults(func=cmd_reconcile_exact_bills)

    ub = sub.add_parser(
        "unreconcile-tally-bill-mismatches",
        help="unlink reviewed payment/invoice pairs contradicted by Tally bills")
    ub.add_argument("--confirm", action="store_true",
                    help="submit standard Unreconcile Payment documents")
    ub.add_argument("--plan", default=None,
                    help="plan JSON (default: data/reports/tally_bill_unreconciliation_plan.json)")
    ub.set_defaults(func=cmd_unreconcile_tally_bill_mismatches)

    ep = sub.add_parser(
        "reconcile-evidence-payment",
        help="apply one reviewed exact payment/invoice evidence pair")
    ep.add_argument("--confirm", action="store_true", help="execute reconciliation")
    ep.add_argument("--payment", required=True, help="exact Payment Entry name")
    ep.add_argument("--invoice", required=True, help="exact Sales/Purchase Invoice name")
    ep.add_argument("--plan", default=None,
                    help="evidence plan JSON (default: data/reports/evidence_payment_allocation_plan.json)")
    ep.add_argument(
        "--acknowledge-tally-bill-deviation", action="store_true",
        help="required with --confirm when the reviewed link changes Tally bill-wise status")
    ep.add_argument(
        "--acknowledge-weaker-evidence", action="store_true",
        help="required for an explicitly reviewed review/manual-confidence pair")
    ep.set_defaults(func=cmd_reconcile_evidence_payment)

    lf = sub.add_parser(
        "load-ledger-fidelity",
        help="reclass ERPNext/India Compliance substituted invoice accounts "
             "back to exact mapped Tally ledgers")
    lf.add_argument("--confirm", action="store_true", help="execute writes")
    lf.add_argument("--guid", action="append",
                    help="limit to this exact staged Tally GUID (repeatable)")
    lf.set_defaults(func=cmd_load_ledger_fidelity)

    cs = sub.add_parser("load-closing-stock",
                        help="post year-end closing-stock adjustment Journal Entries")
    cs.add_argument("--confirm", action="store_true", help="execute writes")
    cs.set_defaults(func=cmd_load_closing_stock)

    rpl = sub.add_parser(
        "repair-pnl-party-ledgers",
        help="reclass Income/Expense ledgers wrongly loaded as Customer/Supplier")
    rpl.add_argument("--confirm", action="store_true", help="execute writes")
    rpl.add_argument(
        "--reopen-period-closings", action="store_true",
        help="cancel and recreate migration PCVs covering the reclass years")
    rpl.set_defaults(func=cmd_repair_pnl_party_ledgers)

    vac = sub.add_parser(
        "repair-vendor-advance-control",
        help="one Current Assets Advances to Vendors leaf; keep suppliers; "
             "reclass Creditors debit (UAT only)")
    vac.add_argument("--confirm", action="store_true", help="execute writes")
    vac.add_argument(
        "--reopen-period-closings", action="store_true",
        help="cancel and recreate FY 2025-26 PCV only (2026-27 stays open)")
    vac.add_argument(
        "--rebuild-year-ends", action="store_true",
        help="cancel existing control JEs and post Advances deltas at each "
             "FY end from 2022-23 so every year-end BS matches Tally")
    vac.set_defaults(func=cmd_repair_vendor_advance_control)

    pcv = sub.add_parser(
        "load-period-closing",
        help="close each migrated fiscal year's P&L to retained earnings")
    pcv.add_argument("--confirm", action="store_true", help="execute writes")
    pcv.set_defaults(func=cmd_load_period_closing)

    fy = sub.add_parser("ensure-fiscal-years",
                        help="create fiscal years spanning the Tally window (fresh-site setup)")
    fy.add_argument("--confirm", action="store_true", help="execute writes")
    fy.set_defaults(func=cmd_ensure_fiscal_years)

    op = sub.add_parser("load-openings",
                        help="post Tally ledger opening balances as one opening JE")
    op.add_argument("--confirm", action="store_true", help="execute writes")
    op.set_defaults(func=cmd_load_openings)

    pc = sub.add_parser("pl-check", help="compare Tally vs ERPNext Profit & Loss")
    pc.add_argument("--from-date", default=None, help="yyyymmdd")
    pc.add_argument("--to-date", default=None, help="yyyymmdd")
    pc.set_defaults(func=cmd_pl_check)

    bc = sub.add_parser("bs-check", help="compare Tally vs ERPNext Balance Sheet")
    bc.add_argument("--from-date", default=None, help="yyyymmdd")
    bc.add_argument("--to-date", default=None, help="yyyymmdd")
    bc.set_defaults(func=cmd_bs_check)

    args = p.parse_args(argv)
    from .config import set_environment
    set_environment(getattr(args, "env", None))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
