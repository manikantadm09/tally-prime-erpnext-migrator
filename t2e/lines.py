"""Shared parsing of a Tally voucher's ledger entries into normalized GL lines,
plus classification (party / tax / rounding / item) used by both the invoice
loader and the journal/payment loader."""
from __future__ import annotations

import re

TAX_RE = re.compile(r"\b(CGST|SGST|IGST|UTGST|CESS|TDS|TCS)\b", re.IGNORECASE)


def as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def to_float(x) -> float:
    if isinstance(x, dict):
        x = x.get("#text", "")
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_entries(payload: dict) -> list[dict]:
    """Each entry: {ledger, mag, debit, bills:[{name,type,amount}]}.

    Tally's AMOUNT sign is authoritative: negative => debit, positive => credit.
    """
    out = []
    raw = as_list(payload.get("ALLLEDGERENTRIES.LIST")) \
        or as_list(payload.get("LEDGERENTRIES.LIST"))
    for le in raw:
        if not isinstance(le, dict):
            continue
        ledger = le.get("LEDGERNAME")
        if not ledger:
            continue
        amt = to_float(le.get("AMOUNT"))
        bills = []
        for b in as_list(le.get("BILLALLOCATIONS.LIST")):
            if isinstance(b, dict) and b.get("NAME"):
                bills.append({"name": b.get("NAME"), "type": b.get("BILLTYPE"),
                              "amount": abs(to_float(b.get("AMOUNT"))),
                              "bill_date": b.get("BILLDATE")
                                  or b.get("BILLCREATIONDATE"),
                              "credit_period": b.get("BILLCREDITPERIOD")})
        out.append({"ledger": ledger, "mag": abs(amt), "debit": amt < 0,
                    "bills": bills})
    return out


def is_tax_ledger(name: str) -> bool:
    return bool(TAX_RE.search(name or ""))


def is_round_ledger(name: str) -> bool:
    return "ROUND" in (name or "").upper()
