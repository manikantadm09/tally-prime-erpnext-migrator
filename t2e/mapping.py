"""Shared mapping helpers: Tally group -> root type, account naming, and a
ledger resolver that turns a Tally ledger name into an ERPNext posting target
(either a GL account, or a party + its control account)."""
from __future__ import annotations

from dataclasses import dataclass

from .config import get_config
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


class GroupTree:
    """Resolve a Tally group's root_type and account_type by walking ancestry."""

    def __init__(self, store: Staging):
        cfg = get_config().yaml
        self.root_map = cfg["root_type_map"]
        self.overrides = cfg["group_overrides"]
        self.party_groups = cfg["party_groups"]
        # name -> parent
        self.parent: dict[str, str] = {}
        for r in store.masters("group"):
            self.parent[r["name"]] = r["parent"] or ""

    def ancestry(self, group: str) -> list[str]:
        chain, seen = [], set()
        cur = group
        while cur and cur not in seen and cur.lower() != "primary":
            seen.add(cur)
            chain.append(cur)
            cur = self.parent.get(cur, "")
        return chain

    def primary(self, group: str) -> str:
        chain = self.ancestry(group)
        return chain[-1] if chain else group

    def root_type(self, group: str) -> str:
        prim = self.primary(group)
        spec = self.root_map.get(prim)
        return spec["root_type"] if spec else "Asset"

    def account_type(self, group: str) -> str:
        # nearest override in ancestry wins, else primary's mapped account_type
        for g in self.ancestry(group):
            if g in self.overrides:
                return self.overrides[g].get("account_type", "")
        prim = self.primary(group)
        spec = self.root_map.get(prim)
        return spec.get("account_type", "") if spec else ""

    def party_kind(self, group: str) -> str | None:
        chain = set(self.ancestry(group))
        for g in self.party_groups.get("customer", []):
            if g in chain:
                return "Customer"
        for g in self.party_groups.get("supplier", []):
            if g in chain:
                return "Supplier"
        return None


@dataclass
class Resolved:
    kind: str               # "account" | "party"
    account: str            # ERPNext account fullname to post against
    party_type: str | None = None
    party: str | None = None


class LedgerResolver:
    """Map a Tally ledger name -> ERPNext posting target, using staged masters
    (which carry the erp_name assigned during master load)."""

    def __init__(self, store: Staging, defaults: CompanyDefaults):
        self.defaults = defaults
        self.by_name: dict[str, Resolved] = {}
        self.party_by_role: dict[tuple[str, str], Resolved] = {}
        for r in store.masters("ledger"):
            if not r["erp_name"]:
                continue
            if r["erp_doctype"] in ("Customer", "Supplier"):
                ctrl = defaults.receivable if r["erp_doctype"] == "Customer" else defaults.payable
                res = Resolved("party", ctrl, r["erp_doctype"], r["erp_name"])
            else:  # Account
                res = Resolved("account", r["erp_name"])
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
            self.party_by_role[(_norm(role["ledger_name"]), party_type)] = Resolved(
                "party", ctrl, party_type, role["party"])

    def get(self, ledger_name: str) -> Resolved | None:
        return self.by_name.get(_norm(ledger_name))

    def get_party(self, ledger_name: str,
                  party_type: str) -> Resolved | None:
        return self.party_by_role.get((_norm(ledger_name), party_type))


def _norm(name: str) -> str:
    return " ".join((name or "").split())
