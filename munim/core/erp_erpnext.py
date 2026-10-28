"""ErpPort adapter backed by a real ERPNext site. Writes only on Approve; the owner's Approve also submits."""
from __future__ import annotations

import logging
import re
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from munim.core.clock import today_ist
from munim.core.erp import (
    CONVERSIONS,
    ConversionError,
    CreatedDocument,
    Customer,
    DraftLine,
    Item,
    MappedDocument,
    Match,
    OpenInvoice,
    _match,
    money,
)
from munim.core.gst import TAX_NONE, purchase_tax_mode, purchase_template, sales_template, tax_mode
from munim.erpnext.client import ERPNextClient
from munim.erpnext.entity_resolution import EntityCandidate
from munim.erpnext.exceptions import ERPNextError, ERPNextValidationError

SYSTEM_EXPENSES = {"Cost of Goods Sold", "Stock Adjustment", "Round Off", "Write Off", "Exchange Gain/Loss",
                   "Expenses Included In Asset Valuation", "Expenses Included In Valuation", "Depreciation", "Impairment",
                   "Gain/Loss on Asset Disposal", "Tax Expense", "GST Expense", "Customs Duty Expense"}
GST_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
CACHE_SECONDS = 120
# Common Hinglish words owners use for the seeded items. Real tenants would keep these
# as Item aliases in ERPNext; this is demo-only search help, not item truth.
DEMO_ALIASES: dict[str, tuple[str, ...]] = {
    "CEM-50": ("cement", "cement bag", "simant"),
    "TMT-12": ("saria", "sariya", "tmt", "steel"),
    "SAND-T": ("sand", "ret", "balu"),
    "CCTV-2MP": ("cctv", "camera", "cctv camera"),
    "DVR-8": ("dvr",),
}


log = logging.getLogger(__name__)

