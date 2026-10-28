"""Voice notes, photos, documents and contact cards coming in; voice replies going out."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from munim.core import style as styles
from munim.core.engine_common import Inbound, Outbound, _safe, render_speech, text

log = logging.getLogger("munim.core.engine")
from munim.core.i18n import detect_lang, t
from munim.core.media import (
    MAX_AUDIO_BYTES,
    MAX_DOC_BYTES,
    NoTranscriber,
    file_hash,
    parse_vcard,
    prepare_image,
    prepare_pdf,
    raw_block,
    sniff,
)
from munim.core.state import ConversationState, DocumentContext, Draft


class MediaMixin:
    """Voice notes, photos, documents and contact cards coming in; voice replies going out."""

    # ---- media -------------------------------------------------------
    async def _voice(self, msg: Inbound, state: ConversationState, early: Any = None) -> list[Outbound]:
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
        first = [] if early is not None and await early(echo) else [echo]  # they see what we heard at once
        return [*first, *await self._agent_turn(state, user_text=heard, display=f"{label} {heard}", msg=msg)]

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

    async def render_voice(self, item: Outbound) -> Outbound | None:
        return await render_speech(self.speaker, self.media_dir, item)

    def _speaks(self, msg: Inbound | None, state: ConversationState) -> bool:
        return bool(msg and msg.kind == "audio" and state.voice_replies and getattr(self.speaker, "available", False))
