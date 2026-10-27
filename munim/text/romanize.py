from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol


class Transliterator(Protocol):
    def to_roman(self, text: str) -> str: ...


@dataclass(frozen=True)
class EntitySpan:
    raw: str
    roman: str
    start_token: int
    end_token: int


@dataclass(frozen=True)
class CanonicalVoiceCommand:
    raw_transcript: str
    display_transcript: str
    workflow_text: str
    detected_script: str
    normalized_quantities: dict[str, str]
    entity_spans: tuple[EntitySpan, ...]
    normalization_reason_codes: tuple[str, ...]


CanonicalVoiceTranscript = CanonicalVoiceCommand


_DEVANAGARI_START = "\u0900"
_DEVANAGARI_END = "\u097f"

_DOMAIN_TERMS = {
    "ji": "ji",
    "jee": "ji",
    "ko": "ko",
    "ka": "ka",
    "ke": "ke",
    "ki": "ki",
    "cement": "cement",
    "seement": "cement",
    "siment": "cement",
    "simant": "cement",
    "simnt": "cement",
    "bag": "bag",
    "baig": "bag",
    "beg": "bag",
    "bill": "bill",
    "bil": "bill",
    "bila": "bill",
    "invoice": "invoice",
    "quotation": "quotation",
    "quote": "quote",
    "estimate": "estimate",
    "banao": "bana do",
    "banaao": "bana do",
    "bana": "bana",
    "banado": "bana do",
    "bana do": "bana do",
    "outstanding": "outstanding",
    "outstainding": "outstanding",
    "autstainding": "outstanding",
    "autastainding": "outstanding",
    "bakaya": "outstanding",
    "baki": "baaki",
    "baaki": "baaki",
    "dikhao": "dikhao",
    "dikhau": "dikhao",
    "batao": "batao",
    "stock": "stock",
    "customer": "customer",
}

_QUANTITIES = {
    "ek": "1",
    "do": "2",
    "das": "10",
    "bee": "20",
    "bees": "20",
    "bis": "20",
    "tees": "30",
    "tis": "30",
    "pachaas": "50",
    "pachas": "50",
    "pchas": "50",
}
_DISPLAY_QUANTITIES = {
    "bee": "bees",
    "bis": "bees",
    "tis": "tees",
    "pchas": "pachaas",
    "pachas": "pachaas",
}

_INDEPENDENT_VOWELS = {
    "अ": "a",
    "आ": "a",
    "इ": "i",
    "ई": "ee",
    "उ": "u",
    "ऊ": "oo",
    "ए": "e",
    "ऐ": "ai",
    "ओ": "o",
    "औ": "au",
    "ऋ": "ri",
}
_CONSONANTS = {
    "क": "k",
    "ख": "kh",
    "ग": "g",
    "घ": "gh",
    "ङ": "ng",
    "च": "ch",
    "छ": "chh",
    "ज": "j",
    "झ": "jh",
    "ञ": "ny",
    "ट": "t",
    "ठ": "th",
    "ड": "d",
    "ढ": "dh",
    "ण": "n",
    "त": "t",
    "थ": "th",
    "द": "d",
    "ध": "dh",
    "न": "n",
    "प": "p",
    "फ": "ph",
    "ब": "b",
    "भ": "bh",
    "म": "m",
    "य": "y",
    "र": "r",
    "ल": "l",
    "व": "v",
    "श": "sh",
    "ष": "sh",
    "स": "s",
    "ह": "h",
    "ळ": "l",
}
_VOWEL_SIGNS = {
    "ा": "a",
    "ि": "i",
    "ी": "ee",
    "ु": "u",
    "ू": "oo",
    "े": "e",
    "ै": "ai",
    "ो": "o",
    "ौ": "au",
    "ृ": "ri",
}

_FUNCTION_WORDS = {
    "ji",
    "ko",
    "ka",
    "ke",
    "ki",
    "k",
    "aur",
    "se",
    "mein",
    "me",
    "bana",
    "do",
    "dikhao",
    "batao",
    "bill",
    "invoice",
    "quotation",
    "quote",
    "estimate",
    "cement",
    "bag",
    "outstanding",
    "stock",
    "customer",
    "baaki",
}


class DevanagariTransliterator:
    def to_roman(self, text: str) -> str:
        tokens = [_romanize_token(token) for token in _tokens(text)]
        return " ".join(token for token in tokens if token)


DEFAULT_TRANSLITERATOR = DevanagariTransliterator()


def canonicalize_voice_transcript(
    text: str,
    *,
    transliterator: Transliterator = DEFAULT_TRANSLITERATOR,
) -> CanonicalVoiceCommand:
    raw = unicodedata.normalize("NFC", text or "").strip()
    detected_script = detect_script(raw)
    roman = transliterator.to_roman(raw) if _has_devanagari(raw) else _space_text(raw)
    segmented = _segment_domain_tokens(roman)
    display_tokens, reason_codes = _normalize_domain_terms(segmented)
    display_tokens = _capitalize_probable_names(display_tokens)
    workflow_tokens, normalized_quantities = _normalize_quantities(display_tokens)
    entity_spans = _candidate_entity_spans(raw, display_tokens)

    return CanonicalVoiceCommand(
        raw_transcript=raw,
        display_transcript=" ".join(display_tokens),
        workflow_text=" ".join(workflow_tokens),
        detected_script=detected_script,
        normalized_quantities=normalized_quantities,
        entity_spans=tuple(entity_spans),
        normalization_reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def detect_script(text: str) -> str:
    has_devanagari = _has_devanagari(text)
    has_latin = any("A" <= character <= "Z" or "a" <= character <= "z" for character in text)
    if has_devanagari and has_latin:
        return "mixed_devanagari_latin"
    if has_devanagari:
        return "devanagari"
    if has_latin:
        return "latin"
    return "unknown"


def normalize_transliterated_search_form(value: str) -> str:
    roman = DEFAULT_TRANSLITERATOR.to_roman(value) if _has_devanagari(value) else _space_text(value)
    normalized, _ = _normalize_domain_terms(_segment_domain_tokens(roman), normalize_actions=False)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", " ".join(normalized).casefold())).strip()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w\u0900-\u097f]+", unicodedata.normalize("NFC", text), flags=re.UNICODE)


