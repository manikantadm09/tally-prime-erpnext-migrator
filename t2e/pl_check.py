"""Compare the Profit & Loss between Tally and ERPNext.

Two layers:
  * Headline: Tally's native P&L report buckets (Sales, Purchases, Direct/
    Indirect expenses, Closing Stock, Net Profit) vs ERPNext's Income/Expense
    totals from GL Entry -- with the inventory reconciling item (Closing Stock,
    which a voucher-level migration without stock does not reproduce) called out.
  * Per-account: every P&L ledger's net movement in Tally vs the matching
    ERPNext account, so income/expense gaps can be traced to specific accounts.

A full-history window is used (P&L accounts open at zero from inception, so the
period balance equals the cumulative balance).
"""
from __future__ import annotations

import pymysql

from .config import DATA_DIR, get_config
from .mapping import GroupTree, acc_name
from .staging import Staging
from .tally_client import TallyClient, sanitize_xml
from xml.etree import ElementTree as ET


# ---- Tally side ----------------------------------------------------------
def tally_pl_report(client: TallyClient, from_date: str, to_date: str) -> dict[str, float]:
    """Headline P&L buckets from Tally's native 'Profit and Loss' report."""
    envelope = (
        '<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>'
        '<BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Profit and Loss</REPORTNAME>'
        '<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'<SVCURRENTCOMPANY>{client.company}</SVCURRENTCOMPANY>'
        f'<SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE>'
        f'<SVTODATE TYPE="Date">{to_date}</SVTODATE>'
        '</STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>'
    )
    raw = client._post(envelope)
    (DATA_DIR / "raw" / "tally_pl.xml").write_text(raw, encoding="utf-8")
    root = ET.fromstring(sanitize_xml(raw))
    names = [(e.findtext("DSPDISPNAME") or "").strip()
             for e in root.findall(".//DSPACCNAME")]
    amts = []
    for pl in root.findall(".//PLAMT"):
        sub = (pl.findtext("PLSUBAMT") or "").strip()
        main = (pl.findtext("BSMAINAMT") or "").strip()
        amts.append(_f(sub or main))
    buckets = {n: a for n, a in zip(names, amts) if n}
    return buckets


def tally_pl_accounts(client: TallyClient, store: Staging,
                      from_date: str, to_date: str) -> dict[str, float]:
    """Per-ledger closing balance for every P&L (Income/Expense) ledger.

    Tally sign convention: income ledgers carry a credit (negative) closing
    balance, expense ledgers a debit (positive) one. We return the natural
    P&L magnitude: +income, +expense both positive, tagged by root via the sign
    we normalize against ERPNext separately.
    """
    client.from_date, client.to_date = from_date, to_date
    root = client.export_collection(
        "pl_ledgers", "Ledger",
        methods=["Name", "Parent", "ClosingBalance"], dated=True,
        save_as="pl_ledgers")
    tree = GroupTree(store)
    out: dict[str, float] = {}
    for el in root.findall(".//LEDGER"):
        name = (el.get("NAME") or "").strip()
        parent = (el.findtext("PARENT") or "").strip()
        if not name:
            continue
        rt = tree.root_type(parent)
        if rt not in ("Income", "Expense"):
            continue
        bal = _f(el.findtext("CLOSINGBALANCE"))
        # Tally CLOSINGBALANCE sign: credit positive, debit negative. Income is
        # a credit (+bal), expense a debit (-bal); normalize both to positive
        # P&L magnitudes to match the ERPNext side.
        out[" ".join(name.split())] = (bal if rt == "Income" else -bal)
    return out


