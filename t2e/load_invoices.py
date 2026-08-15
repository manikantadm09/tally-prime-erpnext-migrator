"""Load Tally Sales/Purchase vouchers as ERPNext Sales/Purchase Invoices.

Each invoice voucher becomes a real AR/AP document so its outstanding can be
settled (Paid / Partially Paid) by linked payments and journals. Lines are
classified:
  * party  -> debit_to (Sales) / credit_to (Purchase)
  * tax     (CGST/SGST/IGST/CESS) -> paise-exact "Actual" tax rows
  * rounding -> ERPNext rounded-total adjustment (not a visible tax row)
  * expense/income lines -> item rows on a generic non-stock item, posting to
    the line's own account (so the GL matches Tally exactly)

The party's "New Ref" bill name is recorded in the staging bill_ref index so
payments/journals can allocate against this invoice.

Vouchers that can't be modelled as an invoice (no party, party kind mismatched
to the invoice type, unbalanced) are left pending and handled by the journal
loader instead -- nothing is dropped.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import json
import re
import urllib.parse

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .lines import is_round_ledger, is_tax_ledger, parse_entries
from .load_masters import _valid_gstin
from .mapping import CompanyDefaults, LedgerResolver, Resolved
from .staging import Staging

GENERIC_ITEM = "Tally Migration Item"
# India Compliance requires HSN/SAC on Item. 998399 is a valid SAC on this
# CoA ("other professional/technical services") and is only a master-data
# prerequisite so the generic non-stock item can exist; invoice tax still
# comes from Tally tax ledgers, not this code.
GENERIC_ITEM_HSN = "998399"

# Tally voucher -> (ERPNext doctype, party kind, is_return)
INVOICE_SPECS = {
    "Sales":       ("Sales Invoice", "Customer", False),
    "Credit Note": ("Sales Invoice", "Customer", True),
    "Purchase":    ("Purchase Invoice", "Supplier", False),
    "Debit Note":  ("Purchase Invoice", "Supplier", True),
}


def ensure_generic_item(erp: ERPNextClient) -> None:
    if erp.exists("Item", GENERIC_ITEM):
        return
    doc = {
        "item_code": GENERIC_ITEM, "item_name": GENERIC_ITEM,
        "item_group": "All Item Groups", "stock_uom": "Nos",
        "is_stock_item": 0, "is_purchase_item": 1, "is_sales_item": 1,
    }
    if erp.has_field("Item", "gst_hsn_code"):
        doc["gst_hsn_code"] = GENERIC_ITEM_HSN
    erp.insert("Item", doc)


class InvoiceLoader:
    def __init__(self, erp: ERPNextClient, store: Staging,
                 defaults: CompanyDefaults, resolver: LedgerResolver):
        self.erp = erp
        self.store = store
        self.d = defaults
        self.r = resolver
        self.field = get_config().idempotency_field
        self.fallback: list[str] = []   # guids that must go to the journal loader

    # ---- build ----------------------------------------------------------
    def _build(self, vrow):
        spec = INVOICE_SPECS[vrow["vtype"]]
        doctype, kind, is_return = spec
        import json
        payload = json.loads(vrow["payload"])
        entries = parse_entries(payload)

        party_name = _norm(vrow["party"])
        candidates = [
            (e, self.r.get_party(e["ledger"], kind))
            for e in entries
            if self.r.get_party(e["ledger"], kind)
            and (not party_name or _norm(e["ledger"]) == party_name)
        ]
        if len(candidates) != 1:
            candidates = [
                (e, self.r.get_party(e["ledger"], kind))
                for e in entries if self.r.get_party(e["ledger"], kind)
            ]
        if len(candidates) != 1:
            return None
        party_line, party_res = candidates[0]

        items, taxes, round_lines = [], [], []
        for e in entries:
            if e is party_line:
                continue
            if is_round_ledger(e["ledger"]):
                round_lines.append(e)
            elif is_tax_ledger(e["ledger"]):
                taxes.append(e)
            else:
                items.append(e)

        if party_res is None or not items:
            return None  # can't model as this invoice type
        # sign of return: ERPNext expects negative qty/amount
        sign = -1 if is_return else 1

        account_field = (
            "income_account" if kind == "Customer" else "expense_account"
        )
        control_account = (
            self.d.receivable if kind == "Customer" else self.d.payable
        )
        item_rows = [{
            "item_code": GENERIC_ITEM,
            "item_name": e["ledger"][:140],
            "description": _item_description(
                e["ledger"], payload.get("NARRATION")),
            "qty": sign, "rate": round(e["mag"], 2),
            account_field: (
                self.r.get(e["ledger"]).account if self.r.get(e["ledger"])
                else self.d.suspense),
            "cost_center": self.d.cost_center,
        } for e in items]
        # A second party ledger on the expense/income side cannot post through
        # the invoice control account (ERPNext rejects expense == credit_to).
        # Leave it pending for the journal loader so both parties stay in GL.
        if any(row.get(account_field) == control_account for row in item_rows):
            return None

        tax_rows = []
        for e in taxes:
            gst_kind = _gst_kind(e["ledger"])
            row = {
                "charge_type": "Actual",
                # Taxes remain explicit amounts so paise-exact Tally tax postings
                # are preserved. Round-off is deliberately excluded and handled
                # through ERPNext's rounded-total mechanism below.
                "account_head": (
                    self.r.get(e["ledger"]).account if self.r.get(e["ledger"])
                    else self.d.round_off),
                "description": e["ledger"][:140],
                "rate": _tax_rate(e["ledger"]),
                # A tax/rounding line increases the bill when it sits OPPOSITE
                # the party line (i.e. on the items' side): on a Sale the party
                # is a Tally DEBIT and output GST a CREDIT; on a Purchase the
                # party is a CREDIT and input GST a DEBIT -- both additive.
                # Judging against the party's actual Dr/Cr direction (not the
                # doctype) keeps returns correct too: a Credit Note's party is a
                # credit, so its output-GST debit reverses (negative tax_amount).
                # The `sign` then applies ERPNext's return flip.
                "tax_amount": round(
                    sign * (e["mag"] if e["debit"] != party_line["debit"]
                            else -e["mag"]), 2),
                "cost_center": self.d.cost_center,
            }
            if gst_kind:
                row["gst_tax_type"] = gst_kind
                # Keep charge_type Actual. On Net Total lets India Compliance
                # recompute tax (18% vs Tally 5%, paise rounding) and the
                # invoice no longer matches Tally. Item GST rates are stamped
                # below by _apply_single_rate_item_gst so IC still sees tax
                # on taxable items.
            tax_rows.append(row)
        _apply_single_rate_item_gst(item_rows, tax_rows)

        # party bill reference (New Ref) for later payment allocation
        billname = next((b["name"] for b in party_line["bills"]
                         if b["type"] in ("New Ref", "Agst Ref")), None) \
            or vrow["vnumber"] or vrow["guid"][:20]
        bill = next((b for b in party_line["bills"]
                     if b["type"] in ("New Ref", "Agst Ref")), {})
        posting_date = vrow["vdate"]
        bill_date = (
            _tally_date(bill.get("bill_date"))
            or _tally_date(payload.get("REFERENCEDATE"))
            or posting_date
        )
        due_date = _due_date(
            bill_date or posting_date, bill.get("credit_period"))
        narration = _scalar(payload.get("NARRATION"))[:1000]

        doc = {
            "doctype": doctype,
            "company": self.d.name,
            "posting_date": posting_date,
            "set_posting_time": 1,
            "update_stock": 0,
            # Enable standard rounded total only where Tally explicitly posted a
            # round-off ledger. Invoices that retained paise remain unrounded.
            "disable_rounded_total": 0 if round_lines else 1,
            "is_return": 1 if is_return else 0,
            "due_date": due_date,
            "remarks": narration,
            "items": item_rows,
            "taxes": tax_rows,
            self.field: vrow["guid"],
        }
        # Most Tally round-off lines equal ERPNext's normal banker's rounding
        # and therefore belong in the standard rounded-total fields. A minority
        # are deliberate manual adjustments (whole/multiple rupees, contrary
        # half rounding, or paise-preserving adjustments). Mark those for a
        # two-step draft/update/submit path that uses ERPNext's existing
        # consolidated/manual-rounding preservation branch, still without a
        # visible tax row.
        if round_lines:
            unrounded_total = sign * sum(
                e["mag"] if e["debit"] != party_line["debit"] else -e["mag"]
                for e in entries
                if e is not party_line and not is_round_ledger(e["ledger"])
            )
            source_total = sign * party_line["mag"]
            automatic_total = _bankers_round_to_rupee(unrounded_total)
            if abs(source_total - automatic_total) > 0.004:
                doc["disable_rounded_total"] = 1
                doc["_tally_manual_rounding"] = {
                    "source_total": round(source_total, 2),
                    "unrounded_total": round(unrounded_total, 2),
                }
        party_gstin, gstin_warning = _resolve_party_gstin(
            _scalar(payload.get("PARTYGSTIN")),
            self._valid_source_gstin(party_res.party),
        )
        if gstin_warning:
            narration = f"{narration}\n{gstin_warning}".strip()[:1000]
            doc["remarks"] = narration
        place = _place_of_supply(
            _scalar(payload.get("PLACEOFSUPPLY"))
            or _scalar(payload.get("STATENAME")),
            party_gstin)
        company_gstin = _scalar(
            get_config().erpnext.get("company_gstin", ""))
        place = _tax_driven_place_of_supply(
            place, party_gstin, company_gstin,
            [e["ledger"] for e in taxes])
        if self._supports(doctype, "place_of_supply") and place:
            doc["place_of_supply"] = place
        # India Compliance rejects the contradictory combination of a GSTIN
        # with its default "Unregistered" category.  Tally's voucher-level
        # GSTIN is the authoritative evidence that this transaction is
        # registered, even when an inherited ERPNext party master is stale.
        if (party_gstin and self._supports(doctype, "gst_category")):
            doc["gst_category"] = "Registered Regular"
        if self._supports(doctype, "company_gstin") and company_gstin:
            doc["company_gstin"] = company_gstin
        if self._supports(doctype, "tally_voucher_number"):
            doc["tally_voucher_number"] = vrow["vnumber"]
        if kind == "Customer":
            doc["customer"] = party_res.party
            doc["debit_to"] = self.d.receivable
            doc["naming_series"] = "SRET-.YY.-" if is_return else "SINV-.YY.-"
            # This site sets Sales Invoice autoname to "Prompt". Tally voucher
            # numbers can repeat across years, so append date + GUID to make the
            # ERPNext name deterministic and globally unique.
            base_name = _unique_transaction_name(
                vrow["vnumber"], vrow["vdate"], vrow["guid"], "TLY-SINV")
            doc["name"] = self._available_name(doctype, base_name)
            if self._supports(doctype, "billing_address_gstin") and party_gstin:
                doc["billing_address_gstin"] = party_gstin
        else:
            doc["supplier"] = party_res.party
            doc["credit_to"] = self.d.payable
            # ERPNext normally rejects a repeated supplier invoice number. Tally
            # allows duplicate bill references, so retain the readable source
            # value while adding a deterministic GUID suffix.
            duplicate = self.store.duplicate_bill_key_count(
                party_res.party, billname) > 1
            doc["bill_no"] = _supplier_bill_no(
                billname, vrow["guid"], duplicate)
            doc["bill_date"] = bill_date
            doc["naming_series"] = "PRET-.YY.-" if is_return else "PINV-.YY.-"
            if self._supports(doctype, "supplier_gstin") and party_gstin:
                doc["supplier_gstin"] = party_gstin
            if self._supports(doctype, "tally_supplier_invoice_no"):
                doc["tally_supplier_invoice_no"] = billname

        source_res = self.r.get(party_line["ledger"])
        bridge = None
        if not _same_posting_target(source_res, party_res):
            bridge = {
                "party_line": party_line,
                "source_res": source_res,
                "target_res": party_res,
                "payload": payload,
            }
        return doc, party_res.party, doctype, billname, bridge

    def _supports(self, doctype: str, fieldname: str) -> bool:
        try:
            return self.erp.has_field(doctype, fieldname)
        except (AttributeError, ERPNextError):
            return False

    def _available_name(self, doctype: str, base_name: str) -> str:
        """Return a deterministic replacement name when an immutable cancelled
        document already owns the normal prompt-based Sales Invoice name."""
        if not self.erp.exists(doctype, base_name):
            return base_name
        for number in range(1, 1000):
            candidate = f"{base_name}-R{number}"
            if not self.erp.exists(doctype, candidate):
                return candidate
        raise ERPNextError(
            f"No replacement name available for {doctype} {base_name}")

    # ---- run ------------------------------------------------------------
    def run(self, vtype=None, limit=0, progress=lambda *a: None) -> dict[str, int]:
        types = [vtype] if vtype else list(INVOICE_SPECS)
        rows = []
        for vt in types:
            rows.extend(self.store.vouchers(vtype=vt, status="pending"))
        rows.sort(key=lambda r: (r["vdate"] or "", r["vnumber"] or ""))
        if limit:
            rows = rows[:limit]
        stats = {"planned": 0, "loaded": 0, "skipped": 0,
                 "fallback": 0, "bridged": 0, "error": 0,
                 "cancelled_retries": 0}
        for i, vrow in enumerate(rows, 1):
            party = doctype = billname = None
            bridge = None
            bridge_ok = False
            try:
                built = self._build(vrow)
                if built is None:
                    self.fallback.append(vrow["guid"])
                    stats["fallback"] += 1
                    continue
                doc, party, doctype, billname, bridge = built
                manual_rounding = doc.pop("_tally_manual_rounding", None)
                existing = self.erp.find_by_field(
                    doctype, self.field, vrow["guid"],
                    exclude_cancelled=True)
                if existing:
                    if not self._is_submitted(doctype, existing):
                        # A prior submit failure can leave a tagged draft. It has
                        # no ledger impact; remove it and recreate from source.
                        self.erp.delete(doctype, existing)
                        existing = None
                if existing:
                    if bridge:
                        created = self._ensure_party_bridge(
                            vrow, bridge, doctype, existing)
                        stats["bridged"] += int(created)
                    bridge_ok = True
                    if not self.erp.dry_run:
                        self.store.mark(
                            "voucher", vrow["guid"], "loaded",
                            doctype, existing)
                        self.store.add_bill_ref(
                            party, billname, doctype, existing)
                    stats["skipped"] += 1
                    continue
                if self.erp.dry_run:
                    stats["planned"] += 1
                    if bridge:
                        stats["bridged"] += 1
                    continue
                res = self._insert_invoice(doctype, doc, manual_rounding)
                name = _name_of(res)
                if not name:
                    name = self.erp.find_by_field(
                        doctype, self.field, vrow["guid"],
                        exclude_cancelled=True)
                if not name:
                    raise ERPNextError(
                        f"{doctype} submitted but no document name returned")
                if not self._posted_value_matches(
                        doctype, name, float(vrow["amount"] or 0)):
                    # A target hook can silently remove an Actual tax row for an
                    # unregistered party, or replace its rate from the generic
                    # item's HSN.  A successful submit is therefore not proof
                    # that the source value survived.  Cancel the altered
                    # document and retry with the exact tax ledger amounts as
                    # explicit invoice lines, without guessing new tax data.
                    # ERPNext keeps that cancelled shell; purge it after load.
                    self.erp.cancel(doctype, name)
                    stats["cancelled_retries"] += 1
                    item_doctype = (
                        "Sales Invoice Item" if doctype == "Sales Invoice"
                        else "Purchase Invoice Item"
                    )
                    exact_doc = _taxes_as_invoice_items(
                        doc,
                        doctype,
                        suppress_target_gst=self._supports(
                            item_doctype, "gst_treatment"),
                        non_gst_template=f"Non-GST - {self.d.abbr}",
                    )
                    if doctype == "Sales Invoice" and exact_doc.get("name"):
                        exact_doc["name"] = self._available_name(
                            doctype, exact_doc["name"])
                    try:
                        res = self._insert_and_submit(
                            doctype, exact_doc, manual_rounding)
                    except ERPNextError:
                        self.fallback.append(vrow["guid"])
                        stats["fallback"] += 1
                        continue
                    name = _name_of(res)
                    if not name:
                        name = self.erp.find_by_field(
                            doctype, self.field, vrow["guid"],
                            exclude_cancelled=True)
                    if not name or not self._posted_value_matches(
                            doctype, name, float(vrow["amount"] or 0)):
                        if name:
                            self.erp.cancel(doctype, name)
                            stats["cancelled_retries"] += 1
                        self.fallback.append(vrow["guid"])
                        stats["fallback"] += 1
                        continue
                if bridge:
                    created = self._ensure_party_bridge(
                        vrow, bridge, doctype, name)
                    stats["bridged"] += int(created)
                bridge_ok = True
                self.store.mark("voucher", vrow["guid"], "loaded", doctype, name)
                self.store.add_bill_ref(party, billname, doctype, name)
                stats["loaded"] += 1
            except ERPNextError as exc:
                # A network timeout can happen after the server commits. Query
                # by GUID before marking the source row as failed.
                existing = None
                if not self.erp.dry_run and doctype:
                    existing = self.erp.find_by_field(
                        doctype, self.field, vrow["guid"],
                        exclude_cancelled=True)
                if (
                    existing
                    and self._is_submitted(doctype, existing)
                    and (not bridge or bridge_ok)
                ):
                    self.store.mark(
                        "voucher", vrow["guid"], "loaded", doctype, existing)
                    self.store.add_bill_ref(
                        party, billname, doctype, existing)
                    stats["skipped"] += 1
                elif _is_missing_item_gst_error(exc):
                    self.fallback.append(vrow["guid"])
                    stats["fallback"] += 1
                else:
                    if not self.erp.dry_run:
                        self.store.mark(
                            "voucher", vrow["guid"], "error",
                            error=_error_detail(exc)[:2000])
                    stats["error"] += 1
            if i % 50 == 0:
                self.store.conn.commit()
                progress(i, len(rows), stats)
        self.store.conn.commit()
        progress(len(rows), len(rows), stats)
        return stats

    def _posted_value_matches(
            self, doctype: str, name: str, source_total: float) -> bool:
        """Fail closed when server-side hooks change a submitted invoice."""
        rows = self.erp.get_list(
            "GL Entry",
            fields=["debit", "credit"],
            filters=[
                ["voucher_type", "=", doctype],
                ["voucher_no", "=", name],
                ["is_cancelled", "=", 0],
            ],
            limit=0,
        )
        debit = round(sum(float(row.get("debit") or 0) for row in rows), 2)
        credit = round(sum(float(row.get("credit") or 0) for row in rows), 2)
        expected = round(abs(float(source_total or 0)), 2)
        return (
            bool(rows)
            and abs(debit - credit) < 0.004
            and abs(debit - expected) < 0.004
        )

    def _is_submitted(self, doctype: str, name: str) -> bool:
        # Lightweight mocks used in unit tests do not expose the raw request
        # method; their declared existing document is treated as submitted.
        if not hasattr(self.erp, "_request"):
            return True
        dt = urllib.parse.quote(doctype, safe="")
        nm = urllib.parse.quote(str(name), safe="")
        data = self.erp._request(
            "GET", f"/api/resource/{dt}/{nm}")["data"]
        return int(data.get("docstatus") or 0) == 1

    def _valid_source_gstin(self, party_name: str) -> str:
        """Checksum-valid GSTIN from the staged ledger or another voucher.

        A truncated/typo voucher GSTIN must not block the invoice when Tally
        already has a valid GSTIN on the same party's ledger or on a later
        voucher. Conflicting valid GSTINs are left unused rather than guessed.
        """
        if not hasattr(self, "_source_gstin"):
            mapping: dict[str, str] = {}
            voucher_gstins: dict[str, set[str]] = {}
            masters = getattr(self.store, "masters", None)
            if callable(masters):
                for row in masters("ledger"):
                    gstin = _scalar(
                        json.loads(row["payload"] or "{}").get("PARTYGSTIN")
                    ).strip().upper()
                    if _valid_gstin(gstin):
                        mapping[_norm(row["name"])] = gstin
            vouchers = getattr(self.store, "vouchers", None)
            if callable(vouchers):
                for row in vouchers():
                    gstin = _scalar(
                        json.loads(row["payload"] or "{}").get("PARTYGSTIN")
                    ).strip().upper()
                    if not _valid_gstin(gstin):
                        continue
                    voucher_gstins.setdefault(
                        _norm(row["party"]), set()).add(gstin)
            for party, gstins in voucher_gstins.items():
                if party in mapping:
                    continue
                if len(gstins) == 1:
                    mapping[party] = next(iter(gstins))
            self._source_gstin = mapping
        return self._source_gstin.get(_norm(party_name), "")

    def _insert_invoice(
            self, doctype: str, doc: dict,
            manual_rounding: dict | None):
        """Insert the invoice; on India Compliance item-GST rejection, retry
        with tax ledgers as explicit Non-GST lines so the Tally amount survives.
        """
        try:
            return self._insert_and_submit(doctype, doc, manual_rounding)
        except ERPNextError as exc:
            if not _is_missing_item_gst_error(exc):
                raise
            item_doctype = (
                "Sales Invoice Item" if doctype == "Sales Invoice"
                else "Purchase Invoice Item"
            )
            exact_doc = _taxes_as_invoice_items(
                doc,
                doctype,
                suppress_target_gst=self._supports(
                    item_doctype, "gst_treatment"),
                non_gst_template=f"Non-GST - {self.d.abbr}",
            )
            if doctype == "Sales Invoice" and exact_doc.get("name"):
                exact_doc["name"] = self._available_name(
                    doctype, exact_doc["name"])
            return self._insert_and_submit(
                doctype, exact_doc, manual_rounding)

    def _insert_and_submit(
            self, doctype: str, doc: dict,
            manual_rounding: dict | None):
        if not manual_rounding:
            return self.erp.insert_and_submit(doctype, doc)
        if self.erp.dry_run:
            return {"_dry_run": True, "doctype": doctype}

        # First save a normal draft so ERPNext calculates and persists every
        # item/base/net field. Then enable its manual-rounding preservation flag
        # and supply the exact Tally party total before submission.
        res = self.erp.insert(doctype, doc)
        name = _name_of(res)
        if not name:
            raise ERPNextError(
                f"{doctype} manual-rounding draft returned no name")
        dt = urllib.parse.quote(doctype, safe="")
        nm = urllib.parse.quote(str(name), safe="")
        draft = self.erp._request(
            "GET", f"/api/resource/{dt}/{nm}")["data"]
        grand_total = float(draft.get("grand_total") or 0)
        source_total = float(manual_rounding["source_total"])
        adjustment = round(source_total - grand_total, 2)
        self.erp.update(doctype, name, {
            "is_consolidated": 1,
            "disable_rounded_total": 0,
            "rounded_total": source_total,
            "base_rounded_total": source_total,
            "rounding_adjustment": adjustment,
            "base_rounding_adjustment": adjustment,
        })
        self.erp.update(doctype, name, {"docstatus": 1})
        saved = self.erp._request(
            "GET", f"/api/resource/{dt}/{nm}")["data"]
        if (
            int(saved.get("docstatus") or 0) != 1
            or abs(float(saved.get("rounded_total") or 0) - source_total) > 0.004
            or abs(
                float(saved.get("rounding_adjustment") or 0) - adjustment
            ) > 0.004
        ):
            raise ERPNextError(
                f"{doctype} {name} did not preserve Tally manual rounded total")
        return {"data": {"name": name}}

    def _ensure_party_bridge(
            self, vrow, bridge: dict, invoice_doctype: str,
            invoice_name: str) -> bool:
        """Preserve a nonstandard Tally party ledger without losing the invoice.

        The invoice posts to ERPNext's Receivable/Payable control. This paired JE
        immediately moves that party amount to the source Tally account/other
        party control, leaving the final GL identical. It deliberately does not
        reference the invoice: this row is a reclassification, not a payment,
        and must not change invoice outstanding or aging.
        """
        key = f"{vrow['guid']}:party-control-bridge"
        existing = self.erp.find_by_field(
            "Journal Entry", self.field, key, exclude_cancelled=True)
        if existing:
            return False
        party_line = bridge["party_line"]
        source: Resolved | None = bridge["source_res"]
        target: Resolved = bridge["target_res"]
        if source is None:
            raise ERPNextError(
                f"Cannot bridge unresolved source party ledger "
                f"{party_line['ledger']}")
        amount = round(party_line["mag"], 2)
        target_row = {
            "account": target.account,
            "party_type": target.party_type,
            "party": target.party,
        }
        source_row = {"account": source.account}
        if source.kind == "party":
            source_row["party_type"] = source.party_type
            source_row["party"] = source.party
        else:
            source_row["cost_center"] = self.d.cost_center
        _put_side(target_row, not party_line["debit"], amount)
        _put_side(source_row, party_line["debit"], amount)
        doc = {
            "company": self.d.name,
            "posting_date": vrow["vdate"],
            "voucher_type": "Journal Entry",
            "title": f"Tally party-control bridge {vrow['vnumber'] or ''}".strip(),
            "user_remark": (
                f"ERPNext invoice control reclassification to source Tally "
                f"ledger {party_line['ledger']}. "
                f"{_scalar(bridge['payload'].get('NARRATION'))}"
            )[:1000],
            "accounts": [target_row, source_row],
            self.field: key,
        }
        self.erp.submit_doc("Journal Entry", doc)
        return True


def _norm(value) -> str:
    return " ".join(_scalar(value).split())


def _taxes_as_invoice_items(
        doc: dict, doctype: str, *, suppress_target_gst: bool = False,
        non_gst_template: str = "") -> dict:
    """Preserve exact tax-ledger postings when target tax hooks rewrite rows.

    The normal path continues to use ERPNext's Taxes table. This representation
    is used only after submitted GL proves that the target changed the source
    amount. Tax ledgers remain visible on the invoice and post to their mapped
    accounts without inferring or fabricating a tax rate.
    """
    exact = deepcopy(doc)
    account_field = (
        "income_account" if doctype == "Sales Invoice" else "expense_account"
    )
    for tax in exact.get("taxes") or []:
        amount = round(float(tax.get("tax_amount") or 0), 2)
        if not amount:
            continue
        label = str(
            tax.get("description") or tax.get("account_head") or "Tax"
        )
        exact.setdefault("items", []).append({
            "item_code": GENERIC_ITEM,
            "item_name": label[:140],
            "description": label[:1000],
            "qty": 1 if amount > 0 else -1,
            "rate": abs(amount),
            account_field: tax["account_head"],
            "cost_center": tax.get("cost_center"),
        })
    exact["taxes"] = []
    exact.pop("taxes_and_charges", None)
    if suppress_target_gst:
        # India Compliance can recalculate tax from the migration item's HSN at
        # submit time even after explicit taxes are removed.  Mark only this
        # exact-ledger fallback representation Non-GST so it does not fabricate
        # a second tax.  The source tax ledgers remain explicit invoice lines.
        template = non_gst_template or "Non-GST"
        for item in exact.get("items") or []:
            item["gst_treatment"] = "Non-GST"
            item["gst_hsn_code"] = ""
            item["item_tax_template"] = template
            item["item_tax_rate"] = "{}"
            for kind in ("igst", "cgst", "sgst", "cess"):
                item[f"{kind}_rate"] = 0
                item[f"{kind}_amount"] = 0
    return exact


def _bankers_round_to_rupee(value: float) -> float:
    """Match this target's System Settings + INR currency rounding."""
    clean = Decimal(str(round(float(value), 8)))
    return float(clean.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _error_detail(exc: ERPNextError) -> str:
    body = getattr(exc, "body", None)
    return f"{exc}: {body}" if body else str(exc)


def _is_missing_item_gst_error(exc: ERPNextError) -> bool:
    text = f"{exc} {getattr(exc, 'body', '') or ''}"
    return "No GST is being charged on Taxable Items" in text


def _resolve_party_gstin(voucher_gstin: str, ledger_gstin: str) -> tuple[str, str]:
    """Return a checksum-valid GSTIN without inventing a replacement.

    Prefer the voucher GSTIN when it is valid. If it is missing or fails
    checksum/shape, use a valid ledger-master GSTIN. Otherwise omit it and
    keep the exact Tally string in a remark — never block the invoice.
    """
    voucher = (voucher_gstin or "").strip().upper()
    ledger = (ledger_gstin or "").strip().upper()
    if _valid_gstin(voucher):
        return voucher, ""
    if _valid_gstin(ledger):
        if voucher and voucher != ledger:
            return ledger, (
                f"[Migration: voucher GSTIN {voucher} is not a valid GSTIN; "
                f"used Tally source GSTIN {ledger}.]"
            )
        return ledger, ""
    if voucher:
        return "", (
            f"[Migration: Tally GSTIN {voucher} is not a valid GSTIN and was "
            "omitted from ERPNext GSTIN fields.]"
        )
    return "", ""


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return _scalar(value.get("#text", ""))
    if isinstance(value, list):
        return _scalar(value[0]) if value else ""
    return str(value).strip()


def _item_description(ledger: str, narration) -> str:
    note = _scalar(narration)
    return f"{ledger} — {note}"[:1000] if note else ledger[:1000]


def _tax_rate(ledger: str) -> float:
    match = re.search(r"@\s*(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*%",
                      ledger or "")
    if not match:
        return 0.0
    return float(match.group(1) or match.group(2))


def _gst_kind(value: str) -> str | None:
    text = _norm(value).casefold()
    for kind in ("cgst", "sgst", "igst", "cess"):
        if kind in text:
            return kind
    return None


def _allocate_money(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    denominator = sum(weights, Decimal("0.00"))
    if not denominator:
        return []
    result, remaining = [], total
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            value = remaining
        else:
            value = (total * weight / denominator).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            remaining -= value
        result.append(value)
    return result


def _apply_single_rate_item_gst(items: list[dict], taxes: list[dict]) -> bool:
    """Populate item GST fields only when source rates are unambiguous."""
    grouped: dict[str, list[dict]] = {}
    for tax in taxes:
        kind = _gst_kind(tax.get("description") or tax.get("account_head"))
        if kind:
            grouped.setdefault(kind, []).append(tax)
    if not grouped or not items:
        return False
    # One source ledger/rate per GST kind is safe. Multiple rates need the
    # dedicated splitter because a generic ledger item cannot identify which
    # taxable base belongs to which rate.
    if any(
        len(rows) != 1 or float(rows[0].get("rate") or 0) <= 0
        for rows in grouped.values()
    ):
        return False
    weights = [
        abs(Decimal(str(item.get("qty") or 0))
            * Decimal(str(item.get("rate") or 0)))
        for item in items
    ]
    if not sum(weights, Decimal("0.00")):
        return False
    account_rates = {
        rows[0]["account_head"]: float(rows[0]["rate"])
        for rows in grouped.values()
    }
    allocations = {
        kind: _allocate_money(
            Decimal(str(rows[0]["tax_amount"])).quantize(Decimal("0.01")),
            weights,
        )
        for kind, rows in grouped.items()
    }
    for index, item in enumerate(items):
        item["gst_treatment"] = "Taxable"
        item["taxable_value"] = float(weights[index])
        item["item_tax_rate"] = json.dumps(account_rates, sort_keys=True)
        for kind, rows in grouped.items():
            item[f"{kind}_rate"] = float(rows[0]["rate"])
            item[f"{kind}_amount"] = float(allocations[kind][index])
    return True


def _tally_date(value) -> str | None:
    raw = _scalar(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return None


def _due_date(bill_date: str, credit_period) -> str:
    match = re.search(r"(\d+)", _scalar(credit_period))
    days = int(match.group(1)) if match else 0
    return (
        datetime.strptime(bill_date, "%Y-%m-%d").date()
        + timedelta(days=days)
    ).isoformat()


_GST_STATE_BY_CODE = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa",
    "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman and Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}


def _place_of_supply(state: str, gstin: str) -> str:
    """Return a valid GST ``NN-State`` place-of-supply value.

    The party GSTIN identifies the party's registration state, which can differ
    from the transaction's place of supply.  Prefer Tally's explicit state and
    derive that state's code.  Use the GSTIN only when no state was supplied.
    """
    raw_state = _scalar(state).strip()
    prefixed = re.fullmatch(r"(\d{2})\s*-\s*(.+)", raw_state)
    if prefixed:
        code, supplied_name = prefixed.groups()
        canonical = _GST_STATE_BY_CODE.get(code)
        if canonical and _norm(canonical).casefold() == _norm(supplied_name).casefold():
            return f"{code}-{canonical}"
        raw_state = supplied_name.strip()

    aliases = {
        "orissa": "Odisha",
        "pondicherry": "Puducherry",
        "jammu & kashmir": "Jammu and Kashmir",
        "andaman & nicobar islands": "Andaman and Nicobar Islands",
    }
    state_name = aliases.get(raw_state.casefold(), raw_state)
    if state_name:
        for code, canonical in _GST_STATE_BY_CODE.items():
            if _norm(canonical).casefold() == _norm(state_name).casefold():
                return f"{code}-{canonical}"
        return state_name

    gstin_code = gstin[:2] if len(gstin) >= 2 and gstin[:2].isdigit() else ""
    gstin_state = _GST_STATE_BY_CODE.get(gstin_code, "")
    return f"{gstin_code}-{gstin_state}" if gstin_state else ""


def _tax_driven_place_of_supply(place: str, party_gstin: str,
                                company_gstin: str,
                                tax_ledgers: list[str]) -> str:
    """Resolve target POS from the GST ledgers actually posted in Tally.

    ERPNext/India Compliance uses this field to decide IGST vs CGST/SGST. On a
    purchase Tally's text can identify the delivery/company state even though
    the supplier-to-destination supply is interstate. The tax ledger is the
    stronger accounting evidence in that conflict.
    """
    kinds = {_gst_kind(ledger) for ledger in tax_ledgers}
    if "igst" in kinds and party_gstin:
        return _place_of_supply("", party_gstin) or place
    if kinds & {"cgst", "sgst"} and company_gstin:
        return _place_of_supply("", company_gstin) or place
    return place


def _same_posting_target(source: Resolved | None,
                         target: Resolved) -> bool:
    if source is None or source.account != target.account:
        return False
    if source.kind != "party":
        return False
    return (
        source.party_type == target.party_type
        and source.party == target.party
    )


def _put_side(row: dict, debit: bool, amount: float) -> None:
    if debit:
        row["debit_in_account_currency"] = amount
    else:
        row["credit_in_account_currency"] = amount


def _gst_safe_name(raw: str | None) -> str:
    """GST transaction names allow only alphanumerics, '-' and '/', starting
    with an alphanumeric."""
    import re
    s = re.sub(r"[^A-Za-z0-9/-]+", "-", (raw or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-/")
    return s


def _with_suffix(base: str, suffix: str, max_length: int = 140) -> str:
    room = max(1, max_length - len(suffix))
    return f"{base[:room]}{suffix}"


def _guid_key(guid: str) -> str:
    # Tally GUIDs from one company often share a long prefix; hash the complete
    # value instead of slicing the prefix.
    import hashlib
    return hashlib.sha1(guid.encode("utf-8")).hexdigest()[:12]


def _unique_transaction_name(raw: str | None, posting_date: str | None,
                             guid: str, fallback: str) -> str:
    base = _gst_safe_name(raw) or fallback
    date = (posting_date or "undated").replace("-", "")
    key = _guid_key(guid)
    return _with_suffix(base, f"-{date}-{key}")


def _unique_bill_no(raw: str | None, guid: str) -> str:
    base = _gst_safe_name(raw) or "TLY-BILL"
    key = _guid_key(guid)
    return _with_suffix(base, f"-{key}")


def _supplier_bill_no(raw: str | None, guid: str,
                      duplicate: bool) -> str:
    """Keep the external supplier number exact unless Tally truly duplicated it."""
    base = _scalar(raw) or "TLY-BILL"
    return _with_suffix(base, f"-{_guid_key(guid)}") if duplicate else base[:140]


def _name_of(res):
    if isinstance(res, dict):
        msg = res.get("message")
        if isinstance(msg, dict):
            return msg.get("name")
        if isinstance(res.get("data"), dict):
            return res["data"].get("name")
    return None
