"""The Approve button: the only place that writes to the ERP, for the exact preview version shown."""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

from munim.core.drafts import KIND_DOCTYPE, fmt_rs
from munim.core.engine_common import Outbound, _approved_text, _freq_label, text

log = logging.getLogger("munim.core.engine")
from munim.core.erp import ConversionError, DraftLine
from munim.core.i18n import kind_label, t
from munim.core.state import ConversationState


class ApproveMixin:
    """The Approve button: the only place that writes to the ERP, for the exact preview version shown."""

    async def approve(self, state: ConversationState) -> list[Outbound]:
        """Make the open preview's document (the Approve button, or confirm_preview over MCP)."""
        return await self._approve(state)

    async def _approve(self, state: ConversationState) -> list[Outbound]:
        draft = state.draft
        assert draft is not None
        reference = f"munim-{draft.id}-v{draft.version}"
        lang = state.lang
        out: list[Outbound] = []
        if draft.kind in KIND_DOCTYPE:
            for code, hsn in draft.hsn.items():  # the HSN the user confirmed on this preview
                await self.erp.set_item_hsn(code, hsn)
            lines = [DraftLine(line.item_code, Decimal(line.qty), Decimal(line.rate)) for line in draft.lines]
            if draft.amend:  # cancel the wrong final document, make the corrected one
                try:
                    doc = await self.erp.amend_document(draft.amend["doctype"], draft.amend["name"], lines, reference,
                                                        final=state.can_submit)
                except ConversionError as exc:
                    return [text(t("cancel.failed", lang, name=draft.amend["name"], reason=str(exc)[:300]))]
            elif draft.source:  # ERPNext's mapper: links to the quotation/order/challan are kept
                doc = await self.erp.create_from_source(draft.source["doctype"], draft.source["name"], KIND_DOCTYPE[draft.kind],
                                                        lines, reference, final=state.can_submit)
            else:
                doc = await self.erp.create_sales_draft(KIND_DOCTYPE[draft.kind], draft.customer_id or "", lines, reference,
                                                        final=state.can_submit)
            label = kind_label(draft.kind, lang)
            if doc.doctype != KIND_DOCTYPE[draft.kind]:  # the in-between order could not be finalised
                reply = t("order_kept_draft", lang, name=doc.name)
            else:
                reply = _approved_text(doc, label, draft.customer_name or "", state.can_submit, lang)
                if draft.source:
                    reply += "\n" + t("preview.from", lang, source=f"{draft.source['doctype']} {draft.source['name']}")
                if doc.via:
                    reply += "\n" + t("via_order", lang, name=doc.via)
                if draft.amend:
                    reply += "\n" + t("corrected", lang, old=draft.amend["name"])
            if doc.total is not None:
                reply += "\n" + t("erp_total", lang, total=fmt_rs(doc.total))
            created_meta = {"kind": "document", "doc": doc.name, "doctype": doc.doctype, "customer": draft.customer_name,
                            "submitted": doc.submitted, "source": draft.source, "customer_id": draft.customer_id,
                            "total": str(doc.total) if doc.total is not None else None}
            if doc.pdf_path:
                out.append(Outbound("document", reply, path=doc.pdf_path, filename=f"{doc.name}.pdf", meta=created_meta))
            else:
                out.append(Outbound("text", reply, meta=created_meta))
            created = {"draft_id": draft.id, "label": label, "name": doc.name, "customer": draft.customer_name or "",
                       "doctype": doc.doctype}
            if draft.source:
                created["from"] = f"{draft.source['doctype']} {draft.source['name']}"
            if doc.submitted:
                created["submitted"] = "1"
        elif draft.kind == "payment":
            doc = await self.erp.create_payment_draft(
                draft.customer_id or "", Decimal(draft.payment_amount or 0), draft.payment_mode or "Cash", reference,
                [(a["invoice"], Decimal(a["amount"])) for a in draft.allocations], final=state.can_submit,
            )
            reply = (_approved_text(doc, "Payment Entry", draft.customer_name or "", True, lang) if state.can_submit
                     else t("approved_payment", lang, name=doc.name, customer=draft.customer_name))
            out.append(Outbound("text", reply, meta={"kind": "document", "doc": doc.name, "doctype": "Payment Entry",
                                                     "customer": draft.customer_name, "submitted": doc.submitted}))
            created = {"draft_id": draft.id, "label": "Payment Entry", "name": doc.name, "customer": draft.customer_name or "",
                       "doctype": "Payment Entry"}
            if doc.submitted:
                created["submitted"] = "1"
        elif draft.kind == "supplier_payment":
            doc = await self.erp.create_supplier_payment(
                draft.customer_id or "", Decimal(draft.payment_amount or 0), draft.payment_mode or "Cash", reference,
                [(a["invoice"], Decimal(a["amount"])) for a in draft.allocations], final=state.can_submit)
            label = kind_label("supplier_payment", lang)
            reply = _approved_text(doc, label, draft.customer_name or "", state.can_submit, lang)
            out.append(Outbound("text", reply, meta={"kind": "document", "doc": doc.name, "doctype": "Payment Entry",
                                                     "customer": draft.customer_name, "submitted": doc.submitted}))
            created = {"draft_id": draft.id, "label": label, "name": doc.name, "customer": draft.customer_name or "",
                       "doctype": "Payment Entry"}
            if doc.submitted:
                created["submitted"] = "1"
        elif draft.kind == "debit_note":
            lines = [DraftLine(line.item_code, Decimal(line.qty), Decimal(line.rate)) for line in draft.lines]
            try:
                doc = await self.erp.create_purchase_return(draft.source["name"], lines, reference, final=state.can_submit)
            except ConversionError as exc:
                return [text(t("cancel.failed", lang, name=draft.source["name"], reason=str(exc)[:300]))]
            label = kind_label("debit_note", lang)
            reply = _approved_text(doc, label, draft.customer_name or "", state.can_submit, lang)
            reply += "\n" + t("preview.against", lang, source=f"Purchase Invoice {draft.source['name']}")
            out.append(Outbound("text", reply, meta={"kind": "document", "doc": doc.name, "doctype": doc.doctype,
                                                     "customer": draft.customer_name, "submitted": doc.submitted, "source": draft.source}))
            created = {"draft_id": draft.id, "label": label, "name": doc.name, "customer": draft.customer_name or "",
                       "doctype": doc.doctype, "from": f"Purchase Invoice {draft.source['name']}"}
            if doc.submitted:
                created["submitted"] = "1"
        elif draft.kind == "credit_note":
            lines = [DraftLine(line.item_code, Decimal(line.qty), Decimal(line.rate)) for line in draft.lines]
            try:
                doc = await self.erp.create_return(draft.source["name"], lines, reference, final=state.can_submit)
            except ConversionError as exc:
                return [text(t("cancel.failed", lang, name=draft.source["name"], reason=str(exc)[:300]))]
            label = kind_label("credit_note", lang)
            reply = _approved_text(doc, label, draft.customer_name or "", state.can_submit, lang)
            reply += "\n" + t("preview.against", lang, source=f"Sales Invoice {draft.source['name']}")
            meta = {"kind": "document", "doc": doc.name, "doctype": doc.doctype, "customer": draft.customer_name,
                    "submitted": doc.submitted, "source": draft.source}
            out.append(Outbound("document", reply, path=doc.pdf_path, filename=f"{doc.name}.pdf", meta=meta) if doc.pdf_path
                       else Outbound("text", reply, meta=meta))
            created = {"draft_id": draft.id, "label": label, "name": doc.name, "customer": draft.customer_name or "",
                       "doctype": doc.doctype, "from": f"Sales Invoice {draft.source['name']}"}
            if doc.submitted:
                created["submitted"] = "1"
        elif draft.kind == "bahi":
            page = Path(state.document.media_path) if state.document and state.document.mime.startswith(("image/", "application/pdf")) else None
            booked, new = [], 0
            for row in draft.entries:
                due = Decimal(row["due"])
                if due <= 0:
                    continue  # settled or an advance: nothing to book as due
                customer_id = row["customer_id"]
                if not customer_id:
                    customer_id = (await self.erp.create_customer(row["party"], None, None)).id
                    new += 1
                doc = await self.erp.book_old_due(customer_id, due, f"From bahi page: {row['lines']} entries", page)
                booked.append(f"• {row['customer']}: {fmt_rs(due)} ({doc.name})")
            reply = t("bahi.done", lang, n=len(booked), new=new) + ("\n" + "\n".join(booked) if booked else "")
            out.append(text(reply))
            created = {"draft_id": draft.id, "label": "Old dues", "name": f"{len(booked)} opening invoices", "customer": ""}
        elif draft.kind == "brand":
            brand = dict(draft.brand or {})
            details = brand.pop("details", {})
            try:
                await self.brand[1](brand, details)
            except Exception:
                log.exception("brand apply failed")
                return [text(t("brand.failed", lang))]
            reply = t("brand.done", lang)
            out.append(text(reply))
            sample = next((d for d in state.last_created if d.get("doctype") in {"Sales Invoice", "Quotation", "Sales Order", "Delivery Note"}), None)
            if sample:  # show the new design on their latest document
                pdf = await self.erp.document_pdf(sample["doctype"], sample["name"])
                if pdf:
                    path = self.media_dir / "brand" / f"{sample['name']}.pdf"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(pdf)
                    out.append(Outbound("document", t("brand.sample", lang, name=sample["name"]), path=path,
                                        filename=f"{sample['name']}.pdf"))
            created = {"draft_id": draft.id, "label": "Bill design", "name": "AI Munim print formats", "customer": ""}
        elif draft.kind == "pricing":
            pct = Decimal(draft.payment_amount or 0)
            name = await self.erp.set_customer_discount(draft.customer_id or "", pct)
            shown = f"{pct.normalize():f}"
            reply = t("pricing.done" if pct else "pricing.off", lang, customer=draft.customer_name or "", pct=shown)
            out.append(text(reply))
            created = {"draft_id": draft.id, "label": "Pricing Rule", "name": name, "customer": draft.customer_name or "",
                       "doctype": "Pricing Rule"}
        elif draft.kind == "repeat":
            try:
                doc = await self.erp.create_auto_repeat(draft.source["doctype"], draft.source["name"], draft.note or "Monthly",
                                                        draft.bill_date or "", final=state.can_submit)
            except ConversionError as exc:
                state.draft = None
                return [text(t("cancel.failed", lang, name=draft.source["name"], reason=str(exc)[:300]))]
            reply = t("repeat.done", lang, name=draft.source["name"], freq=_freq_label(draft.note or "Monthly", lang),
                      first=draft.bill_date or "", auto=t("repeat.final" if state.can_submit else "repeat.draft", lang))
            out.append(text(reply))
            created = {"draft_id": draft.id, "label": "Auto Repeat", "name": doc.name, "customer": draft.customer_name or "",
                       "doctype": "Auto Repeat", "from": f"Sales Invoice {draft.source['name']}"}
        elif draft.kind == "cancel":
            doctype, name = draft.source["doctype"], draft.source["name"]
            try:
                await self.erp.cancel_document(doctype, name)
            except ConversionError as exc:
                state.draft = None
                return [text(t("cancel.failed", lang, name=name, reason=str(exc)[:300]))]
            reply = t("cancel.done", lang, doctype=doctype, name=name)
            out.append(text(reply))
            for entry in state.last_created:
                if entry.get("name") == name:
                    entry["cancelled"] = "1"
            created = {"draft_id": draft.id, "label": "Cancel", "name": name, "customer": draft.customer_name or "",
                       "doctype": doctype, "cancelled": "1"}
        elif draft.kind == "purchase":
            for code, hsn in draft.hsn.items():
                await self.erp.set_item_hsn(code, hsn)
            lines = [DraftLine(line.item_code, Decimal(line.qty), Decimal(line.rate)) for line in draft.lines]
            doc = await self.erp.create_purchase_invoice(
                draft.customer_id or "", lines, draft.bill_no, draft.bill_date, Path(draft.attachment) if draft.attachment else None,
                reference, final=state.can_submit)
            label = kind_label("purchase", lang)
            reply = _approved_text(doc, label, draft.customer_name or "", state.can_submit, lang)
            if doc.total is not None:
                reply += "\n" + t("erp_total", lang, total=fmt_rs(doc.total))
            out.append(Outbound("text", reply, meta={"kind": "document", "doc": doc.name, "doctype": doc.doctype,
                                                     "customer": draft.customer_name, "submitted": doc.submitted}))
            created = {"draft_id": draft.id, "label": label, "name": doc.name, "customer": draft.customer_name or "",
                       "doctype": doc.doctype}
            if doc.submitted:
                created["submitted"] = "1"
        elif draft.kind == "expense":
            doc = await self.erp.create_expense(draft.expense_account or "", Decimal(draft.payment_amount or 0), draft.payment_mode or "Cash",
                                                draft.note or "", reference, final=state.can_submit)
            label = kind_label("expense", lang)
            reply = _approved_text(doc, label, draft.expense_account or "", state.can_submit, lang)
            out.append(Outbound("text", reply, meta={"kind": "document", "doc": doc.name, "doctype": doc.doctype,
                                                     "customer": draft.expense_account, "submitted": doc.submitted}))
            created = {"draft_id": draft.id, "label": label, "name": doc.name, "customer": draft.expense_account or "",
                       "doctype": doc.doctype}
            if doc.submitted:
                created["submitted"] = "1"
        elif draft.kind == "new_supplier":
            info = draft.new_customer or {}
            supplier = await self.erp.create_supplier(info.get("name", ""), info.get("phone"), info.get("gstin"))
            reply = t("supplier_added", lang, name=supplier.name)
            out.append(text(reply))
            created = {"draft_id": draft.id, "label": "Supplier", "name": supplier.name, "customer": supplier.name}
        else:
            info = draft.new_customer or {}
            customer = await self.erp.create_customer(info.get("name", ""), info.get("phone"), info.get("gstin"))
            reply = t("customer_added", lang, name=customer.name)
            out.append(text(reply))
            created = {"draft_id": draft.id, "label": "Customer", "name": customer.name, "customer": customer.name}
        state.last_created = ([created] + state.last_created)[:5]
        state.draft = None
        state.add_turn("user", "[tapped Approve]", at=self.clock())
        state.add_turn("assistant", reply, at=self.clock())
        self._audit("approved", state.sender, {"reference": reference, **created})
        if state.next_steps:  # the rest of a multi-document request: keep the promise without being asked again
            step = state.next_steps.pop(0)
            try:
                out += await self._agent_turn(
                    state, user_text=step, display=f"[queued from my earlier request] {step}",
                    note="Continuing the user's earlier multi-document request; propose this one now.")
            except Exception:
                log.exception("queued step failed")
                state.next_steps = []
                out.append(text(t("queue_failed", lang, step=step)))
        return out
