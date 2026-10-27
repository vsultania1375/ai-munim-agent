"""Returns, cancel, correcting a final document, discounts (fake LLM, no cost). Rules checked live on ERPNext 16 + IC."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.state import StateStore
from tests.test_agent import ScriptedLLM

ME = "919800000001"


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "docs", today=date(2026, 10, 24))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m",
                         audit_path=tmp_path / "a.jsonl")
    return engine, llm, erp


def say(text: str, n: int = 1) -> Inbound:
    return Inbound(f"m{n}", ME, "text", text=text)


async def approve(engine, out, **kw):
    card = next(o for o in out if o.kind == "buttons")
    return await engine.handle(Inbound("tap-" + card.buttons[0]["id"], ME, "button", button_id=card.buttons[0]["id"]), **kw)


async def sell(engine, llm, qty: int = 10) -> str:
    llm.script([("propose_sales_document", {"kind": "invoice", "customer_id": "CUST-0001", "lines": [
        {"item_code": "CEM-50", "qty": qty, "rate_source": "price_list"}]})], "Preview")
    done = await approve(engine, await engine.handle(say("Gupta ji ka bill")))
    return done[0].meta["doc"]


async def test_part_return_credit_note_and_nothing_more_than_sold(setup):
    engine, llm, erp = setup
    bill = await sell(engine, llm)
    llm.script([("propose_return", {"invoice": bill}),
                ("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 2, "rate_source": "document"}]})], "Return preview.")
    out = await engine.handle(say("Gupta ji ne 2 bag wapas kiye", 2))
    card = out[-1].text
    assert "Credit Note (Maal wapsi)" in card and f"Sales Invoice {bill} ke against" in card and "2 Bag × ₹450" in card
    assert f"₹1,152 Sales Invoice {bill} ke baaki me se kam ho jaayega" in card
    done = await approve(engine, out)
    note = erp.documents[-1]
    assert note["is_return"] == 1 and note["return_against"] == bill and note["total"] == "-1152.00" and note["docstatus"] == 1
    assert "ban gaya" in done[0].text
    llm.script([("propose_return", {"invoice": bill})], "Baaki 8.")
    out = await engine.handle(say("baaki bhi wapas", 3))
    assert "8 Bag" in out[-1].text  # only what is still returnable


async def test_cancel_is_owner_only_and_blocked_by_linked_return(setup):
    engine, llm, erp = setup
    bill = await sell(engine, llm)
    llm.script([("propose_cancel", {"doctype": "Sales Invoice", "name": bill})], "...")
    await engine.handle(say("cancel karo", 2), can_submit=False)
    assert "only the owner" in llm.results[-1][0]["error"]
    llm.script([("propose_cancel", {"doctype": "Sales Invoice", "name": bill})], "Pakka?")
    out = await engine.handle(say("ye bill cancel karo", 3))
    assert "Ye document cancel karein?" in out[-1].text and "kuch delete nahi hota" in out[-1].text
    done = await approve(engine, out)
    assert erp.documents[0]["docstatus"] == 2 and "cancel ho gaya" in done[0].text

    second = await sell(engine, llm, 5)
    llm.script([("propose_return", {"invoice": second})], "Return.")
    await approve(engine, await engine.handle(say("sab wapas", 4)))
    llm.script([("propose_cancel", {"doctype": "Sales Invoice", "name": second})], "Pakka?")
    blocked = await approve(engine, await engine.handle(say("ise cancel karo", 5)))
    assert "cancel nahi hone diya" in blocked[0].text and "linked with a return" in blocked[0].text


async def test_correcting_a_final_bill_cancels_it_and_makes_the_right_one(setup):
    engine, llm, erp = setup
    bill = await sell(engine, llm)
    llm.script([("propose_correction", {"doctype": "Sales Invoice", "name": bill}),
                ("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 10, "rate": 430, "rate_source": "user_stated"}]}),
                ("edit_draft", {"kind": "quotation"})], "Sahi bill.")
    out = await engine.handle(say(f"{bill} me rate 430 karna tha", 2))
    assert "a correction keeps the document type" in llm.results[-1][2]["error"]
    card = out[-1].text
    assert f"*{bill}* ki jagah (purana cancel hoga)" in card and "10 Bag × ₹430" in card
    done = await approve(engine, out)
    old, new = erp.documents[0], erp.documents[-1]
    assert old["docstatus"] == 2 and new["amended_from"] == bill and new["docstatus"] == 1
    assert f"*{bill}* cancel karke sahi wala bana diya" in done[0].text


async def test_discount_percent_on_the_preview(setup):
    engine, llm, _ = setup
    llm.script([("propose_sales_document", {"kind": "quotation", "customer_id": "CUST-0001", "lines": [
        {"item_code": "CEM-50", "qty": 10, "rate_source": "price_list"}, {"item_code": "TMT-12", "qty": 3, "rate_source": "price_list"}]}),
        ("apply_discount", {"percent": 5, "item_codes": ["CEM-50"]})], "5% off.")
    out = await engine.handle(say("Gupta ji ko 10 cement 3 saria, cement pe 5% discount"))
    card = out[-1].text
    assert "10 Bag × ₹427.50" in card and "3 Kg × ₹65" in card and "Rates par 5% discount laga hai" in card
    assert llm.results[-1][1]["version"] == 2


async def test_permanent_customer_discount(setup):
    engine, llm, erp = setup
    llm.script([("set_customer_discount", {"customer_id": "CUST-0001", "percent": 5})], "...")
    await engine.handle(say("Gupta ji ko hamesha 5%"), can_submit=False)
    assert "only the owner" in llm.results[-1][0]["error"]
    llm.script([("set_customer_discount", {"customer_id": "CUST-0001", "percent": 60}),
                ("set_customer_discount", {"customer_id": "CUST-0001", "percent": 5})], "Preview.")
    out = await engine.handle(say("Gupta ji ko hamesha 5% discount dena", 2))
    assert "up to 50" in llm.results[-1][0]["error"]
    assert "Permanent discount" in out[-1].text and "*5%*" in out[-1].text
    done = await approve(engine, out)
    assert erp.discounts["CUST-0001"] == 5 and "har bill pe 5%" in done[0].text

    llm.script([("propose_sales_document", {"kind": "invoice", "customer_id": "CUST-0001", "lines": [
        {"item_code": "CEM-50", "qty": 10, "rate_source": "price_list"},
        {"item_code": "TMT-12", "qty": 10, "rate": 70, "rate_source": "user_stated"}]})], "Bill.")
    out = await engine.handle(say("Gupta ji ka bill 10 cement, saria 70 pe", 3))
    card = out[-1].text
    assert "10 Bag × ₹427.50" in card and "10 Kg × ₹70" in card  # list price gets 5% off; a stated rate stays
    assert "permanent 5% discount" in card

    llm.script([("set_customer_discount", {"customer_id": "CUST-0001", "percent": 0})], "Band.")
    await approve(engine, await engine.handle(say("Gupta ji ka discount band", 4)))
    assert erp.discounts["CUST-0001"] == 0
    llm.script([("propose_sales_document", {"kind": "invoice", "customer_id": "CUST-0001", "lines": [
        {"item_code": "CEM-50", "qty": 1, "rate_source": "price_list"}]})], "Bill.")
    out = await engine.handle(say("ek cement", 5))
    assert "1 Bag × ₹450" in out[-1].text and "permanent" not in out[-1].text
