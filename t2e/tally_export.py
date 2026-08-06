"""Extract Tally masters and vouchers into the SQLite staging store.

Masters are exported as whole collections. Vouchers are exported **month by
month** across the configured date window: each chunk is a small, resilient
request, parsed, staged, and counted -- so a failure in one month never loses
the rest, and per-chunk counts feed the completeness check.
"""
from __future__ import annotations

import datetime as dt
from xml.etree import ElementTree as ET

from .staging import Staging
from .tally_client import TallyClient


def _text(el: ET.Element, tag: str, default: str = "") -> str:
    v = el.findtext(tag)
    return v.strip() if v else default


def _norm(name: str) -> str:
    """Collapse internal/leading/trailing whitespace in a Tally master name."""
    return " ".join((name or "").split())


def _elem_to_dict(el: ET.Element):
    """Recursively convert a Tally element into a JSON-friendly structure.

    * Leaf elements (no children) collapse to their stripped text -- attributes
      such as ``TYPE="Amount"`` are intentionally dropped so values like AMOUNT
      stay plain numeric strings rather than ``{'#text': ...}`` wrappers.
    * Container elements become dicts; repeated child tags (Tally's ``*.LIST``
      members) are gathered into lists so nothing is dropped.
    """
    if len(el) == 0:
        return (el.text or "").strip()
    node: dict = {}
    for child in el:
        cd = _elem_to_dict(child)
        if child.tag in node:
            if not isinstance(node[child.tag], list):
                node[child.tag] = [node[child.tag]]
            node[child.tag].append(cd)
        else:
            node[child.tag] = cd
    return node


# ---------------------------------------------------------------------------
# Master extraction
# ---------------------------------------------------------------------------
_MASTER_SPECS = [
    # (kind, collection type, scalar methods captured for quick columns)
    ("taxunit",    "TaxUnit",       ["Name", "GSTRegNumber", "StateName",
                                      "PinCode", "AddressName", "Address", "GUID"]),
    ("unit",       "Unit",          ["Name", "BaseUnits", "GUID"]),
    ("godown",     "Godown",        ["Name", "Parent", "GUID"]),
    ("costcategory", "CostCategory", ["Name", "GUID"]),
    ("costcentre", "CostCentre",    ["Name", "Parent", "Category", "GUID"]),
    ("group",      "Group",         ["Name", "Parent", "IsRevenue", "IsDeemedPositive", "GUID"]),
    ("stockgroup", "StockGroup",    ["Name", "Parent", "GUID"]),
    ("stockitem",  "StockItem",     ["Name", "Parent", "BaseUnits", "GUID", "OpeningBalance", "OpeningValue"]),
    ("ledger",     "Ledger",        ["Name", "Parent", "OpeningBalance", "GUID",
                                      "GSTRegistrationType", "PartyGSTIN", "LedgerPhone",
                                      "Email", "LedgerContact", "Address", "PinCode",
                                      "CountryName", "LedgerStateName", "BillCreditPeriod"]),
    ("vouchertype", "VoucherType",  ["Name", "Parent", "GUID"]),
]


def extract_masters(client: TallyClient, store: Staging) -> dict[str, int]:
    results: dict[str, int] = {}
    for kind, ctype, methods in _MASTER_SPECS:
        root = client.export_collection(
            f"masters_{kind}", ctype, methods=methods, save_as=f"master_{kind}")
        tag = ctype.upper()
        n = 0
        with store.tx():
            for el in root.findall(f".//{tag}"):
                name = _norm(el.get("NAME") or _text(el, "NAME"))
                if not name:
                    continue
                guid = _text(el, "GUID") or f"{kind}:{name}"  # synth key if no GUID
                payload = _elem_to_dict(el)
                payload["NAME"] = name
                parent = _norm(_text(el, "PARENT")) or None
                store.upsert_master(kind, guid, name, parent, payload)
                n += 1
        results[kind] = n
    return results


# ---------------------------------------------------------------------------
# Voucher extraction (month-chunked)
# ---------------------------------------------------------------------------
def _month_windows(from_yyyymmdd: str, to_yyyymmdd: str):
    start = dt.datetime.strptime(from_yyyymmdd, "%Y%m%d").date()
    end = dt.datetime.strptime(to_yyyymmdd, "%Y%m%d").date()
    # Clamp absurd upper bound to today to avoid thousands of empty months.
    end = min(end, dt.date.today())
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        win_end = min(nxt - dt.timedelta(days=1), end)
        yield cur.strftime("%Y%m%d"), win_end.strftime("%Y%m%d")
        cur = nxt


def _parse_voucher_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _voucher_total_debit(v: ET.Element) -> float:
    """Sum of absolute debit amounts across ledger entries (= voucher value)."""
    total = 0.0
    for le in v.findall("ALLLEDGERENTRIES.LIST") + v.findall("LEDGERENTRIES.LIST"):
        amt = _text(le, "AMOUNT")
        dpos = _text(le, "ISDEEMEDPOSITIVE")
        try:
            val = float(amt)
        except ValueError:
            continue
        if val < 0:  # negative AMOUNT = debit side
            total += abs(val)
    return round(total, 2)


