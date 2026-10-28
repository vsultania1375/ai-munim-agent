"""What the turn engine's parts share: message types in and out, time limits, small helpers."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from munim.core.clock import gap, stamp
from munim.core.i18n import t
from munim.core.state import ConversationState

log = logging.getLogger("munim.core.engine")  # one logger for the whole engine


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


async def render_speech(speaker: Any, media_dir: Path, item: Outbound) -> Outbound | None:
    """A deferred voice reply ('speech') -> an OGG voice note on disk, or None if speech isn't available."""
    if speaker is None or not item.text:
        return None
    audio = await speaker.speak(item.text, item.meta.get("lang"))
    if not audio:
        return None
    path = media_dir / "voice" / f"{item.meta.get('id') or uuid.uuid4().hex}.ogg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
    return Outbound("audio", "", path=path, filename=path.name, meta={"kind": "voice_reply", "spoken": item.text[:200]})


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
