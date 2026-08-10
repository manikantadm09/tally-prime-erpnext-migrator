from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET

from t2e.config import Config
from t2e.approve_change import ApprovalError, approve, preview
from t2e.load_invoices import (
    InvoiceLoader,
    _supplier_bill_no,
    _taxes_as_invoice_items,
    _unique_bill_no,
    _unique_transaction_name,
)
from t2e.load_masters import (
    MasterLoader,
    _insert_with_source_validation_fallback,
)
from t2e.ledger_fidelity import account_deltas
from t2e.load_period_closing import PeriodClosingLoader
from t2e.load_vouchers import VoucherLoader
from t2e.mapping import CompanyDefaults, Resolved
from t2e.reconcile import _count_migrated
from t2e.staging import Staging
from t2e.sync_report import build_report
from t2e.tally_export import effective_from_date, extract_vouchers, stage_voucher_export
from t2e.wipe import wipe


def defaults() -> CompanyDefaults:
    return CompanyDefaults(
        name="Test Company",
        abbr="TC",
        receivable="Debtors - TC",
        payable="Creditors - TC",
        round_off="Round Off - TC",
        cost_center="Main - TC",
        currency="INR",
        suspense="Tally Migration Suspense - TC",
        root_by_type={},
    )


class TemporaryStaging:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = SimpleNamespace(
            staging_db=Path(self.tmp.name) / "staging.sqlite")
        self.patcher = patch("t2e.staging.get_config", return_value=cfg)
        self.patcher.start()
        self.store = Staging()
        return self.store

    def __exit__(self, exc_type, exc, tb):
        self.store.close()
        self.patcher.stop()
        self.tmp.cleanup()


class NamingTests(unittest.TestCase):
    def test_sales_names_are_deterministic_and_unique(self):
        a = _unique_transaction_name(
            "1", "2023-04-01", "guid-aaaaaaaa", "TLY-SINV")
        b = _unique_transaction_name(
            "1", "2024-04-01", "guid-bbbbbbbb", "TLY-SINV")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 140)

    def test_supplier_bill_numbers_include_guid(self):
        a = _unique_bill_no("INV-1", "guid-aaaaaaaa")
        b = _unique_bill_no("INV-1", "guid-bbbbbbbb")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 140)

    def test_unique_supplier_bill_number_stays_exact(self):
        self.assertEqual(
            _supplier_bill_no("37991", "guid-aaaaaaaa", duplicate=False),
            "37991")
        self.assertNotEqual(
            _supplier_bill_no("37991", "guid-aaaaaaaa", duplicate=True),
            "37991")

    def test_cancelled_prompt_name_gets_deterministic_replacement(self):
        class ERP:
            @staticmethod
            def exists(doctype, name):
                return name == "TLY-SINV-1"

        loader = InvoiceLoader.__new__(InvoiceLoader)
        loader.erp = ERP()
        self.assertEqual(
            loader._available_name("Sales Invoice", "TLY-SINV-1"),
            "TLY-SINV-1-R1")