def stage_voucher_export(
    store: Staging,
    vouchers: list[ET.Element],
    export_from: str,
    export_to: str,
    overall_to: str,
) -> tuple[dict[str, int], bool, str]:
    """Validate and stage one Tally voucher response.

    Some TallyPrime builds honour ``SVFROMDATE`` on a Voucher collection but
    ignore ``SVTODATE``.  In that case a nominal monthly request returns the
    complete suffix of the Day Book (from the requested month through the most
    recent voucher).  We accept that response only when it has the unmistakable
    shape of a complete suffix: no active voucher precedes ``export_from``, the
    response spans at least two extra months, and its latest active voucher is
    within 31 days of the requested overall end.  The whole suffix is then
    replaced atomically.  A small or ambiguous window leak still fails closed.
    """
    records: list[tuple[ET.Element, str, str, str, str, str, bool, str | None]] = []
    for idx, v in enumerate(vouchers):
        cancelled = (_text(v, "ISCANCELLED").lower() == "yes"
                     or _text(v, "ISDELETED").lower() == "yes")
        optional = _text(v, "ISOPTIONAL").lower() == "yes"
        source_present = not (cancelled or optional)
        inactive_state = "cancelled" if cancelled else ("optional" if optional else None)
        entries = (
            v.findall("ALLLEDGERENTRIES.LIST")
            + v.findall("LEDGERENTRIES.LIST")
        )
        guid = _text(v, "GUID")
        # Empty placeholder <VOUCHER> elements (no guid, no postings): skip.
        if not guid and not entries:
            continue
        vtype = (
            _text(v, "VOUCHERTYPENAME")
            or v.get("VCHTYPE")
            or "Unknown"
        )
        vnum = _text(v, "VOUCHERNUMBER")
        raw_date = _text(v, "DATE")
        parsed_date = _parse_voucher_date(raw_date)
        if not parsed_date:
            raise RuntimeError(
                "Tally returned a voucher with an invalid date: "
                f"date={raw_date!r}, type={vtype!r}, number={vnum!r}.")
        compact_date = parsed_date.replace("-", "")
        if not guid:
            guid = f"{vtype}:{vnum}:{raw_date}:{export_from}:{idx}"
        records.append(
            (v, guid, vtype, vnum, parsed_date, compact_date,
             source_present, inactive_state))

    authoritative_to = export_to
    suffix_export = False
    out_of_window = [
        r for r in records
        if not export_from <= r[5] <= export_to
    ]
    if out_of_window:
        dates = [r[5] for r in records]
        earliest = min(dates)
        latest = max(dates)
        overall_end = dt.datetime.strptime(overall_to, "%Y%m%d").date()
        latest_date = dt.datetime.strptime(latest, "%Y%m%d").date()
        export_end = dt.datetime.strptime(export_to, "%Y%m%d").date()
        looks_like_complete_suffix = (
            earliest >= export_from
            and latest <= overall_to
            and (latest_date - export_end).days >= 62
            and 0 <= (overall_end - latest_date).days <= 31
        )
        if not looks_like_complete_suffix:
            v, _guid, vtype, vnum, parsed_date, _compact, _present, _state = out_of_window[0]
            raise RuntimeError(
                "Tally returned an out-of-window voucher for "
                f"{export_from}-{export_to}: date={parsed_date!r}, "
                f"type={vtype!r}, number={vnum!r}. Refusing to stage "
                "an ambiguous or contaminated chunk.")
        authoritative_to = overall_to
        suffix_export = True

    per_type: dict[str, int] = {}
    with store.tx():
        store.clear_voucher_window(
            _parse_voucher_date(export_from),
            _parse_voucher_date(authoritative_to))
        for v, guid, vtype, vnum, parsed_date, _compact, source_present, inactive_state in records:
            payload = _elem_to_dict(v)
            store.upsert_voucher(
                guid, vtype, vnum, parsed_date,
                _text(v, "PARTYLEDGERNAME") or _text(v, "PARTYNAME"),
                _voucher_total_debit(v), payload,
                source_present=source_present, source_state=inactive_state)
            if source_present:
                per_type[vtype] = per_type.get(vtype, 0) + 1
    return per_type, suffix_export, authoritative_to


# Header fields + AllLedgerEntries (which carries amounts, Dr/Cr flag and bill
# allocations). The Voucher *collection* honours SVFROMDATE/SVTODATE (the Day
# Book report does not), so we drive extraction with a per-month collection.
_VCH_FETCH = [
    "Date", "VoucherTypeName", "VoucherNumber", "PartyLedgerName", "PartyName",
    "Narration", "Reference", "ReferenceDate", "GUID", "MasterId", "AlterId",
    "PlaceOfSupply", "StateName", "PartyGSTIN", "ISCANCELLED", "ISDELETED",
    "IsOptional", "IsPostDated", "EffectiveDate",
    "AllLedgerEntries",
]


def extract_vouchers(client: TallyClient, store: Staging,
                     progress=lambda *a: None) -> dict[str, int]:
    from_d = str(client.from_date) or "20000101"
    to_d = str(client.to_date) or dt.date.today().strftime("%Y%m%d")
    windows = list(_month_windows(from_d, to_d))
    if not windows:
        return {"_total": 0}
    overall_to = windows[-1][1]
    total = 0
    per_type: dict[str, int] = {}
    for win_from, win_to in windows:
        client.from_date, client.to_date = win_from, win_to
        root = client.export_collection(
            f"vch_{win_from}", "Voucher", fetch=_VCH_FETCH,
            dated=True, save_as=f"vch_{win_from[:6]}")
        vch = root.findall(".//VOUCHER")
        chunk_types, suffix_export, authoritative_to = stage_voucher_export(
            store, vch, win_from, win_to, overall_to)
        for vtype, count in chunk_types.items():
            per_type[vtype] = per_type.get(vtype, 0) + count
            total += count
        progress(win_from, authoritative_to, len(vch))
        if suffix_export:
            break
    per_type["_total"] = total
    return per_type
