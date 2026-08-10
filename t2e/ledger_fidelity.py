"""Reclassify target-substituted invoice accounts to exact Tally ledgers.

ERPNext/India Compliance can preserve an invoice's total while substituting a
statutory GST account, normalising an account name, or adding a rounding row.
This loader compares the complete source ledger vector with the active GL for
each migrated invoice (including party-control bridges), then creates one
same-date, balanced, idempotent Journal Entry for only the difference.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .lines import parse_entries
from .mapping import CompanyDefaults, LedgerResolver
from .staging import Staging

PENNY = Decimal("0.01")
MIGRATED_DOCTYPES = (
    "Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry",
)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PENNY, rounding=ROUND_HALF_UP)


def account_deltas(desired: dict, actual: dict) -> dict:
    """Return non-zero desired-minus-actual Dr-minus-Cr account deltas."""
    result = {}
    for key in set(desired) | set(actual):
        delta = (_money(desired.get(key)) - _money(actual.get(key))).quantize(PENNY)
        if delta:
            result[key] = delta
    return result


class LedgerFidelityLoader:
    def __init__(self, erp: ERPNextClient, store: Staging,
                 defaults: CompanyDefaults, resolver: LedgerResolver):
        self.erp = erp
        self.store = store
        self.defaults = defaults
        self.resolver = resolver
        self.cfg = get_config()

    def _plans(self):
        company = self.defaults.name
        accounts = self.erp.get_list(
            "Account",
            fields=["name"],
            filters=[["company", "=", company]],
            limit=0,
        )
        canonical_account = {
            " ".join(row["name"].split()).lower(): row["name"]
            for row in accounts
        }

        def canonical(name: str) -> str:
            return canonical_account.get(" ".join(name.split()).lower(), name)

        documents = {}
        by_tag = defaultdict(list)
        for doctype in MIGRATED_DOCTYPES:
            rows = self.erp.get_list(
                doctype,
                fields=["name", self.cfg.idempotency_field, "docstatus"],
                filters=[
                    ["company", "=", company],
                    [self.cfg.idempotency_field, "is", "set"],
                    ["docstatus", "=", 1],
                ],
                limit=0,
            )
            for row in rows:
                documents[row["name"]] = doctype
                by_tag[row[self.cfg.idempotency_field]].append(row["name"])

        gl_rows = self.erp.get_list(
            "GL Entry",
            fields=[
                "voucher_no", "account", "party_type", "party", "debit", "credit",
            ],
            filters=[["company", "=", company], ["is_cancelled", "=", 0]],
            limit=0,
        )
        gl_by_document = defaultdict(list)
        for row in gl_rows:
            if row["voucher_no"] in documents:
                gl_by_document[row["voucher_no"]].append(row)

        invoice_rows = [
            row for row in self.store.vouchers()
            if row["load_status"] == "loaded"
            and row["erp_doctype"] in ("Sales Invoice", "Purchase Invoice")
        ]
        plans = []
        for row in invoice_rows:
            guid = row["guid"]
            bridge_tag = f"{guid}:ledger-fidelity-bridge"
            desired = defaultdict(lambda: Decimal("0.00"))
            for entry in parse_entries(json.loads(row["payload"])):
                resolved = self.resolver.get(entry["ledger"])
                if resolved is None:
                    raise ERPNextError(
                        f"Ledger-fidelity source is unresolved for {guid}: "
                        f"{entry['ledger']}")
                key = (
                    canonical(resolved.account),
                    resolved.party_type if resolved.kind == "party" else None,
                    resolved.party if resolved.kind == "party" else None,
                )
                desired[key] += _money(entry["mag"]) * (
                    1 if entry["debit"] else -1)

            actual = defaultdict(lambda: Decimal("0.00"))
            for tag in (
                guid, f"{guid}:party-control-bridge", bridge_tag,
            ):
                for name in by_tag.get(tag, []):
                    for gl in gl_by_document.get(name, []):
                        key = (
                            gl["account"], gl.get("party_type"), gl.get("party"),
                        )
                        actual[key] += _money(gl.get("debit")) - _money(gl.get("credit"))

            deltas = account_deltas(desired, actual)
            if not deltas:
                continue
            residual = sum(deltas.values(), Decimal("0.00"))
            if residual:
                raise ERPNextError(
                    f"Ledger-fidelity plan is unbalanced for {guid}: {residual}")
            plans.append((row, bridge_tag, deltas))
        return invoice_rows, plans

    def run(self, progress=lambda *args: None) -> dict[str, int]:
        invoice_rows, plans = self._plans()
        stats = {
            "checked": len(invoice_rows),
            "planned": len(plans) if self.erp.dry_run else 0,
            "created": 0,
            "error": 0,
        }
        if self.erp.dry_run:
            return stats
        for index, (row, bridge_tag, deltas) in enumerate(plans, 1):
            accounts = []
            for (account, party_type, party), delta in sorted(
                    deltas.items(),
                    key=lambda item: tuple(str(value or "") for value in item[0])):
                line = {"account": account}
                if party_type and party:
                    line.update({"party_type": party_type, "party": party})
                else:
                    line["cost_center"] = self.defaults.cost_center
                if delta > 0:
                    line["debit_in_account_currency"] = float(delta)
                else:
                    line["credit_in_account_currency"] = float(-delta)
                accounts.append(line)
            try:
                self.erp.submit_doc("Journal Entry", {
                    "company": self.defaults.name,
                    "posting_date": row["vdate"],
                    "voucher_type": "Journal Entry",
                    "title": (
                        f"Tally ledger fidelity bridge {row['vnumber'] or ''}"
                    ).strip(),
                    "user_remark": (
                        "Reclassifies ERPNext/India Compliance substituted "
                        "accounts back to exact mapped Tally ledgers; no voucher "
                        "value change."
                    ),
                    "accounts": accounts,
                    self.cfg.idempotency_field: bridge_tag,
                })
                stats["created"] += 1
            except ERPNextError:
                # A timeout may occur after commit. Re-check the idempotency key
                # before treating the bridge as failed.
                existing = self.erp.find_by_field(
                    "Journal Entry", self.cfg.idempotency_field, bridge_tag,
                    exclude_cancelled=True,
                )
                if existing:
                    stats["created"] += 1
                else:
                    stats["error"] += 1
                    raise
            if index % 50 == 0:
                progress(index, len(plans), stats)
        progress(len(plans), len(plans), stats)
        return stats
