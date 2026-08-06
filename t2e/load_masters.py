"""Load staged Tally masters into ERPNext (idempotent).

Order matters: UOMs -> account-group tree (parent first) -> leaf GL accounts ->
parties (customers/suppliers) -> cost centers -> items. Every created/reused
ERPNext name is written back to staging so the voucher loader can resolve
ledger names to posting targets.
"""
from __future__ import annotations

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .lines import is_round_ledger
from .mapping import CompanyDefaults, GroupTree, acc_name
from .staging import Staging

IDEMPOTENT_DOCTYPES = [
    "Account", "Customer", "Supplier", "Item", "Cost Center",
    "Journal Entry", "Payment Entry", "Sales Invoice", "Purchase Invoice",
    "Period Closing Voucher", "Address", "Contact",
]


def fetch_company_defaults(erp: ERPNextClient) -> CompanyDefaults:
    cfg = get_config()
    cname = cfg.erpnext["company"]
    c = erp._request("GET", f"/api/resource/Company/{cname.replace(' ', '%20')}")["data"]
    roots = erp.get_list("Account",
                         fields=["name", "root_type"],
                         filters=[["company", "=", cname], ["parent_account", "is", "not set"]])
    root_by_type = {r["root_type"]: r["name"] for r in roots}
    # Equity has no dedicated root in the India CoA -> fold under Liability root.
    root_by_type.setdefault("Equity", root_by_type.get("Liability"))
    return CompanyDefaults(
        name=cname, abbr=c["abbr"],
        receivable=c["default_receivable_account"],
        payable=c["default_payable_account"],
        round_off=c.get("round_off_account") or acc_name("Round Off", c["abbr"]),
        cost_center=c.get("cost_center") or acc_name("Main", c["abbr"]),
        currency=c["default_currency"],
        suspense=acc_name("Tally Migration Suspense", c["abbr"]),
        root_by_type=root_by_type,
    )


def ensure_fiscal_years(erp: ERPNextClient, from_yyyymmdd: str, to_yyyymmdd: str,
                        dry_run: bool = True) -> dict[str, str]:
    """Create every Indian FY (Apr 1 - Mar 31) spanning the migration window, so
    transactions and the pre-window opening entry all fall in an existing fiscal
    year. Idempotent: existing years are left untouched. A fresh ERPNext site
    ships with only the current FY, so this is the one setup step a new target
    server needs before a migration.
    """
    import datetime as dt
    start = dt.datetime.strptime(from_yyyymmdd, "%Y%m%d").date() - dt.timedelta(days=1)
    end = min(dt.datetime.strptime(to_yyyymmdd, "%Y%m%d").date(), dt.date.today())

    def fy_start_year(d: dt.date) -> int:
        return d.year if d.month >= 4 else d.year - 1

    out: dict[str, str] = {}
    for y in range(fy_start_year(start), fy_start_year(end) + 1):
        name = f"{y}-{y + 1}"
        if erp.exists("Fiscal Year", name):
            out[name] = "exists"
            continue
        if dry_run:
            out[name] = "would-create"
            continue
        try:
            erp.insert("Fiscal Year", {
                "year": name,
                "year_start_date": f"{y}-04-01",
                "year_end_date": f"{y + 1}-03-31",
            })
            out[name] = "created"
        except ERPNextError as exc:
            out[name] = f"error: {str(exc)[:120]}"
    return out


# GST state code (first 2 digits of a GSTIN) -> ERPNext state name.
_GST_STATE = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "28": "Andhra Pradesh", "29": "Karnataka", "30": "Goa",
    "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh",
}

_COUNTRY_ALIASES = {
    "UK": "United Kingdom",
    "U.K.": "United Kingdom",
    "UAE": "United Arab Emirates",
    "U.A.E.": "United Arab Emirates",
}


