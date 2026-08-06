"""Human-approved repair for one changed, missing, or cancelled Tally voucher.

Normal extract/load commands never overwrite a submitted ERPNext document.
This module is the explicit, per-GUID exception after an operator reviews the
source-delta report.  It does not automate payment unwinding or fiscal-year
closing reversal.
"""
from __future__ import annotations

from .erpnext_client import ERPNextClient

RESOLVABLE_STATES = ("changed", "missing", "cancelled")


class ApprovalError(RuntimeError):
    """The repair was refused before any ERPNext write was attempted."""


def _fiscal_year_of(vdate: str) -> str:
    year, month, _day = (int(p) for p in vdate.split("-"))
    start = year if month >= 4 else year - 1
    return f"{start}-{start + 1}"


def _closed_fiscal_year(erp: ERPNextClient, field: str, vdate: str | None) -> str | None:
    if not vdate:
        return None
    fy = _fiscal_year_of(vdate)
    key = f"period-closing-{fy}"
    return fy if erp.find_by_field("Period Closing Voucher", field, key) else None


def _invoice_has_allocations(erp: ERPNextClient, doctype: str, name: str) -> bool:
    if doctype not in ("Sales Invoice", "Purchase Invoice"):
        return False
    rows = erp.get_list(
        doctype, fields=["outstanding_amount", "grand_total"],
        filters=[["name", "=", name]], limit=1)
    if not rows:
        return False
    outstanding = float(rows[0].get("outstanding_amount") or 0)
    total = float(rows[0].get("grand_total") or 0)
    return abs(outstanding - total) > 0.01


def preview(erp: ERPNextClient, store, guid: str, field: str) -> dict:
    """Read-only risk assessment for one explicitly named Tally GUID."""
    row = store.voucher_by_guid(guid)
    if row is None:
        raise ApprovalError(f"no staged voucher for GUID {guid!r}")
    if row["source_state"] not in RESOLVABLE_STATES:
        raise ApprovalError(
            f"GUID {guid!r} is {row['source_state']!r}, not one of "
            f"{RESOLVABLE_STATES}; nothing to approve")
    info = {
        "guid": guid, "source_state": row["source_state"],
        "vtype": row["vtype"], "vdate": row["vdate"], "vnumber": row["vnumber"],
        "erp_doctype": row["erp_doctype"], "erp_name": row["erp_name"],
        "blocking": [], "closed_fiscal_year": None,
    }
    if row["erp_doctype"] and row["erp_name"]:
        if _invoice_has_allocations(erp, row["erp_doctype"], row["erp_name"]):
            info["blocking"].append(
                f"{row['erp_doctype']} {row['erp_name']} has a payment allocated "
                "against it (outstanding != grand total); unwind the payment(s) "
                "in ERPNext before approving this GUID")
    info["closed_fiscal_year"] = _closed_fiscal_year(erp, field, row["vdate"])
    return info


def approve(erp: ERPNextClient, store, guid: str, field: str,
            acknowledge_closed_period: bool = False) -> dict:
    """Cancel one approved target doc and stage a changed source for reload."""
    info = preview(erp, store, guid, field)
    if info["blocking"]:
        raise ApprovalError("; ".join(info["blocking"]))
    if info["closed_fiscal_year"] and not acknowledge_closed_period:
        raise ApprovalError(
            f"{info['vdate']} falls inside fiscal year {info['closed_fiscal_year']}, "
            "which already has a Period Closing Voucher. Cancelling/reloading this "
            "voucher will not update it. Re-run with --acknowledge-closed-period "
            "only once finance has a plan to reverse and redo the closing voucher(s).")
    erp_doctype, erp_name = info["erp_doctype"], info["erp_name"]
    if erp_doctype and erp_name:
        erp.cancel(erp_doctype, erp_name)
    if erp.dry_run:
        target = f"{erp_doctype} {erp_name}" if erp_name else "(nothing staged in ERPNext)"
        return {**info, "action": f"dry-run: would cancel {target}"}
    if info["source_state"] == "changed":
        store.reopen_for_reload(guid)
        action = "cancelled; staged for reload with the new Tally content"
    else:
        store.resolve(guid)
        action = "cancelled and resolved (no replacement to load)"
    store.conn.commit()
    return {**info, "action": action}