# ---- ERPNext side --------------------------------------------------------
def erpnext_pl_accounts(company: str) -> tuple[dict[str, float], float, float]:
    """Per-account P&L net from GL Entry, plus income & expense totals."""
    p = get_config().db_params
    conn = pymysql.connect(host=p["host"], port=p["port"], user=p["user"],
                           password=p["password"], database=p["database"],
                           connect_timeout=20)
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT a.account_name, a.root_type,
                      ROUND(SUM(g.credit-g.debit),2)
               FROM `tabGL Entry` g JOIN `tabAccount` a ON g.account=a.name
               WHERE g.company=%s AND g.is_cancelled=0
                 AND a.root_type IN ('Income','Expense')
               GROUP BY a.account_name, a.root_type""", (company,))
        accounts, inc, exp = {}, 0.0, 0.0
        for acc_nm, rt, net in cur.fetchall():
            net = float(net)
            # income: credit positive => +net ; expense: debit positive => -net
            val = net if rt == "Income" else -net
            accounts[" ".join(acc_nm.split())] = val
            if rt == "Income":
                inc += val
            else:
                exp += val
        return accounts, round(inc, 2), round(exp, 2)
    finally:
        conn.close()


def _f(s) -> float:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return 0.0


# ---- comparison ----------------------------------------------------------
def run(from_date="20000101", to_date="20990101", top=25) -> None:
    cfg = get_config()
    company = cfg.erpnext["company"]
    client, store = TallyClient(), Staging()

    print("Fetching Tally P&L report ...")
    buckets = tally_pl_report(client, from_date, to_date)
    print("Fetching Tally per-ledger balances ...")
    t_acc = tally_pl_accounts(client, store, from_date, to_date)
    print("Fetching ERPNext P&L from GL ...")
    e_acc, e_inc, e_exp = erpnext_pl_accounts(company)

    closing_stock = abs(buckets.get("Less: Closing Stock", 0.0))
    t_sales = abs(buckets.get("Sales Accounts", 0.0))
    t_ind_inc = abs(buckets.get("Indirect Incomes", 0.0))
    t_income = t_sales + t_ind_inc
    t_purch = abs(buckets.get("Add: Purchase Accounts", 0.0))
    t_direct = abs(buckets.get("Direct Expenses", 0.0))
    t_indirect = abs(buckets.get("Indirect Expenses", 0.0))
    t_expense = t_purch + t_direct + t_indirect
    t_net = t_income - t_expense + closing_stock

    print("\n================  P&L HEADLINE  ================")
    print(f"{'':22}{'Tally':>18}{'ERPNext':>18}{'Diff':>15}")
    _line("Income", t_income, e_inc)
    _line("Expense", t_expense, e_exp)
    print(f"  {'Closing Stock (Tally inv.)':22}{closing_stock:>18,.2f}{0:>18,.2f}"
          f"{closing_stock:>15,.2f}")
    e_net = e_inc - e_exp
    _line("Net Profit (incl. stock)", t_net, e_net)
    _line("Net Profit (excl. stock)", t_net - closing_stock, e_net)
    print("  Note: Tally P&L includes Closing Stock (inventory) which a "
          "voucher-only\n        migration does not reproduce -- that is the "
          "main reconciling item.")

    # per-account diffs (matched case-insensitively on normalized name)
    t_low = {k.lower(): (k, v) for k, v in t_acc.items()}
    e_low = {k.lower(): (k, v) for k, v in e_acc.items()}
    rows = []
    for key in sorted(set(t_low) | set(e_low)):
        name = (e_low.get(key) or t_low.get(key))[0]
        tv = t_low.get(key, (None, 0.0))[1]
        ev = e_low.get(key, (None, 0.0))[1]
        d = round(ev - tv, 2)
        if abs(d) >= 1.0:
            rows.append((abs(d), name, tv, ev, d))
    rows.sort(reverse=True)

    rep = DATA_DIR / "reports" / "pl_compare.csv"
    import csv
    with rep.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["account", "tally", "erpnext", "diff(erp-tally)"])
        for _, name, tv, ev, d in rows:
            w.writerow([name, tv, ev, d])

    print(f"\n===========  TOP {top} ACCOUNT DIFFERENCES  ===========")
    print(f"{'account':<34}{'Tally':>16}{'ERPNext':>16}{'Diff':>14}")
    for _, name, tv, ev, d in rows[:top]:
        print(f"  {name[:32]:<32}{tv:>16,.2f}{ev:>16,.2f}{d:>14,.2f}")
    print(f"\nFull per-account comparison -> {rep}")
    print(f"Accounts with a difference >= Rs 1: {len(rows)}")
    store.close()


def _line(label, t, e):
    print(f"  {label:22}{t:>18,.2f}{e:>18,.2f}{e - t:>15,.2f}")