class InvoiceFidelityTests(unittest.TestCase):
    def test_ledger_fidelity_delta_reclassifies_without_changing_value(self):
        desired = {
            ("IGST INPUT @ 18 % - TC", None, None): Decimal("180.00"),
            ("Creditors - TC", "Supplier", "Vendor A"): Decimal("-1180.00"),
            ("Purchases - TC", None, None): Decimal("1000.00"),
        }
        actual = {
            ("Input Tax IGST - TC", None, None): Decimal("180.00"),
            ("Creditors - TC", "Supplier", "Vendor A"): Decimal("-1180.00"),
            ("Purchases - TC", None, None): Decimal("1000.00"),
        }
        deltas = account_deltas(desired, actual)
        self.assertEqual(
            deltas[("IGST INPUT @ 18 % - TC", None, None)],
            Decimal("180.00"),
        )
        self.assertEqual(
            deltas[("Input Tax IGST - TC", None, None)],
            Decimal("-180.00"),
        )
        self.assertEqual(sum(deltas.values()), Decimal("0.00"))

    def test_server_rewritten_taxes_can_fall_back_to_exact_invoice_items(self):
        source = {
            "items": [{
                "item_code": "Tally Migration Item",
                "qty": 1,
                "rate": 100,
                "expense_account": "Purchases - TC",
            }],
            "taxes": [{
                "account_head": "Input CGST - TC",
                "description": "CGST INPUT @ 9%",
                "tax_amount": 9,
                "cost_center": "Main - TC",
            }],
        }
        exact = _taxes_as_invoice_items(source, "Purchase Invoice")
        self.assertEqual(source["taxes"][0]["tax_amount"], 9)
        self.assertEqual(exact["taxes"], [])
        self.assertEqual(len(exact["items"]), 2)
        self.assertEqual(exact["items"][1]["rate"], 9)
        self.assertEqual(
            exact["items"][1]["expense_account"], "Input CGST - TC")

        india_exact = _taxes_as_invoice_items(
            source, "Purchase Invoice", suppress_target_gst=True)
        self.assertTrue(all(
            item["gst_treatment"] == "Non-GST"
            and item["gst_hsn_code"] == ""
            and item["item_tax_rate"] == "{}"
            for item in india_exact["items"]
        ))

    def test_rounding_narration_gstin_reference_and_due_date(self):
        class ERP:
            dry_run = True

            def has_field(self, doctype, fieldname):
                return True

        class Store:
            @staticmethod
            def duplicate_bill_key_count(party, billname):
                return 1

        supplier = Resolved(
            "party", "Creditors - TC", "Supplier", "Supplier A")
        accounts = {
            "Supplier A": supplier,
            "GST PURCHASE": Resolved("account", "GST Purchase - TC"),
            "CGST INPUT @ 9%": Resolved("account", "CGST - TC"),
            "SGST INPUT @ 9%": Resolved("account", "SGST - TC"),
            "ROUNDING OFF": Resolved("account", "Round Off - TC"),
        }

        class Resolver:
            @staticmethod
            def get(name):
                return accounts.get(name)

            @staticmethod
            def get_party(name, party_type):
                return supplier if (
                    name == "Supplier A" and party_type == "Supplier"
                ) else None

        payload = {
            "NARRATION": "TOWARDS PURCHASE OF PLYWOOD",
            "PARTYGSTIN": "29AAAFV8767D1ZR",
            "PLACEOFSUPPLY": "Karnataka",
            "REFERENCEDATE": "20260722",
            "ALLLEDGERENTRIES.LIST": [
                {
                    "LEDGERNAME": "Supplier A", "AMOUNT": "143266.00",
                    "BILLALLOCATIONS.LIST": {
                        "NAME": "37991", "BILLTYPE": "New Ref",
                        "AMOUNT": "143266.00",
                        "BILLDATE": "20260722",
                        "BILLCREDITPERIOD": "10 Days",
                    },
                },
                {"LEDGERNAME": "GST PURCHASE", "AMOUNT": "-121411.94"},
                {"LEDGERNAME": "CGST INPUT @ 9%", "AMOUNT": "-10927.07"},
                {"LEDGERNAME": "SGST INPUT @ 9%", "AMOUNT": "-10927.07"},
                {"LEDGERNAME": "ROUNDING OFF", "AMOUNT": "0.08"},
            ],
        }
        row = {
            "vtype": "Purchase", "party": "Supplier A",
            "payload": json.dumps(payload), "vdate": "2024-01-01",
            "vnumber": "145", "guid": "guid-37991",
        }
        loader = InvoiceLoader(
            ERP(), Store(), defaults(), Resolver())
        doc, party, doctype, billname, bridge = loader._build(row)
        self.assertEqual(party, "Supplier A")
        self.assertEqual(doctype, "Purchase Invoice")
        self.assertEqual(billname, "37991")
        self.assertIsNone(bridge)
        self.assertEqual(doc["bill_no"], "37991")
        self.assertEqual(doc["remarks"], "TOWARDS PURCHASE OF PLYWOOD")
        self.assertEqual(doc["disable_rounded_total"], 0)
        self.assertEqual(doc["due_date"], "2026-08-01")
        self.assertEqual(doc["supplier_gstin"], "29AAAFV8767D1ZR")
        self.assertEqual(doc["gst_category"], "Registered Regular")
        self.assertEqual(doc["place_of_supply"], "29-Karnataka")
        self.assertEqual(len(doc["taxes"]), 2)
        self.assertEqual([t["rate"] for t in doc["taxes"]], [9.0, 9.0])
        self.assertNotIn(
            "ROUND", " ".join(t["description"] for t in doc["taxes"]))
        self.assertIn("TOWARDS PURCHASE", doc["items"][0]["description"])
        self.assertNotIn("_tally_manual_rounding", doc)

    def test_nonstandard_whole_rupee_round_is_preserved_manually(self):
        class ERP:
            dry_run = True

            @staticmethod
            def has_field(doctype, fieldname):
                return True

        class Store:
            @staticmethod
            def duplicate_bill_key_count(party, billname):
                return 1

        supplier = Resolved(
            "party", "Creditors - TC", "Supplier", "DAWOOD PAINTS")
        accounts = {
            "DAWOOD PAINTS": supplier,
            "GST PURCHASE": Resolved("account", "GST Purchase - TC"),
            "CGST INPUT @ 9%": Resolved("account", "CGST - TC"),
            "SGST INPUT @ 9%": Resolved("account", "SGST - TC"),
            "ROUNDING OFF": Resolved("account", "Round Off - TC"),
        }

        class Resolver:
            @staticmethod
            def get(name):
                return accounts.get(name)

            @staticmethod
            def get_party(name, party_type):
                return supplier if name == "DAWOOD PAINTS" else None

        row = {
            "vtype": "Purchase", "party": "DAWOOD PAINTS",
            "payload": json.dumps({
                "ALLLEDGERENTRIES.LIST": [
                    {"LEDGERNAME": "DAWOOD PAINTS", "AMOUNT": "2850"},
                    {"LEDGERNAME": "GST PURCHASE", "AMOUNT": "-2415"},
                    {"LEDGERNAME": "CGST INPUT @ 9%", "AMOUNT": "-217"},
                    {"LEDGERNAME": "SGST INPUT @ 9%", "AMOUNT": "-217"},
                    {"LEDGERNAME": "ROUNDING OFF", "AMOUNT": "-1"},
                ],
            }),
            "vdate": "2022-07-29", "vnumber": "70",
            "guid": "manual-round-guid",
        }
        doc = InvoiceLoader(
            ERP(), Store(), defaults(), Resolver())._build(row)[0]
        self.assertEqual(doc["disable_rounded_total"], 1)
        self.assertEqual(
            doc["_tally_manual_rounding"],
            {"source_total": 2850.0, "unrounded_total": 2849.0})
        self.assertEqual(len(doc["taxes"]), 2)

    def test_nonstandard_source_party_requires_control_bridge(self):
        class ERP:
            dry_run = True

            @staticmethod
            def has_field(doctype, fieldname):
                return False

        class Store:
            @staticmethod
            def duplicate_bill_key_count(party, billname):
                return 1

        supplier = Resolved(
            "party", "Creditors - TC", "Supplier", "Vendor Advance")
        advance = Resolved("account", "Advances to Vendors - TC")

        class Resolver:
            @staticmethod
            def get(name):
                return {
                    "Vendor Advance": advance,
                    "Purchases": Resolved("account", "Purchases - TC"),
                }.get(name)

            @staticmethod
            def get_party(name, party_type):
                return supplier if name == "Vendor Advance" else None

        row = {
            "vtype": "Purchase", "party": "Vendor Advance",
            "payload": json.dumps({
                "ALLLEDGERENTRIES.LIST": [
                    {"LEDGERNAME": "Vendor Advance", "AMOUNT": "100"},
                    {"LEDGERNAME": "Purchases", "AMOUNT": "-100"},
                ],
            }),
            "vdate": "2026-01-01", "vnumber": "1", "guid": "bridge-guid",
        }
        built = InvoiceLoader(
            ERP(), Store(), defaults(), Resolver())._build(row)
        self.assertIsNotNone(built[-1])


