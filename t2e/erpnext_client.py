"""ERPNext (Frappe v16) REST client.

Token auth (api_key:api_secret). All mutating calls (insert/update/delete/
submit) are routed through ``_write`` which is a no-op unless the client was
constructed with ``dry_run=False`` -- the CLI only disables dry-run when the
user passes ``--confirm``.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import requests

from .config import get_config


class ERPNextError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ERPNextClient:
    def __init__(self, dry_run: bool = True) -> None:
        cfg = get_config()
        self.base = cfg.erp_url
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {cfg.erp_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.session.verify = cfg.erp_verify_ssl
        if not cfg.erp_verify_ssl:
            import urllib3
            urllib3.disable_warnings()  # insecure SSL is intentional per .env
        self.timeout = 60
        self._field_cache: dict[tuple[str, str], bool] = {}

    # ---- low-level -------------------------------------------------------
    def _request(self, method: str, path: str, **kw) -> Any:
        url = f"{self.base}{path}"
        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kw)
                if resp.status_code in (429, 502, 503, 504):
                    time.sleep(2 * attempt)
                    continue
                if not resp.ok:
                    raise ERPNextError(
                        f"{method} {path} -> {resp.status_code}",
                        resp.status_code, resp.text[:2000],
                    )
                return resp.json() if resp.content else {}
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 * attempt)
        raise ERPNextError(f"{method} {path} failed: {last}")

    def _write(self, method: str, path: str, **kw) -> Any:
        if self.dry_run:
            return {"_dry_run": True, "method": method, "path": path,
                    "payload": kw.get("json")}
        return self._request(method, path, **kw)

    def run_doc_method(self, method: str, doc: dict,
                       args: dict | None = None) -> dict:
        """Call a whitelisted controller method on an (unsaved) doc, exactly the
        way the Frappe desk form does (``run_doc_method``): the server builds the
        doc in memory, runs ``method`` on it and returns the mutated doc.

        Returns the updated doc (``docs[0]``) so the caller can chain calls, e.g.
        get_unreconciled_entries -> allocate_entries -> reconcile on Payment
        Reconciliation. This is always routed through ``_request`` (even in
        dry-run) because read-only methods must run to produce a plan; callers
        MUST gate any state-changing method (e.g. ``reconcile``) on
        ``self.dry_run`` themselves.
        """
        body: dict[str, Any] = {"method": method, "docs": json.dumps(doc)}
        if args is not None:
            body["args"] = json.dumps(args)
        res = self._request("POST", "/api/method/run_doc_method", json=body)
        docs = res.get("docs") or []
        return docs[0] if docs else {}

    # ---- reads -----------------------------------------------------------
    def get_count(self, doctype: str, filters: dict | None = None) -> int:
        params = {"doctype": doctype}
        if filters:
            params["filters"] = json.dumps([[k, "=", v] for k, v in filters.items()])
        dt = urllib.parse.quote(doctype)
        # frappe.client.get_count expects doctype + filters as query args
        q = urllib.parse.urlencode(params)
        return self._request("GET", f"/api/method/frappe.client.get_count?{q}")["message"]

    def get_list(self, doctype: str, fields: list[str] | None = None,
                 filters: list | None = None, limit: int = 0) -> list[dict]:
        dt = urllib.parse.quote(doctype)
        params: dict[str, str] = {}
        params["fields"] = json.dumps(fields or ["name"])
        if filters:
            params["filters"] = json.dumps(filters)
        params["limit_page_length"] = str(limit)
        out: list[dict] = []
        start = 0
        page = limit or 500
        while True:
            params["limit_start"] = str(start)
            params["limit_page_length"] = str(page)
            q = urllib.parse.urlencode(params)
            data = self._request("GET", f"/api/resource/{dt}?{q}")["data"]
            out.extend(data)
            if limit or len(data) < page:
                break
            start += page
        return out

    def exists(self, doctype: str, name: str) -> bool:
        dt = urllib.parse.quote(doctype)
        nm = urllib.parse.quote(str(name), safe="")
        try:
            self._request("GET", f"/api/resource/{dt}/{nm}")
            return True
        except ERPNextError as exc:
            if exc.status == 404:
                return False
            raise

    def find_by_field(self, doctype: str, field: str, value: str,
                      exclude_cancelled: bool = False) -> str | None:
        filters = [[field, "=", value]]
        if exclude_cancelled:
            filters.append(["docstatus", "!=", 2])
        rows = self.get_list(doctype, fields=["name"], filters=filters, limit=1)
        return rows[0]["name"] if rows else None

    def has_field(self, doctype: str, fieldname: str) -> bool:
        """Whether a standard or custom field exists on a doctype."""
        key = (doctype, fieldname)
        if key in self._field_cache:
            return self._field_cache[key]
        dt = urllib.parse.quote(doctype, safe="")
        doc = self._request("GET", f"/api/resource/DocType/{dt}")["data"]
        found = any(
            f.get("fieldname") == fieldname for f in doc.get("fields", []))
        if not found:
            cf_name = f"{doctype}-{fieldname}"
            found = self.exists("Custom Field", cf_name)
        self._field_cache[key] = found
        return found

    # ---- writes ----------------------------------------------------------
    def insert(self, doctype: str, doc: dict) -> Any:
        dt = urllib.parse.quote(doctype)
        doc = {**doc, "doctype": doctype}
        return self._write("POST", f"/api/resource/{dt}", json=doc)

    def update(self, doctype: str, name: str, values: dict) -> Any:
        dt = urllib.parse.quote(doctype)
        nm = urllib.parse.quote(str(name), safe="")
        return self._write("PUT", f"/api/resource/{dt}/{nm}", json=values)

    def upload_file(self, filename: str, content: bytes, attach_to_doctype: str,
                    attach_to_name: str, is_private: int = 1) -> Any:
        """Upload a file and attach it to a doc via Frappe's upload_file endpoint.

        Multipart, so the session's default JSON Content-Type must be dropped for
        this one call (requests sets the multipart boundary itself)."""
        if self.dry_run:
            return {"_dry_run": True, "file": filename,
                    "attached_to": f"{attach_to_doctype}/{attach_to_name}"}
        # Null Content-Type so requests drops the session's JSON default and sets
        # the multipart boundary itself (auth still comes from the session).
        headers = {"Content-Type": None}
        data = {
            "is_private": str(is_private),
            "folder": "Home/Attachments",
            "doctype": attach_to_doctype,
            "docname": attach_to_name,
        }
        resp = self.session.post(
            f"{self.base}/api/method/upload_file",
            files={"file": (filename, content)}, data=data,
            headers=headers, timeout=max(self.timeout, 300))
        if not resp.ok:
            raise ERPNextError(f"upload_file {filename} -> {resp.status_code}",
                               resp.status_code, resp.text[:1000])
        return resp.json()

    def submit_doc(self, doctype: str, doc: dict) -> Any:
        """Insert a submittable doc and submit it in one shot."""
        doc = {**doc, "doctype": doctype, "docstatus": 1}
        return self._write("POST", "/api/method/frappe.client.submit",
                           json={"doc": json.dumps(doc)})

    def insert_and_submit(self, doctype: str, doc: dict) -> Any:
        """Insert a (possibly prompt-named) doc as a draft, then submit it.

        Needed for doctypes whose autoname is "Prompt" (e.g. Sales Invoice here):
        frappe.client.submit treats a named doc as pre-existing and fails, so we
        create the draft via the resource API then submit by name.
        """
        if self.dry_run:
            return {"_dry_run": True, "doctype": doctype}
        dt = urllib.parse.quote(doctype)
        draft = {k: v for k, v in doc.items() if k != "docstatus"}
        draft["doctype"] = doctype
        res = self._request("POST", f"/api/resource/{dt}", json=draft)
        name = res["data"]["name"]
        nm = urllib.parse.quote(str(name), safe="")
        self._request("PUT", f"/api/resource/{dt}/{nm}", json={"docstatus": 1})
        return {"data": {"name": name}}

    def delete(self, doctype: str, name: str) -> Any:
        dt = urllib.parse.quote(doctype)
        nm = urllib.parse.quote(str(name), safe="")
        return self._write("DELETE", f"/api/resource/{dt}/{nm}")

    def delete_items(self, doctype: str, names: list[str], chunk: int = 30,
                     timeout: int = 300) -> int:
        """Bulk-delete docs by name (Frappe's reportview.delete_items), chunked.
        Batches >10 are enqueued server-side; deletes that cascade to attachments
        can be slow, so use a generous per-call timeout. Errors on a chunk are
        swallowed (the caller re-queries to see what actually went). Returns the
        number of names submitted; no-op in dry-run."""
        if self.dry_run or not names:
            return 0
        saved, self.timeout = self.timeout, timeout
        try:
            for i in range(0, len(names), chunk):
                batch = names[i:i + chunk]
                # delete_items expects `items` as a JSON string (it json.loads it).
                try:
                    self._request("POST", "/api/method/frappe.desk.reportview.delete_items",
                                  json={"doctype": doctype, "items": json.dumps(batch)})
                except ERPNextError:
                    pass  # server may still have enqueued it; caller verifies
        finally:
            self.timeout = saved
        return len(names)

    def cancel(self, doctype: str, name: str) -> Any:
        return self._write("POST", "/api/method/frappe.client.cancel",
                           json={"doctype": doctype, "name": name})

    def ensure_custom_field(
        self,
        doctype: str,
        fieldname: str,
        label: str | None = None,
        fieldtype: str = "Data",
        *,
        insert_after: str = "name",
        read_only: int = 1,
        hidden: int = 0,
        allow_on_submit: int = 0,
        print_hide: int = 0,
    ) -> None:
        """Create a custom field (idempotency key) on a doctype if absent."""
        cf_name = f"{doctype}-{fieldname}"
        if self.exists("Custom Field", cf_name):
            return
        self.insert("Custom Field", {
            "dt": doctype,
            "fieldname": fieldname,
            "label": label or fieldname.replace("_", " ").title(),
            "fieldtype": fieldtype,
            "read_only": read_only,
            "no_copy": 1,
            "insert_after": insert_after,
            "hidden": hidden,
            "allow_on_submit": allow_on_submit,
            "print_hide": print_hide,
            "translatable": 0,
        })
        self._field_cache[(doctype, fieldname)] = True
