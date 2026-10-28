"""Agent Core v2 turn engine: one message in, a list of outbound messages out."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from munim.core import style as styles
from munim.core.clock import gap, now_line, stamp, today_ist
from munim.core.drafts import KIND_DOCTYPE, KIND_LABEL, fmt_rs
from munim.core.erp import ConversionError, DraftLine, ErpPort
from munim.core.guards import BudgetExceeded
from munim.core.i18n import detect_lang, kind_label, t
from munim.core.llm import AgentLLM
from munim.core.media import (
    MAX_AUDIO_BYTES,
    MAX_DOC_BYTES,
    NoTranscriber,
    Transcriber,
    file_hash,
    parse_vcard,
    prepare_image,
    prepare_pdf,
    raw_block,
    sniff,
)
from munim.core.prompt import SYSTEM_PROMPT
from munim.core.resolver import NullResolver, TurnResolver
from munim.core.state import ConversationState, DocumentContext, Draft, StateStore
from munim.core.tools import TOOL_SPECS, ToolExecutor
from munim.core.walog import WALog

log = logging.getLogger(__name__)

DOCUMENT_FOCUS_SECONDS = 60 * 60
PREVIEW_TTL_SECONDS = 24 * 60 * 60
QUESTION_TTL_SECONDS = 30 * 60

RESET_WORDS = {"reset", "/reset", "reset demo", "restart"}
VOICE_OFF = {"voice reply band", "voice band", "awaaz band", "voice replies off", "stop voice replies"}
VOICE_ON = {"voice reply chalu", "voice chalu", "awaaz chalu", "voice replies on", "start voice replies"}
SPEAK_NOTE = ("The user sent a voice note. After your reply add ONE last line starting 'SPEAK:' with the same answer "
              "said aloud in short simple Hindi in Devanagari (English if they spoke English): at most two sentences, no "
              "emojis or symbols, amounts as plain numbers followed by रुपये. Never put anything after that line.")
GAP_MARKER_SECONDS = 3 * 60 * 60


@dataclass
class Inbound:
    message_id: str
    sender: str
    kind: str  # text | button | document | image | audio | other
    text: str | None = None
    button_id: str | None = None
    media: bytes | None = None
    mime: str | None = None
    filename: str | None = None
    sent_at: float | None = None  # when the user sent it (WhatsApp timestamp)
    reply_to: str | None = None  # wamid of the message they swiped to reply to
    forwarded: bool = False
    frequently_forwarded: bool = False
    voice: bool = False  # recorded voice note (vs an audio file)
    media_sha256: str | None = None


@dataclass
class Outbound:
    kind: str  # text | buttons | document
    text: str = ""
    buttons: list[dict[str, str]] = field(default_factory=list)
    path: Path | None = None
    filename: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # what this message is, for reply-to lookups


def text(body: str) -> Outbound:
    return Outbound("text", body)


class AgentEngine:
    def __init__(
        self,
        *,
        store: StateStore,
        erp: ErpPort,
        llm: AgentLLM,
        resolver: TurnResolver | None = None,
        tenant: str = "demo",
        media_dir: Path = Path(".run/v2/media"),
        audit_path: Path = Path(".run/v2/audit.jsonl"),
        clock: Any = time.time,
        welcome_key: str = "welcome",
        transcriber: Transcriber | None = None,
        walog: WALog | None = None,
        brand: tuple[Any, Any] | None = None,
        speaker: Any = None,
        payments: Any = None,
    ) -> None:
        self.payments = payments  # async (action, sender) -> dict: online payments setup, or None
        self.walog = walog
        self.brand = brand  # (get, apply) on this business's ERP site, or None
        self.speaker = speaker  # local TTS for voice replies, or None
        self.welcome_key = welcome_key
        self.transcriber = transcriber or NoTranscriber()
        self.store = store
        self.erp = erp
        self.llm = llm
        self.resolver = resolver or NullResolver()
        self.tenant = tenant
        self.media_dir = media_dir
        self.audit_path = audit_path
        self.clock = clock

    # ---- entry point -------------------------------------------------
    async def handle(self, msg: Inbound, *, can_submit: bool = True) -> list[Outbound]:
        state = self.store.load(self.tenant, msg.sender)
        state.can_submit = can_submit
        self._expire(state)
        try:
            out = await self._dispatch(msg, state, can_submit)
        except BudgetExceeded:
            log.warning("daily LLM budget reached")
            out = [text(t("busy", state.lang))]
        except Exception:
            incident = uuid.uuid4().hex[:8]
            log.exception("turn failed incident=%s", incident)
            self._audit("turn_failed", msg.sender, {"incident": incident, "kind": msg.kind})
            out = [text(t("error", state.lang))]
        if not styles.uses_emoji(state.style):  # they write without emojis: so do we (buttons keep their icons)
            for item in out:
                if item.text:
                    item.text = styles.strip_emoji(item.text)
        self.store.save(self.tenant, state)
        return out

    async def _dispatch(self, msg: Inbound, state: ConversationState, can_submit: bool = True) -> list[Outbound]:
        if msg.kind in {"text", "document", "image"} and msg.text:
            state.lang = detect_lang(msg.text, state.lang)
            state.style = styles.observe(state.style, msg.text)
        lang = state.lang
        if msg.kind == "button" and msg.button_id:
            return await self._button(msg, state, can_submit)
        words = (msg.text or "").strip().lower().rstrip("!.") if msg.kind == "text" else ""
        if words in VOICE_OFF | VOICE_ON:
            state.voice_replies = words in VOICE_ON
            return [text(t("voice_on" if state.voice_replies else "voice_off", lang))]
        if msg.kind == "text" and (msg.text or "").strip().lower() in RESET_WORDS:
            self.store.reset(self.tenant, msg.sender)
            state.__dict__.update(ConversationState(sender=state.sender).__dict__)
            return [text(t("reset_done", lang) + t(self.welcome_key, lang))]
        if msg.kind == "audio":
            return await self._voice(msg, state)
        if msg.kind in {"document", "image"}:
            return await self._media(msg, state)
        if msg.kind == "contact":
            return await self._contact(parse_vcard(msg.text or ""), state)
        if msg.kind == "location":
            state.add_turn("user", f"[location] {msg.text or ''}", at=self._at(msg))
            return [text(t("location_noted", lang))]
        if msg.kind == "video":
            return [text(t("video_unsupported", lang))]
        if msg.kind == "reaction":
            return self._reaction(msg, state)
        if msg.kind == "ignore":
            return []
        if msg.kind != "text" or not (msg.text or "").strip():
            return [text(t("type_unsupported", lang))]
        if not state.turns and _is_greeting(msg.text or "") and not msg.reply_to:
            state.add_turn("user", msg.text or "", at=self._at(msg))
            welcome = t(self.welcome_key, lang)
            state.add_turn("assistant", welcome, at=self.clock())
            return [text(welcome)]
        return await self._agent_turn(state, user_text=msg.text or "", msg=msg)

    # ---- buttons -----------------------------------------------------
    async def _button(self, msg: Inbound, state: ConversationState, can_submit: bool = True) -> list[Outbound]:
        action, _, rest = (msg.button_id or "").partition(":")
        lang = state.lang
        if action in {"submit", "subok", "subno"}:
            return await self._submit_flow(action, rest, state, can_submit)
        if action == "opt":
            question = state.question
            option = None
            if question:
                option = next((o for o in question.options if o["id"] == msg.button_id), None)
            if option is None:
                return [text(t("option_stale", lang))]
            state.question = None
            return await self._agent_turn(
                state, user_text=option["title"], note=f'User tapped "{option["title"]}" for: {question.question}', msg=msg
            )
        draft_id, _, version = rest.partition(":")
        draft = state.draft
        executed = next((d for d in state.last_created if d.get("draft_id") == draft_id), None)
        if executed:
            return [text(t("already_created", lang, label=executed["label"], name=executed["name"]))]
        if draft is None or draft.id != draft_id:
            return [text(t("preview_closed", lang))]
        if action == "approve":
            if str(draft.version) != version:
                state.add_turn("user", "[tapped Approve on an older preview]")
                return [
                    text(t("stale_preview", lang)),
                    await self._card(state),
                ]
            return await self._approve(state)
        if action == "edit":
            reply = t("edit_prompt", lang)
            state.add_turn("user", "[tapped Edit]")
            state.add_turn("assistant", reply)
            return [text(reply)]
        if action == "cancel":
            label = kind_label(draft.kind, lang)
            state.draft = None
            state.next_steps = []
            reply = t("cancelled", lang, label=label)
            state.add_turn("user", "[tapped Cancel]")
            state.add_turn("assistant", reply)
            return [text(reply)]
        return [text(t("button_unknown", lang))]

    def _reaction(self, msg: Inbound, state: ConversationState) -> list[Outbound]:
        """A reaction is context, never an approval; on the open preview we point at the button."""
        emoji = (msg.text or "").strip()
        rec = self.walog.get(msg.reply_to) if self.walog else None
        if not emoji or rec is None:
            return []  # reaction removed, or on something we don't know
        meta = rec.meta
        what = meta.get("kind", rec.kind)
        state.add_turn("user", f"[reacted {emoji} to our {what}: {rec.text[:80]}]", at=self._at(msg))
        draft = state.draft
        if what == "card" and draft and draft.id == meta.get("draft_id") and draft.version == meta.get("version"):
            reply = t("reaction_card", state.lang, emoji=emoji)
            state.add_turn("assistant", reply, at=self.clock())
            return [text(reply)]
        return []

    async def _submit_flow(self, action: str, rest: str, state: ConversationState, can_submit: bool = True) -> list[Outbound]:
        lang = state.lang
        doctype, _, name = rest.partition(":")
        mine = next((d for d in state.last_created if d.get("name") == name and d.get("doctype") == doctype), None)
        if mine is None:
            return [text(t("preview_closed", lang))]
        if action == "subno":
            return [text(t("submit_later", lang))]
        if not can_submit:
            return [text(t("submit_staff_only", lang))]
        if mine.get("submitted"):
            return [text(t("submitted", lang, name=name))]
        if action == "submit":
            return [Outbound("buttons", t("submit_confirm", lang, name=name), buttons=[
                {"id": f"subok:{doctype}:{name}", "title": t("btn.yes_submit", lang)},
                {"id": f"subno:{doctype}:{name}", "title": t("btn.no", lang)},
            ], meta={"kind": "submit_confirm", "doc": name, "doctype": doctype})]
        try:
            await self.erp.submit_document(doctype, name)
        except Exception:  # noqa: BLE001 - ERPNext validation; the draft stays as it was
            log.warning("submit failed for %s", doctype)
            return [text(t("submit_failed", lang, name=name))]
        mine["submitted"] = "1"
        state.add_turn("assistant", f"[submitted {doctype} {name}]")
        self._audit("submitted", state.sender, {"doctype": doctype, "name": name})
        return [text(t("submitted", lang, name=name))]

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

    async def _card(self, state: ConversationState) -> Outbound:
        executor = ToolExecutor(self.erp, state, demo_label=getattr(self.erp, "label", None))
        if self.brand:
            executor.brand_get = self.brand[0]
        body = await executor.render()
        return self._card_from(state, body)

    @staticmethod
    def _card_from(state: ConversationState, body: str) -> Outbound:
        draft = state.draft
        assert draft is not None
        return Outbound(
            "buttons",
            body[:1024],
            buttons=[
                {"id": f"approve:{draft.id}:{draft.version}", "title": "✅ Approve"},
                {"id": f"edit:{draft.id}", "title": "✏️ Edit"},
                {"id": f"cancel:{draft.id}", "title": "❌ Cancel"},
            ],
            meta={"kind": "card", "draft_id": draft.id, "version": draft.version, "draft_kind": draft.kind,
                  "customer": draft.customer_name},
        )

    # ---- media -------------------------------------------------------
    async def _voice(self, msg: Inbound, state: ConversationState) -> list[Outbound]:
        if not msg.media or isinstance(self.transcriber, NoTranscriber):
            return [text(t("voice_unsupported", state.lang))]
        if len(msg.media) > MAX_AUDIO_BYTES:
            return [text(t("file_too_big", state.lang, mb=MAX_AUDIO_BYTES // (1024 * 1024)))]
        heard = (await self.transcriber.transcribe(msg.media)).strip()
        if len(heard) < 2:
            return [text(t("voice_unclear", state.lang))]
        state.lang = detect_lang(heard, state.lang)
        state.style = styles.observe({**state.style, "emoji": state.style.get("emoji", 0.0)}, heard)
        echo = text(t("voice_heard", state.lang, text=heard[:500]))
        label = "[voice note]" if msg.voice else "[audio file]"
        if self.walog:
            self.walog.update_meta(msg.message_id, transcript=heard[:1000])
        return [echo, *await self._agent_turn(state, user_text=heard, display=f"{label} {heard}", msg=msg)]

    async def _contact(self, card: dict[str, str | None], state: ConversationState) -> list[Outbound]:
        """A shared contact becomes a new-customer preview without any model call."""
        if not card.get("name"):
            return [text(t("contact_no_name", state.lang))]
        state.draft = Draft(kind="new_customer", new_customer={"name": card["name"], "phone": card.get("phone"), "gstin": None})
        state.add_turn("user", f"[shared contact] {card['name']} {card.get('phone') or ''}".strip(), at=self.clock())
        card_out = await self._card(state)
        state.add_turn("assistant", f"[preview card sent]\n{card_out.text}", at=self.clock())
        return [card_out]

    async def _media(self, msg: Inbound, state: ConversationState) -> list[Outbound]:
        data = msg.media or b""
        known = self.store.media_get(self.tenant, f"meta:{msg.media_sha256}") if msg.media_sha256 else None
        if not data and known:  # same file as before (WhatsApp's own hash): not even downloaded
            state.document = DocumentContext(known.get("path", ""), known.get("mime", ""), msg.filename or known.get("filename"),
                                             self.clock(), facts=known.get("facts"))
            caption = (msg.text or "").strip()
            return await self._agent_turn(state, user_text=caption, msg=msg, display=f"[document again: {msg.filename or 'file'}] {caption}".strip(),
                                          note="This exact document was read earlier; its facts are in <state>. Do not call save_document_facts.")
        if not data:
            return [text(t("media_unsupported", state.lang))]
        if len(data) > MAX_DOC_BYTES:
            return [text(t("file_too_big", state.lang, mb=MAX_DOC_BYTES // (1024 * 1024)))]
        kind = sniff(data)
        if kind == "vcard":
            return await self._contact(parse_vcard(data.decode("utf-8", errors="ignore")), state)
        if kind in {"xlsx", "csv"}:
            return [text(t("sheet_own_account", state.lang))]
        if kind not in {"pdf", "jpeg", "png", "webp"}:
            return [text(t("media_unsupported", state.lang))]
        folder = self.media_dir / _safe(msg.sender)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe(msg.message_id)}.{ 'pdf' if kind == 'pdf' else 'jpg'}"
        path.write_bytes(data)
        digest = file_hash(data)
        cached = self.store.media_get(self.tenant, digest)
        mime = "application/pdf" if kind == "pdf" else f"image/{kind}"
        if self.walog:
            self.walog.update_meta(msg.message_id, media_path=str(path), mime=mime, filename=msg.filename, digest=digest)
        state.document = DocumentContext(str(path), mime, msg.filename, self.clock(), facts=cached)
        caption = (msg.text or "").strip()
        label = f"[{'PDF' if kind == 'pdf' else 'photo'}: {msg.filename or 'document'}]"
        if cached:  # same file seen before: reuse its facts, send no pixels/pages to the model
            self._remember_meta_hash(msg, path, mime, cached)
            return await self._agent_turn(state, user_text=caption, display=f"{label} {caption}".strip(), msg=msg,
                                          note="This exact document was read earlier; its facts are in <state>. Do not call save_document_facts.")
        try:
            blocks = (await asyncio.to_thread(prepare_pdf if kind == "pdf" else prepare_image, data)).blocks
        except Exception:  # noqa: BLE001 - damaged/odd file: let the model read the original instead
            log.warning("local %s preparation failed; sending original", kind)
            blocks = [raw_block(data, mime)]
        out = await self._agent_turn(state, user_text=caption, media_blocks=blocks, display=f"{label} {caption}".strip(), msg=msg)
        if state.document and state.document.facts:
            self.store.media_put(self.tenant, digest, state.document.facts)
            self._remember_meta_hash(msg, path, mime, state.document.facts)
        return out

    def _remember_meta_hash(self, msg: Inbound, path: Path, mime: str, facts: dict[str, Any] | None) -> None:
        if msg.media_sha256 and facts:
            self.store.media_put(self.tenant, f"meta:{msg.media_sha256}",
                                 {"facts": facts, "path": str(path), "mime": mime, "filename": msg.filename})

    # ---- agent turn --------------------------------------------------
    async def _agent_turn(
        self,
        state: ConversationState,
        *,
        user_text: str,
        note: str | None = None,
        media_blocks: list[dict[str, Any]] | None = None,
        display: str | None = None,
        msg: Inbound | None = None,
    ) -> list[Outbound]:
        executor = ToolExecutor(
            self.erp, state, demo_label=getattr(self.erp, "label", None),
            aliases=(lambda k, a: self.store.alias_get(self.tenant, k, a), lambda k, a, v: self.store.alias_put(self.tenant, k, a, v)),
        )
        if self.brand:
            executor.brand_get = self.brand[0]
        if self.payments:
            executor.payments = lambda action, sender=state.sender: self.payments(action, sender)
        executor.memory = (lambda: self.store.memory_list(self.tenant),
                           lambda fact, about, sender=state.sender: self.store.memory_add(self.tenant, fact, about, sender),
                           lambda memory_id: self.store.memory_forget(self.tenant, memory_id))
        signals = self._signals(msg, state)  # first: a reply to an old photo re-selects that document
        if self._speaks(msg, state):
            note = f"{note} {SPEAK_NOTE}" if note else SPEAK_NOTE
        context = self._state_block(state) + signals
        judgment = await self.resolver.judge(context, user_text or "(document only)")
        if judgment:
            context += f"\nresolver_hint: {judgment.label} ({judgment.confidence:.2f})"
        body = f"<state>\n{context}\n</state>\n\n"
        if note:
            body += f"({note})\n"
        body += f"User: {user_text or '(no caption)'}"
        content: list[dict[str, Any]] = []
        content.extend(media_blocks or [])
        content.append({"type": "text", "text": body})
        messages = _history(state) + [{"role": "user", "content": content}]

        result = await self.llm.run_turn(
            system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SPECS, execute=executor.execute
        )
        effects = executor.effects
        out: list[Outbound] = []
        reply = result.text
        speech = None
        if reply and "SPEAK:" in reply:  # the spoken line asked for on a voice turn; never shown as text
            head, _, tail = reply.rpartition("SPEAK:")
            reply, speech = head.rstrip(), tail.strip().splitlines()[0].strip() if tail.strip() else None
        if result.stop_reason == "refusal" and not reply:
            reply = t("refusal", state.lang)
        if not reply and not effects.card and not effects.question:
            reply = t("unclear", state.lang)
        if effects.question and not effects.card:
            reply = ""  # the question card carries the message; avoid saying it twice
        if reply:
            out.append(text(reply[:4000]))
        for message in effects.messages:  # fixed text a tool must deliver exactly (a sign-in link)
            out.append(text(message))
        if effects.card and state.draft:
            out.append(self._card_from(state, effects.card))
        if speech and self._speaks(msg, state):
            audio = await self.speaker.speak(speech, state.lang)
            if audio:
                path = self.media_dir / "voice" / f"{_safe(msg.message_id)}.ogg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(audio)
                out.append(Outbound("audio", "", path=path, filename=path.name, meta={"kind": "voice_reply", "spoken": speech[:200]}))
        if effects.question:
            state.question = effects.question
            out.append(Outbound("buttons", effects.question.question[:1024], buttons=effects.question.options,
                                meta={"kind": "question", "question": effects.question.question}))

        state.add_turn("user", display if display is not None else (user_text or "(no caption)"), at=self._at(msg))
        assistant_memory = reply
        if effects.card:
            assistant_memory += f"\n[preview card sent]\n{effects.card}"
        if effects.question:
            options = " / ".join(o["title"] for o in effects.question.options)
            assistant_memory += f"\n[asked with buttons] {effects.question.question} ({options})"
        state.add_turn("assistant", assistant_memory, at=self.clock())
        for request in effects.requests:  # what people ask for that we can't do yet: the build list
            self._audit("feature_request", state.sender, {"request": request})
        self._audit(
            "turn",
            state.sender,
            {
                "tools": effects.tool_log,
                "stop": result.stop_reason,
                "iterations": result.iterations,
                "usage": result.usage,
                "hint": judgment.label if judgment else None,
            },
        )
        return out

    def _state_block(self, state: ConversationState) -> str:
        now = self.clock()
        lines = [f"now: {now_line(now)}", f"today: {getattr(self.erp, 'today', today_ist()).isoformat()}",
                 f"user_style: {styles.hint(state.style)}"]
        facts = self.store.memory_list(self.tenant)
        if facts:  # what the owner asked us to keep in mind; ERP data and tool results still win
            lines.append("business_memory: " + " | ".join(
                f"#{f['id']} {f['fact']}" + (f" (about {f['about']})" if f.get("about") else "") for f in facts))
        draft = state.draft
        if draft is None:
            lines.append("open_preview: none")
        elif draft.kind in KIND_DOCTYPE:
            rows = "; ".join(
                f"{line.item_name} [{line.item_code}] qty {line.qty} rate {line.rate} ({line.rate_source})" for line in draft.lines
            )
            origin = f" from {draft.source['doctype']} {draft.source['name']}" if draft.source else ""
            if draft.amend:
                origin += f" correcting {draft.amend['doctype']} {draft.amend['name']}"
            if draft.discount:
                origin += f" with {draft.discount}% discount"
            lines.append(
                f"open_preview: {KIND_LABEL[draft.kind]}{origin} for {draft.customer_name} [{draft.customer_id}] — {rows} (version {draft.version})"
            )
        elif draft.kind == "payment":
            lines.append(
                f"open_preview: Payment from {draft.customer_name} [{draft.customer_id}] amount {draft.payment_amount} mode {draft.payment_mode}"
            )
        elif draft.kind == "purchase":
            rows = "; ".join(f"{x.item_name} [{x.item_code}] qty {x.qty} rate {x.rate} ({x.rate_source})" for x in draft.lines)
            lines.append(f"open_preview: Purchase bill from supplier {draft.customer_name} [{draft.customer_id}] bill {draft.bill_no or '-'} "
                         f"dated {draft.bill_date or '-'} — {rows} (version {draft.version})")
        elif draft.kind == "credit_note":
            rows = "; ".join(f"{x.item_name} [{x.item_code}] qty {x.qty} rate {x.rate}" for x in draft.lines)
            lines.append(f"open_preview: Credit Note (return) against Sales Invoice {draft.source['name']} for {draft.customer_name} "
                         f"— {rows} (version {draft.version})")
        elif draft.kind == "bahi":
            lines.append("open_preview: Bahi page dues " + "; ".join(f"{r['customer']} due {r['due']}" for r in draft.entries))
        elif draft.kind == "brand":
            lines.append(f"open_preview: Bill design {(draft.brand or {}).get('primary_name')} {(draft.brand or {}).get('primary')}")
        elif draft.kind == "pricing":
            lines.append(f"open_preview: Permanent discount {draft.payment_amount}% for {draft.customer_name} (was {draft.note}%)")
        elif draft.kind == "repeat":
            lines.append(f"open_preview: Recurring copy of Sales Invoice {draft.source['name']} {draft.note}, first {draft.bill_date}")
        elif draft.kind == "cancel":
            lines.append(f"open_preview: Cancel {draft.source['doctype']} {draft.source['name']} (waiting for Approve)")
        elif draft.kind == "expense":
            lines.append(f"open_preview: Expense {draft.expense_account} amount {draft.payment_amount} paid by {draft.payment_mode} "
                         f"note {draft.note or '-'}")
        else:
            who = "supplier" if draft.kind == "new_supplier" else "customer"
            lines.append(f"open_preview: New {who} {json.dumps(draft.new_customer, ensure_ascii=False)}")
        if state.question:
            options = " / ".join(o["title"] for o in state.question.options)
            lines.append(f"pending_question: {state.question.question} (options: {options})")
        else:
            lines.append("pending_question: none")
        doc = state.document
        if doc:
            age = int((now - doc.received_at) / 60)
            facts = json.dumps(doc.facts, ensure_ascii=False) if doc.facts else "not read yet"
            lines.append(f"uploaded_document: {doc.filename or doc.mime}, {age} min ago, facts: {facts}")
        else:
            lines.append("uploaded_document: none")
        if state.next_steps:
            lines.append("queued_after_approve: " + " | ".join(state.next_steps))
        if state.last_created:
            recent = "; ".join(
                f"{d.get('doctype') or d['label']} {d['name']} ({d['customer']}, {'final' if d.get('submitted') else 'draft'}"
                + (f", from {d['from']}" if d.get("from") else "") + ")"
                for d in state.last_created[:3]
            )
            lines.append(f"recently_created: {recent}")
        return "\n".join(lines)

    def _speaks(self, msg: Inbound | None, state: ConversationState) -> bool:
        return bool(msg and msg.kind == "audio" and state.voice_replies and getattr(self.speaker, "available", False))

    def _at(self, msg: Inbound | None) -> float:
        """When the user actually sent it; falls back to now (and never in the future)."""
        now = self.clock()
        return min(msg.sent_at, now) if msg and msg.sent_at else now

    def _signals(self, msg: Inbound | None, state: ConversationState) -> str:
        """Everything WhatsApp told us about this message that matters for meaning."""
        if msg is None:
            return ""
        lines: list[str] = []
        now = self.clock()
        last_at = next((turn.get("at") for turn in reversed(state.turns) if turn.get("at")), None)
        if last_at and self._at(msg) - last_at > GAP_MARKER_SECONDS:
            lines.append(f"since_last_message: {gap(self._at(msg) - last_at)} (last activity {stamp(last_at)} IST)")
        if msg.sent_at and now - msg.sent_at > 120:
            lines.append(f"this_message_sent: {stamp(msg.sent_at)} IST ({gap(now - msg.sent_at)} ago; delivered late)")
        if msg.reply_to:
            lines.append(f"replied_to: {self._replied_to(msg, state)}")
        if msg.forwarded:
            lines.append("forwarded: yes" + (" (frequently forwarded)" if msg.frequently_forwarded else "")
                         + " — likely from someone else (supplier/customer)")
        if msg.kind == "audio":
            lines.append("input: " + ("voice note, transcribed on our server" if msg.voice else "audio file, transcribed"))
        if self.walog:
            last = self.walog.last_outbound(msg.sender)
            if last and last.status == "failed":
                lines.append(f"delivery_problem: our last message ({last.meta.get('kind', last.kind)}) did not reach the user")
        return ("\n" + "\n".join(lines)) if lines else ""

    def _replied_to(self, msg: Inbound, state: ConversationState) -> str:
        rec = self.walog.get(msg.reply_to) if self.walog else None
        if rec is None:
            return "an older message we have no record of"
        meta = rec.meta
        if rec.direction == "in":
            if meta.get("media_path"):
                facts = self.store.media_get(self.tenant, meta.get("digest", ""))
                state.document = DocumentContext(meta["media_path"], meta.get("mime", ""), meta.get("filename"), self.clock(), facts=facts)
                return f"the user's own earlier document {meta.get('filename') or ''} (now the selected uploaded_document)".strip()
            said = meta.get("transcript") or rec.text
            return f"the user's own earlier message ({stamp(rec.at)}): \"{said[:200]}\""
        kind = meta.get("kind", rec.kind)
        if kind == "card":
            draft = state.draft
            if draft and draft.id == meta.get("draft_id"):
                if draft.version == meta.get("version"):
                    return "the CURRENT open preview — apply their words to it"
                return f"an OLDER version (v{meta.get('version')}) of the open preview; the open one is v{draft.version}"
            done = next((d for d in state.last_created if d.get("draft_id") == meta.get("draft_id")), None)
            if done:
                return f"the preview that became {done['label']} {done['name']} (already created)"
            return f"a preview that was cancelled or expired ({meta.get('draft_kind')} for {meta.get('customer')})"
        if kind in {"document", "submit_offer", "submit_confirm"}:
            done = next((d for d in state.last_created if d.get("name") == meta.get("doc")), None)
            status = "submitted" if done and done.get("submitted") else "draft"
            return f"{meta.get('doctype')} {meta.get('doc')} for {meta.get('customer') or 'the customer'} ({status})"
        if kind == "question":
            return f"our question: \"{meta.get('question', rec.text)[:200]}\""
        return f"our earlier message ({stamp(rec.at)}): \"{rec.text[:200]}\""

    def _expire(self, state: ConversationState) -> None:
        now = self.clock()
        if state.document and now - state.document.received_at > DOCUMENT_FOCUS_SECONDS:
            state.document = None
        if state.question and now - state.question.asked_at > QUESTION_TTL_SECONDS:
            state.question = None
        if state.draft and now - state.draft.created_at > PREVIEW_TTL_SECONDS:
            state.draft = None  # a day-old preview must not be approvable

    def _audit(self, event: str, sender: str, data: dict[str, Any]) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            record = {"at": self.clock(), "event": event, "sender_hash": _hash(sender), **data}
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.warning("audit write failed")


def _freq_label(freq: str, lang: str | None) -> str:
    hi = {"Daily": "roz", "Weekly": "har hafte", "Monthly": "har mahine", "Quarterly": "har teen mahine",
          "Half-yearly": "har chhe mahine", "Yearly": "har saal"}
    return hi.get(freq, freq) if lang == "hi" else freq.lower()


def _approved_text(doc: Any, label: str, customer: str, can_submit: bool, lang: str | None) -> str:
    if not can_submit:
        return t("approved_staff", lang, label=label, name=doc.name, customer=customer)
    if doc.submit_failed or not doc.submitted:
        return t("approved_kept_draft", lang, label=label, name=doc.name)
    return t("approved_final", lang, label=label, name=doc.name, customer=customer)


def _history(state: ConversationState) -> list[dict[str, Any]]:
    turns = list(state.turns)
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if turns and turns[-1]["role"] == "user":
        turns.pop()  # an unanswered user turn (crash); the new turn supersedes it
    rendered, previous = [], None
    for turn in turns:
        at = turn.get("at")
        prefix = ""
        if at and turn["role"] == "user":  # stamping our own lines would teach the model to echo stamps
            if previous and at - previous > GAP_MARKER_SECONDS:
                prefix = f"[— {gap(at - previous)} later —] "
            prefix += f"[{stamp(at)}] "
            previous = at
        rendered.append({"role": turn["role"], "content": prefix + turn["text"]})
    return rendered



def _is_greeting(value: str) -> bool:
    words = value.lower().strip(" !.?👋🙏").split()
    return bool(words) and len(words) <= 3 and words[0] in {"hi", "hii", "hiii", "hello", "hey", "namaste", "hlo", "helo"}


def _safe(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in "-_")[:80] or "x"


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:12]
