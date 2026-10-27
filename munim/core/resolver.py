"""Turn resolver: a fast judgment of what the incoming message is about (TypeSafe Jev).

Advisory only. The agent receives the label and confidence as a hint; a resolver
failure never blocks the turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

TURN_LABELS: dict[str, str] = {
    "about_open_preview": "Changes, approves, rejects or asks about the quotation/invoice/payment preview that is currently open",
    "answer_to_question": "Answers the question the assistant just asked (including short replies like haan, nahi, 1, ok)",
    "about_document": "Refers to the document or photo the user uploaded recently (iska, isse, is bill ka)",
    "new_business_request": "A new accounting/billing request: quotation, bill, challan, payment, outstanding, rate, customer",
    "small_talk": "Greeting, thanks, how are you, help, what can you do",
    "off_topic": "Not about this business assistant (coding, horoscope, flirting, abuse, general knowledge)",
}


@dataclass(frozen=True)
class TurnJudgment:
    label: str
    confidence: float


class TurnResolver(Protocol):
    async def judge(self, context: str, message: str) -> TurnJudgment | None: ...


class NullResolver:
    async def judge(self, context: str, message: str) -> TurnJudgment | None:
        return None


class JevTurnResolver:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "jev-latest",
        base_url: str = "https://api.typesafe.ai",
        timeout: float = 4.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.url = f"{base_url.rstrip('/')}/v1/systemone"
        self.http = http or httpx.AsyncClient(timeout=timeout)

    async def judge(self, context: str, message: str) -> TurnJudgment | None:
        body: dict[str, Any] = {
            "model": self.model,
            "state": f"{context}\n\nNEW USER MESSAGE: {message}",
            "questions": {
                "turn": {
                    "type": "choice",
                    "instructions": "What is the new user message about, given the conversation state?",
                    "criteria": TURN_LABELS,
                }
            },
        }
        try:
            response = await self.http.post(
                self.url, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
            )
            response.raise_for_status()
            answer = response.json()["answers"]["turn"]
            label = str(answer.get("choice"))
            if label not in TURN_LABELS:
                return None
            return TurnJudgment(label, float(answer.get("confidence") or 0.0))
        except Exception as exc:  # noqa: BLE001 - advisory, never break the turn
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning("jev resolver failed: %s%s", type(exc).__name__, f" {status}" if status else "")
            return None
