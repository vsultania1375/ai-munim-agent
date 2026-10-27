"""Meta WhatsApp Cloud API webhook parsing: every inbound field we can use, plus delivery statuses."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MEDIA_KINDS = ("audio", "image", "document", "video", "sticker")


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    sender: str
    kind: str
    business_phone_number_id: str | None = None
    timestamp: datetime | None = None  # when the user sent it (not when we processed it)
    text: str | None = None
    button_payload: str | None = None
    list_id: str | None = None
    context_message_id: str | None = None  # the message this one replies to (swipe-reply)
    media_id: str | None = None
    caption: str | None = None
    filename: str | None = None
    declared_mime: str | None = None
    extra: str | None = None  # JSON for contacts/location messages
    context_from: str | None = None  # who sent the replied-to message
    forwarded: bool = False
    frequently_forwarded: bool = False
    voice: bool = False  # audio recorded as a voice note (vs an audio file)
    media_sha256: str | None = None
    reply_title: str | None = None  # visible title of the tapped button/list row
    reaction_to: str | None = None
    reaction_emoji: str | None = None
    referral: str | None = None  # JSON: the ad/post the user came from
    new_wa_id: str | None = None  # system message: user changed their number
    profile_name: str | None = None


@dataclass(frozen=True)
class StatusEvent:
    message_id: str  # our outbound wamid
    status: str  # sent | delivered | read | failed
    recipient: str
    timestamp: datetime | None = None
    error_code: int | None = None
    error_title: str | None = None
    category: str | None = None  # pricing/conversation category when given


def verify_meta_signature(body: bytes, signature: str | None, app_secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _values(payload: dict[str, Any]):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            yield change.get("value", {})


def parse_webhook_payload(raw: str | bytes) -> list[IncomingMessage]:
    payload = json.loads(raw)
    messages: list[IncomingMessage] = []
    for value in _values(payload):
        business_phone_number_id = value.get("metadata", {}).get("phone_number_id")
        names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
        for msg in value.get("messages", []):
            kind = msg.get("type", "unknown")
            context = msg.get("context", {}) or {}
            interactive = msg.get("interactive", {}) or {}
            button_reply = interactive.get("button_reply") or {}
            list_reply = interactive.get("list_reply") or {}
            template_button = msg.get("button") or {}
            reaction = msg.get("reaction") or {}
            system = msg.get("system") or {}
            messages.append(
                IncomingMessage(
                    message_id=msg["id"],
                    sender=msg["from"],
                    kind=kind,
                    business_phone_number_id=business_phone_number_id,
                    timestamp=_timestamp(msg.get("timestamp")),
                    text=(msg.get("text") or {}).get("body") or template_button.get("text"),
                    button_payload=button_reply.get("id") or template_button.get("payload"),
                    list_id=list_reply.get("id"),
                    context_message_id=context.get("id"),
                    media_id=_media_field(msg, "id"),
                    caption=_media_field(msg, "caption"),
                    filename=_media_field(msg, "filename"),
                    declared_mime=_media_field(msg, "mime_type"),
                    extra=json.dumps(msg[kind]) if kind in {"contacts", "location"} and kind in msg else None,
                    context_from=context.get("from"),
                    forwarded=bool(context.get("forwarded") or context.get("frequently_forwarded")),
                    frequently_forwarded=bool(context.get("frequently_forwarded")),
                    voice=bool((msg.get("audio") or {}).get("voice")),
                    media_sha256=_media_field(msg, "sha256"),
                    reply_title=button_reply.get("title") or list_reply.get("title"),
                    reaction_to=reaction.get("message_id"),
                    reaction_emoji=reaction.get("emoji"),
                    referral=json.dumps(msg["referral"]) if msg.get("referral") else None,
                    new_wa_id=system.get("wa_id") or system.get("new_wa_id"),
                    profile_name=names.get(msg["from"]),
                )
            )
    return messages


def parse_statuses(raw: str | bytes) -> list[StatusEvent]:
    payload = json.loads(raw)
    events: list[StatusEvent] = []
    for value in _values(payload):
        for st in value.get("statuses", []):
            error = (st.get("errors") or [{}])[0]
            events.append(
                StatusEvent(
                    message_id=st.get("id", ""),
                    status=st.get("status", ""),
                    recipient=st.get("recipient_id", ""),
                    timestamp=_timestamp(st.get("timestamp")),
                    error_code=error.get("code"),
                    error_title=error.get("title") or error.get("message"),
                    category=(st.get("pricing") or {}).get("category") or ((st.get("conversation") or {}).get("origin") or {}).get("type"),
                )
            )
    return events


def _timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError, OSError):
        return None


def _media_field(message: dict[str, Any], field: str) -> str | None:
    for key in MEDIA_KINDS:
        media = message.get(key)
        if not isinstance(media, dict):
            continue
        value = media.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
