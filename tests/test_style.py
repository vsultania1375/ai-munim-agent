"""The bot answers in the person's style: emojis or none, script, tone, length (code-measured, no model)."""
from __future__ import annotations

from munim.core.style import hint, observe, strip_emoji, uses_emoji


def feed(*messages: str) -> dict:
    s: dict = {}
    for m in messages:
        s = observe(s, m)
    return s


def test_plain_writer_gets_no_emojis_and_meaning_survives():
    s = feed("Gupta ka bill bana do", "10 bag cement")
    assert not uses_emoji(s) and "no emojis" in hint(s)
    assert strip_emoji("✅ Bill ban gaya 🎉\n⚠️ GST nahi laga\nRamesh 🆕") == "✓ Bill ban gaya\nNote: GST nahi laga\nRamesh (new)"


def test_emoji_and_joking_writer():
    s = feed("haha bhai mast 😂", "bill bana do yaar 🔥")
    assert uses_emoji(s) and "witty" in hint(s) and "very short" in hint(s)


def test_formal_devanagari_and_long_writer():
    s = feed("कृपया गुप्ता जी का बिल बना दीजिए, aap please dhyaan dijiye " + "विवरण " * 40)
    h = hint(s)
    assert "Devanagari" in h and "formal" in h and "detail" in h


def test_one_emoji_does_not_flip_a_plain_writer():
    s = feed("bill", "rate batao", "theek hai", "ok 👍")
    assert not uses_emoji(s)