def ensure_company_address(erp: ERPNextClient, company_name: str,
                           dry_run: bool = True) -> str:
    """Ensure the company has a GST billing Address so India Compliance can fetch
    the Company GSTIN onto invoices. A fresh site sets the Company GSTIN but no
    address, which makes every Sales/Purchase Invoice fail with a MandatoryError.
    Idempotent; derives the state from the GSTIN. No-op for non-GST companies."""
    comp = erp._request("GET", f"/api/resource/Company/{company_name.replace(' ', '%20')}")["data"]
    cfg = get_config()
    gstin = (
        comp.get("gstin")
        or cfg.erpnext.get("company_gstin", "")
        or ""
    ).strip()
    if not gstin:
        return "no-company-gstin (skipped)"
    if not dry_run:
        values = {}
        if not (comp.get("gstin") or "").strip() and erp.has_field(
                "Company", "gstin"):
            values["gstin"] = gstin
        if not (comp.get("tax_id") or "").strip() and erp.has_field(
                "Company", "tax_id"):
            values["tax_id"] = gstin
        if values:
            erp.update("Company", company_name, values)
    existing = erp.get_list("Address", fields=["name"],
                            filters=[["is_your_company_address", "=", 1]], limit=1)
    if existing:
        if not dry_run and erp.has_field("Address", "gstin"):
            erp.update("Address", existing[0]["name"], {"gstin": gstin})
        return f"exists ({existing[0]['name']})"
    if dry_run:
        return "would-create"
    doc = {
        "address_title": f"{company_name} - Registered",
        "address_type": "Billing",
        "address_line1": comp.get("city") or "NA",
        "city": comp.get("city") or "NA",
        "state": (
            _GST_STATE.get(gstin[:2], "")
            or cfg.erpnext.get("company_state", "")
        ),
        "country": comp.get("country") or "India",
        "links": [{"link_doctype": "Company", "link_name": company_name}],
    }
    if erp.has_field("Address", "gstin"):
        doc["gstin"] = gstin
    if erp.has_field("Address", "gst_category"):
        doc["gst_category"] = (
            comp.get("gst_category") or "Registered Regular")
    if erp.has_field("Address", "is_your_company_address"):
        doc["is_your_company_address"] = 1
    res = erp.insert("Address", doc)
    name = (res.get("data") or {}).get("name") if isinstance(res, dict) else None
    return f"created ({name})"


def ensure_idempotency_field(erp: ERPNextClient) -> None:
    field = get_config().idempotency_field
    for dt in IDEMPOTENT_DOCTYPES:
        try:
            erp.ensure_custom_field(dt, field, "Tally GUID")
        except ERPNextError as exc:  # non-fatal; field may already exist
            print(f"  ! custom field on {dt}: {exc}")
    for dt, fieldname, label in [
        ("Purchase Invoice", "tally_supplier_invoice_no",
         "Tally Supplier Invoice No"),
        ("Purchase Invoice", "tally_voucher_number", "Tally Voucher Number"),
        ("Sales Invoice", "tally_voucher_number", "Tally Voucher Number"),
    ]:
        try:
            erp.ensure_custom_field(dt, fieldname, label)
        except ERPNextError as exc:
            print(f"  ! custom field {dt}.{fieldname}: {exc}")
    # Purchase Invoice does not expose Sales Invoice's built-in
    # ``is_consolidated`` flag. ERPNext's taxes-and-totals controller uses that
    # flag to preserve an explicitly supplied rounded_total/rounding_adjustment.
    # A small minority of Tally invoices deliberately round contrary to
    # ERPNext's automatic nearest-rupee result (including whole-rupee and
    # multi-rupee adjustments). This hidden flag lets those invoices use the
    # controller's existing manual-rounding path without a visible tax row.
    try:
        if not erp.has_field("Purchase Invoice", "is_consolidated"):
            erp.ensure_custom_field(
                "Purchase Invoice",
                "is_consolidated",
                "Tally Preserve Manual Rounded Total",
                fieldtype="Check",
            )
        if not erp.dry_run and erp.exists(
                "Custom Field", "Purchase Invoice-is_consolidated"):
            erp.update(
                "Custom Field",
                "Purchase Invoice-is_consolidated",
                {"hidden": 1, "read_only": 1, "no_copy": 1},
            )
    except ERPNextError as exc:
        print(f"  ! custom field Purchase Invoice.is_consolidated: {exc}")
    ensure_gst_fields(erp)


