"""ERP port for Agent Core v2 plus an in-memory demo business.

All money arithmetic lives here and in `drafts.py` (Iron Rule 1). The LLM never
supplies a computed amount; it supplies item, qty and an optional stated rate.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Protocol

from munim.core.clock import today_ist
from munim.erpnext.entity_resolution import (
    EntityCandidate,
    ResolutionStatus,
    resolve_entity,
)
from munim.pdf import write_demo_pdf

PAISA = Decimal("0.01")


def money(value: Decimal | float | str) -> Decimal:
    # ERPNext sites use "Banker's Rounding": an exact half paisa goes to the even side (794.825 -> 794.82)
    return Decimal(str(value)).quantize(PAISA, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    phone: str | None = None
    gstin: str | None = None


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    uom: str
    rate: Decimal
    gst_rate: Decimal
    aliases: tuple[str, ...] = ()
    hsn: str | None = None  # HSN/SAC code; India Compliance needs it on GST invoices


@dataclass(frozen=True)
class Match:
    status: str  # resolved | ambiguous | not_found
    candidates: tuple[dict, ...] = ()


@dataclass(frozen=True)
class OpenInvoice:
    name: str
    customer_id: str
    posting_date: date
    due_date: date
    grand_total: Decimal
    outstanding: Decimal


@dataclass(frozen=True)
class DraftLine:
    item_code: str
    qty: Decimal
    rate: Decimal


@dataclass(frozen=True)
class CreatedDocument:
    doctype: str
    name: str
    pdf_path: Path | None
    total: Decimal | None = None  # grand total as stored by the ERP, when known
    submitted: bool = False
    submit_failed: bool = False  # asked to finalise, ERPNext refused: it stays a draft
    via: str | None = None  # an in-between document ERPNext required (Quotation -> Sales Order -> Challan)


# (source doctype, target doctype) -> ERPNext mapper, or "via:<doctype>" when ERPNext needs a step between.
CONVERSIONS: dict[tuple[str, str], str] = {
    ("Quotation", "Sales Order"): "erpnext.selling.doctype.quotation.quotation.make_sales_order",
    ("Quotation", "Sales Invoice"): "erpnext.selling.doctype.quotation.quotation.make_sales_invoice",
    ("Quotation", "Delivery Note"): "via:Sales Order",
    ("Sales Order", "Delivery Note"): "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
    ("Sales Order", "Sales Invoice"): "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
    ("Delivery Note", "Sales Invoice"): "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice",
    ("Sales Invoice", "Delivery Note"): "erpnext.accounts.doctype.sales_invoice.sales_invoice.make_delivery_note",
}


class ConversionError(Exception):
    """A document can't become the next one; the message is safe to show (ERPNext's reason)."""


@dataclass(frozen=True)
class MappedDocument:
    source_doctype: str
    source_name: str
    customer_id: str
    customer_name: str
    lines: tuple[DraftLine, ...]  # only what is still pending


class ErpPort(Protocol):
    label: str

    async def find_customer(self, query: str) -> Match: ...

    async def find_item(self, query: str) -> Match: ...

    async def get_customer(self, customer_id: str) -> Customer | None: ...

    async def get_item(self, item_code: str) -> Item | None: ...

    async def customer_outstanding(self, customer_id: str) -> list[OpenInvoice]: ...

    async def recent_documents(self, customer_id: str, limit: int = 5) -> list[dict]: ...

    async def create_customer(self, name: str, phone: str | None, gstin: str | None) -> Customer: ...

    async def book_old_due(self, customer_id: str, amount: Decimal, remark: str, attachment: Path | None) -> CreatedDocument: ...

    async def create_sales_draft(
        self, doctype: str, customer_id: str, lines: list[DraftLine], reference: str, *, final: bool = False
    ) -> CreatedDocument: ...

    async def create_payment_draft(
        self, customer_id: str, amount: Decimal, mode: str, reference: str,
        allocations: list[tuple[str, Decimal]] | None = None, *, final: bool = False,
    ) -> CreatedDocument: ...

    async def submit_document(self, doctype: str, name: str) -> CreatedDocument: ...

    async def map_document(self, source_doctype: str, source_name: str, target_doctype: str) -> MappedDocument: ...

    async def tax_mode(self, customer_id: str) -> str: ...  # gst.TAX_NONE | TAX_INTRA | TAX_INTER

    async def report_data(self, kind: str, start: date, end: date, item_code: str | None = None) -> list[dict]: ...

    async def map_return(self, invoice: str) -> MappedDocument: ...  # what can still be returned (positive qty)

    async def create_return(self, invoice: str, lines: list[DraftLine], reference: str, *, final: bool = False) -> CreatedDocument: ...

    async def cancel_document(self, doctype: str, name: str) -> None: ...  # ConversionError carries ERPNext's reason

    async def document_lines(self, doctype: str, name: str) -> MappedDocument: ...

    async def amend_document(self, doctype: str, name: str, lines: list[DraftLine], reference: str, *,
                             final: bool = False) -> CreatedDocument: ...  # cancel + corrected copy

    async def create_auto_repeat(self, doctype: str, name: str, frequency: str, start_date: str, *,
                                 final: bool = False) -> CreatedDocument: ...  # ERPNext makes the copies on schedule

    async def document_pdf(self, doctype: str, name: str) -> bytes | None: ...  # None if it doesn't exist

    async def customer_discount(self, customer_id: str) -> Decimal: ...  # permanent % (0 if none)

    async def set_customer_discount(self, customer_id: str, percent: Decimal) -> str: ...  # 0 switches it off

    async def document_details(self, doctype: str, name: str) -> dict | None: ...  # what is on it, as stored

    async def find_supplier(self, query: str) -> Match: ...

    async def get_supplier(self, supplier_id: str) -> Customer | None: ...

    async def create_supplier(self, name: str, phone: str | None, gstin: str | None) -> Customer: ...

    async def purchase_tax_mode(self, supplier_id: str) -> str: ...

    async def supplier_outstanding(self, supplier_id: str) -> list[OpenInvoice]: ...  # their final bills we still owe on

    async def create_supplier_payment(self, supplier_id: str, amount: Decimal, mode: str, reference: str,
                                      allocations: list[tuple[str, Decimal]], *, final: bool = False) -> CreatedDocument: ...

    async def map_purchase_return(self, invoice: str) -> MappedDocument: ...  # what can still go back (positive qty)

    async def create_purchase_return(self, invoice: str, lines: list[DraftLine], reference: str, *,
                                     final: bool = False) -> CreatedDocument: ...

    async def expense_accounts(self) -> list[str]: ...

    async def create_purchase_invoice(
        self, supplier_id: str, lines: list[DraftLine], bill_no: str | None, bill_date: str | None,
        attachment: Path | None, reference: str, *, final: bool = False,
    ) -> CreatedDocument: ...

    async def create_expense(self, account: str, amount: Decimal, mode: str, note: str, reference: str, *,
                             final: bool = False) -> CreatedDocument: ...
    # kinds: invoices, invoice_items, open_invoices, payments, gl, stock, pending (raw rows; core/reports sums them)

    async def hsn_valid(self, hsn: str) -> bool: ...

    async def set_item_hsn(self, item_code: str, hsn: str) -> None: ...

    async def create_from_source(
        self, source_doctype: str, source_name: str, target_doctype: str, lines: list[DraftLine],
        reference: str, *, final: bool = False,
    ) -> CreatedDocument: ...


def _match(query: str, candidates: list[EntityCandidate], payload: dict[str, dict]) -> Match:
    result = resolve_entity(query, candidates, threshold=78.0)
    if result.status == ResolutionStatus.RESOLVED:
        return Match("resolved", (payload[result.matches[0].id],))
    if result.status == ResolutionStatus.AMBIGUOUS:
        return Match("ambiguous", tuple(payload[c.id] for c in result.matches[:5]))
    return Match("not_found")


DOC_PREFIX = {
    "Quotation": "SAL-QTN",
    "Sales Order": "SAL-ORD",
    "Sales Invoice": "ACC-SINV",
    "Delivery Note": "MAT-DN",
    "Payment Entry": "ACC-PAY",
}


@dataclass
class DemoERP:
    """Seeded in-memory business used on the laptop and in tests.

    Not ERPNext. Every created document is labelled DEMO.
    """

    artifact_dir: Path = Path(".run/v2/documents")
    today: date = field(default_factory=today_ist)
    label: str = "DEMO ERP (simulated data, not ERPNext)"
    customers: dict[str, Customer] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    invoices: list[OpenInvoice] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    company_gstin: str | None = "27AAICA1234B1ZA"  # sample (checksum-valid) Maharashtra GSTIN
    suppliers: dict[str, Customer] = field(default_factory=dict)
    discounts: dict[str, Decimal] = field(default_factory=dict)
    expense_heads: tuple[str, ...] = ("Office Rent", "Travel Expenses", "Salary", "Telephone Expenses", "Utility Expenses",
                                      "Freight and Forwarding Charges", "Office Maintenance Expenses", "Print and Stationery",
                                      "Miscellaneous Expenses", "Marketing Expenses")
    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        if not self.customers:
            self._seed()

    def _seed(self) -> None:
        for c in (
            Customer("CUST-0001", "Gupta Traders", ("Gupta ji", "Gupta", "Gupta Trader")),
            Customer("CUST-0002", "Sharma Enterprises", ("Sharma ji", "Sharma"), gstin="08AABCG5678K1Z4"),  # Rajasthan
            Customer("CUST-0003", "Verma Hardware", ("Verma ji", "Verma")),
            Customer("CUST-0004", "Gupta Electricals", ("Gupta Electric",)),
        ):
            self.customers[c.id] = c
        for i in (
            Item("CEM-50", "Cement Bag 50 KG", "Bag", money(450), Decimal(28), ("cement", "cement bag", "simant"), "2523"),
            Item("TMT-12", "TMT Steel Bar 12mm", "Kg", money(65), Decimal(18), ("saria", "sariya", "tmt", "steel"), "7214"),
            Item("SAND-T", "River Sand", "Tonne", money(1800), Decimal(5), ("sand", "ret", "balu"), "2505"),
            Item("CCTV-2MP", "CCTV Camera 2MP", "Nos", money(1850), Decimal(18), ("cctv", "camera", "cctv camera"), "8525"),
            Item("DVR-8", "DVR 8 Channel", "Nos", money(4200), Decimal(18), ("dvr",)),  # no HSN yet: the bot asks
        ):
            self.items[i.code] = i
        self.invoices.append(
            OpenInvoice(
                "ACC-SINV-DEMO-0101",
                "CUST-0001",
                self.today - timedelta(days=50),
                self.today - timedelta(days=20),
                money(15000),
                money(15000),
            )
        )
        self.invoices.append(
            OpenInvoice(
                "ACC-SINV-DEMO-0102",
                "CUST-0003",
                self.today - timedelta(days=10),
                self.today + timedelta(days=20),
                money(8260),
                money(3260),
            )
        )

    @staticmethod
    def customer_payload(c: Customer) -> dict:
        return {"customer_id": c.id, "name": c.name}

    @staticmethod
    def item_payload(i: Item) -> dict:
        return {"item_code": i.code, "name": i.name, "uom": i.uom, "price_list_rate": str(i.rate)}

    async def find_customer(self, query: str) -> Match:
        candidates = [EntityCandidate(c.id, c.name, c.aliases) for c in self.customers.values()]
        payload = {c.id: self.customer_payload(c) for c in self.customers.values()}
        return _match(query, candidates, payload)

    async def find_item(self, query: str) -> Match:
        candidates = [EntityCandidate(i.code, i.name, i.aliases) for i in self.items.values()]
        payload = {i.code: self.item_payload(i) for i in self.items.values()}
        return _match(query, candidates, payload)

    async def get_customer(self, customer_id: str) -> Customer | None:
        return self.customers.get(customer_id)

    async def get_item(self, item_code: str) -> Item | None:
        return self.items.get(item_code)

    async def customer_outstanding(self, customer_id: str) -> list[OpenInvoice]:
        return [inv for inv in self.invoices if inv.customer_id == customer_id and inv.outstanding > 0]

    async def recent_documents(self, customer_id: str, limit: int = 5) -> list[dict]:
        docs = [d for d in self.documents if d["customer_id"] == customer_id]
        return docs[-limit:][::-1]

    async def book_old_due(self, customer_id: str, amount: Decimal, remark: str, attachment: Path | None) -> CreatedDocument:
        name = f"SINV-OPEN-DEMO-{next(self._counter):04d}"
        self.documents.append({"doctype": "Sales Invoice", "name": name, "customer_id": customer_id, "total": str(money(amount)),
                               "docstatus": 1, "is_opening": "Yes", "remark": remark, "attached": attachment is not None,
                               "date": self.today.isoformat(), "lines": [], "net": "0", "tax": "0"})
        self.invoices.append(OpenInvoice(name, customer_id, self.today, self.today, money(amount), money(amount)))
        return CreatedDocument("Sales Invoice", name, None, money(amount), submitted=True)

    async def create_customer(self, name: str, phone: str | None, gstin: str | None) -> Customer:
        customer = Customer(f"CUST-{len(self.customers) + 1:04d}", name.strip(), (), phone, gstin)
        self.customers[customer.id] = customer
        return customer

    def _next_name(self, doctype: str) -> str:
        return f"{DOC_PREFIX[doctype]}-DEMO-{self.today.year}-{next(self._counter):04d}"

    async def create_sales_draft(
        self, doctype: str, customer_id: str, lines: list[DraftLine], reference: str, *, final: bool = False
    ) -> CreatedDocument:
        from munim.core.drafts import compute_lines

        customer = self.customers[customer_id]
        computed = compute_lines([(self.items[line.item_code], line.qty, line.rate) for line in lines],
                                 await self.tax_mode(customer_id))
        name = self._next_name(doctype)
        pdf_lines = [f"Customer: {customer.name}", f"Date: {self.today.isoformat()}", f"Ref: {reference}", ""]
        for row in computed.rows:
            pdf_lines.append(f"{row.item.name} | {row.qty} {row.item.uom} x Rs {row.rate} = Rs {row.amount}")
        pdf_lines += ["", f"Subtotal: Rs {computed.subtotal}", f"GST: Rs {computed.tax}", f"Total: Rs {computed.total}"]
        pdf = write_demo_pdf(self.artifact_dir / f"{name}.pdf", f"{doctype} {name}" + ("" if final else " (DRAFT)"), pdf_lines)
        self.documents.append(
            {"doctype": doctype, "name": name, "customer_id": customer_id, "total": str(computed.total), "docstatus": int(final),
             "lines": [(line.item_code, str(line.qty), str(line.rate)) for line in lines], "origins": [],
             "date": self.today.isoformat(), "net": str(computed.subtotal), "tax": str(computed.tax)}
        )
        return CreatedDocument(doctype, name, pdf, money(computed.total), submitted=final)

    async def create_payment_draft(
        self, customer_id: str, amount: Decimal, mode: str, reference: str,
        allocations: list[tuple[str, Decimal]] | None = None, *, final: bool = False,
    ) -> CreatedDocument:
        name = self._next_name("Payment Entry")
        self.documents.append(
            {"doctype": "Payment Entry", "name": name, "customer_id": customer_id, "total": str(money(amount)), "docstatus": int(final),
             "date": self.today.isoformat(), "mode": mode}
        )
        return CreatedDocument("Payment Entry", name, None, money(amount), submitted=final)

    async def map_return(self, invoice: str) -> MappedDocument:
        source = self._doc("Sales Invoice", invoice)
        if source is None or source.get("is_return"):
            raise ConversionError(f"Sales Invoice {invoice} not found")
        if source["docstatus"] != 1:
            raise ConversionError(f"{invoice} is not final; only a final bill can take a return")
        back: dict[str, Decimal] = {}
        for doc in self.documents:
            if doc.get("return_against") == invoice and doc["docstatus"] == 1:
                for code, qty, _ in doc["lines"]:
                    back[code] = back.get(code, Decimal(0)) + Decimal(qty)
        lines = [DraftLine(c, Decimal(q) - back.get(c, Decimal(0)), Decimal(r)) for c, q, r in source["lines"]
                 if Decimal(q) - back.get(c, Decimal(0)) > 0]
        if not lines:
            raise ConversionError(f"everything on {invoice} has already been returned")
        customer = self.customers[source["customer_id"]]
        return MappedDocument("Sales Invoice", invoice, customer.id, customer.name, tuple(lines))

    async def create_return(self, invoice: str, lines: list[DraftLine], reference: str, *, final: bool = False) -> CreatedDocument:
        mapped = await self.map_return(invoice)
        left = {x.item_code: x.qty for x in mapped.lines}
        if any(x.qty > left.get(x.item_code, Decimal(0)) for x in lines):
            raise ConversionError("more than was sold on that bill")
        made = await self.create_sales_draft("Sales Invoice", mapped.customer_id, lines, reference, final=final)
        doc = self._doc("Sales Invoice", made.name)
        doc.update({"is_return": 1, "return_against": invoice, "total": str(-Decimal(doc["total"])),
                    "net": str(-Decimal(doc["net"])), "tax": str(-Decimal(doc["tax"]))})
        return CreatedDocument("Sales Invoice", made.name, made.pdf_path, -(made.total or Decimal(0)), submitted=final)

    async def cancel_document(self, doctype: str, name: str) -> None:
        doc = self._doc(doctype, name)
        if doc is None or doc["docstatus"] != 1:
            raise ConversionError(f"{doctype} {name} is not a final document")
        if any(d.get("return_against") == name and d["docstatus"] == 1 for d in self.documents):
            raise ConversionError(f"Cannot cancel because {name} is linked with a return")
        doc["docstatus"] = 2

    async def document_lines(self, doctype: str, name: str) -> MappedDocument:
        doc = self._doc(doctype, name)
        if doc is None:
            raise ConversionError(f"{doctype} {name} not found")
        if doc["docstatus"] != 1:
            raise ConversionError(f"{name} is not final (draft or cancelled)")
        customer = self.customers[doc["customer_id"]]
        return MappedDocument(doctype, name, customer.id, customer.name,
                              tuple(DraftLine(c, Decimal(q), Decimal(r)) for c, q, r in doc["lines"]))

    async def amend_document(self, doctype: str, name: str, lines: list[DraftLine], reference: str, *,
                             final: bool = False) -> CreatedDocument:
        original = self._doc(doctype, name)
        await self.cancel_document(doctype, name)
        made = await self.create_sales_draft(doctype, original["customer_id"], lines, reference, final=final)
        self._doc(doctype, made.name)["amended_from"] = name
        return made

    async def create_auto_repeat(self, doctype: str, name: str, frequency: str, start_date: str, *,
                                 final: bool = False) -> CreatedDocument:
        await self.document_lines(doctype, name)  # must be final
        made = f"AUT-AR-DEMO-{next(self._counter):04d}"
        self.documents.append({"doctype": "Auto Repeat", "name": made, "customer_id": "", "reference": name, "frequency": frequency,
                               "start_date": start_date, "submit_on_creation": int(final), "docstatus": 0, "total": "0"})
        return CreatedDocument("Auto Repeat", made, None)

    async def customer_discount(self, customer_id: str) -> Decimal:
        return self.discounts.get(customer_id, Decimal(0))

    async def set_customer_discount(self, customer_id: str, percent: Decimal) -> str:
        self.discounts[customer_id] = percent
        return f"PRLE-DEMO-{customer_id}"

    async def document_details(self, doctype: str, name: str) -> dict | None:
        doc = self._doc(doctype, name)
        if doc is None:
            return None
        customer = self.customers.get(doc.get("customer_id", ""))
        return {"doctype": doctype, "name": name, "status": {0: "Draft", 1: "Submitted", 2: "Cancelled"}[doc["docstatus"]],
                "date": doc.get("date"), "party": customer.name if customer else None,
                "items": [{"item": self.items[c].name if c in self.items else c, "qty": q, "rate": r} for c, q, r in doc.get("lines", [])],
                "grand_total": doc.get("total"), "return_against": doc.get("return_against"), "amended_from": doc.get("amended_from")}

    async def document_pdf(self, doctype: str, name: str) -> bytes | None:
        doc = self._doc(doctype, name)
        if doc is None:
            return None
        path = write_demo_pdf(self.artifact_dir / f"{name}.pdf", f"{doctype} {name}", [f"Total: Rs {doc.get('total')}"])
        return path.read_bytes()

    def _seed_suppliers(self) -> None:
        for s in (Customer("SUP-0001", "Shree Cement Agency", ("Shree", "Shree Cement"), gstin="27AAFCS4321M1Z6"),  # Maharashtra
                  Customer("SUP-0002", "Jaipur Steel Co", ("Jaipur Steel",), gstin="08AABCG5678K1Z4"),  # Rajasthan
                  Customer("SUP-0003", "Raju Transport", ("Raju",))):  # not GST-registered
            self.suppliers[s.id] = s

    async def find_supplier(self, query: str) -> Match:
        if not self.suppliers:
            self._seed_suppliers()
        candidates = [EntityCandidate(s.id, s.name, s.aliases) for s in self.suppliers.values()]
        return _match(query, candidates, {s.id: {"supplier_id": s.id, "name": s.name} for s in self.suppliers.values()})

    async def get_supplier(self, supplier_id: str) -> Customer | None:
        if not self.suppliers:
            self._seed_suppliers()
        return self.suppliers.get(supplier_id)

    async def create_supplier(self, name: str, phone: str | None, gstin: str | None) -> Customer:
        supplier = Customer(f"SUP-{len(self.suppliers) + 1:04d}", name.strip(), (), phone, gstin)
        self.suppliers[supplier.id] = supplier
        return supplier

    async def purchase_tax_mode(self, supplier_id: str) -> str:
        from munim.core.gst import purchase_tax_mode

        supplier = await self.get_supplier(supplier_id)
        return purchase_tax_mode(self.company_gstin, supplier.gstin if supplier else None)

    async def expense_accounts(self) -> list[str]:
        return list(self.expense_heads)

    async def create_purchase_invoice(
        self, supplier_id: str, lines: list[DraftLine], bill_no: str | None, bill_date: str | None,
        attachment: Path | None, reference: str, *, final: bool = False,
    ) -> CreatedDocument:
        from munim.core.drafts import compute_lines

        computed = compute_lines([(self.items[line.item_code], line.qty, line.rate) for line in lines],
                                 await self.purchase_tax_mode(supplier_id))
        name = f"PINV-DEMO-{next(self._counter):04d}"
        self.documents.append({"doctype": "Purchase Invoice", "name": name, "customer_id": supplier_id, "total": str(computed.total),
                               "docstatus": int(final), "bill_no": bill_no, "attached": attachment is not None,
                               "date": self.today.isoformat(), "lines": [(x.item_code, str(x.qty), str(x.rate)) for x in lines]})
        return CreatedDocument("Purchase Invoice", name, None, computed.total, submitted=final)

    def _purchase(self, invoice: str) -> dict:
        doc = next((d for d in self.documents if d["doctype"] == "Purchase Invoice" and d["name"] == invoice), None)
        if doc is None:
            raise ConversionError(f"Purchase Invoice {invoice} not found")
        if doc["docstatus"] != 1:
            raise ConversionError(f"{invoice} is not final (draft or cancelled)")
        return doc

    async def supplier_outstanding(self, supplier_id: str) -> list[OpenInvoice]:
        found = []
        for doc in self.documents:
            if doc["doctype"] != "Purchase Invoice" or doc["customer_id"] != supplier_id or doc["docstatus"] != 1:
                continue
            left = money(Decimal(doc["total"]) - Decimal(doc.get("settled", "0")))
            if left > 0:
                day = date.fromisoformat(doc["date"])
                found.append(OpenInvoice(doc["name"], supplier_id, day, day, money(Decimal(doc["total"])), left))
        return found

    async def create_supplier_payment(self, supplier_id: str, amount: Decimal, mode: str, reference: str,
                                      allocations: list[tuple[str, Decimal]], *, final: bool = False) -> CreatedDocument:
        name = f"PAY-OUT-DEMO-{next(self._counter):04d}"
        self.documents.append({"doctype": "Payment Entry", "name": name, "customer_id": supplier_id, "payment_type": "Pay",
                               "total": str(money(amount)), "mode": mode, "docstatus": int(final), "date": self.today.isoformat()})
        if final:
            for invoice, part in allocations:
                doc = self._purchase(invoice)
                doc["settled"] = str(money(Decimal(doc.get("settled", "0")) + part))
        return CreatedDocument("Payment Entry", name, None, money(amount), submitted=final)

    async def map_purchase_return(self, invoice: str) -> MappedDocument:
        doc = self._purchase(invoice)
        back = doc.setdefault("returned", {})
        lines = tuple(DraftLine(c, Decimal(q) - Decimal(back.get(c, "0")), Decimal(r)) for c, q, r in doc["lines"]
                      if Decimal(q) - Decimal(back.get(c, "0")) > 0)
        if not lines:
            raise ConversionError(f"everything on {invoice} has already been returned")
        supplier = await self.get_supplier(doc["customer_id"])
        return MappedDocument("Purchase Invoice", invoice, doc["customer_id"], supplier.name if supplier else doc["customer_id"], lines)

    async def create_purchase_return(self, invoice: str, lines: list[DraftLine], reference: str, *,
                                     final: bool = False) -> CreatedDocument:
        from munim.core.drafts import compute_lines

        open_lines = {x.item_code: x.qty for x in (await self.map_purchase_return(invoice)).lines}
        for line in lines:
            if line.qty > open_lines.get(line.item_code, Decimal(0)):
                raise ConversionError(f"more {line.item_code} than is left to return on that bill")
        doc = self._purchase(invoice)
        computed = compute_lines([(self.items[x.item_code], x.qty, x.rate) for x in lines], await self.purchase_tax_mode(doc["customer_id"]))
        name = f"PINV-RET-DEMO-{next(self._counter):04d}"
        self.documents.append({"doctype": "Purchase Invoice", "name": name, "customer_id": doc["customer_id"], "is_return": 1,
                               "return_against": invoice, "total": str(-computed.total), "docstatus": int(final),
                               "date": self.today.isoformat(), "lines": [(x.item_code, str(-x.qty), str(x.rate)) for x in lines]})
        if final:
            back = doc.setdefault("returned", {})
            for x in lines:
                back[x.item_code] = str(Decimal(back.get(x.item_code, "0")) + x.qty)
            doc["settled"] = str(money(Decimal(doc.get("settled", "0")) + computed.total))
        return CreatedDocument("Purchase Invoice", name, None, computed.total, submitted=final)

    async def create_expense(self, account: str, amount: Decimal, mode: str, note: str, reference: str, *,
                             final: bool = False) -> CreatedDocument:
        name = f"JV-DEMO-{next(self._counter):04d}"
        self.documents.append({"doctype": "Journal Entry", "name": name, "customer_id": "", "account": account, "mode": mode,
                               "total": str(money(amount)), "docstatus": int(final), "date": self.today.isoformat(), "note": note})
        return CreatedDocument("Journal Entry", name, None, money(amount), submitted=final)

    async def report_data(self, kind: str, start: date, end: date, item_code: str | None = None) -> list[dict]:
        def within(doc: dict) -> bool:
            return doc["docstatus"] == 1 and start.isoformat() <= doc.get("date", "") <= end.isoformat()
        def cname(cid: str) -> str:
            return self.customers[cid].name if cid in self.customers else cid
        invoices = [d for d in self.documents if d["doctype"] == "Sales Invoice" and within(d)]
        if kind == "invoices":
            return [{"name": d["name"], "customer": d["customer_id"], "customer_name": cname(d["customer_id"]),
                     "date": d["date"], "net": d["net"], "tax": d["tax"], "total": d["total"]} for d in invoices]
        if kind == "invoice_items":
            return [{"item_code": c, "item_name": self.items[c].name, "qty": q, "net": str(money(Decimal(q) * Decimal(r)))}
                    for d in invoices for c, q, r in d["lines"]]
        if kind == "open_invoices":
            return [{"name": i.name, "customer": i.customer_id, "customer_name": cname(i.customer_id), "due_date": i.due_date.isoformat(),
                     "outstanding": str(i.outstanding)} for i in self.invoices if i.outstanding > 0]
        if kind == "payments":
            return [{"customer": d["customer_id"], "customer_name": cname(d["customer_id"]), "amount": d["total"], "mode": d.get("mode"),
                     "date": d["date"]} for d in self.documents if d["doctype"] == "Payment Entry" and within(d)]
        if kind == "gl":
            income = [{"root_type": "Income", "debit": "0", "credit": d["net"]} for d in invoices]
            spent = [{"root_type": "Expense", "debit": d["total"], "credit": "0"} for d in self.documents
                     if d["doctype"] == "Journal Entry" and within(d)]
            return income + spent
        if kind == "pending":
            return [{"doctype": d["doctype"], "name": d["name"], "customer": cname(d["customer_id"]), "total": d["total"],
                     "status": "Draft" if d["docstatus"] == 0 else "Open"} for d in self.documents
                    if d["docstatus"] == 0 or d["doctype"] in {"Quotation", "Sales Order"}]
        if kind == "bill_stock":  # bought minus sold from final bills (returns carry negative qty)
            moved: dict[str, Decimal] = {}
            for doc in self.documents:
                sign = {"Purchase Invoice": 1, "Sales Invoice": -1}.get(doc["doctype"])
                if sign is None or doc["docstatus"] != 1 or doc.get("is_opening") == "Yes":
                    continue
                for code, qty, _ in doc.get("lines", []):
                    if item_code is None or code == item_code:
                        moved[code] = moved.get(code, Decimal(0)) + sign * Decimal(qty)
            return [{"item_code": c, "item_name": self.items[c].name, "qty": q} for c, q in moved.items() if q]
        return []  # demo items are not stock-tracked per warehouse

    async def tax_mode(self, customer_id: str) -> str:
        from munim.core.gst import tax_mode

        customer = self.customers.get(customer_id)
        return tax_mode(self.company_gstin, customer.gstin if customer else None)

    async def hsn_valid(self, hsn: str) -> bool:
        from munim.core.gst import clean_hsn

        return clean_hsn(hsn) is not None

    async def set_item_hsn(self, item_code: str, hsn: str) -> None:
        from dataclasses import replace

        self.items[item_code] = replace(self.items[item_code], hsn=hsn)

    def _doc(self, doctype: str, name: str) -> dict | None:
        return next((d for d in self.documents if d["doctype"] == doctype and d["name"] == name), None)

    def _pending(self, source: dict, target_doctype: str) -> list[DraftLine]:
        done: dict[str, Decimal] = {}
        for doc in self.documents:
            if doc["doctype"] == target_doctype and (source["doctype"], source["name"]) in doc.get("origins", []):
                for code, qty, _ in doc["lines"]:
                    done[code] = done.get(code, Decimal(0)) + Decimal(qty)
        pending = []
        for code, qty, rate in source.get("lines", []):
            left = Decimal(qty) - done.get(code, Decimal(0))
            if left > 0:
                pending.append(DraftLine(code, left, Decimal(rate)))
        return pending

    async def map_document(self, source_doctype: str, source_name: str, target_doctype: str) -> MappedDocument:
        if (source_doctype, target_doctype) not in CONVERSIONS:
            raise ConversionError(f"{source_doctype} can't be turned into {target_doctype}")
        source = self._doc(source_doctype, source_name)
        if source is None:
            raise ConversionError(f"{source_doctype} {source_name} not found")
        if source["docstatus"] != 1:
            raise ConversionError(f"{source_name} is still a draft; it must be final first")
        lines = self._pending(source, target_doctype)
        if not lines:
            raise ConversionError(f"everything in {source_name} is already in a {target_doctype}")
        customer = self.customers[source["customer_id"]]
        return MappedDocument(source_doctype, source_name, customer.id, customer.name, tuple(lines))

    async def create_from_source(
        self, source_doctype: str, source_name: str, target_doctype: str, lines: list[DraftLine],
        reference: str, *, final: bool = False,
    ) -> CreatedDocument:
        mapped = await self.map_document(source_doctype, source_name, target_doctype)
        source = self._doc(source_doctype, source_name)
        origins = [(source_doctype, source_name), *source.get("origins", [])]
        via = None
        if CONVERSIONS[(source_doctype, target_doctype)].startswith("via:"):
            order = await self.create_sales_draft("Sales Order", mapped.customer_id, list(mapped.lines), reference, final=True)
            self._doc("Sales Order", order.name)["origins"] = origins
            origins, via = [("Sales Order", order.name), *origins], order.name
        created = await self.create_sales_draft(target_doctype, mapped.customer_id, lines, reference, final=final)
        self._doc(target_doctype, created.name)["origins"] = origins
        return CreatedDocument(created.doctype, created.name, created.pdf_path, created.total, submitted=final, via=via)

    async def submit_document(self, doctype: str, name: str) -> CreatedDocument:
        for doc in self.documents:
            if doc["name"] == name and doc["doctype"] == doctype:
                doc["docstatus"] = 1
                return CreatedDocument(doctype, name, None, Decimal(doc["total"]))
        raise KeyError(name)
