"""Bahi page photo: the model copies entries, code nets each party, Approve books old dues (fake LLM, no cost)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.state import DocumentContext, StateStore
from tests.test_agent import ScriptedLLM

ME = "919800000001"
PAGE = [
    {"party": "Gupta ji", "type": "udhaar", "amount": 4500},
    {"party": "Gupta ji", "type": "received", "amount": 2000},
    {"party": "Gupta ji", "type": "sale", "amount": 1250.5},
    {"party": "Ramesh Kirana", "type": "udhaar", "amount": 800},
    {"party": "Verma ji", "type": "received", "amount": 300},
    {"party": "", "type": "udhaar", "amount": 99},
    {"party": "Kuch", "type": "kharcha", "amount": 50},
]


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "d", today=date(2026, 10, 27))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m", audit_path=tmp_path / "a")
    page = tmp_path / "bahi.jpg"
    page.write_bytes(b"\xff\xd8\xff\xe0page")
    state = engine.store.load("demo", ME)
    state.document = DocumentContext(str(page), "image/jpeg", "bahi.jpg", engine.clock())
    engine.store.save("demo", state)
    return engine, llm, erp


async def test_page_is_netted_by_code_and_booked_with_the_photo(setup):
    engine, llm, erp = setup
    llm.script([("propose_bahi_import", {"entries": PAGE})], "Bahi preview.")
    out = await engine.handle(Inbound("m1", ME, "text", text="ye purani bahi ka panna hai, khaate me chadha do"))
    card = out[-1].text
    assert "Gupta Traders: diya ₹5,750.50, jama ₹2,000 → baaki ₹3,750.50" in card  # resolved to the existing customer
    assert "Ramesh Kirana (new)" in card and "Verma Hardware: diya ₹0, jama ₹300 → baaki -₹300" in card  # no emojis: they write without them
    assert "Purane baaki khaate me: ₹4,550.50 (2 party)" in card and "2 line saaf nahi thi" in card
    assert llm.results[-1][0]["new_customers_on_approve"] == ["Ramesh Kirana"]
    button = next(o for o in out if o.kind == "buttons").buttons[0]["id"]
    done = await engine.handle(Inbound("a1", ME, "button", button_id=button))
    booked = [d for d in erp.documents if d.get("is_opening") == "Yes"]
    assert [(erp.customers[d["customer_id"]].name, d["total"], d["attached"]) for d in booked] == [
        ("Gupta Traders", "3750.50", True), ("Ramesh Kirana", "800.00", True)]  # Verma's advance is not a due
    assert "2 purane baaki" in done[0].text and "1 naye customer" in done[0].text
    dues = {i.customer_id: i.outstanding for i in await erp.customer_outstanding("CUST-0001")}
    assert any(str(v) == "3750.50" for v in dues.values())  # payments now settle it


async def test_only_the_owner_books_a_bahi(setup):
    engine, llm, _ = setup
    llm.script([("propose_bahi_import", {"entries": PAGE[:1]})], "...")
    await engine.handle(Inbound("m1", ME, "text", text="bahi"), can_submit=False)
    assert "only the owner" in llm.results[-1][0]["error"]
