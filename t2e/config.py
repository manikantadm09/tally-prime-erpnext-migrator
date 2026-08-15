"""Configuration loading: secrets from .env files, mappings from config.yaml."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

# Keep code and runtime data separable.  Production uses the reviewed canonical
# package from this repository while credentials, config, staging and reports
# remain in a dedicated (gitignored) run directory.  The override is evaluated
# at process start, before Config is constructed.
ROOT = Path(
    os.environ.get("T2E_RUNTIME_ROOT", Path(__file__).resolve().parent.parent)
).expanduser().resolve()
DATA_DIR = ROOT / "data"


def _read_env(name: str) -> dict[str, str]:
    path = ROOT / name
    if not path.exists():
        raise FileNotFoundError(f"Missing env file: {path}")
    # dotenv_values does not pollute os.environ and tolerates comments.
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


# Env files carry both PRD_ and DEV_ prefixed variables so one checkout can target
# either server. We pick the active environment's set and strip the prefix, so the
# rest of the code reads plain names (ERPNEXT_URL, ERPNEXT_DB_HOST, ...).
_KNOWN_ENVS = ("PRD", "DEV", "UAT")
_ENV_OVERRIDE: str | None = None


def set_environment(name: str | None) -> None:
    """Force the active environment (e.g. from a CLI flag). Clears the cached
    Config so the next get_config() reloads against the chosen environment."""
    global _ENV_OVERRIDE, _cfg
    if name:
        _ENV_OVERRIDE = name.strip().upper()
        _cfg = None


def _select_prefixed(d: dict[str, str], env: str, fname: str) -> dict[str, str]:
    prefix = f"{env}_"
    sel = {k[len(prefix):]: v for k, v in d.items() if k.startswith(prefix)}
    if sel:
        return sel
    # Unprefixed keys are the frozen-dev/legacy target. Other env prefixes
    # (e.g. UAT_) may coexist in the same gitignored file.
    unprefixed = {
        k: v for k, v in d.items()
        if k.split("_", 1)[0] not in _KNOWN_ENVS
    }
    if unprefixed:
        return unprefixed
    avail = sorted({
        k.split("_", 1)[0] for k in d if k.split("_", 1)[0] in _KNOWN_ENVS
    })
    raise KeyError(f"No '{prefix}' variables in {fname}; available: {avail}")


class Config:
    def __init__(self) -> None:
        with (ROOT / "config.yaml").open(encoding="utf-8") as fh:
            self.yaml: dict[str, Any] = yaml.safe_load(fh)

        # precedence: explicit override (CLI) > T2E_ENV > config.yaml > PRD
        env = (_ENV_OVERRIDE or os.environ.get("T2E_ENV")
               or self.yaml.get("environment") or "PRD")
        self.env_name = env.strip().upper()

        # Load secret files only when an ERPNext/DB property is requested.
        # Read-only Tally extraction must work without target credentials.
        self._env_db: dict[str, str] | None = None
        self._env_erp: dict[str, str] | None = None

        DATA_DIR.mkdir(exist_ok=True)
        (DATA_DIR / "raw").mkdir(exist_ok=True)
        (DATA_DIR / "reports").mkdir(exist_ok=True)

    def _db_env(self) -> dict[str, str]:
        if self._env_db is None:
            self._env_db = _select_prefixed(
                _read_env(".env.db"), self.env_name, ".env.db")
        return self._env_db

    def _erp_env(self) -> dict[str, str]:
        if self._env_erp is None:
            self._env_erp = _select_prefixed(
                _read_env(".env.erpnext"), self.env_name, ".env.erpnext")
        return self._env_erp

    # ---- convenience accessors -------------------------------------------
    @property
    def tally(self) -> dict[str, Any]:
        data = dict(self.yaml["tally"])
        url = os.environ.get("TALLY_URL")
        if url:
            data["url"] = url.rstrip("/")
        return data

    @property
    def erpnext(self) -> dict[str, Any]:
        return self.yaml["erpnext"]

    @property
    def idempotency_field(self) -> str:
        return self.yaml["idempotency_field"]

    @property
    def staging_db(self) -> Path:
        return DATA_DIR / "staging.sqlite"

    # ERPNext REST
    @property
    def erp_url(self) -> str:
        return self._erp_env()["ERPNEXT_URL"].rstrip("/")

    @property
    def erp_token(self) -> str:
        env = self._erp_env()
        return f"{env['ERPNEXT_API_KEY']}:{env['ERPNEXT_API_SECRET']}"

    @property
    def erp_verify_ssl(self) -> bool:
        return self._erp_env().get(
            "ERPNEXT_INSECURE_SSL", "0") not in ("1", "true", "True")

    # ERPNext DB (used only for fast read-only reconciliation counts)
    @property
    def db_params(self) -> dict[str, Any]:
        env = self._db_env()
        return {
            "host": env["ERPNEXT_DB_HOST"],
            "port": int(env.get("ERPNEXT_DB_PORT", 3306)),
            "user": env["ERPNEXT_DB_USER"],
            "password": env["ERPNEXT_DB_PASSWORD"],
            "database": env["ERPNEXT_DB_NAME"],
        }


_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
