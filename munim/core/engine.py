"""Agent Core v2 turn engine: one message in, a list of outbound messages out."""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from munim.core import style as styles
from munim.core.engine_approve import ApproveMixin
from munim.core.engine_common import (
    DOCUMENT_FOCUS_SECONDS,
    PREVIEW_TTL_SECONDS,
    QUESTION_TTL_SECONDS,
    RESET_WORDS,
    SPEAK_NOTE,
    VOICE_OFF,
    VOICE_ON,
    Inbound,
    Outbound,
    _hash,
    _history,
    _is_greeting,
    _safe,
    render_speech,
    text,
)
from munim.core.engine_context import ContextMixin
from munim.core.engine_media import MediaMixin
from munim.core.erp import ErpPort
from munim.core.guards import BudgetExceeded
from munim.core.i18n import detect_lang, kind_label, t
from munim.core.llm import AgentLLM
from munim.core.media import (
    NoTranscriber,
    Transcriber,
    parse_vcard,
)
from munim.core.prompt import SYSTEM_PROMPT
from munim.core.resolver import NullResolver, TurnResolver
from munim.core.state import ConversationState, StateStore
from munim.core.tools import TOOL_SPECS, ToolExecutor
from munim.core.walog import WALog

log = logging.getLogger("munim.core.engine")

__all__ = ["AgentEngine", "Inbound", "Outbound", "render_speech", "text"]  # imported from here by other modules


class AgentEngine(ApproveMixin, MediaMixin, ContextMixin):
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
    async def handle(self, msg: Inbound, *, can_submit: bool = True, early: Any = None) -> list[Outbound]:
        """One turn. `early` (optional async callback) gets messages worth sending before the turn ends."""
        state = self.store.load(self.tenant, msg.sender)
        state.can_submit = can_submit
        self._expire(state)

        async def send_early(item: Outbound) -> bool:
            if early is None:
                return False
            if item.text and not styles.uses_emoji(state.style):
                item.text = styles.strip_emoji(item.text)
            try:
                await early(item)
                return True
            except Exception:  # noqa: BLE001 - it goes with the reply instead
                log.warning("early send failed")
                return False

        try:
            out = await self._dispatch(msg, state, can_submit, send_early)
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

    async def _dispatch(self, msg: Inbound, state: ConversationState, can_submit: bool = True, early: Any = None) -> list[Outbound]:
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
            return await self._voice(msg, state, early)
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
        if speech and self._speaks(msg, state):  # spoken after the text is sent (render_voice), so text never waits
            out.append(Outbound("speech", speech[:600], meta={"kind": "voice_reply", "lang": state.lang, "id": _safe(msg.message_id)}))
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
