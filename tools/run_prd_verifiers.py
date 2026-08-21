"""Point the existing read-only verifiers at production, then run them."""
from __future__ import annotations

import os
import sys

from tools.live_tally_prd_e2e import point_config_at_production
from t2e.config import get_config


def main() -> int:
    os.environ.setdefault("TALLY_URL", "http://127.0.0.1:9001")
    point_config_at_production()
    cfg = get_config()
    print(f"ERPNext: {cfg.erp_url}")
    which = sys.argv[1] if len(sys.argv) > 1 else "all"

    if which in ("financials", "all"):
        print("\n===== verify_financials_api =====", flush=True)
        from tools.verify_financials_api import main as financials
        financials()
    if which in ("outstandings", "all"):
        print("\n===== verify_invoice_outstandings_api =====", flush=True)
        from tools.verify_invoice_outstandings_api import main as outstandings
        outstandings()
    if which in ("tax", "all"):
        print("\n===== audit_invoice_tax_structure_api =====", flush=True)
        sys.argv = [
            "audit_invoice_tax_structure_api",
            "--company", cfg.erpnext["company"],
            "--output", str(cfg.staging_db.parent / "reports" / "prd_invoice_tax_audit.json"),
        ]
        from tools.audit_invoice_tax_structure_api import main as tax
        tax()
    if which in ("native", "all"):
        print("\n===== verify_native_financial_reports_api =====", flush=True)
        from tools.verify_native_financial_reports_api import main as native
        native()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