class ConfigurationTests(unittest.TestCase):
    def test_tally_config_does_not_load_erp_secret_files(self):
        with patch(
                "t2e.config._read_env",
                side_effect=AssertionError("secret file should be lazy")):
            cfg = Config()
            self.assertEqual(cfg.tally["url"], "http://127.0.0.1:9000")


class MasterDryRunTests(unittest.TestCase):
    def test_dry_run_does_not_mark_master_as_loaded(self):
        with TemporaryStaging() as store:
            store.upsert_master("ledger", "master-guid", "New Ledger", None, {})
            loader = MasterLoader.__new__(MasterLoader)
            loader.erp = SimpleNamespace(dry_run=True)
            loader.store = store
            loader._mark("master-guid", "Account", "New Ledger - TC")
            self.assertEqual(store.masters("ledger")[0]["load_status"], "pending")


class TallyExtractionTests(unittest.TestCase):
    @staticmethod
    def voucher(date, guid, optional="No", cancelled="No"):
        return ET.fromstring(
            "<VOUCHER>"
            f"<DATE>{date}</DATE><GUID>{guid}</GUID>"
            "<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>{guid}</VOUCHERNUMBER>"
            f"<ISOPTIONAL>{optional}</ISOPTIONAL>"
            f"<ISCANCELLED>{cancelled}</ISCANCELLED>"
            "</VOUCHER>")

    def test_complete_suffix_response_is_replaced_atomically(self):
        vouchers = [
            self.voucher("20220205", "active-1"),
            self.voucher("20260722", "active-2"),
            self.voucher("20250702", "optional", optional="Yes"),
            self.voucher("20250620", "cancelled", cancelled="Yes"),
        ]
        with TemporaryStaging() as store:
            store.upsert_voucher(
                "stale", "Journal", "old", "2025-01-01", None, 0, {})
            types, suffix, authoritative_to = stage_voucher_export(
                store, vouchers, "20220201", "20220228", "20260727")
            self.assertTrue(suffix)
            self.assertEqual(authoritative_to, "20260727")
            self.assertEqual(types, {"Journal": 2})
            self.assertEqual(
                [r["guid"] for r in store.vouchers()],
                ["active-1", "active-2"])

    def test_small_window_leak_still_fails_closed(self):
        vouchers = [
            self.voucher("20220205", "active-1"),
            self.voucher("20220301", "leak"),
        ]
        with TemporaryStaging() as store:
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                stage_voucher_export(
                    store, vouchers, "20220201", "20220228", "20260727")

    def test_changed_loaded_voucher_is_reported_not_requeued(self):
        with TemporaryStaging() as store:
            store.upsert_voucher(
                "guid-1", "Journal", "1", "2024-01-10", None, 100,
                {"GUID": "guid-1", "AlterId": "1", "Narration": "before"})
            store.mark("voucher", "guid-1", "loaded", "Journal Entry", "ACC-JV-1")
            store.clear_voucher_window("2024-01-01", "2024-01-31")
            store.upsert_voucher(
                "guid-1", "Journal", "1", "2024-01-10", None, 120,
                {"GUID": "guid-1", "AlterId": "2", "Narration": "after"})
            store.conn.commit()
            row = store.vouchers()[0]
            self.assertEqual(row["load_status"], "loaded")
            self.assertEqual(row["source_state"], "changed")
            report = build_report(store)
            self.assertEqual(report["summary"]["by_source_state"], {"changed": 1})

    def test_missing_source_voucher_is_retained_for_review(self):
        with TemporaryStaging() as store:
            store.upsert_voucher("guid-1", "Journal", "1", "2024-01-10", None, 100, {})
            store.mark("voucher", "guid-1", "loaded", "Journal Entry", "ACC-JV-1")
            store.clear_voucher_window("2024-01-01", "2024-01-31")
            store.conn.commit()
            self.assertEqual(store.vouchers(), [])
            row = store.vouchers(include_inactive=True)[0]
            self.assertEqual(row["source_state"], "missing")
            self.assertEqual(row["erp_name"], "ACC-JV-1")

    def test_reexported_unchanged_voucher_clears_temporary_missing_state(self):
        payload = {"GUID": "guid-1", "AlterId": "1"}
        with TemporaryStaging() as store:
            store.upsert_voucher("guid-1", "Journal", "1", "2024-01-10", None, 100, payload)
            store.mark("voucher", "guid-1", "loaded", "Journal Entry", "ACC-JV-1")
            store.clear_voucher_window("2024-01-01", "2024-01-31")
            store.upsert_voucher("guid-1", "Journal", "1", "2024-01-10", None, 100, payload)
            row = store.vouchers()[0]
            self.assertEqual(row["source_state"], "unchanged")
            self.assertEqual(row["source_present"], 1)

    def test_optional_voucher_is_kept_as_inactive_audit_evidence(self):
        vouchers = [self.voucher("20240110", "guid-optional", optional="Yes")]
        with TemporaryStaging() as store:
            stage_voucher_export(store, vouchers, "20240101", "20240131", "20240131")
            self.assertEqual(store.vouchers(), [])
            row = store.vouchers(include_inactive=True)[0]
            self.assertEqual(row["source_state"], "optional")
            self.assertEqual(row["source_present"], 0)

    def test_unloaded_cancelled_source_is_evidence_not_a_repair_blocker(self):
        vouchers = [self.voucher("20240110", "guid-cancelled", cancelled="Yes")]
        with TemporaryStaging() as store:
            stage_voucher_export(store, vouchers, "20240101", "20240131", "20240131")
            report = build_report(store)
            self.assertEqual(report["summary"]["requires_decision"], 0)
            self.assertTrue(report["summary"]["safe_to_load_new"])


