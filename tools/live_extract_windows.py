"""Resumable read-only voucher extraction from a live Tally company.

Each configured window is requested and committed independently, so completed
windows remain staged if Tally becomes slow or unavailable on a later window.
This script never connects to ERPNext.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from t2e.staging import Staging
from t2e.tally_client import TallyClient
from t2e.tally_export import (
    _VCH_FETCH,
    _elem_to_dict,
    _parse_voucher_date,
    _text,
    _voucher_total_debit,
)


def quarter_windows(from_date: str, to_date: str):
    start = dt.datetime.strptime(from_date, "%Y%m%d").date()
    end = dt.datetime.strptime(to_date, "%Y%m%d").date()
    current = start
    while current <= end:
        quarter = (current.month - 1) // 3
        quarter_end_month = quarter * 3 + 3
        if quarter_end_month == 12:
            next_quarter = dt.date(current.year + 1, 1, 1)
        else:
            next_quarter = dt.date(current.year, quarter_end_month + 1, 1)
        window_end = min(end, next_quarter - dt.timedelta(days=1))
        yield current.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")
        current = window_end + dt.timedelta(days=1)


def extract_window(
    client: TallyClient,
    store: Staging,
    start: str,
    end: str,
    raw_prefix: str = "live_vch",
):
    client.from_date, client.to_date = start, end
    root = client.export_collection(
        f"live_vch_{start}",
        "Voucher",
        fetch=_VCH_FETCH,
        dated=True,
        save_as=f"{raw_prefix}_{start}_{end}",
    )
    raw_count = 0
    staged_count = 0
    skipped_count = 0
    out_of_window = 0
    per_type: collections.Counter[str] = collections.Counter()
    vouchers = root.findall(".//VOUCHER")
    with store.tx():
        for index, voucher in enumerate(vouchers):
            raw_count += 1
            if (
                _text(voucher, "ISCANCELLED") == "Yes"
                or _text(voucher, "ISDELETED") == "Yes"
            ):
                skipped_count += 1
                continue
            entries = voucher.findall("ALLLEDGERENTRIES.LIST")
            entries += voucher.findall("LEDGERENTRIES.LIST")
            guid = _text(voucher, "GUID")
            if not guid and not entries:
                skipped_count += 1
                continue
            voucher_type = (
                _text(voucher, "VOUCHERTYPENAME")
                or voucher.get("VCHTYPE")
                or "Unknown"
            )
            voucher_number = _text(voucher, "VOUCHERNUMBER")
            raw_date = _text(voucher, "DATE")
            parsed_date = _parse_voucher_date(raw_date)
            compact_date = (parsed_date or "").replace("-", "")
            if not parsed_date or not (start <= compact_date <= end):
                out_of_window += 1
                continue
            if not guid:
                guid = (
                    f"{voucher_type}:{voucher_number}:{raw_date}:"
                    f"{start}:{index}"
                )
            payload = _elem_to_dict(voucher)
            store.upsert_voucher(
                guid,
                voucher_type,
                voucher_number,
                parsed_date,
                _text(voucher, "PARTYLEDGERNAME")
                or _text(voucher, "PARTYNAME"),
                _voucher_total_debit(voucher),
                payload,
            )
            staged_count += 1
            per_type[voucher_type] += 1
    return {
        "raw": raw_count,
        "staged": staged_count,
        "skipped": skipped_count,
        "out_of_window": out_of_window,
        "types": dict(sorted(per_type.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", default="20220101")
    parser.add_argument(
        "--to-date", default=dt.date.today().strftime("%Y%m%d")
    )
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--db",
        default=None,
        help="optional isolated staging database path",
    )
    parser.add_argument(
        "--raw-prefix",
        default="live_vch",
        help="prefix for captured raw XML files",
    )
    args = parser.parse_args()

    client = TallyClient()
    client.timeout = args.timeout
    store = Staging(args.db)
    try:
        for start, end in quarter_windows(args.from_date, args.to_date):
            print(f"START {start}-{end}", flush=True)
            result = extract_window(
                client,
                store,
                start,
                end,
                raw_prefix=args.raw_prefix,
            )
            print(f"DONE  {start}-{end} {result}", flush=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
