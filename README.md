# Tally Prime ERPNext Migrator

An auditable migration tool for moving accounting masters and vouchers from
Tally Prime into ERPNext through Tally's local XML gateway and the ERPNext REST
API.

The project captures source XML before transformation, identifies source
records by Tally GUID, requires explicit confirmation before writes, and
compares migrated ERPNext GL entries with the source voucher postings.

## Data safety

This repository intentionally contains no client data, credentials, Tally XML,
ERPNext exports, or database files. Keep all of these local:

- `.env.erpnext`
- `data/`
- `*.xml`, `*.pdf`, `*.sqlite`
- the real `config.yaml` for a client

Never commit a client's company name, GSTIN, vouchers, ERPNext API key, API
secret, or financial reports.

## Architecture

```text
Tally Prime XML gateway
        |
        v
data/raw/                 captured source responses (local only)
        |
        v
data/staging.sqlite        parsed, idempotent staging (local only)
        |
        v
ERPNext REST API
        |
        v
Submitted ERPNext documents and GL entries
        |
        v
data/reports/              reconciliation evidence (local only)
```

## What it migrates

- Tally groups and ledgers into ERPNext accounts.
- Sundry Debtors and Sundry Creditors into Customers and Suppliers.
- Party billing addresses, contacts, and valid GST registrations.
- Sales, Purchase, Credit Note, and Debit Note vouchers into ERPNext invoices.
- Receipts, Payments, Journals, and Contra vouchers into Payment Entries or
  Journal Entries while preserving their GL lines.
- Tally bill references for invoice/payment allocation.
- Tally opening balances, optional closing-stock adjustments, and fiscal-year
  Period Closing Vouchers.

## Prerequisites

- Tally Prime with the intended company loaded and its XML/HTTP gateway enabled.
- An isolated ERPNext test company or test site.
- An ERPNext API user with permissions for Company, Account, Customer, Supplier,
  invoices, Payment Entry, Journal Entry, GL Entry, Fiscal Year, and Custom
  Fields.
- Python 3.11 or later.

Check the local services before a run:

```powershell
Test-NetConnection 127.0.0.1 -Port 9000
Invoke-WebRequest http://127.0.0.1:8000/api/method/ping
```

## Setup

```powershell
git clone <your-new-repository-url>
cd tally-prime-erpnext-migrator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.erpnext.example .env.erpnext
```

Edit the local-only files before running:

1. Put the ERPNext URL and API credentials in `.env.erpnext`.
2. Set Tally company, source dates, ERPNext company, state, GSTIN, and stock
   settings in `config.yaml`.
3. Add approved Tally closing-stock balances and fiscal years only when they
   apply to the client.

`config.yaml` in this repository is a safe template. Do not run it unchanged.

## Safe migration workflow

All state-changing commands require `--confirm`.

```powershell
# 1. Extract Tally into local-only raw XML and SQLite staging.
python -m t2e extract

# 2. Inspect expected source records and mapping before any write.
python -m t2e reconcile

# 3. Build masters and vouchers in the isolated ERPNext target.
python -m t2e run-all --confirm

# 4. Re-run reconciliation and financial checks.
python -m t2e reconcile
python -m t2e pl-check --from-date 20240401 --to-date 20250331
python -m t2e bs-check --from-date 20240401 --to-date 20250331
```

For a controlled, incremental run, use `load-masters`, `load-invoices`,
`load-vouchers`, `load-openings`, `load-closing-stock`,
`load-ledger-fidelity`, and `load-period-closing` individually, in that order.
`load-ledger-fidelity` compares each invoice's submitted GL with the exact
source ledger vector and creates only balanced, same-date reclassifications for
accounts substituted by ERPNext/India Compliance. It must run before period
closing. Run the default dry-run path first and take an ERPNext backup before
using `--confirm`.

`load-masters` also rebinds staged ledger mappings to the current target
company. This matters when the same staging database was first used on a local
test company with a different abbreviation. It promotes only blank or
Unregistered parties when a checksum-valid voucher GSTIN proves registration;
invalid GSTINs are reported and are not forced into ERPNext. Tally master kinds
outside the accounting-only scope (Godown, Cost Category, Tax Unit, and Voucher
Type metadata) are explicitly marked skipped in staging instead of remaining
misleadingly pending.

Payment allocation is intentionally not applied by `run-all`. The legacy
`reconcile-payments` command uses FIFO within each party. FIFO can be useful as
an explicitly approved ERPNext business policy, but it **does not reproduce
Tally bill references** and can incorrectly mark deliberately open bills as
paid. It must not be used for a Tally-fidelity migration. A live FIFO run now
requires the additional `--acknowledge-non-tally-fifo` acknowledgement:

```powershell
python -m t2e reconcile-payments
# Inspect data/reports/payment_reconciliation.{json,csv} and obtain approval.
python -m t2e reconcile-payments --confirm --acknowledge-non-tally-fifo
```

Party-control bridge Journal Entries are GL reclassifications, not payments.
Current versions create them without invoice references. For a site migrated by
an older version, inspect and unlink those references separately (dry-run first,
then a single-document pilot using `--name`). This uses ERPNext's supported
Unreconcile Payment workflow and does not cancel/repost historical accounting
entries or alter debit/credit values:

```powershell
python -m t2e repair-party-bridges
python -m t2e repair-party-bridges --name ACC-JV-YYYY-NNNNN --confirm
```

For bill-wise fidelity, export Tally's native outstanding reports, compare
them with the submitted invoices, and build a source-reference plan:

