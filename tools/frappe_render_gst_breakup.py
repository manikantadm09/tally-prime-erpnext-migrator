"""Read-only: render India Compliance GST breakup for one invoice."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--doctype", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    import frappe

    sites_path = Path("/apps/frappe-bench/sites")
    os.chdir(sites_path)
    frappe.init(site=args.site, sites_path=str(sites_path), force=True)
    frappe.connect()
    try:
        from india_compliance.gst_india.utils.jinja import get_gst_breakup

        print(get_gst_breakup(frappe.get_doc(args.doctype, args.name)))
        frappe.db.rollback()
        return 0
    finally:
        frappe.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