class BillReferenceTests(unittest.TestCase):
    def test_duplicate_party_bill_references_are_retained(self):
        with TemporaryStaging() as store:
            store.add_bill_ref(
                "Supplier A", "DUP-1", "Purchase Invoice", "PI-1")
            store.add_bill_ref(
                "Supplier A", "DUP-1", "Purchase Invoice", "PI-2")
            store.conn.commit()
            refs = store.get_bill_refs("Supplier A", "DUP-1")
            self.assertEqual(
                [r["invoice"] for r in refs], ["PI-1", "PI-2"])

    def test_duplicate_bill_allocation_is_fifo(self):
        class ERP:
            dry_run = True

            def get_list(self, doctype, fields=None, filters=None, limit=0):
                name = filters[0][2]
                amount = {"PI-1": 100.0, "PI-2": 80.0}[name]
                return [{"outstanding_amount": amount}]

        with TemporaryStaging() as store:
            store.add_bill_ref(
                "Supplier A", "DUP-1", "Purchase Invoice", "PI-1")
            store.add_bill_ref(
                "Supplier A", "DUP-1", "Purchase Invoice", "PI-2")
            loader = VoucherLoader(ERP(), store, defaults(), resolver=None)
            refs = loader._bill_references(
                "Supplier A",
                [{"name": "DUP-1", "type": "Agst Ref", "amount": 150.0}],
                150.0,
            )
            self.assertEqual(
                [(r["reference_name"], r["allocated_amount"]) for r in refs],
                [("PI-1", 100.0), ("PI-2", 50.0)],
            )