```powershell
python -m tools.fetch_tally_native_report bills-receivable --from-date 20220101 --to-date 20260812
python -m tools.fetch_tally_native_report bills-payable --from-date 20220101 --to-date 20260812
python -m tools.verify_invoice_outstandings_api
python -m tools.plan_exact_bill_allocations_api
python -m t2e reconcile-exact-bills                 # full dry-run validation
python -m t2e reconcile-exact-bills --party "Name" # recommended pilot
python -m t2e reconcile-exact-bills --party "Name" --confirm
python -m t2e reconcile-exact-bills --confirm       # reviewed remainder
```

The exact reconciler is bound to a fresh hashed verification report, validates
live invoice/payment availability before every write, targets one named
invoice/payment pair at a time, and verifies that active GL remains unchanged.
It refuses Sales/Purchase Invoice rows as payment sources: ERPNext reconciles a
return invoice by creating a same-control-account Journal Entry, which preserves
balances but adds debit/credit turnover absent from Tally. Those cases are
reported as `erpnext_return_reconciliation_turnover_exception`; do not feed
them back into the ordinary exact reconciler. Negative
differences where Tally's bill amount exceeds the underlying invoice GL are
reported separately as `source_bill_vs_gl_exception`; never alter accounting
entries merely to imitate such bill metadata.

For the five proved Spaceki return documents, use the narrowly scoped
server-side `tools/frappe_repair_return_bill_references.py`. It delinks the five
self-referencing Payment Ledger rows, creates six exact Tally bill references
(the shared ₹16,231 return is split as ₹124 + ₹16,107), and recalculates invoice
status without creating GL Entries. It is plan-only by default, is bound to the
exact company/documents/parties/accounts/before-and-after amounts, requires a
real database backup for `--confirm`, and rolls back unless active GL and total
Payment Ledger values remain unchanged:

```bash
# Run from the Frappe bench root with its Python.
./env/bin/python /path/to/frappe_repair_return_bill_references.py \
  --site dev.spaceki.com \
  --company "Spaceki Designs LLP" \
  --confirm-company "Spaceki Designs LLP"

# Only after reviewing the plan and taking a fresh backup:
./env/bin/python /path/to/frappe_repair_return_bill_references.py \
  --site dev.spaceki.com \
  --company "Spaceki Designs LLP" \
  --confirm-company "Spaceki Designs LLP" \
  --backup /apps/frappe-bench/sites/dev.spaceki.com/private/backups/FRESH-database.sql.gz \
  --confirm --report /tmp/return_bill_reference_repair.json
```

After applying, rerun `tools.verify_invoice_outstandings_api`,
`tools.audit_invoice_status_api`, and `tools.verify_financials_api`. The six
return exceptions must disappear, ordinary overdue amounts must continue to
match Tally, and the GL replay/signature must remain exact.

If a reviewed run was interrupted after ERPNext created return-reconciliation
JEs, cancel them with `tools.revert_generated_return_reconciliations` and use
the narrowly scoped server-side
`tools/frappe_purge_generated_return_journals.py` after a fresh site backup to
remove their immutable cancelled shells. Always run the purge without
`--confirm` first and require identical active-GL signatures before/after.

## Repeat extraction and source changes

For later client updates, first run:

```powershell
python -m t2e extract
python -m t2e sync-report
```

The read-only `sync-report` writes `data/reports/source_delta.json` and CSV
evidence that classifies vouchers as `new`, `changed`, `missing`, `cancelled`,
or `optional`. New pending vouchers can continue through the normal dry-run
then confirmed loader. Changed, missing, and cancelled source vouchers are
never overwritten or cancelled automatically: an ERPNext document may have
allocations or period closing downstream and needs an explicitly approved
repair workflow.

For one reviewed GUID, preview the guarded repair first:

```powershell
python -m t2e approve-change --guid <tally-guid>
```

Only use `--confirm` after approval. The command refuses invoices with payment
allocations and requires an explicit closed-period acknowledgement when a
Period Closing Voucher exists. It never reverses Period Closing Vouchers.

Routine extracts use a 90-day configurable checkpoint lookback after the first
clean run. Before production cutover or final sign-off, force a complete source
sweep so edits or cancellations older than that window are also checked:

```powershell
python -m t2e extract --full-history
python -m t2e sync-report
```

## Acceptance criteria

Accept a migration only after all of the following are true:

- Source extraction count matches the approved Tally Day Book population.
- Optional, cancelled, deleted, and post-dated voucher handling is agreed with
  the client and applied consistently.
- Every approved source voucher has a matching, balanced ERPNext GL result.
- Opening and closing ledger balances reconcile.
- Balance Sheet, Profit and Loss, Accounts Receivable, and Accounts Payable
  agree with the client-approved Tally reports.
- Any ERPNext-only stock or period-closing entries are documented and separately
  reconciled; do not compare their gross Trial Balance movement directly with
  raw Tally Day Book turnover.

Use `python tools/verify_financials_api.py` for the read-only, document-linked
GL replay. The verifier resolves accounts and submitted documents from their
live `tally_guid`, so a stale test-site suffix in staging cannot create a false
result. It reports exact voucher matches separately from accepted one-paise
tolerance exceptions and names every exception.

ERPNext's standard Balance Sheet headline can differ visually from Tally even
when the ledger replay is exact. Tally may move debit balances under Current
Liabilities to the asset side and display the current loss as `Profit & Loss
A/c`; ERPNext shows the same current loss as Provisional Profit/Loss. Compare
the underlying ledger balances and the accounting equation, not only the four
dashboard cards.

## Development

```powershell
python -m unittest discover -s tests -v
python -m compileall t2e tools
```

## License

Add the license chosen for the new repository before publishing it.
