"""Restore one Frappe site database from its own full backup, without DB root.

This is an operational rollback utility for a database that already exists.
It uses the site's own credentials, accepts only a backup inside that site's
private/backups directory, validates the dump database name, enables maintenance
mode for the import, and never prints credentials.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile


def write_json_atomic(path: Path, payload: dict) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_name, path.stat().st_mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--confirm-site", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm or args.site != args.confirm_site:
        raise SystemExit("--confirm and an exact --confirm-site are required")

    bench = Path(args.bench).resolve()
    site_dir = (bench / "sites" / args.site).resolve()
    config_path = site_dir / "site_config.json"
    backup_dir = (site_dir / "private" / "backups").resolve()
    backup = Path(args.backup).resolve()
    if backup.parent != backup_dir or not backup.name.endswith("-database.sql.gz"):
        raise SystemExit("backup must be this site's *-database.sql.gz file")
    if not backup.is_file() or backup.stat().st_size == 0:
        raise SystemExit("backup is absent or empty")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    db_name = str(config["db_name"])
    db_user = str(config.get("db_user") or db_name)
    db_password = str(config["db_password"])
    db_host = str(config.get("db_host") or "127.0.0.1")
    db_port = str(config.get("db_port") or 3306)
    with gzip.open(backup, "rt", encoding="utf-8", errors="replace") as handle:
        header = "".join(handle.readline() for _ in range(20))
    if f"Database: {db_name}" not in header:
        raise SystemExit("backup database identity does not match site_config")

    lock_path = Path(f"/tmp/t2e_restore_{args.site.replace('.', '_')}.lock")
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original_maintenance = config.get("maintenance_mode", 0)
        maintenance = dict(config)
        maintenance["maintenance_mode"] = 1
        write_json_atomic(config_path, maintenance)
        try:
            environment = os.environ.copy()
            environment["MYSQL_PWD"] = db_password
            gzip_process = subprocess.Popen(
                ["gzip", "-dc", str(backup)], stdout=subprocess.PIPE)
            assert gzip_process.stdout is not None
            database_process = subprocess.run(
                [
                    "mariadb", "--host", db_host, "--port", db_port,
                    "--user", db_user, db_name,
                ],
                stdin=gzip_process.stdout,
                env=environment,
                check=False,
            )
            gzip_process.stdout.close()
            gzip_status = gzip_process.wait()
            if gzip_status or database_process.returncode:
                raise RuntimeError(
                    f"restore failed: gzip={gzip_status}, "
                    f"mariadb={database_process.returncode}")
        finally:
            restored_config = json.loads(config_path.read_text(encoding="utf-8"))
            restored_config["maintenance_mode"] = original_maintenance
            write_json_atomic(config_path, restored_config)

    print(json.dumps({
        "site": args.site,
        "backup": str(backup),
        "database": db_name,
        "restored": True,
        "maintenance_mode": original_maintenance,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