class IdempotencyTests(unittest.TestCase):
    def test_invoice_loader_skips_existing_guid(self):
        class ERP:
            dry_run = False
            inserted = False

            def find_by_field(
                    self, doctype, field, value, exclude_cancelled=False):
                self.exclude_cancelled = exclude_cancelled
                return "PI-EXISTING"

            def insert_and_submit(self, doctype, doc):
                self.inserted = True
                return {}

        with TemporaryStaging() as store:
            store.upsert_voucher(
                "guid-1", "Purchase", "1", "2026-01-01",
                "Supplier A", 100.0, {})
            erp = ERP()
            loader = InvoiceLoader(erp, store, defaults(), resolver=None)
            built = (
                {"company": "Test Company"},
                "Supplier A",
                "Purchase Invoice",
                "BILL-1",
                None,
            )
            with patch.object(loader, "_build", return_value=built):
                stats = loader.run()
            self.assertEqual(stats["skipped"], 1)
            self.assertFalse(erp.inserted)
            self.assertTrue(erp.exclude_cancelled)
            row = store.vouchers()[0]
            self.assertEqual(row["erp_name"], "PI-EXISTING")

    def test_voucher_loader_skips_existing_guid(self):
        class ERP:
            dry_run = False
            submitted = False

            def find_by_field(self, doctype, field, value, exclude_cancelled=False):
                return "PE-EXISTING" if doctype == "Payment Entry" else None

            def submit_doc(self, doctype, doc):
                self.submitted = True
                return {}

        with TemporaryStaging() as store:
            store.upsert_voucher(
                "guid-2", "Payment", "2", "2026-01-02",
                "Supplier A", 50.0, json.loads("{}"))
            erp = ERP()
            loader = VoucherLoader(erp, store, defaults(), resolver=None)
            stats = loader.run()
            self.assertEqual(stats["skipped"], 1)
            self.assertFalse(erp.submitted)
            row = store.vouchers()[0]
            self.assertEqual(row["erp_name"], "PE-EXISTING")

    def test_voucher_loader_reloads_when_only_a_cancelled_document_exists(self):
        class ERP:
            dry_run = False
            submitted = False

            def find_by_field(self, doctype, field, value, exclude_cancelled=False):
                if exclude_cancelled:
                    return None
                return "PE-CANCELLED" if doctype == "Payment Entry" else None

            def submit_doc(self, doctype, doc):
                self.submitted = True
                return {"data": {"name": "PE-NEW"}}

        with TemporaryStaging() as store:
            store.upsert_voucher(
                "guid-3", "Payment", "3", "2026-01-03",
                "Supplier A", 50.0, json.loads("{}"))
            erp = ERP()
            loader = VoucherLoader(erp, store, defaults(), resolver=None)
            stats = loader.run()
            self.assertEqual(stats["loaded"], 1)
            self.assertTrue(erp.submitted)
            self.assertEqual(store.vouchers()[0]["erp_name"], "PE-NEW")


