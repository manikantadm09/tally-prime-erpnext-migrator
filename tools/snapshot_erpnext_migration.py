"""Create and verify a read-only API snapshot before cleaning migration data."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from t2e.config import get_config  # noqa: E402
from t2e.erpnext_client import ERPNextClient  # noqa: E402
from t2e.wipe import MASTER_DOCTYPES, TXN_DOCTYPES  # noqa: E402


def fetch_doc(doctype: str, name: str) -> dict:
    erp = ERPNextClient(dry_run=True)
    dt = urllib.parse.quote(doctype, safe="")
    nm = urllib.parse.quote(name, safe="")
    return erp._request("GET", f"/api/resource/{dt}/{nm}")["data"]


def main() -> int:
    cfg = get_config()
    company = cfg.erpnext["company"]
    field = cfg.idempotency_field
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT / "data" / "backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    data_path = backup_dir / "erpnext_migration_documents.jsonl.gz"
    staging_src = ROOT / "data" / "staging.sqlite"
    staging_dst = backup_dir / "staging.sqlite"
    shutil.copy2(staging_src, staging_dst)

    erp = ERPNextClient(dry_run=True)
    targets: list[tuple[str, str]] = []
    source_counts = Counter()
    for doctype in TXN_DOCTYPES + MASTER_DOCTYPES:
        filters = [[field, "is", "set"]]
        if doctype in TXN_DOCTYPES + ["Account", "Cost Center"]:
            filters.insert(0, ["company", "=", company])
        try:
            rows = erp.get_list(
                doctype, fields=["name"], filters=filters)
        except Exception:
            # New doctypes/custom fields may not exist in the pre-repair site.
            rows = []
        source_counts[doctype] = len(rows)
        targets.extend((doctype, row["name"]) for row in rows)

    # Include target-company settings and migration custom fields because the
    # repair may update them even though they are not migration-tagged docs.
    targets.append(("Company", company))
    source_counts["Company"] = 1
    custom_fields = erp.get_list(
        "Custom Field",
        fields=["name"],
        filters=[["fieldname", "in", [
            field, "tally_supplier_invoice_no", "tally_voucher_number",
        ]]],
    )
    targets.extend(("Custom Field", row["name"]) for row in custom_fields)
    source_counts["Custom Field"] = len(custom_fields)

    hasher = hashlib.sha256()
    written_counts = Counter()
    errors = []
    with gzip.open(data_path, "wt", encoding="utf-8", newline="\n") as fh:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_doc, doctype, name): (doctype, name)
                for doctype, name in targets
            }
            for future in as_completed(futures):
                doctype, name = futures[future]
                try:
                    record = {
                        "doctype": doctype,
                        "name": name,
                        "data": future.result(),
                    }
                    line = json.dumps(
                        record, ensure_ascii=False, separators=(",", ":"))
                    fh.write(line + "\n")
                    hasher.update((line + "\n").encode("utf-8"))
                    written_counts[doctype] += 1
                except Exception as exc:
                    errors.append({
                        "doctype": doctype, "name": name,
                        "error": str(exc),
                    })

    # Independent read-back verification: valid gzip/JSON and identical counts.
    verified_counts = Counter()
    verify_hasher = hashlib.sha256()
    with gzip.open(data_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            verified_counts[record["doctype"]] += 1
            verify_hasher.update(line.encode("utf-8"))

    manifest = {
        "created_at": datetime.now().isoformat(),
        "erp_url": erp.base,
        "company": company,
        "migration_field": field,
        "source_counts": dict(source_counts),
        "written_counts": dict(written_counts),
        "verified_counts": dict(verified_counts),
        "document_sha256": hasher.hexdigest(),
        "verified_sha256": verify_hasher.hexdigest(),
        "staging_sha256": hashlib.sha256(
            staging_dst.read_bytes()).hexdigest(),
        "errors": errors,
        "usable": (
            not errors
            and dict(written_counts) == dict(verified_counts)
            and hasher.hexdigest() == verify_hasher.hexdigest()
        ),
    }
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "backup_dir": str(backup_dir),
        "manifest": str(manifest_path),
        "counts": dict(written_counts),
        "errors": len(errors),
        "usable": manifest["usable"],
    }, indent=2))
    return 0 if manifest["usable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
