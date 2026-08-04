"""Read-only full-company audit of optional and post-dated Tally vouchers."""
from __future__ import annotations

from datetime import date
import json

from t2e.config import get_config
from t2e.staging import Staging
from t2e.tally_client import TallyClient
from t2e.tally_export import _text
from tools.compare_staging_snapshots import compact


FIELDS = [
    "Date",
    "VoucherTypeName",
    "VoucherNumber",
    "GUID",
    "MasterId",
    "AlterId",
    "IsOptional",
    "IsPostDated",
    "EffectiveDate",
]


def main() -> None:
    cfg = get_config()
    client = TallyClient()
    client.from_date = str(cfg.tally["from_date"])
    client.to_date = str(cfg.tally["to_date"])
    root = client.export_collection(
        "audit_voucher_flags",
        "Voucher",
        fetch=FIELDS,
        dated=True,
        save_as="audit_voucher_flags",
    )
    flags = {}
    for element in root.findall(".//VOUCHER"):
        guid = _text(element, "GUID")
        if not guid:
            continue
        optional = _text(element, "ISOPTIONAL")
        postdated = _text(element, "ISPOSTDATED")
        if optional == "Yes" or postdated == "Yes":
            flags[guid] = {
                "guid": guid,
                "date": _text(element, "DATE"),
                "type": _text(element, "VOUCHERTYPENAME"),
                "number": _text(element, "VOUCHERNUMBER"),
                "master_id": _text(element, "MASTERID"),
                "alter_id": _text(element, "ALTERID"),
                "is_optional": optional,
                "is_postdated": postdated,
                "effective_date": _text(element, "EFFECTIVEDATE"),
            }
    store = Staging()
    try:
        staged = {
            row["guid"]: {
                **dict(row),
                "lines": {},
            }
            for row in store.vouchers()
        }
        # Reuse compact's source metadata but retain the authoritative captured
        # ledger lines from its payload parser.
        from tools.compare_staging_snapshots import voucher_lines

        rows = []
        for guid, flag in sorted(
            flags.items(), key=lambda item: (item[1]["date"], item[0])
        ):
            source = staged.get(guid)
            if source:
                source["lines"] = voucher_lines(source["payload"])
                detail = compact(source)
            else:
                detail = {"guid": guid, "not_in_migrated_snapshot": True}
            rows.append({**flag, "migrated_snapshot": detail})
    finally:
        store.close()
    report = {
        "generated_at": date.today().isoformat(),
        "company": client.company,
        "flagged_count": len(rows),
        "optional_count": sum(row["is_optional"] == "Yes" for row in rows),
        "postdated_count": sum(row["is_postdated"] == "Yes" for row in rows),
        "vouchers": rows,
    }
    path = (
        cfg.staging_db.parent
        / "reports"
        / f"tally_flagged_vouchers_{date.today().isoformat()}.json"
    )
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"REPORT {path}")


if __name__ == "__main__":
    main()