class ScopeSafetyTests(unittest.TestCase):
    def test_wipe_treats_missing_migration_field_as_empty_scope(self):
        class ERP:
            dry_run = True

            @staticmethod
            def has_field(doctype, fieldname):
                return False

            @staticmethod
            def get_list(*args, **kwargs):
                raise AssertionError("invalid filtered query must not be sent")

        result = wipe(ERP(), progress=lambda *_: None)
        self.assertTrue(result)
        self.assertTrue(all(count == 0 for count in result.values()))

    def test_wipe_filters_every_transaction_by_migration_guid(self):
        class ERP:
            dry_run = True

            def __init__(self):
                self.filters = []

            def get_list(self, doctype, fields=None, filters=None, limit=0):
                self.filters.append((doctype, filters))
                return []

            def cancel(self, doctype, name):
                raise AssertionError("no rows should be returned")

            def delete(self, doctype, name):
                raise AssertionError("no rows should be returned")

        erp = ERP()
        wipe(erp)
        self.assertTrue(erp.filters)
        for _, filters in erp.filters:
            self.assertIn(["tally_guid", "is", "set"], filters)

    def test_wipe_cancels_submitted_docs_without_deleting_immutable_ledger(self):
        class ERP:
            dry_run = False

            def __init__(self):
                self.cancelled = []
                self.deleted = []

            @staticmethod
            def has_field(doctype, fieldname):
                return True

            @staticmethod
            def get_list(doctype, fields=None, filters=None, limit=0):
                if doctype == "Journal Entry":
                    return [{"name": "JE-1", "docstatus": 1}]
                return []

            def cancel(self, doctype, name):
                self.cancelled.append((doctype, name))

            def delete(self, doctype, name):
                self.deleted.append((doctype, name))

        erp = ERP()
        wipe(erp, progress=lambda *_: None)
        self.assertEqual(erp.cancelled, [("Journal Entry", "JE-1")])
        self.assertEqual(erp.deleted, [])

    def test_reconciliation_count_is_company_scoped(self):
        class ERP:
            def get_list(self, doctype, fields=None, filters=None, limit=0):
                self.filters = filters
                return [{"name": "JE-1"}]

        erp = ERP()
        self.assertEqual(
            _count_migrated(
                erp, "Journal Entry", "tally_guid", "Test Company"),
            1,
        )
        self.assertIn(["company", "=", "Test Company"], erp.filters)


class PeriodClosingTests(unittest.TestCase):
    def test_indian_fiscal_year_dates(self):
        self.assertEqual(
            PeriodClosingLoader.dates("2025-2026"),
            ("2025-04-01", "2026-03-31"))


