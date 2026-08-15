"""Shared mapping helpers: Tally group -> root type, account naming, and a
ledger resolver that turns a Tally ledger name into an ERPNext posting target
(either a GL account, or a party + its control account)."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from .config import get_config
from .lines import is_round_ledger, is_tax_ledger, parse_entries
from .staging import Staging


@dataclass
class CompanyDefaults:
    name: str
    abbr: str
    receivable: str
    payable: str
    round_off: str
    cost_center: str
    currency: str
    suspense: str                  # fallback account for unresolved ledgers
    root_by_type: dict[str, str]   # root_type -> ERPNext root account name


def acc_name(account_name: str, abbr: str) -> str:
    """ERPNext account fullname is '<account_name> - <abbr>'."""
    return f"{account_name} - {abbr}"


def _fold(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def _truthy(value) -> bool:
    return str(value or "").strip().casefold() in {"yes", "y", "1", "true"}


# Keyword fallback when a client renamed the standard Tally party groups.
_CUSTOMER_HINTS = (
    "sundry debtor",
    "advances to debtor",
    "advance to debtor",
    "advance from customer",
    "advances from customer",
)
_SUPPLIER_HINTS = (
    "sundry creditor",
    "advances to vendor",
    "advance to vendor",
    "advances to supplier",
    "advance to supplier",
)
_NON_PARTY_ACCOUNT_TYPES = {"Bank", "Cash", "Tax", "Stock"}
_PNL_ROOT_TYPES = {"Income", "Expense"}
_INVOICE_CUSTOMER_TYPES = {"Sales", "Credit Note"}
_INVOICE_SUPPLIER_TYPES = {"Purchase", "Debit Note"}


class GroupTree:
    """Resolve a Tally group's root_type and account_type by walking ancestry."""

    def __init__(self, store: Staging):
        cfg = get_config().yaml
        self.root_map = cfg["root_type_map"]
        self.overrides = cfg["group_overrides"]
        self.party_groups = cfg["party_groups"]
        self._root_map_fold = {_fold(k): v for k, v in self.root_map.items()}
        self._overrides_fold = {_fold(k): v for k, v in self.overrides.items()}
        # name -> parent
        self.parent: dict[str, str] = {}
        self._parent_fold: dict[str, str] = {}
        for r in store.masters("group"):
            self.parent[r["name"]] = r["parent"] or ""
            self._parent_fold[_fold(r["name"])] = r["parent"] or ""

    def ancestry(self, group: str) -> list[str]:
        chain, seen = [], set()
        cur = group
        while cur and cur not in seen and cur.lower() != "primary":
            seen.add(cur)
            chain.append(cur)
            cur = self.parent.get(cur) or self._parent_fold.get(_fold(cur), "")
        return chain

    def primary(self, group: str) -> str:
        chain = self.ancestry(group)
        return chain[-1] if chain else group

    def root_type(self, group: str) -> str:
        prim = self.primary(group)
        spec = self.root_map.get(prim) or self._root_map_fold.get(_fold(prim))
        return spec["root_type"] if spec else "Asset"

    def account_type(self, group: str) -> str:
        # nearest override in ancestry wins, else primary's mapped account_type
        for g in self.ancestry(group):
            spec = self.overrides.get(g) or self._overrides_fold.get(_fold(g))
            if spec:
                return spec.get("account_type", "")
        prim = self.primary(group)
        spec = self.root_map.get(prim) or self._root_map_fold.get(_fold(prim))
        return spec.get("account_type", "") if spec else ""

    def party_kind(self, group: str) -> str | None:
        chain = self.ancestry(group)
        folded = {_fold(g) for g in chain}
        for g in self.party_groups.get("customer", []):
            if _fold(g) in folded:
                return "Customer"
        for g in self.party_groups.get("supplier", []):
            if _fold(g) in folded:
                return "Supplier"
        blob = " | ".join(folded)
        supplier = any(hint in blob for hint in _SUPPLIER_HINTS)
        customer = any(hint in blob for hint in _CUSTOMER_HINTS)
        if supplier and not customer:
            return "Supplier"
        if customer and not supplier:
            return "Customer"
        return None

    def is_non_party_account(self, group: str) -> bool:
        return self.account_type(group) in _NON_PARTY_ACCOUNT_TYPES


@dataclass
class Resolved:
    kind: str               # "account" | "party"
    account: str            # ERPNext account fullname to post against
    party_type: str | None = None
    party: str | None = None
    account_type: str = ""


