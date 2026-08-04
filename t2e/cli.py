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
    print("  vouchers:", tx.extract_vouchers(c, s, progress=prog))
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
    print("  cost centers:", ml.load_cost_centers())
    print("  items:", ml.load_items())
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
    from .wipe import wipe
    erp = ERPNextClient(dry_run=not args.confirm)
    _banner(erp.dry_run)
    if erp.dry_run:
        print("  (dry-run: would cancel+delete transactions; pass --confirm to execute)")
    print("  wiped:", wipe(erp, with_masters=args.with_masters))
    if not erp.dry_run:
        _reset_staging_after_wipe(args.with_masters)
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


def cmd_reconcile(args) -> int:
    from .reconcile import build_report, print_summary
    erp, s = ERPNextClient(dry_run=True), Staging()
    print_summary(build_report(erp, s))
    s.close()
    return 0


def cmd_reconcile_payments(args) -> int:
    from .load_masters import fetch_company_defaults
    from .reconcile_payments import PaymentReconciler
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
    from .load_masters import ensure_fiscal_years
    from .load_openings import OpeningsLoader
    from .load_period_closing import PeriodClosingLoader
    from .load_vouchers import VoucherLoader
    from .mapping import LedgerResolver
    from .reconcile import build_report, print_summary

    erp, s = ERPNextClient(dry_run=not args.confirm), Staging()
    _banner(erp.dry_run)

    print("\n[1/9] Safe wipe of migration-created records")
    if not erp.dry_run:
        from .wipe import wipe
        print("  wiped:", wipe(erp, with_masters=args.with_masters))
        _reset_staging_after_wipe(args.with_masters)

    print("\n[2/9] Ensure fiscal years and load masters")
    cfg = get_config()
    print("  fiscal years:", ensure_fiscal_years(
        erp,
        str(cfg.tally.get("from_date", "20220101")),
        str(cfg.tally.get("to_date", "20990101")),
        dry_run=erp.dry_run))
    defaults = _masters(erp, s)

    print("\n[3/9] Load source opening balances")
    opening_stats, _, opening_name = OpeningsLoader(
        erp, s, defaults).run()
    print("  opening balances:", opening_stats, "->", opening_name)

    print("\n[4/9] Load Sales/Purchase invoices")
    if not erp.dry_run:
        ensure_generic_item(erp)
    resolver = LedgerResolver(s, defaults)
    il = InvoiceLoader(erp, s, defaults, resolver)

    def iprog(i, total, st):
        print(f"  {i}/{total}  planned={st.get('planned', 0)} "
              f"loaded={st['loaded']} skipped={st.get('skipped', 0)} "
              f"fallback={st['fallback']} error={st['error']}")
    print("  invoices:", il.run(progress=iprog))

    print("\n[5/9] Load payments / journals (linked to invoices)")
    resolver = LedgerResolver(s, defaults)  # refresh after invoices/bill index
    vl = VoucherLoader(erp, s, defaults, resolver)

    def prog(i, total, stats):
        print(f"  {i}/{total}  planned={stats.get('planned', 0)} "
              f"loaded={stats['loaded']} skipped={stats.get('skipped', 0)} "
              f"error={stats['error']}")
    print("  voucher load:", vl.run(progress=prog))
    if vl.unresolved:
        print(f"  ! {len(vl.unresolved)} unresolved ledgers")

    print("\n[6/9] Load Tally closing-stock adjustments")
    closing_stats, closing_results = ClosingStockLoader(
        erp, defaults).run()
    for row in closing_results:
        print(" ", row)
    print("  closing-stock:", closing_stats)

    print("\n[7/9] Close fiscal-year P&L to retained earnings")
    pc_stats, pc_results = PeriodClosingLoader(erp, defaults).run()
    for row in pc_results:
        print(" ", row)
    print("  period-closing:", pc_stats)

    print("\n[8/9] Reconcile payment advances against invoices")
    from .reconcile_payments import PaymentReconciler
    payment_stats = PaymentReconciler(erp, defaults).run()
    print("  payment reconciliation:", payment_stats)

    print("\n[9/9] Reconcile")
    print_summary(build_report(erp, s))
    s.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="t2e", description="Tally -> ERPNext migration")
    p.add_argument("--env", choices=["prd", "dev", "PRD", "DEV"], default=None,
                   help="target environment (PRD or DEV); default from config/PRD. "
                        "Use as: python -m t2e --env dev <command>")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("extract").set_defaults(func=cmd_extract)

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

    sub.add_parser("reconcile").set_defaults(func=cmd_reconcile)

    rp = sub.add_parser("reconcile-payments",
                        help="net unallocated payments/advances against outstanding "
                             "invoices (FIFO, per party)")
    rp.add_argument("--confirm", action="store_true", help="execute reconciliation")
    rp.add_argument("--party", default=None, help="only this customer/supplier")
    rp.add_argument("--limit", type=int, default=0, help="max parties to process")
    rp.set_defaults(func=cmd_reconcile_payments)

    cs = sub.add_parser("load-closing-stock",
                        help="post year-end closing-stock adjustment Journal Entries")
    cs.add_argument("--confirm", action="store_true", help="execute writes")
    cs.set_defaults(func=cmd_load_closing_stock)

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