class ERPNextPort:
    label = "ERPNext (demo company)"

    def __init__(
        self,
        client: ERPNextClient,
        *,
        company: str,
        artifact_dir: Path = Path(".run/v2/documents"),
        label: str | None = None,
    ) -> None:
        if label is not None:
            self.label = label  # "" for a real business: no demo footer on previews
        self.client = client
        self.company = company
        self.artifact_dir = artifact_dir
        self._abbr: str | None = None
        self._company_gstin: tuple[float, str | None] | None = None
        self._tax_rows: dict[str, list[dict[str, Any]]] = {}
        self._modes: dict[str, tuple[float, str]] = {}
        self._suppliers: tuple[float, dict[str, Customer]] | None = None
        self._expense_heads: tuple[float, list[str]] | None = None
        self._customers: tuple[float, dict[str, Customer]] | None = None
        self._items: tuple[float, dict[str, Item]] | None = None

    @property
    def today(self) -> date:
        return today_ist()  # the VM clock is UTC; India's date flips at 18:30 UTC

    async def _company_abbr(self) -> str:
        """Account and template names carry the company abbreviation stored in ERPNext."""
        if self._abbr is None:
            doc = await self.client.get_document("Company", self.company)
            self._abbr = str(doc.data["abbr"])
        return self._abbr

    # ---- GST (India Compliance) ---------------------------------------
    async def _gstin(self) -> str | None:
        if self._company_gstin is None or time.time() - self._company_gstin[0] > CACHE_SECONDS:
            doc = await self.client.get_document("Company", self.company)
            self._company_gstin = (time.time(), doc.data.get("gstin") or None)
        return self._company_gstin[1]

    async def tax_mode(self, customer_id: str) -> str:
        cached = self._modes.get(customer_id)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        company_gstin = await self._gstin()
        gstin = state = None
        if company_gstin and customer_id:
            try:
                gstin = (await self.client.get_document("Customer", customer_id)).data.get("gstin")
            except ERPNextError:
                gstin = None
            if not gstin:
                rows = await self.client.list_documents(
                    "Address", filters=[["Dynamic Link", "link_doctype", "=", "Customer"], ["Dynamic Link", "link_name", "=", customer_id]],
                    fields=["state", "gstin", "is_primary_address", "address_type"], limit_page_length=10)
                rows.sort(key=lambda r: (not r.get("is_primary_address"), r.get("address_type") != "Billing"))
                if rows:
                    gstin, state = rows[0].get("gstin"), rows[0].get("state")
        mode = tax_mode(company_gstin, gstin, state)
        self._modes[customer_id] = (time.time(), mode)
        return mode

    async def _apply_taxes(self, payload: dict[str, Any], customer_id: str) -> None:
        """India Compliance's In-state/Out-state template with its rows (a server insert doesn't fetch them)."""
        mode = await self.tax_mode(customer_id)
        template = sales_template(mode, await self._company_abbr()) if mode != TAX_NONE else None
        if template is None:
            payload.pop("taxes_and_charges", None)
            payload["taxes"] = []
            return
        payload["taxes_and_charges"] = template
        payload["taxes"] = await self._template_rows("Sales Taxes and Charges Template", template)

    async def _template_rows(self, doctype: str, template: str) -> list[dict[str, Any]]:
        if template not in self._tax_rows:
            doc = await self.client.get_document(doctype, template)
            keep = ("charge_type", "account_head", "description", "rate", "cost_center", "included_in_print_rate", "category",
                    "add_deduct_tax")
            self._tax_rows[template] = [{k: row.get(k) for k in keep if row.get(k) is not None} for row in doc.data.get("taxes") or []]
        return [dict(row) for row in self._tax_rows[template]]

    # ---- corrections: returns, cancel, amend ----------------------------
    async def map_return(self, invoice: str) -> MappedDocument:
        source = await self._final("Sales Invoice", invoice)
        if source.data.get("is_return"):
            raise ConversionError(f"{invoice} is itself a return")
        mapped = await self._return_doc(invoice)
        lines = _merged_lines(mapped.get("items") or [], absolute=True)
        if not lines:
            raise ConversionError(f"everything on {invoice} has already been returned")
        return MappedDocument("Sales Invoice", invoice, source.data["customer"], source.data.get("customer_name") or source.data["customer"],
                              tuple(lines))

    async def _return_doc(self, invoice: str) -> dict[str, Any]:
        try:
            return (await self.client.call_method("erpnext.accounts.doctype.sales_invoice.sales_invoice.make_sales_return",
                                                  {"source_name": invoice})).data
        except ERPNextError as exc:
            raise ConversionError(str(exc)) from exc

    async def create_return(self, invoice: str, lines: list[DraftLine], reference: str, *, final: bool = False) -> CreatedDocument:
        doc = _align_return(await self._return_doc(invoice), lines)
        doc["update_outstanding_for_self"] = 0  # the credit reduces what is due on the original bill
        await self._apply_taxes(doc, doc.get("customer") or "")  # today's GST template (old bills used a retired one)
        try:
            made = await self.client.create_draft_document("Sales Invoice", _clean(doc))
        except ERPNextValidationError as exc:
            raise ConversionError(str(exc)) from exc
        return await self._after_insert("Sales Invoice", made, final)

    # ---- supplier side: what we owe, paying it, sending goods back ------
    async def supplier_outstanding(self, supplier_id: str) -> list[OpenInvoice]:
        rows = await self.client.list_documents(
            "Purchase Invoice", filters={"company": self.company, "supplier": supplier_id, "docstatus": 1, "outstanding_amount": [">", 0]},
            fields=["name", "posting_date", "due_date", "grand_total", "outstanding_amount"], limit_page_length=50,
            order_by="posting_date asc")
        return [OpenInvoice(r["name"], supplier_id, date.fromisoformat(str(r["posting_date"])),
                            date.fromisoformat(str(r.get("due_date") or r["posting_date"])), money(r["grand_total"]),
                            money(r["outstanding_amount"])) for r in rows]

    async def create_supplier_payment(self, supplier_id: str, amount: Decimal, mode: str, reference: str,
                                      allocations: list[tuple[str, Decimal]], *, final: bool = False) -> CreatedDocument:
        cash = mode == "Cash"
        abbr = await self._company_abbr()
        payload: dict[str, Any] = {
            "payment_type": "Pay", "party_type": "Supplier", "party": supplier_id, "company": self.company,
            "paid_amount": float(amount), "received_amount": float(amount),
            "mode_of_payment": "Cash" if cash else "Bank Draft",
            "paid_from": f"Cash - {abbr}" if cash else f"Bank - {abbr}", "paid_to": f"Creditors - {abbr}",
        }
        if not cash:
            payload.update({"reference_no": f"WhatsApp {mode}", "reference_date": self.today.isoformat()})
        if allocations:
            payload["references"] = [{"reference_doctype": "Purchase Invoice", "reference_name": inv, "allocated_amount": float(amt)}
                                     for inv, amt in allocations]
        doc = await self.client.create_draft_document("Payment Entry", payload)
        submitted, failed = await self._finalise("Payment Entry", doc.name) if final else (False, False)
        return CreatedDocument("Payment Entry", doc.name, None, money(amount), submitted=submitted, submit_failed=failed)

    async def _debit_note_doc(self, invoice: str) -> dict[str, Any]:
        try:
            return (await self.client.call_method("erpnext.accounts.doctype.purchase_invoice.purchase_invoice.make_debit_note",
                                                  {"source_name": invoice})).data
        except ERPNextError as exc:
            raise ConversionError(str(exc)) from exc

    async def map_purchase_return(self, invoice: str) -> MappedDocument:
        source = await self._final("Purchase Invoice", invoice)
        if source.data.get("is_return"):
            raise ConversionError(f"{invoice} is itself a return")
        lines = _merged_lines((await self._debit_note_doc(invoice)).get("items") or [], absolute=True)
        if not lines:
            raise ConversionError(f"everything on {invoice} has already been returned")
        return MappedDocument("Purchase Invoice", invoice, source.data["supplier"], source.data.get("supplier_name") or source.data["supplier"],
                              tuple(lines))

    async def create_purchase_return(self, invoice: str, lines: list[DraftLine], reference: str, *,
                                     final: bool = False) -> CreatedDocument:
        doc = _align_return(await self._debit_note_doc(invoice), lines)
        doc["update_outstanding_for_self"] = 0  # the debit reduces what we owe on the original bill
        template = purchase_template(await self.purchase_tax_mode(doc.get("supplier") or ""), await self._company_abbr())
        if template:  # today's input-GST template, like a new purchase bill
            doc.update({"taxes_and_charges": template,
                        "taxes": await self._template_rows("Purchase Taxes and Charges Template", template)})
        try:
            made = await self.client.create_draft_document("Purchase Invoice", _clean(doc))
        except ERPNextValidationError as exc:
            raise ConversionError(str(exc)) from exc
        submitted, failed = await self._finalise("Purchase Invoice", made.name) if final else (False, False)
        total = made.data.get("grand_total")
        return CreatedDocument("Purchase Invoice", made.name, None, money(abs(Decimal(str(total)))) if total is not None else None,
                               submitted=submitted, submit_failed=failed)

    async def _final(self, doctype: str, name: str) -> Any:
        try:
            doc = await self.client.get_document(doctype, name)
        except ERPNextError as exc:
            raise ConversionError(f"{doctype} {name} not found") from exc
        if doc.docstatus != 1:
            raise ConversionError(f"{name} is not final ({'draft' if doc.docstatus == 0 else 'cancelled'})")
        return doc

    async def cancel_document(self, doctype: str, name: str) -> None:
        await self._final(doctype, name)
        try:
            await self.client.cancel_document(doctype, name)
        except ERPNextError as exc:  # e.g. a payment or return is linked to it
            raise ConversionError(str(exc)) from exc

    async def document_lines(self, doctype: str, name: str) -> MappedDocument:
        doc = await self._final(doctype, name)
        party = doc.data.get("customer") or doc.data.get("party_name") or ""
        return MappedDocument(doctype, name, party, doc.data.get("customer_name") or party,
                              tuple(_merged_lines(doc.data.get("items") or [])))

    async def amend_document(self, doctype: str, name: str, lines: list[DraftLine], reference: str, *,
                             final: bool = False) -> CreatedDocument:
        original = (await self._final(doctype, name)).data
        await self.cancel_document(doctype, name)
        copy = _align({k: v for k, v in _clean(original).items() if k not in _NOT_COPIED}, lines)
        if len(name) + 2 <= 16:  # GST: document numbers are at most 16 characters (amended = NAME-1)
            copy["amended_from"] = name
        else:
            copy["remarks"] = f"Corrects {name} (cancelled)"
        if doctype in {"Sales Invoice", "Delivery Note"}:
            copy["set_posting_time"] = 1  # keep the original date
        await self._apply_taxes(copy, original.get("customer") or original.get("party_name") or "")
        copy["ignore_pricing_rule"] = 1
        made = await self.client.create_draft_document(doctype, copy)
        return await self._after_insert(doctype, made, final)

    async def create_auto_repeat(self, doctype: str, name: str, frequency: str, start_date: str, *,
                                 final: bool = False) -> CreatedDocument:
        await self._final(doctype, name)
        try:
            made = await self.client.create_draft_document("Auto Repeat", {
                "reference_doctype": doctype, "reference_document": name, "frequency": frequency, "start_date": start_date,
                "submit_on_creation": int(final), "notify_by_email": 0})
        except ERPNextError as exc:
            raise ConversionError(str(exc)) from exc
        return CreatedDocument("Auto Repeat", made.name, None)

    async def document_details(self, doctype: str, name: str) -> dict | None:
        try:
            doc = (await self.client.get_document(doctype, name)).data
        except ERPNextError:
            return None
        links = sorted({row.get(k) for row in doc.get("items") or [] for k in ("prevdoc_docname", "against_sales_order", "sales_order",
                                                                         "delivery_note", "against_sales_invoice") if row.get(k)})
        return {
            "doctype": doctype, "name": doc.get("name"), "status": doc.get("status") or {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(doc.get("docstatus")),
            "date": doc.get("posting_date") or doc.get("transaction_date"), "due_date": doc.get("due_date"),
            "party": doc.get("customer_name") or doc.get("supplier_name") or doc.get("party_name") or doc.get("party"),
            "bill_no": doc.get("bill_no"),
            "items": [{"item": r.get("item_name") or r.get("item_code"), "qty": r.get("qty"), "uom": r.get("uom"), "rate": r.get("rate"),
                       "amount": r.get("amount"), "hsn": r.get("gst_hsn_code")} for r in doc.get("items") or []],
            "net_total": doc.get("net_total"),
            "taxes": [{"tax": (r.get("account_head") or "").split(" - ")[0], "amount": r.get("tax_amount")} for r in doc.get("taxes") or []
                      if r.get("tax_amount")],
            "grand_total": doc.get("grand_total") or doc.get("paid_amount") or doc.get("total_debit"),
            "outstanding": doc.get("outstanding_amount"), "remarks": doc.get("remarks") or doc.get("user_remark"),
            "is_return": bool(doc.get("is_return")), "return_against": doc.get("return_against"),
            "amended_from": doc.get("amended_from"), "made_from": links,
        }

    async def _discount_rule(self, customer_id: str) -> dict[str, Any] | None:
        rows = await self.client.list_documents(
            "Pricing Rule", filters={"applicable_for": "Customer", "customer": customer_id, "selling": 1, "company": self.company,
                                    "title": ["like", "AI Munim:%"]},
            fields=["name", "discount_percentage", "disable"], limit_page_length=1)
        return rows[0] if rows else None

    async def customer_discount(self, customer_id: str) -> Decimal:
        rule = await self._discount_rule(customer_id) if customer_id else None
        return Decimal(str(rule["discount_percentage"] or 0)) if rule and not rule.get("disable") else Decimal(0)

    async def set_customer_discount(self, customer_id: str, percent: Decimal) -> str:
        """A permanent selling discount for one customer, as an ERPNext Pricing Rule (the web ERP uses it too)."""
        rule = await self._discount_rule(customer_id)
        if rule:
            await self.client.update_master_fields("Pricing Rule", rule["name"], {"discount_percentage": float(percent),
                                                                                  "disable": int(percent == 0)})
            return rule["name"]
        doc = await self.client.create_draft_document("Pricing Rule", {
            "title": f"AI Munim: {customer_id} {percent}%", "apply_on": "Item Group", "item_groups": [{"item_group": "All Item Groups"}],
            "applicable_for": "Customer", "customer": customer_id, "selling": 1, "company": self.company,
            "rate_or_discount": "Discount Percentage", "discount_percentage": float(percent), "priority": 1,
            "disable": int(percent == 0)})
        return doc.name

    async def document_pdf(self, doctype: str, name: str) -> bytes | None:
        try:
            return await self.client.download_pdf(doctype, name)
        except ERPNextError:
            return None

    # ---- purchases & expenses ------------------------------------------
    async def _supplier_map(self) -> dict[str, Customer]:
        if self._suppliers and time.time() - self._suppliers[0] < CACHE_SECONDS:
            return self._suppliers[1]
        rows = await self.client.list_all_documents("Supplier", fields=["name", "supplier_name", "gstin"], filters={"disabled": 0})
        found = {}
        for row in rows:
            name = row.get("supplier_name") or row["name"]
            found[row["name"]] = Customer(row["name"], name, (name.split()[0],), gstin=row.get("gstin") or None)
        self._suppliers = (time.time(), found)
        return found

    async def find_supplier(self, query: str) -> Match:
        suppliers = await self._supplier_map()
        candidates = [EntityCandidate(s.id, s.name, s.aliases) for s in suppliers.values()]
        return _match(query, candidates, {s.id: {"supplier_id": s.id, "name": s.name} for s in suppliers.values()})

    async def get_supplier(self, supplier_id: str) -> Customer | None:
        return (await self._supplier_map()).get(supplier_id)

    async def create_supplier(self, name: str, phone: str | None, gstin: str | None) -> Customer:
        payload: dict[str, Any] = {"supplier_name": name.strip(), "supplier_group": "All Supplier Groups", "supplier_type": "Company"}
        if phone:
            payload["mobile_no"] = phone
        if gstin:
            payload.update({"tax_id": gstin, "gstin": gstin, "gst_category": "Registered Regular"})
        doc = await self.client.create_draft_document("Supplier", payload)
        self._suppliers = None
        if gstin:
            await self._gst_address("Supplier", doc.name, name, gstin)
        return Customer(doc.name, name.strip(), gstin=gstin)

    async def _gst_address(self, link_doctype: str, link_name: str, title: str, gstin: str) -> None:
        from munim.core.gstin import STATES

        state = STATES.get(gstin[:2], "")
        try:
            await self.client.create_draft_document("Address", {
                "address_title": title.strip(), "address_type": "Billing", "address_line1": "As per GST registration",
                "city": state, "state": state, "country": "India", "gstin": gstin, "gst_category": "Registered Regular",
                "is_primary_address": 1, "links": [{"link_doctype": link_doctype, "link_name": link_name}]})
        except ERPNextError:
            log.warning("party address not created")

    async def purchase_tax_mode(self, supplier_id: str) -> str:
        supplier = await self.get_supplier(supplier_id)
        return purchase_tax_mode(await self._gstin(), supplier.gstin if supplier else None)

    async def expense_accounts(self) -> list[str]:
        """Leaf expense heads an owner would name (system accounts like Round Off are left out)."""
        if self._expense_heads and time.time() - self._expense_heads[0] < CACHE_SECONDS:
            return self._expense_heads[1]
        rows = await self.client.list_all_documents("Account", filters={"company": self.company, "root_type": "Expense", "is_group": 0},
                                                    fields=["account_name", "account_type"], max_records=500)
        heads = sorted(r["account_name"] for r in rows if not r.get("account_type") and r["account_name"] not in SYSTEM_EXPENSES)
        self._expense_heads = (time.time(), heads)
        return heads

    async def create_purchase_invoice(
        self, supplier_id: str, lines: list[DraftLine], bill_no: str | None, bill_date: str | None,
        attachment: Path | None, reference: str, *, final: bool = False,
    ) -> CreatedDocument:
        payload: dict[str, Any] = {"company": self.company, "supplier": supplier_id,
                                   "items": [{"item_code": x.item_code, "qty": float(x.qty), "rate": float(x.rate)} for x in lines]}
        if bill_no:
            payload["bill_no"] = bill_no[:140]
        if bill_date:
            payload["bill_date"] = bill_date
        template = purchase_template(await self.purchase_tax_mode(supplier_id), await self._company_abbr())
        if template:
            payload.update({"taxes_and_charges": template, "taxes": await self._template_rows("Purchase Taxes and Charges Template", template)})
        doc = await self.client.create_draft_document("Purchase Invoice", payload)
        if attachment is not None and attachment.exists():
            try:
                mime = "application/pdf" if attachment.suffix == ".pdf" else "image/jpeg"
                await self.client.attach_file("Purchase Invoice", doc.name, attachment.name, attachment.read_bytes(), mime)
            except ERPNextError:
                log.warning("bill attachment failed")
        submitted, failed = await self._finalise("Purchase Invoice", doc.name) if final else (False, False)
        total = doc.data.get("grand_total")
        return CreatedDocument("Purchase Invoice", doc.name, None, money(total) if total is not None else None,
                               submitted=submitted, submit_failed=failed)

    async def create_expense(self, account: str, amount: Decimal, mode: str, note: str, reference: str, *,
                             final: bool = False) -> CreatedDocument:
        abbr = await self._company_abbr()
        paid_from = f"Cash - {abbr}" if mode == "Cash" else f"Bank - {abbr}"
        payload = {"company": self.company, "voucher_type": "Journal Entry", "posting_date": self.today.isoformat(),
                   "user_remark": (note or account)[:140] + " (via WhatsApp)",
                   "accounts": [{"account": f"{account} - {abbr}", "debit_in_account_currency": float(amount)},
                                {"account": paid_from, "credit_in_account_currency": float(amount)}]}
        if mode != "Cash":
            payload.update({"cheque_no": f"WhatsApp {mode}", "cheque_date": self.today.isoformat()})
        doc = await self.client.create_draft_document("Journal Entry", payload)
        submitted, failed = await self._finalise("Journal Entry", doc.name) if final else (False, False)
        return CreatedDocument("Journal Entry", doc.name, None, money(amount), submitted=submitted, submit_failed=failed)

    # ---- reports: raw rows only; core/reports.py does every sum ------
    async def report_data(self, kind: str, start: date, end: date, item_code: str | None = None) -> list[dict]:
        between = ["between", [start.isoformat(), end.isoformat()]]
        if kind in {"invoices", "invoice_items"}:
            rows = await self.client.list_all_documents(
                "Sales Invoice", filters={"company": self.company, "docstatus": 1, "is_opening": "No", "posting_date": between},
                fields=["name", "customer", "customer_name", "posting_date", "net_total", "total_taxes_and_charges", "grand_total"],
                max_records=3000)
            invoices = [{"name": r["name"], "customer": r["customer"], "customer_name": r.get("customer_name"),
                         "date": str(r["posting_date"]), "net": r.get("net_total"), "tax": r.get("total_taxes_and_charges"),
                         "total": r.get("grand_total")} for r in rows]
            if kind == "invoices":
                return invoices
            names, items = [i["name"] for i in invoices], []
            for chunk in range(0, len(names), 200):
                items += await self.client.list_child_rows(
                    "Sales Invoice Item", "Sales Invoice", ["parent", "item_code", "item_name", "qty", "base_net_amount"],
                    filters=[["parent", "in", names[chunk:chunk + 200]]])
            return [{"item_code": r.get("item_code") or r.get("item_name"), "item_name": r.get("item_name"), "qty": r.get("qty"),
                     "net": r.get("base_net_amount")} for r in items]
        if kind == "open_invoices":
            rows = await self.client.list_all_documents(
                "Sales Invoice", filters={"company": self.company, "docstatus": 1, "outstanding_amount": [">", 0]},
                fields=["name", "customer", "customer_name", "posting_date", "due_date", "outstanding_amount"], max_records=3000)
            return [{"name": r["name"], "customer": r["customer"], "customer_name": r.get("customer_name"),
                     "due_date": str(r.get("due_date") or r["posting_date"]), "outstanding": r["outstanding_amount"]} for r in rows]
        if kind == "payments":
            rows = await self.client.list_all_documents(
                "Payment Entry", filters={"company": self.company, "docstatus": 1, "payment_type": "Receive", "posting_date": between},
                fields=["party", "party_name", "paid_amount", "mode_of_payment", "posting_date"], max_records=3000)
            return [{"customer": r.get("party"), "customer_name": r.get("party_name"), "amount": r.get("paid_amount"),
                     "mode": r.get("mode_of_payment"), "date": str(r.get("posting_date"))} for r in rows]
        if kind == "gl":
            accounts = await self.client.list_all_documents("Account", filters={"company": self.company}, fields=["name", "root_type"],
                                                            max_records=3000)
            root = {a["name"]: a.get("root_type") for a in accounts}
            rows = await self.client.list_all_documents(
                "GL Entry", filters={"company": self.company, "is_cancelled": 0, "is_opening": "No", "posting_date": between},
                fields=["account", "debit", "credit"], max_records=20000)
            return [{"root_type": root.get(r["account"]), "debit": r.get("debit"), "credit": r.get("credit")} for r in rows
                    if root.get(r["account"]) in {"Income", "Expense"}]
        if kind == "bill_stock":  # bought minus sold, from final bills (returns carry negative qty)
            moved: dict[str, Decimal] = {}
            for doctype, sign in (("Purchase Invoice", 1), ("Sales Invoice", -1)):
                names = [r["name"] for r in await self.client.list_all_documents(
                    doctype, filters={"company": self.company, "docstatus": 1, "is_opening": "No"}, fields=["name"], max_records=3000)]
                for chunk in range(0, len(names), 200):
                    filters: list[Any] = [["parent", "in", names[chunk:chunk + 200]]]
                    if item_code:
                        filters.append(["item_code", "=", item_code])
                    for r in await self.client.list_child_rows(f"{doctype} Item", doctype, ["item_code", "qty"], filters=filters):
                        if r.get("item_code"):
                            moved[r["item_code"]] = moved.get(r["item_code"], Decimal(0)) + sign * Decimal(str(r.get("qty") or 0))
            names = {code: item.name for code, item in (await self._item_map()).items()}
            return [{"item_code": code, "item_name": names.get(code), "qty": qty} for code, qty in moved.items() if qty]
        if kind == "stock":
            filters: dict[str, Any] = {"actual_qty": ["!=", 0]}
            if item_code:
                filters["item_code"] = item_code
            rows = await self.client.list_all_documents("Bin", filters=filters, fields=["item_code", "warehouse", "actual_qty"],
                                                        max_records=500)
            names = {code: item.name for code, item in (await self._item_map()).items()}
            return [{"item_code": r["item_code"], "item_name": names.get(r["item_code"]), "warehouse": r.get("warehouse"),
                     "qty": r["actual_qty"]} for r in rows]
        if kind == "pending":
            found: list[dict] = []
            for doctype, party, status in (("Quotation", "party_name", ["Open", "Replied", "Partially Ordered"]),
                                           ("Sales Order", "customer", ["To Deliver and Bill", "To Bill", "To Deliver"])):
                for r in await self.client.list_documents(doctype, filters={"company": self.company, "docstatus": 1, "status": ["in", status]},
                                                          fields=["name", party, "status", "grand_total"], limit_page_length=20):
                    found.append({"doctype": doctype, "name": r["name"], "customer": r.get(party), "status": r.get("status"),
                                  "total": r.get("grand_total")})
            for doctype, party in (("Quotation", "party_name"), ("Sales Order", "customer"), ("Delivery Note", "customer"),
                                   ("Sales Invoice", "customer")):
                for r in await self.client.list_documents(doctype, filters={"company": self.company, "docstatus": 0},
                                                          fields=["name", party, "grand_total"], limit_page_length=10):
                    found.append({"doctype": doctype, "name": r["name"], "customer": r.get(party), "status": "Draft",
                                  "total": r.get("grand_total")})
            return found
        raise ValueError(f"unknown report data {kind}")

    async def hsn_valid(self, hsn: str) -> bool:
        try:
            await self.client.get_document("GST HSN Code", hsn)
            return True
        except ERPNextError:
            return False

    async def set_item_hsn(self, item_code: str, hsn: str) -> None:
        await self.client.update_master_fields("Item", item_code, {"gst_hsn_code": hsn})
        self._items = None

    # ---- masters (cached) --------------------------------------------
    async def _customer_map(self) -> dict[str, Customer]:
        if self._customers and time.time() - self._customers[0] < CACHE_SECONDS:
            return self._customers[1]
        rows = await self.client.list_all_documents(
            "Customer", fields=["name", "customer_name"], filters={"disabled": 0}
        )
        customers = {}
        for row in rows:
            name = row.get("customer_name") or row["name"]
            first = name.split()[0]
            customers[row["name"]] = Customer(row["name"], name, (f"{first} ji", first))
        self._customers = (time.time(), customers)
        return customers

    async def _item_map(self) -> dict[str, Item]:
        if self._items and time.time() - self._items[0] < CACHE_SECONDS:
            return self._items[1]
        rows = await self.client.list_all_documents(
            "Item", fields=["name", "item_name", "stock_uom", "gst_hsn_code"], filters={"disabled": 0, "is_sales_item": 1}
        )
        prices = await self.client.list_all_documents(
            "Item Price", fields=["item_code", "price_list_rate"], filters={"price_list": "Standard Selling"}
        )
        rate_by_code = {p["item_code"]: money(p["price_list_rate"]) for p in prices}
        gst_by_code: dict[str, Decimal] = {}
        for tax in await self.client.list_child_rows("Item Tax", "Item", ["parent", "item_tax_template"]):
            found = GST_RE.search(str(tax.get("item_tax_template") or ""))
            if found and tax.get("parent") not in gst_by_code:
                gst_by_code[tax["parent"]] = Decimal(found.group(1))
        items = {}
        for row in rows:
            gst = gst_by_code.get(row["name"], Decimal(0))
            items[row["name"]] = Item(
                row["name"],
                row.get("item_name") or row["name"],
                row.get("stock_uom") or "Nos",
                rate_by_code.get(row["name"], money(0)),
                gst,
                DEMO_ALIASES.get(row["name"], ()),
                row.get("gst_hsn_code") or None,
            )
        self._items = (time.time(), items)
        return items

    async def find_customer(self, query: str) -> Match:
        customers = await self._customer_map()
        candidates = [EntityCandidate(c.id, c.name, c.aliases) for c in customers.values()]
        payload = {c.id: {"customer_id": c.id, "name": c.name} for c in customers.values()}
        return _match(query, candidates, payload)

    async def find_item(self, query: str) -> Match:
        items = await self._item_map()
        candidates = [EntityCandidate(i.code, i.name, i.aliases) for i in items.values()]
        payload = {
            i.code: {"item_code": i.code, "name": i.name, "uom": i.uom, "price_list_rate": str(i.rate)}
            for i in items.values()
        }
        return _match(query, candidates, payload)

    async def get_customer(self, customer_id: str) -> Customer | None:
        return (await self._customer_map()).get(customer_id)

    async def get_item(self, item_code: str) -> Item | None:
        return (await self._item_map()).get(item_code)

    # ---- reads -------------------------------------------------------
    async def customer_outstanding(self, customer_id: str) -> list[OpenInvoice]:
        data = await self.client.fetch_customer_outstanding(customer_id)
        invoices = []
        for row in data.get("invoices") or []:
            posting = date.fromisoformat(str(row["posting_date"]))
            due = date.fromisoformat(str(row.get("due_date") or row["posting_date"]))
            invoices.append(
                OpenInvoice(row["name"], customer_id, posting, due, money(row["grand_total"]), money(row["outstanding_amount"]))
            )
        return invoices

    async def recent_documents(self, customer_id: str, limit: int = 5) -> list[dict]:
        docs: list[dict[str, Any]] = []
        for doctype in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice"):
            for row in await self.client.get_customer_recent_documents(customer_id, doctype, limit):
                docs.append(
                    {
                        "doctype": doctype,
                        "name": row.get("name"),
                        "date": str(row.get("transaction_date") or row.get("posting_date") or ""),
                        "total": str(row.get("grand_total") or ""),
                        "status": {0: "draft", 1: "submitted"}.get(row.get("docstatus"), "cancelled"),
                        "erp_status": row.get("status") or "",  # e.g. To Deliver and Bill, To Bill, Completed, Unpaid
                    }
                )
        docs.sort(key=lambda d: d["date"], reverse=True)
        return docs[:limit]

    # ---- drafts (Approve button only) --------------------------------
    async def book_old_due(self, customer_id: str, amount: Decimal, remark: str, attachment: Path | None) -> CreatedDocument:
        """An old due from the paper bahi: an opening invoice (outstanding, settles with payments), page attached."""
        abbr = await self._company_abbr()
        day = self.today.isoformat()
        doc = await self.client.create_draft_document("Sales Invoice", {
            "company": self.company, "customer": customer_id, "is_opening": "Yes", "set_posting_time": 1, "posting_date": day,
            "due_date": day, "update_stock": 0, "remarks": remark[:140], "ignore_pricing_rule": 1,
            "items": [{"item_name": "Opening Invoice Item", "description": remark[:140], "qty": 1, "uom": "Nos",
                       "conversion_factor": 1, "rate": float(amount), "income_account": f"Temporary Opening - {abbr}"}]})
        if attachment is not None and attachment.exists():
            try:
                await self.client.attach_file("Sales Invoice", doc.name, attachment.name, attachment.read_bytes(),
                                              "application/pdf" if attachment.suffix == ".pdf" else "image/jpeg")
            except ERPNextError:
                log.warning("bahi page attachment failed")
        submitted, failed = await self._finalise("Sales Invoice", doc.name)
        return CreatedDocument("Sales Invoice", doc.name, None, money(amount), submitted=submitted, submit_failed=failed)

    async def create_customer(self, name: str, phone: str | None, gstin: str | None) -> Customer:
        payload: dict[str, Any] = {
            "customer_name": name.strip(),
            "customer_type": "Company",
            "customer_group": "Commercial",
            "territory": "India",
        }
        if phone:
            payload["mobile_no"] = phone
        if gstin:
            payload.update({"tax_id": gstin, "gstin": gstin, "gst_category": "Registered Regular"})
        doc = await self.client.create_draft_document("Customer", payload)
        self._customers = None
        if gstin:  # their GSTIN's state is the place of supply (IGST when it isn't ours)
            from munim.core.gstin import STATES

            state = STATES.get(gstin[:2], "")
            try:
                await self.client.create_draft_document("Address", {
                    "address_title": name.strip(), "address_type": "Billing", "address_line1": "As per GST registration",
                    "city": state, "state": state, "country": "India", "gstin": gstin, "gst_category": "Registered Regular",
                    "is_primary_address": 1, "links": [{"link_doctype": "Customer", "link_name": doc.name}]})
            except ERPNextError:
                log.warning("customer address not created")
        return Customer(doc.name, name.strip(), gstin=gstin)

    async def create_sales_draft(
        self, doctype: str, customer_id: str, lines: list[DraftLine], reference: str, *, final: bool = False
    ) -> CreatedDocument:
        items = [{"item_code": line.item_code, "qty": float(line.qty), "rate": float(line.rate)} for line in lines]
        payload: dict[str, Any] = {"company": self.company, "items": items, "ignore_pricing_rule": 1}  # rates as previewed
        await self._apply_taxes(payload, customer_id)
        if doctype == "Quotation":
            payload.update({"quotation_to": "Customer", "party_name": customer_id})
        else:
            payload["customer"] = customer_id
        if doctype == "Sales Order":
            _set_delivery_date(payload, self.today)
        doc = await self.client.create_draft_document(doctype, payload)
        return await self._after_insert(doctype, doc, final)

    async def _after_insert(self, doctype: str, doc: Any, final: bool, via: str | None = None) -> CreatedDocument:
        submitted, failed = await self._finalise(doctype, doc.name) if final else (False, False)
        pdf_path = None
        try:  # after submit, so the customer gets the final copy
            content = await self.client.download_pdf(doctype, doc.name)
            pdf_path = self.artifact_dir / f"{doc.name}.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(content)
        except Exception:  # noqa: BLE001 - the document exists; a missing PDF must not hide that
            pdf_path = None
        total = doc.data.get("grand_total")
        return CreatedDocument(doctype, doc.name, pdf_path, money(total) if total is not None else None,
                               submitted=submitted, submit_failed=failed, via=via)

    # ---- document chain (ERPNext's own mappers) ---------------------
    async def _mapped(self, source_doctype: str, source_name: str, target_doctype: str) -> dict[str, Any]:
        method = CONVERSIONS[(source_doctype, target_doctype)]
        try:
            result = await self.client.call_method(method, {"source_name": source_name})
        except ERPNextValidationError as exc:
            raise ConversionError(str(exc)) from exc
        except ERPNextError as exc:
            raise ConversionError(f"ERPNext could not prepare the {target_doctype}") from exc
        return result.data

    async def map_document(self, source_doctype: str, source_name: str, target_doctype: str) -> MappedDocument:
        route = CONVERSIONS.get((source_doctype, target_doctype))
        if route is None:
            raise ConversionError(f"{source_doctype} can't be turned into {target_doctype}")
        try:
            source = await self.client.get_document(source_doctype, source_name)
        except ERPNextError as exc:
            raise ConversionError(f"{source_doctype} {source_name} not found") from exc
        if source.docstatus == 0:
            raise ConversionError(f"{source_name} is still a draft; it must be final (submitted) first")
        if source.docstatus == 2:
            raise ConversionError(f"{source_name} is cancelled")
        step = "Sales Order" if route.startswith("via:") else target_doctype
        mapped = await self._mapped(source_doctype, source_name, step)
        lines: dict[str, DraftLine] = {}
        for row in mapped.get("items") or []:
            qty = Decimal(str(row.get("qty") or 0))
            if qty <= 0:
                continue
            code = row["item_code"]
            rate = Decimal(str(row.get("rate") or 0))
            if code in lines:  # the same item twice in the source: one preview line
                qty += lines[code].qty
            lines[code] = DraftLine(code, qty, money(rate))
        if not lines:
            raise ConversionError(f"everything in {source_name} is already in a {target_doctype}")
        customer_id = mapped.get("customer") or source.data.get("party_name") or source.data.get("customer") or ""
        customer_name = mapped.get("customer_name") or source.data.get("customer_name") or customer_id
        return MappedDocument(source_doctype, source_name, customer_id, customer_name, tuple(lines.values()))

    async def create_from_source(
        self, source_doctype: str, source_name: str, target_doctype: str, lines: list[DraftLine],
        reference: str, *, final: bool = False,
    ) -> CreatedDocument:
        via = None
        if CONVERSIONS[(source_doctype, target_doctype)].startswith("via:"):
            # ERPNext: a challan comes from an order, so the quotation becomes a (final) order first
            # the order takes the whole quotation; the challan below takes only what goes out now
            order = await self._mapped(source_doctype, source_name, "Sales Order")
            _set_delivery_date(order, self.today)
            made = await self.client.create_draft_document("Sales Order", _clean(order))
            submitted, _ = await self._finalise("Sales Order", made.name)
            if not submitted:
                return CreatedDocument("Sales Order", made.name, None, None, submit_failed=True)
            source_doctype, source_name, via = "Sales Order", made.name, made.name
        target = _align(await self._mapped(source_doctype, source_name, target_doctype), lines)
        await self._apply_taxes(target, target.get("customer") or "")  # same GST the preview showed
        target["ignore_pricing_rule"] = 1  # the preview's rates, not a rule applied a second time
        if target_doctype == "Sales Order":
            _set_delivery_date(target, self.today)
        doc = await self.client.create_draft_document(target_doctype, _clean(target))
        return await self._after_insert(target_doctype, doc, final, via)

    async def _finalise(self, doctype: str, name: str) -> tuple[bool, bool]:
        try:
            await self.submit_document(doctype, name)
            return True, False
        except Exception:  # noqa: BLE001 - ERPNext validation (stock, credit limit...): keep the draft
            log.warning("submit refused for %s", doctype)
            return False, True

    async def create_payment_draft(
        self, customer_id: str, amount: Decimal, mode: str, reference: str,
        allocations: list[tuple[str, Decimal]] | None = None, *, final: bool = False,
    ) -> CreatedDocument:
        cash = mode == "Cash"
        abbr = await self._company_abbr()
        payload: dict[str, Any] = {
            "payment_type": "Receive",
            "party_type": "Customer",
            "party": customer_id,
            "company": self.company,
            "paid_amount": float(amount),
            "received_amount": float(amount),
            "mode_of_payment": "Cash" if cash else "Bank Draft",
            "paid_from": f"Debtors - {abbr}",
            "paid_to": f"Cash - {abbr}" if cash else f"Bank - {abbr}",
        }
        if not cash:
            payload.update({"reference_no": f"WhatsApp {mode}", "reference_date": self.today.isoformat()})
        if allocations:
            payload["references"] = [
                {"reference_doctype": "Sales Invoice", "reference_name": inv, "allocated_amount": float(amt)}
                for inv, amt in allocations
            ]
        doc = await self.client.create_draft_document("Payment Entry", payload)
        submitted, failed = await self._finalise("Payment Entry", doc.name) if final else (False, False)
        return CreatedDocument("Payment Entry", doc.name, None, money(amount), submitted=submitted, submit_failed=failed)

    async def submit_document(self, doctype: str, name: str) -> CreatedDocument:
        # frappe.client.submit needs the full document (with its modified timestamp), not just a name.
        current = await self.client.get_document(doctype, name)
        if current.docstatus != 0:
            raise ValueError(f"{doctype} {name} is not a draft")
        doc = await self.client.call_method("frappe.client.submit", {"doc": current.data})
        total = (doc.data or {}).get("grand_total") or current.data.get("grand_total") or current.data.get("paid_amount")
        return CreatedDocument(doctype, doc.name or name, None, money(total) if total is not None else None)


_SYSTEM_KEYS = {"name", "__islocal", "__unsaved", "owner", "creation", "modified", "modified_by", "docstatus", "parent"}


def _clean(doc: dict[str, Any]) -> dict[str, Any]:
    """A mapped (unsaved) ERPNext doc, minus temporary names, ready to insert; link fields stay."""
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key in _SYSTEM_KEYS:
            continue
        if isinstance(value, list):
            value = [{k: v for k, v in row.items() if k not in _SYSTEM_KEYS} if isinstance(row, dict) else row for row in value]
        out[key] = value
    return out


def _align(doc: dict[str, Any], lines: list[DraftLine]) -> dict[str, Any]:
    """Make the mapped rows say exactly what the approved preview said (qty/rate per item), keeping links."""
    wanted = {line.item_code: line for line in lines}
    rows: list[dict[str, Any]] = []
    by_code: dict[str, list[dict[str, Any]]] = {}
    for row in doc.get("items") or []:
        by_code.setdefault(row.get("item_code"), []).append(row)
    for code, line in wanted.items():
        left = line.qty
        mapped_rows = by_code.get(code, [])
        for i, row in enumerate(mapped_rows):
            last = i == len(mapped_rows) - 1
            take = left if last else min(left, Decimal(str(row.get("qty") or 0)))
            if take <= 0:
                continue
            row = {**row, "qty": float(take), "rate": float(line.rate)}
            row.pop("amount", None)
            rows.append(row)
            left -= take
        if not mapped_rows:  # an item the user added on top of the source
            rows.append({"item_code": code, "qty": float(line.qty), "rate": float(line.rate)})
    return {**doc, "items": rows}


def _set_delivery_date(doc: dict[str, Any], today: Any) -> None:
    day = today.isoformat()
    if not doc.get("delivery_date") or str(doc["delivery_date"]) < day:
        doc["delivery_date"] = day
    for row in doc.get("items") or []:
        if not row.get("delivery_date") or str(row["delivery_date"]) < day:
            row["delivery_date"] = day


_NOT_COPIED = {"amended_from", "status", "outstanding_amount", "paid_amount", "payment_schedule", "irn", "ewaybill",
               "einvoice_status", "e_waybill_status", "signed_einvoice", "signed_qr_code", "per_billed", "per_delivered",
               "billing_status", "delivery_status", "advance_paid"}


def _merged_lines(rows: list[dict[str, Any]], *, absolute: bool = False) -> list[DraftLine]:
    lines: dict[str, DraftLine] = {}
    for row in rows:
        qty = Decimal(str(row.get("qty") or 0))
        qty = abs(qty) if absolute else qty
        if qty <= 0 or not row.get("item_code"):
            continue
        code = row["item_code"]
        if code in lines:
            qty += lines[code].qty
        lines[code] = DraftLine(code, qty, money(Decimal(str(row.get("rate") or 0))))
    return list(lines.values())


def _align_return(doc: dict[str, Any], lines: list[DraftLine]) -> dict[str, Any]:
    """Return rows carry negative qty; take what the preview says per item from the mapped rows (links kept)."""
    rows: list[dict[str, Any]] = []
    for line in lines:
        left = line.qty
        for row in (r for r in doc.get("items") or [] if r.get("item_code") == line.item_code):
            take = min(left, abs(Decimal(str(row.get("qty") or 0))))
            if take <= 0:
                continue
            rows.append({**{k: v for k, v in row.items() if k != "amount"}, "qty": float(-take), "rate": float(line.rate)})
            left -= take
        if left > 0:
            raise ConversionError(f"more {line.item_code} than is left to return on that bill")
    return {**doc, "items": rows}
