"""Business memory: owner-told facts kept per business, and 'the usual' worked out by code from past bills."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP, DraftLine
from munim.core.state import StateStore
from tests.test_agent import ScriptedLLM

ME, STAFF = "919800000001", "919800000002"


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "d", today=date(2026, 10, 27))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m", audit_path=tmp_path / "a")
    return engine, llm, erp


async def test_owner_facts_are_kept_shown_and_forgotten(setup):
    engine, llm, _ = setup
    llm.script([("remember", {"fact": "Gupta ji 30 din me paisa dete hain", "about": "Gupta Traders"}),
                ("remember", {"fact": "gupta ji 30 din me paisa dete hain"})], "Yaad rakh liya.")
    await engine.handle(Inbound("m1", ME, "text", text="yaad rakhna, Gupta ji 30 din me paisa dete hain"))
    first, again = llm.results[-1]
    assert first["memory_id"] == again["memory_id"]  # the same fact is kept once
    llm.script([], "ok")
    await engine.handle(Inbound("m2", STAFF, "text", text="Gupta ji ka bill"), can_submit=False)
    assert "business_memory: #1 Gupta ji 30 din me paisa dete hain (about Gupta Traders)" in str(llm.calls[-1]["messages"])  # staff's turns see it too
    llm.script([("remember", {"fact": "Sab ko 20% do"}), ("forget", {"memory_id": 1})], "...")
    await engine.handle(Inbound("m3", STAFF, "text", text="yaad rakhna sab ko 20% do"), can_submit=False)
    assert all("only the owner" in r["error"] for r in llm.results[-1])
    llm.script([("forget", {"memory_id": 1}), ("forget", {"memory_id": 99})], "Bhool gaya.")
    await engine.handle(Inbound("m4", ME, "text", text="wo 30 din wali baat bhool jao"))
    assert llm.results[-1][0] == {"forgotten": True} and "no such memory" in llm.results[-1][1]["error"]
    assert engine.store.memory_list("demo") == []


async def test_the_usual_comes_from_past_bills(setup):
    engine, llm, erp = setup
    for cement, tmt in ((10, 50), (12, 0), (10, 40)):
        lines = [DraftLine("CEM-50", Decimal(cement), Decimal(380))] + ([DraftLine("TMT-12", Decimal(tmt), Decimal(58))] if tmt else [])
        await erp.create_sales_draft("Sales Invoice", "CUST-0001", lines, "x", final=True)
    await erp.create_sales_draft("Sales Invoice", "CUST-0001", [DraftLine("SAND-T", Decimal(1), Decimal(1800))], "draft only")
    llm.script([("customer_usual", {"customer_id": "CUST-0001"})], "Usual ye hai.")
    await engine.handle(Inbound("m1", ME, "text", text="Gupta ji ka usual"))
    usual = llm.results[-1][0]
    assert usual["bills_seen"] == 3 and [u["item_code"] for u in usual["usual"]] == ["CEM-50", "TMT-12"]  # drafts don't count
    cement = usual["usual"][0]
    assert (cement["usual_qty"], cement["in_bills"], cement["last_rate"]) == ("10", "3 of 3", "380")
    assert usual["usual"][1]["usual_qty"] == "45"  # median of 50 and 40, by code