def _space_text(text: str) -> str:
    return " ".join(_tokens(text))


def _has_devanagari(text: str) -> bool:
    return any(_DEVANAGARI_START <= character <= _DEVANAGARI_END for character in text)


def _romanize_token(token: str) -> str:
    output: list[str] = []
    characters = list(token)
    index = 0
    while index < len(characters):
        character = characters[index]
        if character in _CONSONANTS:
            consonant = _CONSONANTS[character]
            next_character = characters[index + 1] if index + 1 < len(characters) else ""
            if next_character == "्":
                output.append(consonant)
                index += 2
                continue
            if next_character in _VOWEL_SIGNS:
                output.append(consonant + _VOWEL_SIGNS[next_character])
                index += 2
                continue
            output.append(consonant)
            if next_character and next_character != "्" and _is_devanagari_letter(next_character):
                output.append("a")
        elif character in _INDEPENDENT_VOWELS:
            output.append(_INDEPENDENT_VOWELS[character])
        elif character in _VOWEL_SIGNS and output:
            output[-1] = output[-1] + _VOWEL_SIGNS[character]
        elif character == "ं":
            output.append("n")
        elif character == "ः":
            output.append("h")
        elif character not in {"ँ", "़", "्"}:
            output.append(character)
        index += 1
    return "".join(output)


def _is_devanagari_letter(character: str) -> bool:
    return character in _CONSONANTS or character in _INDEPENDENT_VOWELS


def _segment_domain_tokens(text: str) -> list[str]:
    tokens = _tokens(text)
    segmented: list[str] = []
    for token in tokens:
        segmented.extend(_split_known_suffixes(token))
    return segmented


def _split_known_suffixes(token: str) -> list[str]:
    if not token:
        return []
    if token in _DOMAIN_TERMS or token in _QUANTITIES:
        return [token]
    suffixes = sorted((*_DOMAIN_TERMS, *_QUANTITIES), key=len, reverse=True)
    for suffix in suffixes:
        if len(token) <= len(suffix) + 1 or not token.endswith(suffix):
            continue
        prefix = token[: -len(suffix)]
        if prefix.endswith("a") and len(prefix) > 2:
            prefix = prefix[:-1]
        if prefix in _DOMAIN_TERMS or prefix in _QUANTITIES or len(prefix) >= 2:
            return [prefix, suffix]
    return [token]


def _normalize_domain_terms(tokens: list[str], *, normalize_actions: bool = True) -> tuple[list[str], list[str]]:
    output: list[str] = []
    reasons: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lower = token.casefold()
        two = f"{lower} {tokens[index + 1].casefold()}" if index + 1 < len(tokens) else ""
        mapped = _DOMAIN_TERMS.get(two)
        if mapped:
            output.extend(mapped.split())
            reasons.append("domain_phrase")
            index += 2
            continue
        mapped = _DOMAIN_TERMS.get(lower)
        if mapped and (normalize_actions or mapped != "bana do"):
            output.extend(mapped.split())
            reasons.append("domain_term")
        elif lower in _QUANTITIES:
            output.append(_DISPLAY_QUANTITIES.get(lower, lower))
            reasons.append("quantity_word")
        else:
            output.append(token)
        index += 1
    return output, reasons


def _normalize_quantities(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    output: list[str] = []
    quantities: dict[str, str] = {}
    for index, token in enumerate(tokens):
        lower = token.casefold()
        value = _QUANTITIES.get(lower)
        if value and not (lower == "do" and index and tokens[index - 1].casefold() == "bana"):
            output.append(value)
            quantities[token] = value
        else:
            output.append(token)
    return output, quantities


def _capitalize_probable_names(tokens: list[str]) -> list[str]:
    capitalized: list[str] = []
    for index, token in enumerate(tokens):
        previous = tokens[index - 1].casefold() if index else ""
        lower = token.casefold()
        if (
            token.isascii()
            and token.isalpha()
            and lower not in _FUNCTION_WORDS
            and lower not in _QUANTITIES
            and (index == 0 or previous in {"aur"})
        ):
            capitalized.append(token[:1].upper() + token[1:])
        else:
            capitalized.append(token)
    return capitalized


def _candidate_entity_spans(raw: str, display_tokens: list[str]) -> list[EntitySpan]:
    spans: list[EntitySpan] = []
    raw_tokens = _tokens(raw)
    for index, token in enumerate(display_tokens):
        lower = token.casefold()
        if lower in _FUNCTION_WORDS or lower in _QUANTITIES or lower.isdigit():
            continue
        raw_token = raw_tokens[index] if index < len(raw_tokens) else token
        spans.append(EntitySpan(raw=raw_token, roman=token, start_token=index, end_token=index + 1))
    return spans


def _is_domainish(token: str) -> bool:
    return token.isascii() and token.replace("_", "").isalnum()
