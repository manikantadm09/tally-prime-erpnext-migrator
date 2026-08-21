#!/usr/bin/env bash
# Run on DESKTOP-S4AL977 inside the unpacked tree. Does not write to ERPNext.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -r requirements.txt

mkdir -p runtime-uat/data
if [[ ! -f runtime-uat/data/staging.sqlite ]]; then
  echo "Missing runtime-uat/data/staging.sqlite (full Tally extract)."
  exit 1
fi

if [[ ! -f runtime-uat/.env.erpnext ]]; then
  cat > runtime-uat/.env.erpnext <<'EOF'
UAT_ERPNEXT_URL=http://dev-site.local:8000
UAT_ERPNEXT_API_KEY=
UAT_ERPNEXT_API_SECRET=
UAT_ERPNEXT_INSECURE_SSL=0
EOF
  echo "Wrote runtime-uat/.env.erpnext — add only UAT API key and secret, then re-run."
  exit 2
fi

export T2E_ENV=UAT
export T2E_RUNTIME_ROOT="$ROOT/runtime-uat"
export PYTHONPATH="$ROOT"
ln -sfn "$ROOT/config.yaml" "$ROOT/runtime-uat/config.yaml"

python - <<'PY'
from urllib.parse import urlparse

from t2e.config import set_environment, get_config
from t2e.erpnext_client import ERPNextClient, ERPNextError

FORBIDDEN_HOSTS = {
    "dev.spaceki.com",
    "erp.spaceki.com",
    "spacekierpnext",
}
COMPANY = "Spaceki Designs LLP"
REQUIRED_APPS = ("frappe", "erpnext", "india_compliance")

set_environment("UAT")
cfg = get_config()
url = cfg.erp_url
host = (urlparse(url).hostname or "").lower()
print("env", cfg.env_name)
print("url", url)
print("staging", cfg.staging_db)

env = cfg._erp_env()
key = (env.get("ERPNEXT_API_KEY") or "").strip()
secret = (env.get("ERPNEXT_API_SECRET") or "").strip()
if not key or not secret:
    raise SystemExit("UAT API key/secret missing in runtime-uat/.env.erpnext")
if host in FORBIDDEN_HOSTS or host.endswith(".spaceki.com"):
    raise SystemExit(f"refusing non-UAT host: {host}")

erp = ERPNextClient(dry_run=True)
ping = erp._request("GET", "/api/method/frappe.ping")
print("ping", ping.get("message"))
if ping.get("message") != "pong":
    raise SystemExit("UAT URL did not return pong")

user = erp._request("GET", "/api/method/frappe.auth.get_logged_user").get("message")
print("api_user", user)
if not user or user == "Guest":
    raise SystemExit("authenticated API check failed")

try:
    company = erp._request(
        "GET", f"/api/resource/Company/{COMPANY.replace(' ', '%20')}"
    )["data"]
except ERPNextError as exc:
    raise SystemExit(f"company missing or unreadable: {COMPANY} ({exc})") from exc
abbr = company.get("abbr")
print("company", company.get("name"), "abbr", abbr)
if abbr != "SDL":
    raise SystemExit(f"company abbr must be SDL, got {abbr!r}")

versions = erp._request("GET", "/api/method/frappe.utils.change_log.get_versions")["message"]
for app in REQUIRED_APPS:
    ver = (versions.get(app) or {}).get("version")
    print(app, ver or "MISSING")
    if not ver:
        raise SystemExit(f"required app not installed: {app}")

print("gate", "PASS")
PY

echo
echo "Gate passed. Do not run --confirm until this output is reviewed."
echo "No Azure, frozen-dev, or production credentials were used."