class SourceMasterValidationTests(unittest.TestCase):
    class RejectOnceERP:
        def __init__(self, message):
            self.message = message
            self.docs = []

        def insert(self, doctype, doc):
            self.docs.append((doctype, dict(doc)))
            if len(self.docs) == 1:
                from t2e.erpnext_client import ERPNextError
                raise ERPNextError("validation failed", 417, self.message)
            return {"data": {"name": "created"}}

    def test_invalid_gstin_is_omitted_without_guessing_replacement(self):
        erp = self.RejectOnceERP("Invalid GSTIN! check digit validation failed")
        _insert_with_source_validation_fallback(erp, "Supplier", {
            "supplier_name": "Supplier A", "gstin": "BAD", "tax_id": "BAD",
        })
        self.assertEqual(len(erp.docs), 2)
        self.assertNotIn("gstin", erp.docs[1][1])
        self.assertNotIn("tax_id", erp.docs[1][1])

    def test_invalid_postal_code_is_omitted_but_address_is_preserved(self):
        erp = self.RejectOnceERP("Postal Code is not associated with Karnataka")
        _insert_with_source_validation_fallback(erp, "Address", {
            "address_title": "Supplier A", "state": "Karnataka",
            "address_line1": "Original address", "pincode": "500003",
        })
        self.assertEqual(len(erp.docs), 2)
        self.assertNotIn("pincode", erp.docs[1][1])
        self.assertEqual(erp.docs[1][1]["address_line1"], "Original address")


class CheckpointTests(unittest.TestCase):
    def test_no_checkpoint_scans_full_configured_history(self):
        self.assertEqual(effective_from_date("20220101", None, 90), "20220101")

    def test_checkpoint_rescans_a_lookback_window_behind_it(self):
        self.assertEqual(effective_from_date("20220101", "20260601", 90), "20260303")

    def test_checkpoint_lookback_never_precedes_configured_floor(self):
        self.assertEqual(effective_from_date("20220101", "20220115", 90), "20220101")

    def test_checkpoint_round_trips_through_staging(self):
        with TemporaryStaging() as store:
            self.assertIsNone(store.get_checkpoint())
            store.set_checkpoint("20260601")
            self.assertEqual(store.get_checkpoint(), "20260601")


