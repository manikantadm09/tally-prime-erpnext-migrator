"""Compare the Balance Sheet between Tally and ERPNext.

Mirrors ``pl_check`` for the balance-sheet (Asset / Liability / Equity) side:

  * Headline: root-type totals (Tally per-ledger closing, natural sign) vs
    ERPNext GL, calling out the two structural reconciling items a GL-faithful
    voucher migration leaves behind --
      - Retained earnings: ERPNext keeps the period's net profit in Income/
        Expense until a Period-Closing voucher is posted, so its books are out of
        balance by exactly that amount, which Tally already parks on the BS as
        the "Profit & Loss A/c" line.
      - Capital grouping: ERPNext's CoA files the Capital Account under
        root_type Liability, whereas Tally reports it as Equity -- so we also
        show the combined Liability+Equity figure, which should match.
  * Party control: Tally keeps a ledger per debtor/creditor; ERPNext consolidates
    them into the Debtors / Creditors control accounts (party as a dimension).
    A name-by-name diff is therefore meaningless for parties, so we aggregate the
    Tally Sundry Debtors / Sundry Creditors groups and compare to the ERPNext
    control totals.
  * Per-account: every non-party BS ledger's balance, matched case-insensitively;
    full table -> data/reports/bs_compare.csv.

Balances are cumulative to ``to_date`` (a balance sheet is a point-in-time
snapshot), so the full-history window is used by default.
"""
from __future__ import annotations

import csv

import pymysql

from .config import DATA_DIR, get_config
from .mapping import GroupTree
from .staging import Staging
from .tally_client import TallyClient

BS_ROOTS = ("Asset", "Liability", "Equity")


def _f(s) -> float:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return 0.0


# ---- Tally side ----------------------------------------------------------
def tally_bs_accounts(client: TallyClient, store: Staging, from_date: str,
                      to_date: str) -> tuple[dict[str, float], dict[str, float],
                                             dict[str, float]]:
    """Per-ledger natural balances for every BS ledger, plus root totals and the
    Sundry Debtors / Sundry Creditors group aggregates (party controls)."""
    client.from_date, client.to_date = from_date, to_date
    root = client.export_collection(
        "bs_ledgers", "Ledger",
        methods=["Name", "Parent", "ClosingBalance"], dated=True,
        save_as="bs_ledgers")
    tree = GroupTree(store)
    accounts: dict[str, float] = {}
    totals = {r: 0.0 for r in BS_ROOTS}
    party = {"Sundry Debtors": 0.0, "Sundry Creditors": 0.0}
    for el in root.findall(".//LEDGER"):
        name = (el.get("NAME") or "").strip()
        if not name:
            continue
        parent = (el.findtext("PARENT") or "").strip()
        rt = tree.root_type(parent)
        if rt not in BS_ROOTS:
            continue
        bal = _f(el.findtext("CLOSINGBALANCE"))
        # Tally CLOSINGBALANCE: credit positive, debit negative. Assets carry a
        # debit balance, so flip them to a positive "natural" figure; liabilities
        # and equity keep their credit-positive value.
        val = -bal if rt == "Asset" else bal
        accounts[" ".join(name.split())] = val
        totals[rt] += val
        anc = set(tree.ancestry(parent))
        for grp in party:
            if grp in anc:
                party[grp] += val
    return accounts, totals, party


