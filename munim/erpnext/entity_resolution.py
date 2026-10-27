from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

from munim.text.romanize import normalize_transliterated_search_form


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    INVALID = "invalid"


@dataclass(frozen=True)
class EntityCandidate:
    id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class ResolutionResult:
    status: ResolutionStatus
    query: str
    matches: tuple[EntityCandidate, ...] = ()
    score: float = 0.0

    @property
    def resolved(self) -> EntityCandidate | None:
        return self.matches[0] if self.status == ResolutionStatus.RESOLVED else None


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    return normalize_transliterated_search_form(value)


def candidate_search_forms(candidate: EntityCandidate) -> tuple[str, ...]:
    forms = {
        normalize_name(candidate.display_name),
        *(normalize_name(alias) for alias in candidate.aliases),
    }
    return tuple(form for form in forms if form)


def resolve_entity(
    query: str,
    candidates: Iterable[EntityCandidate],
    *,
    threshold: float = 82.0,
    ambiguity_margin: float = 5.0,
) -> ResolutionResult:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return ResolutionResult(ResolutionStatus.INVALID, query)

    candidate_list = tuple(candidates)
    exact = [
        c
        for c in candidate_list
        if normalized_query in candidate_search_forms(c)
    ]
    if len(exact) == 1:
        return ResolutionResult(ResolutionStatus.RESOLVED, query, (exact[0],), 100.0)
    if len(exact) > 1:
        return ResolutionResult(ResolutionStatus.AMBIGUOUS, query, tuple(exact), 100.0)

    scored = sorted(
        (
            (
                max(fuzz.token_set_ratio(normalized_query, name) for name in candidate_search_forms(c)),
                c,
            )
            for c in candidate_list
        ),
        reverse=True,
        key=lambda item: item[0],
    )
    viable = [(score, candidate) for score, candidate in scored if score >= threshold]
    if not viable:
        return ResolutionResult(ResolutionStatus.NOT_FOUND, query)
    top_score = viable[0][0]
    top = tuple(candidate for score, candidate in viable if top_score - score <= ambiguity_margin)
    if len(top) == 1:
        return ResolutionResult(ResolutionStatus.RESOLVED, query, top, top_score)
    return ResolutionResult(ResolutionStatus.AMBIGUOUS, query, top, top_score)