class ApproveChangeTests(unittest.TestCase):
    class FakeERP:
        def __init__(self, dry_run=True, closed_fy_keys=(), invoice_outstanding=None):
            self.dry_run = dry_run
            self.closed_fy_keys = set(closed_fy_keys)
            self.invoice_outstanding = invoice_outstanding
            self.cancelled = []

        def cancel(self, doctype, name):
            if not self.dry_run:
                self.cancelled.append((doctype, name))

        def find_by_field(self, doctype, field, value, exclude_cancelled=False):
            if doctype == "Period Closing Voucher":
                return "PCV-1" if value in self.closed_fy_keys else None
            return None

        def get_list(self, doctype, fields=None, filters=None, limit=0):
            if self.invoice_outstanding is None:
                return []
            outstanding, total = self.invoice_outstanding
            return [{"outstanding_amount": outstanding, "grand_total": total}]

    def _stage_changed_je(self, store):
        store.upsert_voucher(
            "guid-changed", "Journal", "1", "2024-06-10", None, 100,
            {"GUID": "guid-changed", "AlterId": "1"})
        store.mark("voucher", "guid-changed", "loaded", "Journal Entry", "ACC-JV-1")
        store.clear_voucher_window("2024-06-01", "2024-06-30")
        store.upsert_voucher(
            "guid-changed", "Journal", "1", "2024-06-10", None, 120,
            {"GUID": "guid-changed", "AlterId": "2"})
        store.conn.commit()

    def test_unknown_guid_is_refused(self):
        with TemporaryStaging() as store:
            with self.assertRaisesRegex(ApprovalError, "no staged voucher"):
                preview(self.FakeERP(), store, "nope", "tally_guid")

    def test_unchanged_voucher_is_refused(self):
        with TemporaryStaging() as store:
            store.upsert_voucher("guid-1", "Journal", "1", "2024-01-10", None, 100, {})
            store.mark("voucher", "guid-1", "loaded", "Journal Entry", "ACC-JV-1")
            with self.assertRaisesRegex(ApprovalError, "not one of"):
                preview(self.FakeERP(), store, "guid-1", "tally_guid")

    def test_changed_voucher_dry_run_does_not_mutate_or_cancel(self):
        with TemporaryStaging() as store:
            self._stage_changed_je(store)
            erp = self.FakeERP(dry_run=True)
            result = approve(erp, store, "guid-changed", "tally_guid")
            self.assertTrue(result["action"].startswith("dry-run"))
            self.assertEqual(erp.cancelled, [])
            row = store.voucher_by_guid("guid-changed")
            self.assertEqual(row["source_state"], "changed")

    def test_changed_voucher_confirmed_cancels_and_reopens_for_reload(self):
        with TemporaryStaging() as store:
            self._stage_changed_je(store)
            erp = self.FakeERP(dry_run=False)
            result = approve(erp, store, "guid-changed", "tally_guid")
            self.assertEqual(erp.cancelled, [("Journal Entry", "ACC-JV-1")])
            self.assertIn("staged for reload", result["action"])
            row = store.voucher_by_guid("guid-changed")
            self.assertEqual(row["source_state"], "new")
            self.assertEqual(row["load_status"], "pending")

    def test_missing_voucher_confirmed_cancels_and_resolves_without_reload(self):
        with TemporaryStaging() as store:
            store.upsert_voucher("guid-gone", "Journal", "1", "2024-01-10", None, 100, {})
            store.mark("voucher", "guid-gone", "loaded", "Journal Entry", "ACC-JV-2")
            store.clear_voucher_window("2024-01-01", "2024-01-31")
            store.conn.commit()
            erp = self.FakeERP(dry_run=False)
            result = approve(erp, store, "guid-gone", "tally_guid")
            self.assertEqual(erp.cancelled, [("Journal Entry", "ACC-JV-2")])
            self.assertIn("resolved", result["action"])
            self.assertEqual(store.voucher_by_guid("guid-gone")["source_state"], "resolved")
            self.assertEqual(store.source_delta_rows(), [])

    def test_invoice_with_payment_allocated_is_blocked(self):
        with TemporaryStaging() as store:
            store.upsert_voucher("guid-paid", "Purchase", "1", "2024-06-10", "Supplier A", 100,
                                 {"GUID": "guid-paid", "AlterId": "1"})
            store.mark("voucher", "guid-paid", "loaded", "Purchase Invoice", "PINV-1")
            store.clear_voucher_window("2024-06-01", "2024-06-30")
            store.upsert_voucher("guid-paid", "Purchase", "1", "2024-06-10", "Supplier A", 120,
                                 {"GUID": "guid-paid", "AlterId": "2"})
            store.conn.commit()
            erp = self.FakeERP(dry_run=False, invoice_outstanding=(30.0, 120.0))
            with self.assertRaisesRegex(ApprovalError, "payment allocated"):
                approve(erp, store, "guid-paid", "tally_guid")
            self.assertEqual(erp.cancelled, [])

    def test_closed_fiscal_year_requires_explicit_acknowledgement(self):
        with TemporaryStaging() as store:
            self._stage_changed_je(store)
            erp = self.FakeERP(dry_run=False, closed_fy_keys={"period-closing-2024-2025"})
            with self.assertRaisesRegex(ApprovalError, "Period Closing Voucher"):
                approve(erp, store, "guid-changed", "tally_guid")
            result = approve(erp, store, "guid-changed", "tally_guid",
                             acknowledge_closed_period=True)
            self.assertEqual(erp.cancelled, [("Journal Entry", "ACC-JV-1")])
            self.assertIn("staged for reload", result["action"])
            store.set_checkpoint("20260701")
            self.assertEqual(store.get_checkpoint(), "20260701")

    def test_full_history_flag_ignores_existing_checkpoint(self):
        class FakeClient:
            from_date = "20220101"
            to_date = "20990101"

            def export_collection(self, *a, **kw):
                return ET.fromstring("<ENVELOPE></ENVELOPE>")

        with TemporaryStaging() as store:
            store.set_checkpoint("20260601")
            routine = extract_vouchers(FakeClient(), store, lookback_days=90)
            self.assertEqual(routine["_range_from"], "20260303")
            full = extract_vouchers(
                FakeClient(), store, lookback_days=90, full_history=True)
            self.assertEqual(full["_range_from"], "20220101")
            self.assertEqual(store.get_checkpoint(), "20260601")


if __name__ == "__main__":
    unittest.main()
