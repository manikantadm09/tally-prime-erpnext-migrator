"""Guarded replacement of invoice-shaped fallback Journal Entries.

The source Journal Entry remains active while the normal InvoiceLoader creates
the replacement invoice (and an optional party-control bridge).  The source is
cancelled only after the replacement has an identical account/party GL
signature.  A failed comparison cancels the candidate documents and leaves the
source Journal Entry active, so the operation fails financially closed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
import urllib.parse

from .config import get_config
from .erpnext_client import ERPNextClient, ERPNextError
from .load_invoices import (
    InvoiceLoader, _apply_single_rate_item_gst, _gst_kind, _place_of_supply,
    _scalar, _tax_rate, _taxes_as_invoice_items, _valid_gstin,
)
from .load_masters import fetch_company_defaults
from .load_vouchers import _name_of
from .lines import is_tax_ledger, parse_entries
from .mapping import LedgerResolver
from .staging import Staging


MONEY = Decimal("0.01")
INVOICE_TYPES = {"Sales", "Purchase", "Credit Note", "Debit Note"}


class FallbackRepairError(RuntimeError):
    pass


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _gl_rows(erp: ERPNextClient, doctype: str, name: str) -> list[dict]:
    return erp.get_list(
        "GL Entry",
        fields=["account", "party_type", "party", "debit", "credit"],
        filters=[
            ["voucher_type", "=", doctype],
            ["voucher_no", "=", name],
            ["is_cancelled", "=", 0],
        ],
        limit=0,
    )


def gl_signature(groups: list[list[dict]]) -> dict[tuple[str, str, str], Decimal]:
    """Net a set of vouchers by account and party at paise precision.

    Gross rows are deliberately netted because a party-control bridge contains
    an equal debit and credit to the normal ERPNext control account.  What must
    equal the source fallback is the resulting account/party balance movement.
    """
    totals: dict[tuple[str, str, str], list[Decimal]] = defaultdict(
        lambda: [Decimal("0.00"), Decimal("0.00")]
    )
    for rows in groups:
        for row in rows:
            key = (
                str(row.get("account") or ""),
                str(row.get("party_type") or ""),
                str(row.get("party") or ""),
            )
            totals[key][0] += _money(row.get("debit"))
            totals[key][1] += _money(row.get("credit"))
    signature = {}
    for key, values in totals.items():
        net = (values[0] - values[1]).quantize(MONEY)
        if net:
            signature[key] = net
    return signature


def _report_signature(signature: dict) -> dict[str, str]:
    return {
        " | ".join(key): f"{value:.2f}"
        for key, value in signature.items()
    }


def _norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def _row_for_invoice_build(source_row, acknowledge_invalid_gstin: bool):
    """Return a transient source row safe for an invalid Tally GSTIN.

    The authoritative staging payload is never mutated. ERPNext cannot accept
    a checksum-invalid GSTIN, so an explicitly acknowledged repair omits it
    from transactional GSTIN fields and retains the exact source string in the
    invoice remarks for auditability.
    """
    payload = json.loads(source_row["payload"])
    party_gstin = _scalar(payload.get("PARTYGSTIN")).strip().upper()
    if not party_gstin or _valid_gstin(party_gstin):
        return source_row, None
    if not acknowledge_invalid_gstin:
        raise FallbackRepairError(
            f"source GSTIN {party_gstin!r} fails checksum; rerun only after "
            "review with --acknowledge-invalid-source-gstin")
    payload.pop("PARTYGSTIN", None)
    warning = (
        f"[Migration source warning: Tally GSTIN {party_gstin} failed "
        "checksum and was omitted from ERPNext GSTIN fields.]"
    )
    narration = _scalar(payload.get("NARRATION")).strip()
    payload["NARRATION"] = f"{narration}\n{warning}".strip()[:1000]
    transient = dict(source_row)
    transient["payload"] = json.dumps(payload)
    return transient, party_gstin


def _invalid_gstin_evidence(erp: ERPNextClient, doctype: str, name: str,
                            invalid_gstin: str | None) -> dict | None:
    if not invalid_gstin:
        return None
    doc = _invoice_doc(erp, doctype, name)
    remarks = str(doc.get("remarks") or "")
    gstin_values = {
        str(doc.get(field) or "").strip().upper()
        for field in (
            "supplier_gstin", "billing_address_gstin", "customer_gstin",
            "shipping_address_gstin",
        )
    }
    evidence = {
        "invalid_source_gstin": invalid_gstin,
        "omitted_from_erpnext_gstin_fields": invalid_gstin not in gstin_values,
        "retained_in_remarks": invalid_gstin in remarks,
    }
    if not all(value for key, value in evidence.items()
               if key != "invalid_source_gstin"):
        raise FallbackRepairError(
            f"invalid source GSTIN evidence was not preserved safely: {evidence}")
    return evidence


def _tax_evidence(erp: ERPNextClient, source_row, doctype: str,
                  name: str) -> dict:
    """Prove Tally tax ledgers survived as invoice tax rows and GST breakup."""
    payload = json.loads(source_row["payload"])
    sign = -1 if source_row["vtype"] in ("Credit Note", "Debit Note") else 1
    expected = Counter(
        (_norm(entry["ledger"]), sign * _money(entry["mag"]))
        for entry in parse_entries(payload)
        if is_tax_ledger(entry["ledger"])
    )
    path = "/api/resource/{}/{}".format(
        urllib.parse.quote(doctype, safe=""),
        urllib.parse.quote(name, safe=""),
    )
    doc = erp._request("GET", path)["data"]
    actual = Counter(
        (_norm(tax.get("description") or tax.get("account_head")),
         _money(tax.get("tax_amount")))
        for tax in doc.get("taxes") or []
        if _money(tax.get("tax_amount"))
    )
    expected_gst: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for (ledger, amount), count in expected.items():
        for kind in ("cgst", "sgst", "igst", "cess"):
            if kind in ledger:
                expected_gst[kind] += amount * count
                break
    actual_gst = {
        kind: sum(
            (_money(item.get(f"{kind}_amount")) for item in doc.get("items") or []),
            Decimal("0.00"),
        )
        for kind in expected_gst
    }
    return {
        "tax_rows_match": actual == expected,
        "gst_breakup_match": all(
            actual_gst[kind] == amount
            for kind, amount in expected_gst.items()
        ),
        "expected_tax_rows": expected,
        "actual_tax_rows": actual,
        "expected_gst_breakup": dict(expected_gst),
        "actual_gst_breakup": actual_gst,
        "items": [
            {
                key: item.get(key)
                for key in (
                    "item_code", "gst_hsn_code", "gst_treatment",
                    "taxable_value", "net_amount", "cgst_rate", "cgst_amount",
                    "sgst_rate", "sgst_amount", "igst_rate", "igst_amount",
                    "item_tax_rate",
                )
            }
            for item in doc.get("items") or []
        ],
    }


def _invoice_doc(erp: ERPNextClient, doctype: str, name: str) -> dict:
    path = "/api/resource/{}/{}".format(
        urllib.parse.quote(doctype, safe=""),
        urllib.parse.quote(name, safe=""),
    )
    return erp._request("GET", path)["data"]


def _parent_totals(doc: dict) -> dict[str, Decimal]:
    return {
        field: _money(doc.get(field))
        for field in (
            "net_total", "total_taxes_and_charges", "grand_total",
            "rounded_total", "rounding_adjustment", "outstanding_amount",
        )
    }


def _populate_item_gst_metadata(erp: ERPNextClient, source_row,
                                doctype: str, name: str) -> dict:
    """Set display-only item GST fields after exact financial submission.

    India Compliance may recompute an explicit Tally tax by a few paise during
    submit, so these fields cannot be supplied before accounting is posted.
    Every parent total and active GL row must remain byte-for-byte equivalent.
    """
    doc = _invoice_doc(erp, doctype, name)
    payload = json.loads(source_row["payload"])
    sign = -1 if source_row["vtype"] in ("Credit Note", "Debit Note") else 1
    source_taxes = [
        entry for entry in parse_entries(payload)
        if is_tax_ledger(entry["ledger"])
    ]
    account_by_kind = {
        _gst_kind(tax.get("description") or tax.get("account_head")):
            tax.get("account_head")
        for tax in doc.get("taxes") or []
        if _gst_kind(tax.get("description") or tax.get("account_head"))
    }
    tax_rows = []
    for entry in source_taxes:
        kind = _gst_kind(entry["ledger"])
        rate = _tax_rate(entry["ledger"])
        account = account_by_kind.get(kind)
        if kind and rate and account:
            tax_rows.append({
                "description": entry["ledger"],
                "account_head": account,
                "rate": rate,
                "tax_amount": float(sign * _money(entry["mag"])),
            })
    templates = [
        {"qty": 1, "rate": abs(float(item.get("net_amount") or 0))}
        for item in doc.get("items") or []
    ]
    if not _apply_single_rate_item_gst(templates, tax_rows):
        return {"applied": False, "reason": "not unambiguous single-rate GST"}

    before_totals = _parent_totals(doc)
    before_gl = _gl_rows(erp, doctype, name)
    child_doctype = f"{doctype} Item"
    fields = (
        "gst_treatment", "taxable_value", "item_tax_rate",
        "cgst_rate", "cgst_amount", "sgst_rate", "sgst_amount",
        "igst_rate", "igst_amount", "cess_rate", "cess_amount",
    )
    for item, template in zip(doc.get("items") or [], templates):
        values = {key: template[key] for key in fields if key in template}
        erp.update(child_doctype, item["name"], values)
    after_doc = _invoice_doc(erp, doctype, name)
    after_totals = _parent_totals(after_doc)
    after_gl = _gl_rows(erp, doctype, name)
    if before_totals != after_totals or before_gl != after_gl:
        raise FallbackRepairError(
            "item GST metadata changed invoice totals or GL")
    return {
        "applied": True,
        "items": len(templates),
        "parent_totals_unchanged": True,
        "gl_unchanged": True,
    }
def _ensure_unallocated_source(erp: ERPNextClient, doctype: str, name: str) -> None:
    """Refuse a source JE already used as a bill-wise payment reference."""
    filters = [["docstatus", "=", 1]]
    fields = ["name", "voucher_type", "voucher_no", "against_voucher_type",
              "against_voucher_no", "amount"]
    rows = []
    rows.extend(erp.get_list(
        "Payment Ledger Entry", fields=fields,
        filters=filters + [["voucher_type", "=", doctype], ["voucher_no", "=", name]],
        limit=0,
    ))
    rows.extend(erp.get_list(
        "Payment Ledger Entry", fields=fields,
        filters=filters + [["against_voucher_type", "=", doctype],
                           ["against_voucher_no", "=", name]],
        limit=0,
    ))
    external = [row for row in rows if not (
        row.get("voucher_type") == doctype
        and row.get("voucher_no") == name
        and row.get("against_voucher_type") == doctype
        and row.get("against_voucher_no") == name
    )]
    if external:
        raise FallbackRepairError(
            f"{doctype} {name} has {len(external)} external payment-ledger "
            "reference(s); repair those links explicitly before replacement"
        )


def _documents_by_field(erp: ERPNextClient, doctype: str, field: str,
                        value: str) -> list[dict]:
    return erp.get_list(
        doctype, fields=["name", "docstatus"],
        filters=[[field, "=", value], ["docstatus", "!=", 2]], limit=0)


def _submitted(erp: ERPNextClient, doctype: str, field: str,
               value: str) -> str | None:
    rows = _documents_by_field(erp, doctype, field, value)
    return next(
        (row["name"] for row in rows if int(row.get("docstatus") or 0) == 1),
        None,
    )


def _remove_candidate(erp: ERPNextClient, doctype: str, name: str | None) -> None:
    if name and erp.exists(doctype, name):
        rows = erp.get_list(doctype, fields=["name", "docstatus"],
                            filters=[["name", "=", name]], limit=1)
        if rows and int(rows[0].get("docstatus") or 0) == 1:
            erp.cancel(doctype, name)
        elif rows and int(rows[0].get("docstatus") or 0) == 0:
            erp.delete(doctype, name)


def repair_one(erp: ERPNextClient, store: Staging, guid: str,
               idempotency_field: str, *, phase: str = "full",
               neutralize_target_gst: bool = False,
               acknowledge_invalid_source_gstin: bool = False) -> dict:
    if phase not in ("full", "prepare", "finalize"):
        raise FallbackRepairError(f"unsupported repair phase {phase!r}")
    row = store.voucher_by_guid(guid)
    if not row:
        raise FallbackRepairError(f"GUID {guid} is absent from production staging")
    if row["vtype"] not in INVOICE_TYPES:
        raise FallbackRepairError(
            f"GUID {guid} is {row['vtype']}, not an invoice-shaped voucher")

    planned_doctype = (
        "Sales Invoice" if row["vtype"] in ("Sales", "Credit Note")
        else "Purchase Invoice"
    )
    invoice_rows = _documents_by_field(
        erp, planned_doctype, idempotency_field, guid)
    existing_invoice = next(
        (doc["name"] for doc in invoice_rows
         if int(doc.get("docstatus") or 0) == 1), None)
    if existing_invoice and phase != "finalize":
        raise FallbackRepairError(
            f"active {planned_doctype} {existing_invoice} already owns GUID {guid}")
    drafts = [
        doc["name"] for doc in invoice_rows
        if int(doc.get("docstatus") or 0) == 0
    ]
    if not erp.dry_run and phase != "finalize":
        for draft in drafts:
            erp.delete(planned_doctype, draft)
    source_je = _submitted(erp, "Journal Entry", idempotency_field, guid)
    if not source_je:
        raise FallbackRepairError(f"no active fallback Journal Entry owns GUID {guid}")
    _ensure_unallocated_source(erp, "Journal Entry", source_je)

    if erp.dry_run:
        return {
            "guid": guid, "source_je": source_je,
            "planned_doctype": planned_doctype, "action": "would-repair",
            "orphan_drafts_to_delete": drafts,
            "phase": phase,
        }

    defaults = fetch_company_defaults(erp)
    loader = InvoiceLoader(
        erp, store, defaults, LedgerResolver(store, defaults))
    build_row, invalid_source_gstin = _row_for_invoice_build(
        row, acknowledge_invalid_source_gstin)
    built = loader._build(build_row)
    if built is None:
        raise FallbackRepairError("canonical InvoiceLoader cannot model this voucher")
    doc, party, built_doctype, billname, bridge_spec = built
    if built_doctype != planned_doctype:
        raise FallbackRepairError("invoice route changed during guarded build")
    manual_rounding = doc.pop("_tally_manual_rounding", None)
    intended_place_of_supply = doc.get("place_of_supply")
    if neutralize_target_gst and doc.get("taxes"):
        cfg = get_config()
        neutral_place = _place_of_supply(
            cfg.erpnext.get("company_state", ""),
            cfg.erpnext.get("company_gstin", ""))
        if neutral_place:
            doc["place_of_supply"] = neutral_place
    candidate = existing_invoice if phase == "finalize" else None
    bridge = _submitted(
        erp, "Journal Entry", idempotency_field,
        f"{guid}:party-control-bridge") if phase == "finalize" else None
    source_cancelled = False
    try:
        if phase != "finalize":
            result = loader._insert_invoice(
                planned_doctype, doc, manual_rounding)
            candidate = _name_of(result) or _submitted(
                erp, planned_doctype, idempotency_field, guid)
            if (
                candidate
                and not loader._posted_value_matches(
                    planned_doctype, candidate, float(row["amount"] or 0))
            ):
                # India Compliance recomputes HSN/template GST and changes the
                # Tally amount. Retry with tax ledgers as explicit item lines
                # (same path as InvoiceLoader.run) so the PI still matches.
                erp.cancel(planned_doctype, candidate)
                item_doctype = f"{planned_doctype} Item"
                exact_doc = _taxes_as_invoice_items(
                    doc,
                    planned_doctype,
                    suppress_target_gst=loader._supports(
                        item_doctype, "gst_treatment"),
                    non_gst_template=f"Non-GST - {loader.d.abbr}",
                )
                result = loader._insert_and_submit(
                    planned_doctype, exact_doc, manual_rounding)
                candidate = _name_of(result) or _submitted(
                    erp, planned_doctype, idempotency_field, guid)
        if not candidate:
            raise FallbackRepairError(
                "no submitted candidate exists for the requested phase")
        if not loader._posted_value_matches(
                planned_doctype, candidate, float(row["amount"] or 0)):
            rows = _gl_rows(erp, planned_doctype, candidate)
            raise FallbackRepairError(
                "submitted candidate value differs from Tally: "
                f"source={_money(row['amount'])} "
                f"debit={sum((_money(r.get('debit')) for r in rows), Decimal('0'))} "
                f"credit={sum((_money(r.get('credit')) for r in rows), Decimal('0'))}")
        if bridge_spec and phase != "finalize":
            loader._ensure_party_bridge(
                row, bridge_spec, planned_doctype, candidate)
            bridge = _submitted(
                erp, "Journal Entry", idempotency_field,
                f"{guid}:party-control-bridge")
            if not bridge:
                raise FallbackRepairError(
                    "required party-control bridge was not submitted")

        invoice_now = _invoice_doc(erp, planned_doctype, candidate)
        taxes_as_items = not any(
            _money(tax.get("tax_amount"))
            for tax in invoice_now.get("taxes") or []
        )
        gst_metadata = (
            {"applied": False, "reason": "Tally tax ledgers posted as invoice item lines"}
            if taxes_as_items else (
                _populate_item_gst_metadata(erp, row, planned_doctype, candidate)
                if phase == "full" else {"applied": False, "phase": phase}
            )
        )
        tax_evidence = (
            {"tax_rows_match": False, "gst_breakup_match": False, "taxes_as_items": True}
            if taxes_as_items else _tax_evidence(
                erp, row, planned_doctype, candidate)
        )
        gstin_exception = _invalid_gstin_evidence(
            erp, planned_doctype, candidate, invalid_source_gstin)
        required_tax_ok = taxes_as_items or (
            tax_evidence["tax_rows_match"] and (
                phase == "prepare" or tax_evidence["gst_breakup_match"]))
        if not required_tax_ok:
            raise FallbackRepairError(
                "replacement invoice tax rows/GST breakup differ from Tally; "
                f"{tax_evidence}")

        before = gl_signature([_gl_rows(erp, "Journal Entry", source_je)])
        replacement_groups = [_gl_rows(erp, planned_doctype, candidate)]
        if bridge:
            replacement_groups.append(_gl_rows(erp, "Journal Entry", bridge))
        after = gl_signature(replacement_groups)
        if before != after:
            raise FallbackRepairError(
                "replacement GL signature differs from fallback Journal Entry; "
                f"source={before} replacement={after}")

        if phase == "prepare":
            return {
                "guid": guid,
                "source_je": source_je,
                "invoice_doctype": planned_doctype,
                "invoice": candidate,
                "bridge": bridge,
                "source_signature": _report_signature(before),
                "replacement_signature": _report_signature(after),
                "tax_evidence": tax_evidence,
                "temporary_place_of_supply": doc.get("place_of_supply"),
                "intended_place_of_supply": intended_place_of_supply,
                "source_gstin_exception": gstin_exception,
                "action": "prepared; source JE remains active pending GST metadata",
            }

        erp.cancel("Journal Entry", source_je)
        source_cancelled = True
        store.mark("voucher", guid, "loaded", planned_doctype, candidate)
        store.conn.execute(
            "UPDATE voucher SET erp_doctype=?,erp_name=?,error=NULL WHERE guid=?",
            (planned_doctype, candidate, guid))
        store.add_bill_ref(party, billname, planned_doctype, candidate)
        store.conn.commit()
        return {
            "guid": guid,
            "source_je": source_je,
            "invoice_doctype": planned_doctype,
            "invoice": candidate,
            "bridge": bridge,
            "source_signature": _report_signature(before),
            "replacement_signature": _report_signature(after),
            "tax_evidence": tax_evidence,
            "gst_metadata": gst_metadata,
            "source_gstin_exception": gstin_exception,
            "action": "repaired",
        }
    except Exception:
        if not source_cancelled and phase != "finalize":
            _remove_candidate(erp, "Journal Entry", bridge)
            _remove_candidate(erp, planned_doctype, candidate)
        raise