def ensure_gst_fields(erp: ERPNextClient) -> None:
    """Provide the GST identity fields used by the migration.

    ERPNext v16 sites without the optional India Compliance app do not expose
    these fields.  Keeping the same conventional fieldnames makes invoice
    metadata and print formats usable while remaining a no-op on sites where
    India Compliance already supplies them.
    """
    fields = [
        ("Company", "gstin", "GSTIN", "tax_id", 0),
        ("Customer", "gstin", "GSTIN", "tax_id", 0),
        ("Supplier", "gstin", "GSTIN", "tax_id", 0),
        ("Address", "gstin", "GSTIN", "pincode", 0),
        (
            "Purchase Invoice", "company_gstin", "Company GSTIN",
            "tax_id", 1,
        ),
        (
            "Purchase Invoice", "supplier_gstin", "Supplier GSTIN",
            "address_display", 1,
        ),
        (
            "Purchase Invoice", "place_of_supply", "Place of Supply",
            "supplier_gstin", 1,
        ),
        (
            "Sales Invoice", "company_gstin", "Company GSTIN",
            "tax_id", 1,
        ),
        (
            "Sales Invoice", "billing_address_gstin", "Customer GSTIN",
            "address_display", 1,
        ),
        (
            "Sales Invoice", "place_of_supply", "Place of Supply",
            "billing_address_gstin", 1,
        ),
    ]
    for doctype, fieldname, label, insert_after, allow_on_submit in fields:
        try:
            if not erp.has_field(doctype, fieldname):
                erp.ensure_custom_field(
                    doctype,
                    fieldname,
                    label,
                    insert_after=insert_after,
                    read_only=1,
                    allow_on_submit=allow_on_submit,
                    print_hide=0,
                )
        except ERPNextError as exc:
            print(f"  ! GST field {doctype}.{fieldname}: {exc}")


