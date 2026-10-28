from __future__ import annotations

from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.guards import BudgetedLLM, BudgetExceeded, UsageLedger, cost_usd
from munim.core.llm import AgentResult
from munim.core.state import StateStore
from munim.core.tenants import TenantStore


class PricedLLM:
    def __init__(self, usage: dict[str, int]) -> None:
        self.usage = usage
        self.calls = 0

    async def run_turn(self, *, system, messages, tools, execute) -> AgentResult:
        self.calls += 1
        return AgentResult("ok", "end_turn", 1, dict(self.usage))


def test_cost_for_haiku_and_unknown_model():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert cost_usd("claude-haiku-5-5", usage) == pytest.approx(0.60)
    assert cost_usd("some-new-model", usage) == pytest.approx(24.0)  # priced like Opus: safe side


async def test_budget_blocks_after_cap(tmp_path: Path):
    ledger = UsageLedger(tmp_path / "u.db")
    inner = PricedLLM({"input_tokens": 1_000_000})  # $0.10 per call on Haiku
    llm = BudgetedLLM(inner, ledger, daily_usd=0.15, model="claude-haiku-5-5")
    await llm.run_turn(system="", messages=[], tools=[], execute=None)
    await llm.run_turn(system="", messages=[], tools=[], execute=None)
    with pytest.raises(BudgetExceeded):
        await llm.run_turn(system="", messages=[], tools=[], execute=None)
    assert inner.calls == 2 and ledger.spent_today() == pytest.approx(0.20)


async def test_one_business_cannot_use_up_everyones_day(tmp_path: Path):
    ledger = UsageLedger(tmp_path / "u.db")
    inner = PricedLLM({"input_tokens": 1_000_000})  # $0.10 per call
    server = BudgetedLLM(inner, ledger, daily_usd=5.0, model="claude-haiku-5-5")
    busy, quiet = server.for_tenant("tenant-1", 0.15), server.for_tenant("tenant-2", 0.15)
    await busy.run_turn(system="", messages=[], tools=[], execute=None)
    await busy.for_feature("onboarding").run_turn(system="", messages=[], tools=[], execute=None)  # still this business
    with pytest.raises(BudgetExceeded):
        await busy.run_turn(system="", messages=[], tools=[], execute=None)
    await quiet.run_turn(system="", messages=[], tools=[], execute=None)  # the others carry on
    assert ledger.spent_today("tenant-1") == pytest.approx(0.20) and ledger.spent_today() == pytest.approx(0.30)


def test_old_ledger_gets_the_business_column(tmp_path: Path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "u.db")
    db.execute("CREATE TABLE usage (day TEXT, at REAL, feature TEXT, usd REAL)")
    db.execute("INSERT INTO usage VALUES ('2026-01-01', 0, 'agent', 1.0)")
    db.commit()
    db.close()
    ledger = UsageLedger(tmp_path / "u.db")
    ledger.record(0.5, "agent", "tenant-1")
    assert ledger.spent_today("tenant-1") == pytest.approx(0.5)


async def test_engine_answers_politely_when_budget_is_used(tmp_path: Path):
    ledger = UsageLedger(tmp_path / "u.db")
    ledger.record(5.0, "agent")
    llm = BudgetedLLM(PricedLLM({}), ledger, daily_usd=1.0, model="claude-haiku-5-5")
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=DemoERP(artifact_dir=tmp_path), llm=llm,
                         media_dir=tmp_path / "m", audit_path=tmp_path / "a.jsonl")
    await engine.handle(Inbound("m0", "919800000001", "text", text="Hi"))
    out = await engine.handle(Inbound("m1", "919800000001", "text", text="Gupta ji ka bill bana do"))
    assert "limit" in out[0].text


def test_daily_site_claims_counted(tmp_path: Path):
    tenants = TenantStore(tmp_path / "t.db")
    tenants.claim_site("911", ["tenant-1", "tenant-2"])
    assert tenants.claims_today() == 1 and tenants.has_claim("911") and not tenants.has_claim("912")


def test_no_false_promises_in_waitlist_text():
    from munim.core.i18n import STRINGS

    for key in ("ob.waitlist", "ob.pool_full"):
        for text in STRINGS[key].values():
            assert "message karunga" not in text and "I'll message" not in text and "bata dunga" not in text
