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

Payment allocation is intentionally not applied by `run-all`. Review it as a
separate operation because it uses FIFO within each party and can change many
invoice/payment references without changing the GL:

```powershell
python -m t2e reconcile-payments
# Inspect data/reports/payment_reconciliation.{json,csv} and obtain approval.
python -m t2e reconcile-payments --confirm
```

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

## Development

```powershell
python -m unittest discover -s tests -v
python -m compileall t2e tools
```

## License

Add the license chosen for the new repository before publishing it.
