"""Clean-slate reset of migrated transactions.

The user chose "wipe & re-migrate clean". To stay safe we only ever cancel +
delete migration-created transactions carrying ``tally_guid`` in the target
company. Unrelated company transactions are never selected. The standard chart
of accounts and party masters are NOT mass-deleted by default. Pass
``with_masters=True`` to also remove migration-created masters (those carrying
the tally_guid field).

Every deletion is gated behind the client's dry-run flag (only off with --confirm).
"""
from __future__ import annotations

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError

TXN_DOCTYPES = [
    "Period Closing Voucher", "Journal Entry", "Payment Entry",
    "Sales Invoice", "Purchase Invoice",
]
MASTER_DOCTYPES = [
    "Address", "Contact", "Customer", "Supplier",
    "Item", "Cost Center", "Account",
]


def _delete_all(erp: ERPNextClient, doctype: str, company: str | None,
                only_migrated: bool, field: str, progress) -> int:
    # A newly introduced migration doctype (for example Period Closing
    # Voucher, Address or Contact on an older target) may not have the custom
    # idempotency field yet.  Such a doctype cannot contain migration-tagged
    # records, so it is safely an empty wipe scope.  Avoid sending an invalid
    # filtered query that Frappe rejects with HTTP 417.
    has_field = getattr(erp, "has_field", None)
    if (
        only_migrated
        and callable(has_field)
        and not has_field(doctype, field)
    ):
        progress(
            f"  skipped {doctype}: custom field {field!r} is not installed")
        return 0
    filters = []
    if company and doctype in (TXN_DOCTYPES + ["Cost Center", "Account"]):
        filters.append(["company", "=", company])
    if only_migrated:
        filters.append([field, "is", "set"])
    rows = erp.get_list(doctype, fields=["name", "docstatus"],
                        filters=filters or None, limit=0)
    n = 0
    for r in rows:
        name = r["name"]
        try:
            if r.get("docstatus") == 1:
                erp.cancel(doctype, name)
            erp.delete(doctype, name)
            n += 1
        except ERPNextError as exc:
            progress(f"  ! {doctype} {name}: {str(exc)[:120]}")
    return n


def wipe(erp: ERPNextClient, with_masters: bool = False,
         progress=print) -> dict[str, int]:
    cfg = get_config()
    company = cfg.erpnext["company"]
    field = cfg.idempotency_field
    result: dict[str, int] = {}

    # Transactions first (they reference masters). Only rows carrying the
    # migration GUID are in scope; never delete unrelated company activity.
    for dt in TXN_DOCTYPES:
        result[dt] = _delete_all(erp, dt, company, only_migrated=True,
                                 field=field, progress=progress)
        progress(f"  wiped {result[dt]} {dt}")

    if with_masters:
        # Only migration-created masters (carry tally_guid). Accounts last.
        for dt in MASTER_DOCTYPES:
            result[dt] = _delete_all(erp, dt, company, only_migrated=True,
                                     field=field, progress=progress)
            progress(f"  wiped {result[dt]} {dt} (migrated only)")
    return result
