"""The closed tool set the model may call (names, descriptions, input schemas). No tool writes to the ERP."""
from __future__ import annotations

from typing import Any

from munim.core.reports import PERIODS, REPORTS
from munim.core.tools_common import (
    _LINE_SCHEMA,
    BAHI_TYPES,
    CANCELLABLE,
    PAYMENT_MODES,
    REPEAT_FREQUENCIES,
    SALES_KINDS,
    SOURCE_DOCTYPES,
)

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
        "name": "propose_supplier_payment",
        "description": (
            "Money PAID to a supplier ('Shree Cement ko 20000 diye'): preview a payment against their open purchase bills, "
            "oldest first (code splits it). Amount must be what the user stated. Call find_supplier first."
        ),
        "input_schema": {"type": "object", "properties": {
            "supplier_id": {"type": "string"}, "amount": {"type": "number"}, "mode": {"type": "string", "enum": list(PAYMENT_MODES)}},
            "required": ["supplier_id", "amount", "mode"]},
    },
    {
        "name": "propose_purchase_return",
        "description": (
            "Goods sent BACK to a supplier on a final Purchase Invoice ('Shree ko 5 bag wapas bheje'): preview a Debit Note "
            "against that bill. Without lines, everything still returnable; with lines [{item_code, qty}] only those. Rates "
            "come from the bill. It reduces what is owed on that bill."
        ),
        "input_schema": {"type": "object", "properties": {
            "invoice": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "item_code": {"type": "string"}, "qty": {"type": "number"}}, "required": ["item_code", "qty"]}}},
            "required": ["invoice"]},
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