class MasterLoader:
    def __init__(self, erp: ERPNextClient, store: Staging, defaults: CompanyDefaults):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.cfg = get_config()
        self.field = self.cfg.idempotency_field
        self.tree = GroupTree(store)
        self.group_erp: dict[str, str] = {}   # tally group name -> erp account fullname
        # ERPNext rejects group-type party groups/territories, so resolve leaves.
        self.customer_group = self._first_nongroup(
            "Customer Group", ["Commercial", "Individual"])
        self.supplier_group = self._first_nongroup(
            "Supplier Group", ["Local", "Distributor"])
        self.territory = self._first_nongroup("Territory", ["India"])

    def _first_nongroup(self, doctype: str, prefer: list[str]) -> str:
        rows = self.erp.get_list(doctype, fields=["name"],
                                filters=[["is_group", "=", 0]], limit=0)
        names = {r["name"] for r in rows}
        for p in prefer:
            if p in names:
                return p
        return next(iter(names)) if names else prefer[0]

    # ---- generic helpers -------------------------------------------------
    def _account_exists(self, fullname: str) -> bool:
        return self.erp.exists("Account", fullname)

    def _is_group_account(self, fullname: str) -> bool:
        rows = self.erp.get_list("Account", fields=["is_group"],
                                filters=[["name", "=", fullname]], limit=1)
        return bool(rows and rows[0].get("is_group"))

    def _mark(self, guid: str, doctype: str, erp_name: str, status="loaded", err=None):
        # Never let a dry-run make staging claim a master exists in ERPNext.
        if not self.erp.dry_run:
            self.store.mark("master", guid, status, doctype, erp_name, err)

    # ---- Suspense fallback account --------------------------------------
    def ensure_suspense(self) -> None:
        """A dedicated account that absorbs any unresolvable ledger so that no
        voucher is ever dropped. Postings here are flagged in the report."""
        if self.erp.exists("Account", self.d.suspense):
            return
        try:
            self.erp.insert("Account", {
                "account_name": "Tally Migration Suspense",
                "parent_account": self.d.root_by_type.get("Asset"),
                "company": self.d.name,
                "is_group": 0,
                "root_type": "Asset",
            })
        except ERPNextError as exc:
            print(f"  ! could not create suspense account: {exc}")

    # ---- UOM -------------------------------------------------------------
    def load_uoms(self) -> int:
        n = 0
        for r in self.store.masters("unit"):
            name = r["name"]
            try:
                if not self.erp.exists("UOM", name):
                    self.erp.insert("UOM", {"uom_name": name})
                self._mark(r["guid"], "UOM", name)
                n += 1
            except ERPNextError as exc:
                self._mark(r["guid"], "UOM", "", "error", str(exc)[:500])
        self.store.conn.commit()
        return n

    # ---- Account tree ----------------------------------------------------
    def load_account_groups(self) -> int:
        groups = self.store.masters("group")
        # parent-first ordering by ancestry depth
        groups = sorted(groups, key=lambda r: len(self.tree.ancestry(r["name"])))
        n = 0
        for r in groups:
            name = r["name"]
            full = acc_name(name, self.d.abbr)
            try:
                if self._account_exists(full):
                    self.group_erp[name] = full
                    # A fresh standard CoA ships some names Tally uses as GROUPS
                    # as leaf accounts (e.g. "Unsecured Loans"). Promote the leaf
                    # to a group so migrated child ledgers can attach beneath it.
                    if not self._is_group_account(full):
                        try:
                            self.erp.update("Account", full, {"is_group": 1})
                        except ERPNextError as exc:
                            self._mark(r["guid"], "Account", full, "error", str(exc)[:500])
                            continue
                    self._mark(r["guid"], "Account", full, "skipped")
                    continue
                root_type = self.tree.root_type(name)
                parent_tally = r["parent"] or ""
                if parent_tally and parent_tally.lower() != "primary" \
                        and parent_tally in self.group_erp:
                    parent_account = self.group_erp[parent_tally]
                else:
                    parent_account = self.d.root_by_type.get(root_type) \
                        or self.d.root_by_type["Asset"]
                self.erp.insert("Account", {
                    "account_name": name,
                    "parent_account": parent_account,
                    "company": self.d.name,
                    "is_group": 1,
                    "root_type": root_type,
                    self.field: r["guid"],
                })
                self.group_erp[name] = full
                self._mark(r["guid"], "Account", full)
                n += 1
            except ERPNextError as exc:
                # Fall back to reusing whatever exists by that name.
                if self._account_exists(full):
                    self.group_erp[name] = full
                    self._mark(r["guid"], "Account", full, "skipped")
                else:
                    self._mark(r["guid"], "Account", "", "error", str(exc)[:500])
        self.store.conn.commit()
        return n

    def _leaf_parent(self, group: str, root_type: str) -> str:
        if group in self.group_erp:
            return self.group_erp[group]
        # group not materialized (e.g. ledger straight under a primary) -> root
        return self.d.root_by_type.get(root_type) or self.d.root_by_type["Asset"]

    def load_ledger_accounts(self, names: set[str] | None = None) -> int:
        n = 0
        for r in self.store.masters("ledger"):
            if names is not None and r["name"] not in names:
                continue
            group = r["parent"] or ""
            if self.tree.party_kind(group):
                continue  # parties handled separately
            name = r["name"]
            # Tally's reserved P&L ledger is the carried earnings account, not a
            # normal Asset. Map it to the target retained-earnings leaf. Period
            # Closing Vouchers later close each FY's P&L into this same account.
            if name.strip().lower() == "profit & loss a/c":
                retained = self.cfg.erpnext.get(
                    "retained_earnings_account", "Reserves and Surplus")
                full = acc_name(retained, self.d.abbr)
                if not self._account_exists(full):
                    parent = acc_name("Capital Account", self.d.abbr)
                    self.erp.insert("Account", {
                        "account_name": retained,
                        "parent_account": parent,
                        "company": self.d.name,
                        "is_group": 0,
                        "root_type": "Liability",
                    })
                self._mark(r["guid"], "Account", full, "skipped")
                n += 1
                continue
            # Standard ERPNext rounded-total posting and source Tally round-off
            # lines must hit one canonical account. Avoid creating a second
            # look-alike ROUNDING OFF account.
            if is_round_ledger(name):
                self._mark(r["guid"], "Account", self.d.round_off, "skipped")
                n += 1
                continue
            full = acc_name(name, self.d.abbr)
            root_type = self.tree.root_type(group)
            try:
                if self._account_exists(full):
                    # A Tally *ledger* can collide (case-insensitively) with an
                    # existing *group* account in the standard CoA (e.g. ledger
                    # "CASH IN HAND" vs group "Cash In Hand"). Group accounts
                    # can't take postings, so create a distinct leaf instead.
                    if self._is_group_account(full):
                        name = f"{name} (Ledger)"
                        full = acc_name(name, self.d.abbr)
                        if not self._account_exists(full):
                            self.erp.insert("Account", {
                                "account_name": name,
                                "parent_account": self._leaf_parent(group, root_type),
                                "company": self.d.name,
                                "is_group": 0,
                                "root_type": root_type,
                                self.field: r["guid"],
                            })
                        self._mark(r["guid"], "Account", full)
                        n += 1
                        continue
                    self._mark(r["guid"], "Account", full, "skipped")
                    n += 1
                    continue
                acct_type = self.tree.account_type(group)
                doc = {
                    "account_name": name,
                    "parent_account": self._leaf_parent(group, root_type),
                    "company": self.d.name,
                    "is_group": 0,
                    "root_type": root_type,
                    self.field: r["guid"],
                }
                if acct_type:
                    doc["account_type"] = acct_type
                self.erp.insert("Account", doc)
                self._mark(r["guid"], "Account", full)
                n += 1
            except ERPNextError as exc:
                if self._account_exists(full):
                    self._mark(r["guid"], "Account", full, "skipped")
                else:
                    self._mark(r["guid"], "Account", "", "error", str(exc)[:500])
        self.store.conn.commit()
        return n

    # ---- Parties ---------------------------------------------------------
    def load_parties(self, names: set[str] | None = None) -> tuple[int, int]:
        nc = ns = 0
        observed: dict[str, set[str]] = {}
        for v in self.store.vouchers():
            party = " ".join((v["party"] or "").split())
            if not party:
                continue
            if v["vtype"] in ("Sales", "Credit Note"):
                observed.setdefault(party, set()).add("Customer")
            elif v["vtype"] in ("Purchase", "Debit Note"):
                observed.setdefault(party, set()).add("Supplier")
        for r in self.store.masters("ledger"):
            if names is not None and r["name"] not in names:
                continue
            group = r["parent"] or ""
            original_kind = self.tree.party_kind(group)
            roles = set(observed.get(" ".join(r["name"].split()), set()))
            if original_kind:
                roles.add(original_kind)
            if not roles:
                continue
            name = r["name"]
            payload = _json(r["payload"])
            try:
                for kind in sorted(roles):
                    if kind == "Customer":
                        if not self.erp.find_by_field(
                                "Customer", "customer_name", name) \
                                and not self.erp.exists("Customer", name):
                            doc = {
                                "customer_name": name,
                                "customer_type": "Company",
                                "customer_group": self.customer_group,
                                "territory": self.territory,
                                self.field: f"{r['guid']}:Customer",
                            }
                            if self.erp.has_field("Customer", "gstin"):
                                doc["gstin"] = (
                                    payload.get("PARTYGSTIN", "") or "")
                            if self.erp.has_field("Customer", "tax_id"):
                                doc["tax_id"] = (
                                    payload.get("PARTYGSTIN", "") or "")
                            self.erp.insert("Customer", doc)
                        nc += 1
                    else:
                        if not self.erp.find_by_field(
                                "Supplier", "supplier_name", name) \
                                and not self.erp.exists("Supplier", name):
                            doc = {
                                "supplier_name": name,
                                "supplier_type": "Company",
                                "supplier_group": self.supplier_group,
                                self.field: f"{r['guid']}:Supplier",
                            }
                            if self.erp.has_field("Supplier", "gstin"):
                                doc["gstin"] = (
                                    payload.get("PARTYGSTIN", "") or "")
                            if self.erp.has_field("Supplier", "tax_id"):
                                doc["tax_id"] = (
                                    payload.get("PARTYGSTIN", "") or "")
                            self.erp.insert("Supplier", doc)
                        ns += 1
                    self.store.add_party_role(
                        r["guid"], name, kind, name)

                # Keep the ledger's original source-GL mapping in master. Extra
                # party roles live in party_role and do not overwrite an Advance
                # account or the opposite control-account classification.
                if original_kind:
                    self._mark(r["guid"], original_kind, name)
                self._ensure_party_address_contact(
                    r["guid"], name, payload, roles)
            except ERPNextError as exc:
                self._mark(
                    r["guid"], original_kind or "Account", "", "error",
                    (
                        f"{exc}: {exc.body}"
                        if getattr(exc, "body", None) else str(exc)
                    )[:2000])
        self.store.conn.commit()
        return nc, ns

    def _ensure_party_address_contact(
            self, guid: str, name: str, payload: dict,
            roles: set[str]) -> None:
        """Create the Address/Contact data that ERPNext invoices and prints use."""
        def text(value) -> str:
            if isinstance(value, list):
                return ", ".join(filter(None, (text(v) for v in value)))
            if isinstance(value, dict):
                return text(value.get("#text") or value.get("ADDRESS"))
            return str(value or "").strip()

        gstin = text(payload.get("PARTYGSTIN"))
        state = text(payload.get("LEDGERSTATENAME"))
        country_raw = text(payload.get("COUNTRYNAME")) or "India"
        country = _COUNTRY_ALIASES.get(country_raw.upper(), country_raw)
        pincode = text(payload.get("PINCODE"))
        address = text(payload.get("ADDRESS"))
        phone = text(payload.get("LEDGERPHONE"))
        email = text(payload.get("EMAIL"))
        contact = text(payload.get("LEDGERCONTACT"))
        links = [
            {"link_doctype": role, "link_name": name}
            for role in sorted(roles)
        ]

        if any((gstin, state, pincode, address)):
            existing = self.erp.find_by_field(
                "Address", self.field, f"{guid}:Address")
            if not existing:
                doc = {
                    "address_title": name,
                    "address_type": "Billing",
                    "address_line1": address or name,
                    "city": state or "NA",
                    "state": state,
                    "pincode": pincode,
                    "country": country,
                    "links": links,
                    self.field: f"{guid}:Address",
                }
                if gstin and self.erp.has_field("Address", "gstin"):
                    doc["gstin"] = gstin
                if gstin and self.erp.has_field("Address", "gst_category"):
                    doc["gst_category"] = "Registered Regular"
                self.erp.insert("Address", doc)

        if any((contact, phone, email)):
            existing = self.erp.find_by_field(
                "Contact", self.field, f"{guid}:Contact")
            if not existing:
                doc = {
                    "first_name": contact or name,
                    "links": links,
                    self.field: f"{guid}:Contact",
                }
                if email:
                    doc["email_ids"] = [
                        {"email_id": email, "is_primary": 1}]
                if phone:
                    doc["phone_nos"] = [
                        {"phone": phone, "is_primary_phone": 1}]
                self.erp.insert("Contact", doc)

    # ---- Cost centers ----------------------------------------------------
    def load_cost_centers(self) -> int:
        n = 0
        root_cc = self.cfg.erpnext["company"]  # company root cost center group
        # ERPNext root cost center is '<Company> - <abbr>'
        root = acc_name(self.cfg.erpnext["company"], self.d.abbr)
        for r in self.store.masters("costcentre"):
            name = r["name"]
            full = acc_name(name, self.d.abbr)
            try:
                if self.erp.exists("Cost Center", full):
                    self._mark(r["guid"], "Cost Center", full, "skipped")
                    n += 1
                    continue
                self.erp.insert("Cost Center", {
                    "cost_center_name": name,
                    "parent_cost_center": root,
                    "company": self.d.name,
                    "is_group": 0,
                    self.field: r["guid"],
                })
                self._mark(r["guid"], "Cost Center", full)
                n += 1
            except ERPNextError as exc:
                self._mark(r["guid"], "Cost Center", "", "error", str(exc)[:500])
        self.store.conn.commit()
        return n

    # ---- Items -----------------------------------------------------------
    def load_items(self) -> int:
        n = 0
        for r in self.store.masters("stockitem"):
            name = r["name"]
            try:
                if not self.erp.exists("Item", name):
                    self.erp.insert("Item", {
                        "item_code": name,
                        "item_name": name[:140],
                        "item_group": "All Item Groups",
                        "stock_uom": _json(r["payload"]).get("BASEUNITS") or "Nos",
                        "is_stock_item": 0,
                        self.field: r["guid"],
                    })
                self._mark(r["guid"], "Item", name)
                n += 1
            except ERPNextError as exc:
                self._mark(r["guid"], "Item", "", "error", str(exc)[:500])
        self.store.conn.commit()
        return n


def _json(s: str) -> dict:
    import json
    try:
        return json.loads(s)
    except Exception:
        return {}
