"""Cost guard: a daily spend ledger and an LLM wrapper that refuses once the budget is used.

Every model call (agent turns, onboarding extraction, column mapping) goes through
`BudgetedLLM`, so one place enforces the caps no matter which feature makes the call: one for the whole
server and one per business, so a single busy (or abused) business can't use up everyone's day.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from munim.core.llm import AgentLLM, AgentResult, ToolRunner

# USD per million tokens (input, output, cache read, cache write). Unknown models are
# priced like Opus so the guard errs on the safe side.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}
FALLBACK_PRICE = PRICES["claude-opus-5-5"]


class BudgetExceeded(RuntimeError):
    pass


def cost_usd(model: str, usage: dict[str, int]) -> float:
    p_in, p_out, p_read, p_write = PRICES.get(model, FALLBACK_PRICE)
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_read_input_tokens", 0) * p_read
        + usage.get("cache_creation_input_tokens", 0) * p_write
    ) / 1_000_000


class UsageLedger:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, at REAL, feature TEXT, usd REAL)")
        if "tenant" not in [r[1] for r in self._db.execute("PRAGMA table_info(usage)")]:
            self._db.execute("ALTER TABLE usage ADD COLUMN tenant TEXT")
        self._db.commit()

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def record(self, usd: float, feature: str, tenant: str | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO usage (day, at, feature, usd, tenant) VALUES (?, ?, ?, ?, ?)",
                             (self._today(), time.time(), feature, usd, tenant))
            self._db.commit()

    def spent_today(self, tenant: str | None = None) -> float:
        with self._lock:
            if tenant is None:
                row = self._db.execute("SELECT COALESCE(SUM(usd), 0) FROM usage WHERE day=?", (self._today(),)).fetchone()
            else:
                row = self._db.execute("SELECT COALESCE(SUM(usd), 0) FROM usage WHERE day=? AND tenant=?",
                                       (self._today(), tenant)).fetchone()
        return float(row[0])


class BudgetedLLM:
    """Wraps any AgentLLM: refuses when today's spend reached the cap, records spend after."""

    def __init__(self, inner: AgentLLM, ledger: UsageLedger, *, daily_usd: float, model: str, feature: str = "agent",
                 tenant: str | None = None, tenant_daily_usd: float | None = None) -> None:
        self.inner = inner
        self.ledger = ledger
        self.daily_usd = daily_usd
        self.model = model
        self.feature = feature
        self.tenant = tenant
        self.tenant_daily_usd = tenant_daily_usd

    def for_feature(self, feature: str) -> BudgetedLLM:
        return BudgetedLLM(self.inner, self.ledger, daily_usd=self.daily_usd, model=self.model, feature=feature,
                           tenant=self.tenant, tenant_daily_usd=self.tenant_daily_usd)

    def for_tenant(self, tenant: str, daily_usd: float) -> BudgetedLLM:
        """The same model, also capped per business (ERP site)."""
        return BudgetedLLM(self.inner, self.ledger, daily_usd=self.daily_usd, model=self.model, feature=self.feature,
                           tenant=tenant, tenant_daily_usd=daily_usd)

    async def run_turn(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                       execute: ToolRunner) -> AgentResult:
        if self.ledger.spent_today() >= self.daily_usd:
            raise BudgetExceeded(f"daily budget ${self.daily_usd:.2f} reached")
        if self.tenant and self.tenant_daily_usd is not None and self.ledger.spent_today(self.tenant) >= self.tenant_daily_usd:
            raise BudgetExceeded(f"daily budget for {self.tenant} reached")
        result = await self.inner.run_turn(system=system, messages=messages, tools=tools, execute=execute)
        self.ledger.record(cost_usd(self.model, result.usage), self.feature, self.tenant)
        return result