class LedgerResolver:
    """Map a Tally ledger name -> ERPNext posting target, using staged masters
    (which carry the erp_name assigned during master load)."""

    def __init__(self, store: Staging, defaults: CompanyDefaults):
        self.defaults = defaults
        self.by_name: dict[str, Resolved] = {}
        self.party_by_role: dict[tuple[str, str], Resolved] = {}
        tree = GroupTree(store)
        for r in store.masters("ledger"):
            if not r["erp_name"]:
                continue
            if r["erp_doctype"] in ("Customer", "Supplier"):
                ctrl = defaults.receivable if r["erp_doctype"] == "Customer" else defaults.payable
                acct_type = "Receivable" if r["erp_doctype"] == "Customer" else "Payable"
                res = Resolved("party", ctrl, r["erp_doctype"], r["erp_name"], acct_type)
            else:  # Account
                res = Resolved(
                    "account", r["erp_name"],
                    account_type=tree.account_type(r["parent"] or ""),
                )
            # Tally ledger names sometimes carry trailing/leading spaces that the
            # voucher reference lacks; key on the normalized form.
            self.by_name[_norm(r["name"])] = res
            if res.kind == "party":
                self.party_by_role[(_norm(r["name"]), res.party_type)] = res
        for role in store.party_roles():
            party_type = role["party_type"]
            ctrl = (
                defaults.receivable
                if party_type == "Customer"
                else defaults.payable
            )
            acct_type = "Receivable" if party_type == "Customer" else "Payable"
            self.party_by_role[(_norm(role["ledger_name"]), party_type)] = Resolved(
                "party", ctrl, party_type, role["party"], acct_type)

    def get(self, ledger_name: str) -> Resolved | None:
        return self.by_name.get(_norm(ledger_name))

    def get_party(self, ledger_name: str,
                  party_type: str) -> Resolved | None:
        return self.party_by_role.get((_norm(ledger_name), party_type))


def classify_party_ledgers(store: Staging, tree: GroupTree) -> dict[str, set[str]]:
    """Return {normalized ledger name: {Customer, Supplier}}.

    A Tally ledger is a party on Debtors/Creditors when it is used as a
    bill-wise party or sits under a debtor/creditor/advance group. Bank,
    cash, tax, rounding, and Income/Expense ledgers stay named GL leaves.
    Bill-wise ON an expense/income ledger must not create a Customer/Supplier:
    those postings belong on P&L, not Debtors/Creditors.
    """
    roles: dict[str, set[str]] = defaultdict(set)
    masters = { _norm(r["name"]): r for r in store.masters("ledger") }

    def skip_named_gl(name: str, group: str) -> bool:
        if is_tax_ledger(name) or is_round_ledger(name):
            return True
        if tree.is_non_party_account(group):
            return True
        if tree.root_type(group) in _PNL_ROOT_TYPES:
            return True
        # Deposits, GST payable, prepaid, cash-advance leaves, etc. are named
        # GL accounts even when Tally bill-wise is on. Only debtor/creditor/
        # advance groups become Customer/Supplier.
        return tree.party_kind(group) is None

    def add(name: str, kind: str | None) -> None:
        key = _norm(name)
        if not key or not kind:
            return
        master = masters.get(key)
        group = (master["parent"] if master else "") or ""
        if skip_named_gl(key, group):
            return
        roles[key].add(kind)

    for r in store.masters("ledger"):
        group = r["parent"] or ""
        if skip_named_gl(r["name"], group):
            continue
        add(r["name"], tree.party_kind(group))
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        if _truthy((payload or {}).get("ISBILLWISEON")):
            kind = tree.party_kind(group)
            if not kind:
                kind = (
                    "Supplier" if tree.root_type(group) == "Asset" else "Customer"
                )
            add(r["name"], kind)

    for v in store.vouchers():
        if v["vtype"] in _INVOICE_CUSTOMER_TYPES:
            add(v["party"] or "", "Customer")
        elif v["vtype"] in _INVOICE_SUPPLIER_TYPES:
            add(v["party"] or "", "Supplier")

    for v in store.vouchers():
        payload = json.loads(v["payload"] or "{}")
        for entry in parse_entries(payload):
            if not entry.get("bills"):
                continue
            ledger = entry["ledger"]
            master = masters.get(_norm(ledger))
            group = (master["parent"] if master else "") or ""
            kind = tree.party_kind(group)
            if not kind:
                if v["vtype"] in _INVOICE_CUSTOMER_TYPES or v["vtype"] == "Receipt":
                    kind = "Customer"
                elif v["vtype"] in _INVOICE_SUPPLIER_TYPES or v["vtype"] == "Payment":
                    kind = "Supplier"
                elif tree.root_type(group) == "Asset":
                    kind = "Supplier"
                else:
                    kind = "Customer"
            add(ledger, kind)

    return {name: kinds for name, kinds in roles.items() if kinds}


def _norm(name: str) -> str:
    return " ".join((name or "").split())
