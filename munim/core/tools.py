"""Closed tool set for the v2 agent. No tool executes an ERP write.

Writes happen only in `engine.py` when the owner taps the Approve button."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from munim.core.drafts import (
    KIND_DOCTYPE,
    KIND_LABEL,
    compute_lines,
    fmt_rs,
    render_cancel_preview,
    render_customer_preview,
    render_debit_note_preview,
    render_expense_preview,
    render_payment_preview,
    render_purchase_preview,
    render_sales_preview,
    render_supplier_payment_preview,
)
from munim.core.erp import CONVERSIONS, ConversionError, ErpPort, money
from munim.core.gst import TAX_NONE, clean_hsn
from munim.core.i18n import t
from munim.core.reports import ReportError
from munim.core.reports import build as build_report
from munim.core.state import ConversationState, Draft, DraftLineState, PendingQuestion
from munim.core.tool_specs import TOOL_SPECS
from munim.core.tools_business import BusinessTools
from munim.core.tools_common import (
    GSTIN_RE,
    MAX_QTY,
    PAYMENT_MODES,
    RATE_SOURCES,
    SALES_KINDS,
    SOURCE_DOCTYPES,
    ToolInputError,
    TurnEffects,
    _decimal,
    _pct,
)
from munim.core.tools_corrections import CorrectionTools
from munim.core.tools_purchase import PurchaseTools

__all__ = ["TOOL_SPECS", "ToolExecutor", "ToolInputError", "TurnEffects"]  # imported from here by other modules


class ToolExecutor(PurchaseTools, CorrectionTools, BusinessTools):
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
        if draft.kind == "supplier_payment":
            return render_supplier_payment_preview(draft.customer_name or "", Decimal(draft.payment_amount or 0), draft.payment_mode or "",
                                                   self.demo_label, self.state.lang, draft.allocations, final=self.state.can_submit)
        if draft.kind == "debit_note":
            rows = [(await self.erp.get_item(x.item_code), Decimal(x.qty), Decimal(x.rate)) for x in draft.lines]
            computed = compute_lines(rows, await self.erp.purchase_tax_mode(draft.customer_id or ""))
            return render_debit_note_preview(draft.customer_name or "", computed, draft.source["name"], self.demo_label, self.state.lang,
                                             final=self.state.can_submit)
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

    async def _lines_from(self, mapped: Any, source: str) -> list[DraftLineState]:
        built = []
        for line in mapped.lines:
            item = await self.erp.get_item(line.item_code)
            if item is None:
                raise ToolInputError(f"item {line.item_code} on {mapped.source_name} is not available any more")
            built.append(DraftLineState(item.code, item.name, item.uom, str(line.qty), str(line.rate), source))
        return built

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