# ---- ERPNext side --------------------------------------------------------
def erpnext_bs_accounts(company: str) -> tuple[dict[str, float], dict[str, float],
                                               dict[str, float], float]:
    """Per-account BS balances from GL, root-type totals, control-account totals,
    and the net P&L still sitting unclosed in Income/Expense."""
    p = get_config().db_params
    conn = pymysql.connect(host=p["host"], port=p["port"], user=p["user"],
                           password=p["password"], database=p["database"],
                           connect_timeout=20)
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT a.account_name, a.root_type,
                      ROUND(SUM(g.debit-g.credit),2)
               FROM `tabGL Entry` g JOIN `tabAccount` a ON g.account=a.name
               WHERE g.company=%s AND g.is_cancelled=0
                 AND a.root_type IN ('Asset','Liability','Equity')
               GROUP BY a.account_name, a.root_type""", (company,))
        accounts, totals, controls = {}, {r: 0.0 for r in BS_ROOTS}, {}
        for acc_nm, rt, net in cur.fetchall():
            net = float(net)
            # debit-credit: assets stay positive; liabilities/equity flip to +.
            val = net if rt == "Asset" else -net
            nm = " ".join(acc_nm.split())
            accounts[nm] = val
            totals[rt] += val
            if nm in ("Debtors", "Creditors"):
                controls[nm] = val
        # Net P&L still in Income/Expense (== the amount the BS is out of balance
        # by, until a period-closing / retained-earnings entry is posted).
        cur.execute(
            """SELECT ROUND(SUM(g.debit-g.credit),2)
               FROM `tabGL Entry` g JOIN `tabAccount` a ON g.account=a.name
               WHERE g.company=%s AND g.is_cancelled=0
                 AND a.root_type IN ('Income','Expense')""", (company,))
        unclosed_pl = float(cur.fetchone()[0] or 0.0)
        return accounts, {k: round(v, 2) for k, v in totals.items()}, controls, unclosed_pl
    finally:
        conn.close()


# ---- comparison ----------------------------------------------------------
def run(from_date="20000101", to_date="20990101", top=25) -> None:
    cfg = get_config()
    company = cfg.erpnext["company"]
    client, store = TallyClient(), Staging()

    print("Fetching Tally balance-sheet ledgers ...")
    t_acc, t_tot, t_party = tally_bs_accounts(client, store, from_date, to_date)
    print("Fetching ERPNext balance sheet from GL ...")
    e_acc, e_tot, e_ctrl, unclosed = erpnext_bs_accounts(company)

    print("\n================  BALANCE SHEET HEADLINE  ================")
    print(f"{'':26}{'Tally':>18}{'ERPNext':>18}{'Diff':>15}")
    for rt in BS_ROOTS:
        _line(rt, t_tot[rt], e_tot[rt])
    # ERPNext files Capital under Liabilities; Tally reports it as Equity. The
    # combined figure is the like-for-like comparison.
    _line("Liability + Equity", t_tot["Liability"] + t_tot["Equity"],
          e_tot["Liability"] + e_tot["Equity"])
    print("\n  Reconciling items (expected for a GL-faithful voucher migration):")
    print(f"  - ERPNext BS is out of balance by {unclosed:,.2f} = net P&L still in")
    print("    Income/Expense (no Period-Closing voucher). Tally shows this on the")
    print("    BS as its 'Profit & Loss A/c' line.")
    print("  - Capital Account is root_type Liability in ERPNext's CoA vs Equity in")
    print("    Tally -- compare the combined Liability+Equity row above.")

    print("\n================  PARTY CONTROL ACCOUNTS  ================")
    print(f"{'':26}{'Tally':>18}{'ERPNext':>18}{'Diff':>15}")
    _line("Debtors (Sundry Debtors)", t_party["Sundry Debtors"],
          e_ctrl.get("Debtors", 0.0))
    _line("Creditors (Sundry Cred.)", t_party["Sundry Creditors"],
          e_ctrl.get("Creditors", 0.0))
    print("  Note: individual party ledgers appear only in Tally; ERPNext holds"
          "\n        them in the control accounts above with the party as a dimension.")

    # per-account diffs, excluding party ledgers (which never match by name)
    party_names = _party_ledger_names(store)
    t_low = {k.lower(): (k, v) for k, v in t_acc.items()}
    e_low = {k.lower(): (k, v) for k, v in e_acc.items()}
    rows = []
    for key in sorted(set(t_low) | set(e_low)):
        name = (e_low.get(key) or t_low.get(key))[0]
        if name in party_names:
            continue
        tv = t_low.get(key, (None, 0.0))[1]
        ev = e_low.get(key, (None, 0.0))[1]
        d = round(ev - tv, 2)
        if abs(d) >= 1.0:
            rows.append((abs(d), name, tv, ev, d))
    rows.sort(reverse=True)

    rep = DATA_DIR / "reports" / "bs_compare.csv"
    with rep.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["account", "tally", "erpnext", "diff(erp-tally)"])
        for _, name, tv, ev, d in rows:
            w.writerow([name, tv, ev, d])

    print(f"\n=======  TOP {top} NON-PARTY ACCOUNT DIFFERENCES  =======")
    print(f"{'account':<34}{'Tally':>16}{'ERPNext':>16}{'Diff':>14}")
    for _, name, tv, ev, d in rows[:top]:
        print(f"  {name[:32]:<32}{tv:>16,.2f}{ev:>16,.2f}{d:>14,.2f}")
    print(f"\nFull per-account comparison -> {rep}")
    print(f"Non-party accounts with a difference >= Rs 1: {len(rows)}")
    store.close()


def _party_ledger_names(store: Staging) -> set[str]:
    """Tally ledgers that map to an ERPNext party -- excluded from the per-account
    diff because ERPNext consolidates them into the control accounts."""
    tree = GroupTree(store)
    names: set[str] = set()
    for row in store.conn.execute("SELECT name, parent FROM master WHERE kind='ledger'"):
        parent = (row["parent"] or "").strip()
        if tree.party_kind(parent):
            names.add(" ".join((row["name"] or "").split()))
    return names


def _line(label, t, e):
    print(f"  {label:26}{t:>18,.2f}{e:>18,.2f}{e - t:>15,.2f}")
