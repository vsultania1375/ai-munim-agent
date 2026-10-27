"""How this person writes, measured in code so the bot can answer in kind: emojis or none, script, tone, length.

Each message nudges a running score (older messages fade), so one joke doesn't flip the bot into a comedian and
one emoji doesn't start an emoji storm. The model gets a plain hint; fixed messages lose their emojis in code when
the person writes without them.
"""
from __future__ import annotations

import re
from typing import Any

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF☀-➿⬀-⯿"
                      "\U0001F1E6-\U0001F1FF‍️⃣]+")
DEVANAGARI_RE = re.compile("[ऀ-ॿ]")
PLAYFUL = re.compile(r"\b(haha+|hehe+|lol|lmao|rofl|mast|majaa|maza|maze|jhakaas|bindaas|arre yaar|yaar|bhai|bro|dude)\b"
                     "|\U0001F602|\U0001F923|\U0001F61C|\U0001F60E|\U0001F604|\U0001F605", re.IGNORECASE)
FORMAL = re.compile(r"\b(please|kindly|kripya|regards|dear|sir|madam|request|thank you|dhanyavaad|aapka|aapki|aap)\b", re.IGNORECASE)
# markers that carry meaning become words or plain ticks (the ticks survive the emoji filter below)
KEEP_MEANING = {"⚠️": "Note:", "⚠": "Note:", "\U0001F195": "(new)", "✅": "✓", "❌": "✗"}
PLAIN_TICKS = "✓✗"
FADE = 0.7  # weight of the past against the newest message


def observe(style: dict[str, Any] | None, text: str) -> dict[str, Any]:
    """Fold one message into the running style."""
    s = dict(style or {})
    text = text or ""
    words = len(re.findall(r"\w+", text))
    if words == 0 and not EMOJI_RE.search(text):
        return s

    def mix(key: str, value: float) -> None:
        s[key] = value if key not in s else FADE * s[key] + (1 - FADE) * value

    mix("emoji", 1.0 if EMOJI_RE.search(text) else 0.0)
    mix("deva", 1.0 if DEVANAGARI_RE.search(text) else 0.0)
    mix("playful", 1.0 if PLAYFUL.search(text) or "!!" in text else 0.0)
    mix("formal", 1.0 if FORMAL.search(text) else 0.0)
    mix("words", float(min(words, 80)))
    s["seen"] = int(s.get("seen", 0)) + 1
    return s


def uses_emoji(style: dict[str, Any] | None) -> bool:
    return bool(style) and style.get("emoji", 0.0) >= 0.35  # one emoji among plain messages is not a habit


def hint(style: dict[str, Any] | None) -> str:
    """One line for the model: how to sound like a good reply to THIS person."""
    if not style:
        return "unknown yet: mirror their first message"
    parts = ["emojis ok, use a few like they do" if uses_emoji(style) else "no emojis at all"]
    if style.get("deva", 0) >= 0.5:
        parts.append("they write Hindi in Devanagari: reply in Devanagari")
    if style.get("playful", 0) >= 0.4:
        parts.append("they're casual and joking: be warm and witty back (never joke about the money itself)")
    elif style.get("formal", 0) >= 0.4:
        parts.append("they're polite/formal: respectful and plain")
    words = style.get("words", 15)
    parts.append("they write very short: answer in 1-2 lines" if words < 8 else
                 "they write in detail: a fuller answer is fine" if words > 35 else "keep it short")
    return "; ".join(parts)


def strip_emoji(text: str) -> str:
    for mark, word in KEEP_MEANING.items():
        text = text.replace(mark, word)
    text = EMOJI_RE.sub(lambda m: "".join(c for c in m.group() if c in PLAIN_TICKS), text)
    return re.sub(r"[ \t]{2,}", " ", "\n".join(line.strip() for line in text.split("\n"))).strip()
