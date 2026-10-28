"""Closed tool set for the v2 agent. No tool executes an ERP write.

Writes happen only in `engine.py` when the owner taps the Approve button.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from munim.core.drafts import (
    DOCTYPE_KIND,
    KIND_DOCTYPE,
    KIND_LABEL,
    compute_lines,
    fmt_rs,
    render_cancel_preview,
    render_customer_preview,
    render_expense_preview,
    render_payment_preview,
    render_purchase_preview,
    render_sales_preview,
)
from munim.core.erp import CONVERSIONS, ConversionError, ErpPort, money
from munim.core.gst import TAX_NONE, clean_hsn
from munim.core.i18n import t
from munim.core.reports import PERIODS, REPORTS, ReportError
from munim.core.reports import build as build_report
from munim.core.state import ConversationState, Draft, DraftLineState, PendingQuestion

SALES_KINDS = ("quotation", "sales_order", "invoice", "delivery_note")
SOURCE_DOCTYPES = ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice")
BAHI_TYPES = ("sale", "udhaar", "received")
REPEAT_FREQUENCIES = ("Daily", "Weekly", "Monthly", "Quarterly", "Half-yearly", "Yearly")
CANCELLABLE = (*SOURCE_DOCTYPES, "Payment Entry", "Purchase Invoice", "Journal Entry")
RATE_SOURCES = ("price_list", "user_stated", "document")
PAYMENT_MODES = ("Cash", "UPI", "Bank Transfer", "Cheque")
MAX_QTY = Decimal(100000)
USUAL_BILLS = 8  # how far back "the usual" looks
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "item_code": {"type": "string", "description": "item_code from find_item"},
        "qty": {"type": "number"},
        "rate": {"type": "number", "description": "Only when rate_source is user_stated or document. Omit it to keep "
                 "the rate of a line that came from a source document."},
        "rate_source": {"type": "string", "enum": list(RATE_SOURCES)},
    },
    "required": ["item_code", "qty", "rate_source"],
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "find_customer",
        "description": "Search customers by name or nickname (e.g. 'Gupta ji'). Returns resolved, ambiguous or not_found.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "find_item",
        "description": "Search items by name or common word (e.g. 'cement', 'saria', 'cctv'). Returns price_list_rate.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "customer_outstanding",
        "description": "Open (unpaid) invoices and total outstanding for a customer, with overdue days.",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "recent_documents",
        "description": "Recently created documents for a customer.",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "business_report",
        "description": (
            "Read-only answers from the books, every figure computed by the system: sales (billed totals vs the "
            "previous period), top_items, top_customers, receivables (who owes how much, by age), collections "
            "(money received, by mode), profit (income - expenses), stock (qty on hand, optional item_code), "
            "pending (open quotations, orders to deliver/bill, drafts). Pick the period from India dates: "
            "aaj=today, kal=yesterday, is hafte=this_week, pichhle mahine=last_month, is saal=this_fy (April-March); "
            "custom needs from_date/to_date (YYYY-MM-DD). Quote figures exactly; never add them up yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {"type": "string", "enum": list(REPORTS)},
                "period": {"type": "string", "enum": list(PERIODS)},
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "item_code": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 15},
            },
            "required": ["report"],
        },
    },
    {
        "name": "propose_sales_document",
        "description": (
            "Create the single open preview for a NEW quotation, sales_order, invoice or delivery_note typed by "
            "the user. Replaces any "
            "unapproved preview. The system computes all amounts and sends the preview card with "
            "Approve/Edit/Cancel buttons itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(SALES_KINDS)},
                "customer_id": {"type": "string"},
                "lines": {"type": "array", "items": _LINE_SCHEMA, "minItems": 1},
            },
            "required": ["kind", "customer_id", "lines"],
        },
    },
    {
        "name": "propose_from_document",
        "description": (
            "Make the next document from an existing one, the way ERPNext does it: links kept, only the "
            "quantity still pending (already delivered/billed parts are left out), rates from the source. "
            "Quotation -> sales_order | invoice | delivery_note; Sales Order -> delivery_note | invoice; "
            "Delivery Note -> invoice; Sales Invoice -> delivery_note. Use it whenever the user refers to an "
            "existing document ('iska bill', 'is order ka challan', 'quotation confirm ho gaya'); never retype "
            "its lines. For a partial challan/bill, call edit_draft afterwards to lower quantities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_doctype": {"type": "string", "enum": list(SOURCE_DOCTYPES)},
                "source_name": {"type": "string"},
                "target_kind": {"type": "string", "enum": ["sales_order", "invoice", "delivery_note"]},
            },
            "required": ["source_doctype", "source_name", "target_kind"],
        },
    },
    {
        "name": "edit_draft",
        "description": (
            "Change the open preview: switch kind, change customer, set qty/rate of an item (adds the item "
            "if missing), or remove items. The system recomputes and re-sends the card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(SALES_KINDS)},
                "customer_id": {"type": "string"},
                "set_lines": {"type": "array", "items": {**_LINE_SCHEMA, "required": ["item_code", "rate_source"]}},
                "remove_item_codes": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "propose_payment",
        "description": "Preview a payment received from a customer. Amount must be what the user stated.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount": {"type": "number"},
                "mode": {"type": "string", "enum": list(PAYMENT_MODES)},
            },
            "required": ["customer_id", "amount", "mode"],
        },
    },
    {
        "name": "propose_new_customer",
        "description": "Preview adding a new customer (after find_customer returned not_found and the user wants it).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "phone": {"type": "string"},
                "gstin": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "set_item_hsn",
        "description": (
            "Record the HSN/SAC code the user gave for an item on the open preview (4, 6 or 8 digits). It is "
            "saved on the item when they tap Approve. You may suggest a likely code, but only the user's "
            "confirmed code goes in."
        ),
        "input_schema": {"type": "object", "properties": {"item_code": {"type": "string"}, "hsn": {"type": "string"}},
                         "required": ["item_code", "hsn"]},
    },
    {
        "name": "queue_next_steps",
        "description": (
            "Only one preview can be open. When the user asks for several documents at once ('dono ka alag "
            "bill', 'teeno challan'), propose the FIRST now and queue the rest here, one short instruction each "
            "with the exact document names (e.g. 'Sales Invoice from Delivery Note MAT-DN-2026-00004'). After "
            "each Approve the system continues with the next one by itself. Replaces any earlier queue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"steps": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
            "required": ["steps"],
        },
    },
    {
        "name": "propose_return",
        "description": (
            "Goods came back on a final Sales Invoice ('Gupta ji ne 2 bag wapas kiye'): preview a Credit Note against "
            "that invoice with everything still returnable; lower quantities with edit_draft for a part return. "
            "The credit reduces what is due on that invoice."
        ),
        "input_schema": {"type": "object", "properties": {"invoice": {"type": "string"}}, "required": ["invoice"]},
    },
    {
        "name": "propose_cancel",
        "description": "Preview cancelling a final document (owner only). Nothing is deleted; ERPNext reverses its entries.",
        "input_schema": {"type": "object", "properties": {"doctype": {"type": "string", "enum": list(CANCELLABLE)},
                                                       "name": {"type": "string"}}, "required": ["doctype", "name"]},
    },
    {
        "name": "propose_correction",
        "description": (
            "Fix a FINAL quotation/order/challan/bill ('ACC-SINV-00004 me rate 430 karna tha'): opens a preview with its "
            "lines; then call edit_draft with the change. On Approve the old one is cancelled and the corrected one "
            "made (links and date kept). Owner only."
        ),
        "input_schema": {"type": "object", "properties": {"doctype": {"type": "string", "enum": list(SOURCE_DOCTYPES)},
                                                       "name": {"type": "string"}}, "required": ["doctype", "name"]},
    },
    {
        "name": "propose_repeat",
        "description": (
            "Make a FINAL Sales Invoice repeat on a schedule ('Sharma ji ka maintenance bill har mahine'): ERPNext "
            "creates the copy every period and the owner gets a WhatsApp. start_date (YYYY-MM-DD, default today) is "
            "when the schedule starts; the first copy comes one period later."
        ),
        "input_schema": {"type": "object", "properties": {
            "invoice": {"type": "string"}, "frequency": {"type": "string", "enum": list(REPEAT_FREQUENCIES)},
            "start_date": {"type": "string"}}, "required": ["invoice"]},
    },
    {
        "name": "propose_bahi_import",
        "description": (
            "Owner only: a photo of a handwritten bahi/khata page. Copy EVERY entry exactly as written, one per line: "
            "party, type (sale = goods given, udhaar = credit/baaki given, received = jama/payment taken), amount. "
            "Do not add or net anything; the system sums each party and shows the dues to book."
        ),
        "input_schema": {"type": "object", "properties": {"entries": {"type": "array", "maxItems": 80, "items": {
            "type": "object", "properties": {"party": {"type": "string"}, "type": {"type": "string", "enum": list(BAHI_TYPES)},
                                               "amount": {"type": "number"}, "date": {"type": "string"}, "note": {"type": "string"}},
            "required": ["party", "type", "amount"]}}}, "required": ["entries"]},
    },
    {
        "name": "set_brand",
        "description": (
            "Owner only: everything about how bills look. use_uploaded_logo=true takes the just-sent picture as the logo "
            "(colours come from it, background left out); use_uploaded_signature=true takes it as the signature (paper "
            "removed, printed above 'Authorised signatory'). Also bank name, account name/number, IFSC, UPI, tagline, "
            "terms & conditions, footer line, remove_logo / remove_signature. Fields not given stay as they were. "
            "Shows a preview; Approve applies it to bills, quotations, orders and challans."
        ),
        "input_schema": {"type": "object", "properties": {
            "use_uploaded_logo": {"type": "boolean"}, "bank_name": {"type": "string"}, "account_name": {"type": "string"},
            "account_no": {"type": "string"}, "ifsc": {"type": "string"}, "upi_id": {"type": "string"},
            "tagline": {"type": "string"}, "use_uploaded_signature": {"type": "boolean"}, "terms": {"type": "string"},
            "footer": {"type": "string"}, "remove_logo": {"type": "boolean"}, "remove_signature": {"type": "boolean"}}},
    },
    {
        "name": "note_request",
        "description": (
            "When the person asks for something none of your tools can do, call this with a one-line summary so the team "
            "builds it, then tell them it is noted and offer the closest thing you CAN do. Never just say it is impossible."
        ),
        "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    },
    {
        "name": "remember",
        "description": (
            "Owner only: keep a lasting fact about the business or a customer/supplier that should shape future "
            "work ('Gupta ji 30 din me paisa dete hain', 'Sharma ji ke challan pe rate mat dikhana', 'Sunday band'). "
            "One short sentence in the owner's words. Not for one-off orders, totals or anything ERPNext already stores "
            "(rates, discounts, dues). Use it when they say yaad rakhna/remember, or state a clear standing rule."
        ),
        "input_schema": {"type": "object", "properties": {
            "fact": {"type": "string"}, "about": {"type": "string", "description": "customer/supplier name, if any"}},
            "required": ["fact"]},
    },
    {
        "name": "forget",
        "description": "Owner only: drop a remembered fact by its id (from business_memory in <state>).",
        "input_schema": {"type": "object", "properties": {"memory_id": {"type": "integer"}}, "required": ["memory_id"]},
    },
    {
        "name": "customer_usual",
        "description": (
            "What a customer usually buys, worked out by code from their recent final bills: items, usual qty, how "
            "often they appear, last rate and date, and how many days apart they buy. For 'Gupta ji ka usual', "
            "'wahi jo hamesha lete hain', or to suggest an order. Then propose the document with those items and qty "
            "(rate_source price_list unless the owner asks for the old rate: then document with last_rate)."
        ),
        "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
    },
    {
        "name": "online_payments",
        "description": (
            "Owner only: online payments through the business's OWN Razorpay account (money goes to their bank; we "
            "never hold it). action=status | on | off | connect. connect (or on, when not connected yet) sends them a "
            "one-time sign-in link to enter their Razorpay keys on the web; keys are never typed in chat. When on, "
            "every final bill gets a pay link and a paid link is recorded against the bill automatically."
        ),
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["status", "on", "off", "connect"]}}, "required": ["action"]},
    },
    {
        "name": "set_customer_discount",
        "description": (
            "Owner only: a PERMANENT discount % for one customer on every future bill ('Gupta ji ko hamesha 5%'), "
            "also used by the web ERP. 0 switches it off. For a one-time discount use apply_discount."
        ),
        "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}, "percent": {"type": "number"}},
                         "required": ["customer_id", "percent"]},
    },
    {
        "name": "apply_discount",
        "description": "Give a percentage discount on the open preview's rates (all items, or the item_codes given).",
        "input_schema": {"type": "object", "properties": {"percent": {"type": "number"},
                                                       "item_codes": {"type": "array", "items": {"type": "string"}}},
                         "required": ["percent"]},
    },
    {
        "name": "find_supplier",
        "description": "Search suppliers (who the business buys from) by name.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "propose_new_supplier",
        "description": "Preview adding a supplier (after find_supplier returned not_found). GSTIN from their bill if shown.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "phone": {"type": "string"},
                                                       "gstin": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "propose_purchase_bill",
        "description": (
            "Preview recording a bill FROM a supplier (goods the business bought). Lines use the rates printed on the "
            "bill (rate_source document) or stated by the user; never the selling price list. Input GST is applied by "
            "the system (none if the supplier has no GSTIN). The uploaded photo/PDF is attached by default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_id": {"type": "string"},
                "lines": {"type": "array", "items": _LINE_SCHEMA, "minItems": 1},
                "bill_no": {"type": "string"},
                "bill_date": {"type": "string", "description": "YYYY-MM-DD"},
                "attach_uploaded_document": {"type": "boolean"},
            },
            "required": ["supplier_id", "lines"],
        },
    },
    {
        "name": "propose_expense",
        "description": (
            "Preview a business expense paid out (petrol, rent, salary, phone bill, freight...). account is an expense "
            "head of this business; an unknown one returns the list to choose from."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "amount": {"type": "number"},
                "mode": {"type": "string", "enum": list(PAYMENT_MODES)},
                "note": {"type": "string"},
            },
            "required": ["account", "amount", "mode"],
        },
    },
    {
        "name": "cancel_draft",
        "description": "Discard the open preview.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask_user",
        "description": (
            "Ask one clarifying question with 2-3 tappable options. Use instead of asking an either/or "
            "question in plain text. Option titles max 20 characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
            },
            "required": ["question", "options"],
        },
    },
    {
        "name": "save_alias",
        "description": (
            "Remember the owner's own word for a customer or item after they confirmed what it means "
            "(e.g. 'saria' = TMT Steel Bar 12mm), so next time find_* resolves it directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["customer", "item"]},
                "alias": {"type": "string"},
                "target_id": {"type": "string", "description": "customer_id or item_code"},
            },
            "required": ["kind", "alias", "target_id"],
        },
    },
    {
        "name": "save_document_facts",
        "description": (
            "Record what the uploaded document says, exactly as printed (no arithmetic). Call once when "
            "a document is attached to this turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "description": "e.g. sales_invoice, purchase_bill, quotation, other"},
                "document_number": {"type": "string"},
                "document_date": {"type": "string"},
                "party_name": {"type": "string"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "qty": {"type": "number"},
                            "rate": {"type": "number"},
                        },
                        "required": ["description"],
                    },
                },
                "printed_total": {"type": "number"},
                "readable": {"type": "boolean"},
            },
            "required": ["doc_type", "readable"],
        },
    },
]


class ToolInputError(ValueError):
    pass


@dataclass
class TurnEffects:
    """What the engine must send after the agent finishes this turn."""

    card: str | None = None  # preview text for Approve/Edit/Cancel buttons
    question: PendingQuestion | None = None
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)  # things asked for that no tool can do yet
    messages: list[str] = field(default_factory=list)  # fixed text sent as is (links the model must not retype)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ToolInputError(f"{name} must be a number")
    if not number.is_finite() or number <= 0:
        raise ToolInputError(f"{name} must be greater than 0")
    return number


class ToolExecutor:
    def __init__(self, erp: ErpPort, state: ConversationState, *, demo_label: str | None, aliases: Any = None) -> None:
        self.erp = erp
        self.state = state
        self.aliases = aliases  # (get, put) for learned names, or None
        self.demo_label = demo_label
        self.effects = TurnEffects()
        self.customer_pct = Decimal(0)

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            result = await handler(**(args or {}))
        except ToolInputError as exc:
            result = {"error": str(exc)}
        except TypeError as exc:
            result = {"error": f"bad arguments: {exc}"}
        self.effects.tool_log.append({"tool": name, "ok": "error" not in result})
        return result

    async def _t_find_customer(self, name: str) -> dict[str, Any]:
        learned = self.aliases and self.aliases[0]("customer", name)
        if learned and (customer := await self.erp.get_customer(learned)):
            return {"status": "resolved", "candidates": [{"customer_id": customer.id, "name": customer.name}], "learned": True}
        match = await self.erp.find_customer(name)
        return {"status": match.status, "candidates": list(match.candidates)}

    async def _t_find_item(self, name: str) -> dict[str, Any]:
        learned = self.aliases and self.aliases[0]("item", name)
        if learned and (item := await self.erp.get_item(learned)):
            return {"status": "resolved", "learned": True, "candidates": [
                {"item_code": item.code, "name": item.name, "uom": item.uom, "price_list_rate": str(item.rate)}]}
        match = await self.erp.find_item(name)
        return {"status": match.status, "candidates": list(match.candidates)}

    async def _t_customer_outstanding(self, customer_id: str) -> dict[str, Any]:
        customer = await self.erp.get_customer(customer_id)
        if customer is None:
            raise ToolInputError("unknown customer_id; call find_customer first")
        invoices = await self.erp.customer_outstanding(customer_id)
        today = getattr(self.erp, "today", None)
        rows = []
        for inv in invoices:
            overdue = (today - inv.due_date).days if today else None
            rows.append(
                {
                    "invoice": inv.name,
                    "date": inv.posting_date.isoformat(),
                    "due_date": inv.due_date.isoformat(),
                    "outstanding": str(inv.outstanding),
                    "overdue_days": overdue if overdue and overdue > 0 else 0,
                }
            )
        total = money(sum((inv.outstanding for inv in invoices), Decimal(0)))
        return {"customer": customer.name, "total_outstanding": str(total), "open_invoices": rows}

    async def _t_business_report(self, report: str, period: str = "this_month", from_date: str | None = None,
                                 to_date: str | None = None, item_code: str | None = None, limit: int = 5) -> dict[str, Any]:
        try:
            return await build_report(self.erp, report, period, from_date=from_date, to_date=to_date, item_code=item_code, limit=limit)
        except ReportError as exc:
            raise ToolInputError(str(exc)) from exc

    async def _t_recent_documents(self, customer_id: str) -> dict[str, Any]:
        return {"documents": await self.erp.recent_documents(customer_id)}

    async def _build_lines(self, lines: list[dict[str, Any]]) -> list[DraftLineState]:
        built: list[DraftLineState] = []
        seen: set[str] = set()
        for raw in lines:
            item = await self.erp.get_item(str(raw.get("item_code", "")))
            if item is None:
                raise ToolInputError(f"unknown item_code {raw.get('item_code')}; call find_item first")
            if item.code in seen:
                raise ToolInputError(f"{item.code} listed twice; merge the quantity")
            seen.add(item.code)
            qty = _decimal(raw.get("qty"), "qty")
            if qty > MAX_QTY:
                raise ToolInputError("qty too large")
            built.append(self._line(item, qty, raw))
        return built

    def _line(self, item: Any, qty: Decimal, raw: dict[str, Any]) -> DraftLineState:
        source = raw.get("rate_source", "price_list")
        if source not in RATE_SOURCES:
            raise ToolInputError("rate_source must be price_list, user_stated or document")
        if source == "price_list":  # the customer's permanent discount comes off the list price, in code
            rate = money(item.rate * (100 - self.customer_pct) / 100) if self.customer_pct else item.rate
        else:
            if raw.get("rate") is None:
                raise ToolInputError("rate required when rate_source is user_stated or document")
            rate = money(_decimal(raw["rate"], "rate"))
        return DraftLineState(item.code, item.name, item.uom, str(qty), str(rate), source)

    async def render(self) -> str:
        draft = self.state.draft
        assert draft is not None
        if draft.kind == "payment":
            return render_payment_preview(
                draft.customer_name or "", Decimal(draft.payment_amount or 0), draft.payment_mode or "", self.demo_label,
                self.state.lang, draft.allocations,
            )
        if draft.kind in {"new_customer", "new_supplier"}:
            info = draft.new_customer or {}
            return render_customer_preview(info.get("name", ""), info.get("phone"), info.get("gstin"), self.state.lang,
                                           kind=draft.kind)
        if draft.kind == "repeat":
            return t("repeat.card", self.state.lang, name=draft.source["name"], customer=draft.customer_name or "",
                     total=fmt_rs(Decimal(draft.payment_amount or 0)) + " + GST", freq=draft.note,
                     first=draft.bill_date, final=t("repeat.final" if self.state.can_submit else "repeat.draft", self.state.lang))
        if draft.kind == "bahi":
            lines = [f"*{t('bahi.title', self.state.lang)}*", ""]
            for r in draft.entries:
                tag = "" if r["customer_id"] else " 🆕"
                lines.append(f"• {r['customer']}{tag}: {t('bahi.row', self.state.lang, given=fmt_rs(Decimal(r['given'])), received=fmt_rs(Decimal(r['received'])), due=fmt_rs(Decimal(r['due'])))}")
            due = sum((max(Decimal(r["due"]), Decimal(0)) for r in draft.entries), Decimal(0))
            lines += ["", t("bahi.total", self.state.lang, total=fmt_rs(due),
                             n=sum(1 for r in draft.entries if Decimal(r["due"]) > 0))]
            if draft.note and draft.note != "0":
                lines.append(t("bahi.skipped", self.state.lang, n=draft.note))
            lines.append(t("bahi.note", self.state.lang))
            return "\n".join(lines)
        if draft.kind == "brand":
            from munim.core.brand import mask_account

            b, d = draft.brand or {}, (draft.brand or {}).get("details") or {}
            bank = " · ".join(x for x in (d.get("bank_name"), mask_account(d["account_no"]) if d.get("account_no") else None,
                                         d.get("ifsc"), ("UPI " + d["upi_id"]) if d.get("upi_id") else None) if x)
            extras = [t("brand.sign", self.state.lang, mark="✅" if b.get("signature_png_b64") else "—")]
            if d.get("terms"):
                extras.append(t("brand.terms", self.state.lang, terms=d["terms"][:80]))
            if d.get("footer"):
                extras.append(t("brand.footer", self.state.lang, footer=d["footer"]))
            bank_line = ("Bank: " + bank) if bank else t("brand.no_bank", self.state.lang)
            return t("brand.card", self.state.lang, logo="✅" if b.get("logo_png_b64") else "—", name=b.get("primary_name", ""),
                     primary=b.get("primary"), accent=b.get("accent"),
                     bg=t("brand.bg", self.state.lang, bg=b["background"]) if b.get("background") else "",
                     bank="\n".join([bank_line, *extras]))
        if draft.kind == "pricing":
            return t("pricing.card", self.state.lang, customer=draft.customer_name or "", pct=_pct(Decimal(draft.payment_amount or 0)),
                     before=_pct(Decimal(draft.note or 0)))
        if draft.kind == "cancel":
            return render_cancel_preview(draft.source["doctype"], draft.source["name"], draft.customer_name, self.state.lang)
        if draft.kind == "expense":
            return render_expense_preview(draft.expense_account or "", Decimal(draft.payment_amount or 0), draft.payment_mode or "",
                                          draft.note, self.demo_label, self.state.lang, final=self.state.can_submit)
        if draft.kind == "purchase":
            rows = [(await self.erp.get_item(x.item_code), Decimal(x.qty), Decimal(x.rate)) for x in draft.lines]
            computed = compute_lines(rows, await self.erp.purchase_tax_mode(draft.customer_id or ""))
            return render_purchase_preview(draft.customer_name or "", computed, draft.bill_no, draft.bill_date, bool(draft.attachment),
                                           self.demo_label, self.state.lang, final=self.state.can_submit,
                                           hsn_missing=await self._hsn_missing())
        rows = []
        for line in draft.lines:
            item = await self.erp.get_item(line.item_code)
            rows.append((item, Decimal(line.qty), Decimal(line.rate)))
        source = f"{draft.source['doctype']} {draft.source['name']}" if draft.source else None
        computed = compute_lines(rows, await self.erp.tax_mode(draft.customer_id or ""))
        replaces = draft.amend["name"] if draft.amend else None
        return render_sales_preview(draft.kind, draft.customer_name or "", computed, self.demo_label, self.state.lang,
                                    final=self.state.can_submit, source=source, hsn_missing=await self._hsn_missing(),
                                    replaces=replaces, discount=draft.discount, customer_discount=draft.customer_discount)

    async def _hsn_missing(self) -> list[str]:
        """Items on the open sales preview that still need an HSN code (only when GST applies)."""
        draft = self.state.draft
        if draft is None or draft.kind not in (*SALES_KINDS, "purchase"):
            return []
        mode = (await self.erp.purchase_tax_mode(draft.customer_id or "") if draft.kind == "purchase"
                else await self.erp.tax_mode(draft.customer_id or ""))
        if mode == TAX_NONE:
            return []
        missing = []
        for line in draft.lines:
            item = await self.erp.get_item(line.item_code)
            if item is not None and not item.hsn and line.item_code not in draft.hsn:
                missing.append(line.item_name)
        return missing

    async def _publish(self) -> dict[str, Any]:
        card = await self.render()
        self.effects.card = card
        draft = self.state.draft
        result = {"ok": True, "draft_id": draft.id, "version": draft.version, "preview_sent_to_user": card}
        missing = await self._hsn_missing()
        if missing:
            result["hsn_missing"] = [line.item_code for line in draft.lines if line.item_name in missing]
        return result

    def _replaced_note(self) -> dict[str, Any]:
        old = self.state.draft
        if old is None:
            return {}
        return {"replaced_previous_unapproved": KIND_LABEL.get(old.kind, old.kind)}

    async def _t_propose_sales_document(self, kind: str, customer_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        if kind not in SALES_KINDS:
            raise ToolInputError("kind must be quotation, sales_order, invoice or delivery_note")
        customer = await self.erp.get_customer(customer_id)
        if customer is None:
            raise ToolInputError("unknown customer_id; call find_customer first")
        self.customer_pct = await self.erp.customer_discount(customer.id)
        built = await self._build_lines(lines)
        note = self._replaced_note()
        self.state.draft = Draft(kind=kind, customer_id=customer.id, customer_name=customer.name, lines=built,
                                 customer_discount=_pct(self.customer_pct))
        return {**await self._publish(), **note}

    async def _t_propose_from_document(self, source_doctype: str, source_name: str, target_kind: str) -> dict[str, Any]:
        if source_doctype not in SOURCE_DOCTYPES:
            raise ToolInputError(f"source_doctype must be one of {', '.join(SOURCE_DOCTYPES)}")
        target = KIND_DOCTYPE.get(target_kind)
        if target is None or target == "Quotation":
            raise ToolInputError("target_kind must be sales_order, invoice or delivery_note")
        route = CONVERSIONS.get((source_doctype, target))
        if route is None:
            raise ToolInputError(f"{source_doctype} can't become {target}; allowed: " + ", ".join(
                f"{a} -> {b}" for a, b in CONVERSIONS))
        if route.startswith("via:") and not self.state.can_submit:
            raise ToolInputError("a challan from a quotation needs a final Sales Order first; staff can propose target_kind=sales_order")
        try:
            mapped = await self.erp.map_document(source_doctype, source_name.strip(), target)
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        built = []
        for line in mapped.lines:
            item = await self.erp.get_item(line.item_code)
            if item is None:
                raise ToolInputError(f"item {line.item_code} from {source_name} is not available any more")
            built.append(DraftLineState(item.code, item.name, item.uom, str(line.qty), str(line.rate), "source"))
        note = self._replaced_note()
        self.state.draft = Draft(kind=target_kind, customer_id=mapped.customer_id, customer_name=mapped.customer_name, lines=built,
                                 source={"doctype": source_doctype, "name": mapped.source_name})
        return {**await self._publish(), **note, "pending_only": True}

    async def _t_edit_draft(
        self,
        kind: str | None = None,
        customer_id: str | None = None,
        set_lines: list[dict[str, Any]] | None = None,
        remove_item_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        draft = self.state.draft
        if draft is None or draft.kind not in (*SALES_KINDS, "purchase", "credit_note"):
            raise ToolInputError("no open quotation/order/invoice/challan/purchase/return preview to edit")
        if draft.amend and kind and kind != draft.kind:
            raise ToolInputError("a correction keeps the document type")
        if draft.kind == "purchase" and kind and kind != "purchase":
            raise ToolInputError("a purchase bill can't become a sales document")
        if draft.source and ((kind and kind != draft.kind) or (customer_id and customer_id != draft.customer_id)):
            raise ToolInputError("this preview comes from " + draft.source["name"] + "; to change the document type call "
                                 "propose_from_document again, the customer can't change")
        if kind:
            if kind not in SALES_KINDS:
                raise ToolInputError("kind must be quotation, sales_order, invoice or delivery_note")
            draft.kind = kind
        if customer_id:
            customer = await self.erp.get_customer(customer_id)
            if customer is None:
                raise ToolInputError("unknown customer_id; call find_customer first")
            draft.customer_id, draft.customer_name = customer.id, customer.name
        if draft.kind in SALES_KINDS:
            self.customer_pct = await self.erp.customer_discount(draft.customer_id or "")
            draft.customer_discount = _pct(self.customer_pct)
        by_code = {line.item_code: line for line in draft.lines}
        for code in remove_item_codes or []:
            by_code.pop(code, None)
        for raw in set_lines or []:
            item = await self.erp.get_item(str(raw.get("item_code", "")))
            if item is None:
                raise ToolInputError(f"unknown item_code {raw.get('item_code')}; call find_item first")
            existing = by_code.get(item.code)
            if raw.get("qty") is not None:
                qty = _decimal(raw["qty"], "qty")
            elif existing is not None:
                qty = Decimal(existing.qty)
            else:
                raise ToolInputError(f"qty required to add {item.name}")
            if existing is not None and existing.rate_source == "source" and raw.get("rate") is None:
                by_code[item.code] = DraftLineState(item.code, item.name, item.uom, str(qty), existing.rate, "source")
                continue  # qty change on a converted line: the source document's rate stays
            if raw.get("rate") is None and raw.get("rate_source") != "price_list" and existing is not None:
                raw = {**raw, "rate": existing.rate, "rate_source": existing.rate_source}
            by_code[item.code] = self._line(item, qty, raw)
        if not by_code:
            raise ToolInputError("preview would be empty; use cancel_draft instead")
        draft.lines = list(by_code.values())
        draft.version += 1
        draft.created_at = time.time()  # an edit renews the preview's lifetime
        return await self._publish()

    async def _t_propose_payment(self, customer_id: str, amount: Any, mode: str) -> dict[str, Any]:
        customer = await self.erp.get_customer(customer_id)
        if customer is None:
            raise ToolInputError("unknown customer_id; call find_customer first")
        if mode not in PAYMENT_MODES:
            raise ToolInputError(f"mode must be one of {', '.join(PAYMENT_MODES)}")
        value = money(_decimal(amount, "amount"))
        open_invoices = sorted(await self.erp.customer_outstanding(customer.id), key=lambda i: (i.due_date, i.posting_date))
        allocations, left = [], value
        for inv in open_invoices:  # oldest dues are settled first; all arithmetic in code
            if left <= 0:
                break
            take = min(left, inv.outstanding)
            allocations.append({"invoice": inv.name, "amount": str(take)})
            left = money(left - take)
        note = self._replaced_note()
        self.state.draft = Draft(
            kind="payment", customer_id=customer.id, customer_name=customer.name, payment_amount=str(value), payment_mode=mode,
            allocations=allocations,
        )
        return {**await self._publish(), **note}

    async def _t_propose_new_customer(self, name: str, phone: str | None = None, gstin: str | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        if len(name) < 2:
            raise ToolInputError("name too short")
        existing = await self.erp.find_customer(name)
        if existing.status == "resolved":
            return {"error": "customer already exists", "existing": list(existing.candidates)}
        if gstin:
            gstin = gstin.strip().upper()
            if not GSTIN_RE.match(gstin):
                raise ToolInputError("GSTIN format invalid (15 characters)")
        note = self._replaced_note()
        self.state.draft = Draft(kind="new_customer", new_customer={"name": name, "phone": phone, "gstin": gstin})
        return {**await self._publish(), **note}

    async def _t_propose_return(self, invoice: str) -> dict[str, Any]:
        try:
            mapped = await self.erp.map_return(invoice.strip())
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        built = await self._lines_from(mapped, "source")
        note = self._replaced_note()
        self.state.draft = Draft(kind="credit_note", customer_id=mapped.customer_id, customer_name=mapped.customer_name, lines=built,
                                 source={"doctype": "Sales Invoice", "name": mapped.source_name})
        return {**await self._publish(), **note, "returnable_only": True}

    async def _t_propose_cancel(self, doctype: str, name: str) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can cancel documents")
        if doctype not in CANCELLABLE:
            raise ToolInputError(f"doctype must be one of {', '.join(CANCELLABLE)}")
        try:
            mapped = await self.erp.document_lines(doctype, name.strip()) if doctype in SOURCE_DOCTYPES else None
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        note = self._replaced_note()
        self.state.draft = Draft(kind="cancel", customer_name=mapped.customer_name if mapped else None,
                                 source={"doctype": doctype, "name": name.strip()})
        return {**await self._publish(), **note}

    async def _t_propose_correction(self, doctype: str, name: str) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can correct a final document")
        kind = DOCTYPE_KIND.get(doctype)
        if kind is None:
            raise ToolInputError("doctype must be Quotation, Sales Order, Delivery Note or Sales Invoice")
        try:
            mapped = await self.erp.document_lines(doctype, name.strip())
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        built = await self._lines_from(mapped, "source")
        note = self._replaced_note()
        self.state.draft = Draft(kind=kind, customer_id=mapped.customer_id, customer_name=mapped.customer_name, lines=built,
                                 amend={"doctype": doctype, "name": mapped.source_name})
        return {**await self._publish(), **note, "next": "apply the user's change with edit_draft"}

    async def _t_propose_repeat(self, invoice: str, frequency: str = "Monthly", start_date: str | None = None) -> dict[str, Any]:
        if frequency not in REPEAT_FREQUENCIES:
            raise ToolInputError(f"frequency must be one of {', '.join(REPEAT_FREQUENCIES)}")
        try:
            mapped = await self.erp.document_lines("Sales Invoice", invoice.strip())
        except ConversionError as exc:
            raise ToolInputError(f"ERPNext: {exc}") from exc
        today = getattr(self.erp, "today", None) or date.today()
        try:
            start = date.fromisoformat(start_date) if start_date else today
        except ValueError as exc:
            raise ToolInputError("start_date must be YYYY-MM-DD") from exc
        if start < today:
            raise ToolInputError("start_date can't be in the past")
        note = self._replaced_note()
        self.state.draft = Draft(kind="repeat", customer_id=mapped.customer_id, customer_name=mapped.customer_name,
                                 source={"doctype": "Sales Invoice", "name": mapped.source_name}, note=frequency,
                                 bill_date=_next_date(start, frequency).isoformat(),
                                 payment_amount=str(sum((x.qty * x.rate for x in mapped.lines), Decimal(0))))
        return {**await self._publish(), **note}

    async def _t_set_customer_discount(self, customer_id: str, percent: Any) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can set a permanent discount")
        customer = await self.erp.get_customer(customer_id)
        if customer is None:
            raise ToolInputError("unknown customer_id; call find_customer first")
        pct = Decimal(str(percent)) if percent not in (None, "") else Decimal(-1)
        if not pct.is_finite() or pct < 0 or pct >= 50:
            raise ToolInputError("percent must be 0 (switch off) up to 50")
        current = await self.erp.customer_discount(customer.id)
        note = self._replaced_note()
        self.state.draft = Draft(kind="pricing", customer_id=customer.id, customer_name=customer.name,
                                 payment_amount=str(pct), note=str(current))
        return {**await self._publish(), **note}

    async def _t_propose_bahi_import(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Entries copied from a bahi page; per-party dues are summed here, never by the model."""
        if not self.state.can_submit:
            raise ToolInputError("only the owner can book old dues")
        if not entries or len(entries) > 80:
            raise ToolInputError("give 1-80 entries copied from the page")
        parties: dict[str, dict[str, Any]] = {}
        skipped = 0
        for raw in entries:
            name = str(raw.get("party") or "").strip()
            kind = raw.get("type")
            if not name or kind not in BAHI_TYPES:
                skipped += 1
                continue
            amount = money(_decimal(raw.get("amount"), "amount"))
            party = parties.setdefault(name.lower(), {"name": name, "given": Decimal(0), "received": Decimal(0), "lines": 0})
            party["given" if kind in {"sale", "udhaar"} else "received"] += amount
            party["lines"] += 1
        rows = []
        for party in parties.values():
            match = await self.erp.find_customer(party["name"])
            found = match.candidates[0] if match.status == "resolved" else None
            rows.append({"party": party["name"], "customer_id": found["customer_id"] if found else None,
                         "customer": found["name"] if found else party["name"], "lines": party["lines"],
                         "given": str(party["given"]), "received": str(party["received"]),
                         "due": str(money(party["given"] - party["received"]))})
        if not rows:
            raise ToolInputError("no usable entries (each needs party, type sale/udhaar/received and amount)")
        note = self._replaced_note()
        self.state.draft = Draft(kind="bahi", entries=rows, note=str(skipped))
        result = {**await self._publish(), **note}
        ambiguous = [r["party"] for r in rows if r["customer_id"] is None]
        if ambiguous:
            result["new_customers_on_approve"] = ambiguous
        return result

    async def _t_set_brand(self, use_uploaded_logo: bool = False, bank_name: str | None = None, account_name: str | None = None,
                           account_no: str | None = None, ifsc: str | None = None, upi_id: str | None = None,
                           tagline: str | None = None, use_uploaded_signature: bool = False, terms: str | None = None,
                           footer: str | None = None, remove_logo: bool = False, remove_signature: bool = False) -> dict[str, Any]:
        from munim.core import brand as branding

        if not self.state.can_submit:
            raise ToolInputError("only the owner can change the bill design")
        brand_get = getattr(self, "brand_get", None)
        if brand_get is None:
            raise ToolInputError("bill design is not available for this business yet")
        current = await brand_get() or {}
        details, problems = branding.clean_bank({"bank_name": bank_name, "account_name": account_name, "account_no": account_no,
                                                 "ifsc": ifsc, "upi_id": upi_id, "tagline": tagline, "terms": terms,
                                                 "footer": footer})
        if problems:
            raise ToolInputError("; ".join(problems))
        details = {**(current.get("details") or {}), **details}
        doc = self.state.document
        image = doc if doc is not None and doc.mime.startswith("image/") else None
        if (use_uploaded_logo or use_uploaded_signature) and image is None:
            raise ToolInputError("no image uploaded in this chat yet; ask the person to send the picture first")
        if use_uploaded_logo and use_uploaded_signature:
            raise ToolInputError("one picture is either the logo or the signature; ask which")
        if use_uploaded_logo:
            palette = branding.from_logo(Path(doc.media_path).read_bytes()).as_dict()  # colours by code, not the model
        elif current:
            palette = {k: v for k, v in current.items() if k != "details"}
        else:
            palette = branding.neutral().as_dict()
        palette.setdefault("signature_png_b64", current.get("signature_png_b64"))
        if use_uploaded_signature:
            try:
                palette["signature_png_b64"] = branding.signature_from_image(Path(image.media_path).read_bytes())
            except ValueError as exc:
                raise ToolInputError(f"{exc}; ask for a clearer photo on plain paper") from exc
        if remove_signature:
            palette["signature_png_b64"] = None
        if remove_logo:
            palette["logo_png_b64"] = None
        note = self._replaced_note()
        self.state.draft = Draft(kind="brand", brand={**palette, "details": details})
        return {**await self._publish(), **note}

    async def _t_remember(self, fact: str, about: str | None = None) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can add to the business memory")
        memory = getattr(self, "memory", None)
        if memory is None:
            raise ToolInputError("memory is not available here")
        fact = " ".join((fact or "").split())[:200]
        if len(fact) < 4:
            raise ToolInputError("fact is empty")
        return {"remembered": True, "memory_id": memory[1](fact, (about or "").strip()[:80] or None)}

    async def _t_forget(self, memory_id: Any) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can change the business memory")
        memory = getattr(self, "memory", None)
        try:
            ok = memory is not None and memory[2](int(memory_id))
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ToolInputError("no such memory id; see business_memory in <state>")
        return {"forgotten": True}

    async def _t_customer_usual(self, customer_id: str) -> dict[str, Any]:
        customer = await self.erp.get_customer(customer_id)
        if customer is None:
            raise ToolInputError("unknown customer_id; call find_customer first")
        docs = [d for d in await self.erp.recent_documents(customer_id, limit=20)
                if d.get("status") == "submitted" or d.get("docstatus") == 1]
        bills = [d for d in docs if d["doctype"] == "Sales Invoice"] or [d for d in docs if d["doctype"] == "Sales Order"]
        bills = bills[:USUAL_BILLS]  # newest first; invoices only, so order -> challan -> bill isn't counted three times
        seen: dict[str, dict[str, Any]] = {}
        for doc in bills:
            try:
                lines = (await self.erp.document_lines(doc["doctype"], doc["name"])).lines
            except ConversionError:
                continue
            for line in lines:
                row = seen.setdefault(line.item_code, {"qtys": [], "last_rate": str(line.rate), "last_date": doc.get("date")})
                row["qtys"].append(line.qty)
        if not seen:
            return {"customer": customer.name, "bills_seen": 0, "usual": [], "note": "no final bills yet"}
        need = 1 if len(bills) < 3 else 2  # with a few bills, anything bought twice is a habit
        usual = []
        for code, row in sorted(seen.items(), key=lambda kv: -len(kv[1]["qtys"])):
            if len(row["qtys"]) < need:
                continue
            item = await self.erp.get_item(code)
            usual.append({"item_code": code, "item_name": item.name if item else code, "usual_qty": str(median(row["qtys"])),
                          "in_bills": f"{len(row['qtys'])} of {len(bills)}", "last_rate": row["last_rate"], "last_date": row["last_date"]})
        dates = sorted({date.fromisoformat(str(d["date"])[:10]) for d in bills if d.get("date")})
        gap = round((dates[-1] - dates[0]).days / (len(dates) - 1)) if len(dates) > 1 else None
        return {"customer": customer.name, "bills_seen": len(bills), "usual": usual[:12], "buys_every_days": gap,
                "last_bill": str(dates[-1]) if dates else None}

    async def _t_online_payments(self, action: str) -> dict[str, Any]:
        if not self.state.can_submit:
            raise ToolInputError("only the owner can manage online payments")
        payments = getattr(self, "payments", None)
        if payments is None:
            raise ToolInputError("online payments are not available here")
        if action not in ("status", "on", "off", "connect"):
            raise ToolInputError("action is status, on, off or connect")
        result = dict(await payments(action))
        if "error" in result:
            raise ToolInputError(result["error"])
        if "send" in result:
            self.effects.messages.append(result.pop("send"))
            result["setup_link_sent"] = "sent as a separate message; do not repeat the link"
        return result

    async def _t_note_request(self, summary: str) -> dict[str, Any]:
        text_ = (summary or "").strip()[:300]
        if not text_:
            raise ToolInputError("summary is empty")
        self.effects.requests.append(text_)
        return {"noted": True, "say": "noted for the team; offer the closest thing that works today"}

    async def _t_apply_discount(self, percent: Any, item_codes: list[str] | None = None) -> dict[str, Any]:
        draft = self.state.draft
        if draft is None or draft.kind not in (*SALES_KINDS, "purchase"):
            raise ToolInputError("no open bill/quotation/order/challan preview")
        pct = _decimal(percent, "percent")
        if pct >= 100:
            raise ToolInputError("percent must be below 100")
        codes = set(item_codes or [line.item_code for line in draft.lines])
        for line in draft.lines:
            if line.item_code in codes:  # rate x (100 - p) / 100, rounded the ERPNext way; the base is the current rate
                line.rate = str(money(Decimal(line.rate) * (100 - pct) / 100))
                line.rate_source = "discount"
        draft.discount = f"{pct.normalize():f}"
        draft.version += 1
        draft.created_at = time.time()
        return await self._publish()

    async def _lines_from(self, mapped: Any, source: str) -> list[DraftLineState]:
        built = []
        for line in mapped.lines:
            item = await self.erp.get_item(line.item_code)
            if item is None:
                raise ToolInputError(f"item {line.item_code} on {mapped.source_name} is not available any more")
            built.append(DraftLineState(item.code, item.name, item.uom, str(line.qty), str(line.rate), source))
        return built

    async def _t_find_supplier(self, name: str) -> dict[str, Any]:
        match = await self.erp.find_supplier(name)
        return {"status": match.status, "candidates": list(match.candidates)}

    async def _t_propose_new_supplier(self, name: str, phone: str | None = None, gstin: str | None = None) -> dict[str, Any]:
        name = (name or "").strip()
        if len(name) < 2:
            raise ToolInputError("name too short")
        existing = await self.erp.find_supplier(name)
        if existing.status == "resolved":
            return {"error": "supplier already exists", "existing": list(existing.candidates)}
        if gstin:
            gstin = gstin.strip().upper()
            if not GSTIN_RE.match(gstin):
                raise ToolInputError("GSTIN format invalid (15 characters)")
        note = self._replaced_note()
        self.state.draft = Draft(kind="new_supplier", new_customer={"name": name, "phone": phone, "gstin": gstin})
        return {**await self._publish(), **note}

    async def _t_propose_purchase_bill(self, supplier_id: str, lines: list[dict[str, Any]], bill_no: str | None = None,
                                       bill_date: str | None = None, attach_uploaded_document: bool = True) -> dict[str, Any]:
        supplier = await self.erp.get_supplier(supplier_id)
        if supplier is None:
            raise ToolInputError("unknown supplier_id; call find_supplier first")
        if any(raw.get("rate_source") == "price_list" for raw in lines):
            raise ToolInputError("a purchase uses the rate on the supplier's bill (rate_source document) or the one the user said")
        built = await self._build_lines(lines)
        if bill_date:
            try:
                bill_date = date.fromisoformat(bill_date).isoformat()
            except ValueError as exc:
                raise ToolInputError("bill_date must be YYYY-MM-DD") from exc
        document = self.state.document if attach_uploaded_document else None
        note = self._replaced_note()
        self.state.draft = Draft(kind="purchase", customer_id=supplier.id, customer_name=supplier.name, lines=built,
                                 bill_no=(bill_no or "").strip()[:60] or None, bill_date=bill_date,
                                 attachment=document.media_path if document else None)
        return {**await self._publish(), **note}

    async def _t_propose_expense(self, account: str, amount: Any, mode: str, note: str | None = None) -> dict[str, Any]:
        heads = await self.erp.expense_accounts()
        wanted = (account or "").split(" - ")[0].strip().lower()
        head = next((h for h in heads if h.lower() == wanted), None)
        if head is None:
            raise ToolInputError("account must be one of: " + ", ".join(heads))
        if mode not in PAYMENT_MODES:
            raise ToolInputError(f"mode must be one of {', '.join(PAYMENT_MODES)}")
        value = money(_decimal(amount, "amount"))
        note_text = self._replaced_note()
        self.state.draft = Draft(kind="expense", payment_amount=str(value), payment_mode=mode, expense_account=head,
                                 note=(note or "").strip()[:120] or None)
        return {**await self._publish(), **note_text}

    async def _t_cancel_draft(self) -> dict[str, Any]:
        if self.state.draft is None:
            return {"ok": True, "note": "nothing was open"}
        kind = KIND_LABEL.get(self.state.draft.kind, self.state.draft.kind)
        self.state.draft = None
        dropped, self.state.next_steps = self.state.next_steps, []
        return {"ok": True, "cancelled": kind, **({"queue_cleared": dropped} if dropped else {})}

    async def _t_set_item_hsn(self, item_code: str, hsn: str) -> dict[str, Any]:
        draft = self.state.draft
        if draft is None or not any(line.item_code == item_code for line in draft.lines):
            raise ToolInputError("item is not on the open preview")
        code = clean_hsn(hsn)
        if code is None:
            raise ToolInputError("HSN must be 4, 6 or 8 digits")
        if not await self.erp.hsn_valid(code):
            raise ToolInputError(f"{code} is not in the GST HSN list; ask the user to check it")
        draft.hsn[item_code] = code
        return {**await self._publish(), "hsn_saved_on_approve": {item_code: code}}

    async def _t_queue_next_steps(self, steps: list[str]) -> dict[str, Any]:
        clean = [str(s).strip()[:200] for s in steps if str(s).strip()][:5]
        self.state.next_steps = clean
        return {"ok": True, "queued": clean, "note": "each runs automatically after the user taps Approve"}

    async def _t_ask_user(self, question: str, options: list[str]) -> dict[str, Any]:
        options = [str(o).strip()[:20] for o in options if str(o).strip()][:3]
        if len(options) < 2:
            raise ToolInputError("need 2-3 options")
        self.effects.question = PendingQuestion(
            question=question.strip(), options=[{"id": f"opt:{n}", "title": t} for n, t in enumerate(options)]
        )
        return {"ok": True, "note": "question with buttons will be sent; end your turn without repeating it"}

    async def _t_save_alias(self, kind: str, alias: str, target_id: str) -> dict[str, Any]:
        if self.aliases is None:
            return {"ok": False, "note": "aliases not available"}
        if kind not in {"customer", "item"} or not (alias or "").strip():
            raise ToolInputError("kind must be customer or item, alias required")
        exists = await (self.erp.get_customer(target_id) if kind == "customer" else self.erp.get_item(target_id))
        if exists is None:
            raise ToolInputError(f"unknown {kind} id {target_id}")
        self.aliases[1](kind, alias.strip()[:60], target_id)
        return {"ok": True}

    async def _t_save_document_facts(self, doc_type: str, readable: bool, **facts: Any) -> dict[str, Any]:
        if self.state.document is None:
            raise ToolInputError("no document attached")
        self.state.document.facts = {"doc_type": doc_type, "readable": bool(readable), **facts}
        return {"ok": True}


def _next_date(start: date, frequency: str) -> date:
    """When ERPNext makes the first copy: one period after the start (end of month clamps)."""
    import calendar
    from datetime import timedelta

    if frequency == "Daily":
        return start + timedelta(days=1)
    if frequency == "Weekly":
        return start + timedelta(days=7)
    months = {"Monthly": 1, "Quarterly": 3, "Half-yearly": 6, "Yearly": 12}[frequency]
    total = start.month - 1 + months
    year, month = start.year + total // 12, total % 12 + 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def _pct(value: Decimal) -> str | None:
    return f"{value.normalize():f}" if value else None
