"""Purchases (supplier bills) and expenses (fake LLM, no cost). Input-GST rules checked live on ERPNext 16 + IC 16.11."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.reports import build
from munim.core.state import DocumentContext, StateStore
from tests.test_agent import ScriptedLLM

ME = "919800000001"


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "docs", today=date(2026, 10, 24))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m",
                         audit_path=tmp_path / "a.jsonl")
    return engine, llm, erp, tmp_path


def say(text: str, n: int = 1) -> Inbound:
    return Inbound(f"m{n}", ME, "text", text=text)


def bill(supplier: str, **extra) -> list:
    return [("propose_purchase_bill", {"supplier_id": supplier, "lines": [
        {"item_code": "CEM-50", "qty": 10, "rate": 380.5, "rate_source": "document"},
        {"item_code": "TMT-12", "qty": 50, "rate": 58.25, "rate_source": "document"}], **extra})]


async def approve(engine, out):
    card = next(o for o in out if o.kind == "buttons")
    return await engine.handle(Inbound("tap-" + card.buttons[0]["id"], ME, "button", button_id=card.buttons[0]["id"]))


async def test_forwarded_supplier_bill_becomes_purchase_with_input_gst_and_attachment(setup):
    engine, llm, erp, tmp = setup
    photo = tmp / "bill.jpg"
    photo.write_bytes(b"jpeg")
    state = engine.store.load("demo", ME)
    state.document = DocumentContext(str(photo), "image/jpeg", "bill.jpg", engine.clock())
    engine.store.save("demo", state)
    llm.script([("find_supplier", {"name": "Shree Cement"})] + bill("SUP-0001", bill_no="B-778", bill_date="2026-10-22"), "Kharid bill.")
    out = await engine.handle(say("ye supplier ka bill hai, entry kar do"))
    card = out[-1].text
    assert "Kharid Bill" in card and "Shree Cement Agency" in card and "B-778" in card and "2026-10-22" in card
    assert "GST: ₹1,589.64 (CGST ₹794.82 + SGST ₹794.82)" in card and "₹8,307.14" in card  # live ERPNext: 794.82 each
    assert "bheja bill saath me attach hoga" in card
    done = await approve(engine, out)
    doc = erp.documents[-1]
    assert doc["doctype"] == "Purchase Invoice" and doc["docstatus"] == 1 and doc["attached"] and doc["bill_no"] == "B-778"
    assert "ban gaya" in done[0].text


async def test_interstate_and_unregistered_suppliers(setup):
    engine, llm, _, _ = setup
    llm.script(bill("SUP-0002", attach_uploaded_document=False), "Preview")
    out = await engine.handle(say("Jaipur Steel ka bill"))
    assert "IGST ₹1,589.65" in out[-1].text and "📎" not in out[-1].text
    llm.script(bill("SUP-0003"), "Preview")
    out = await engine.handle(say("Raju ka bill", 2))
    assert "GST: nahi (supplier GST me registered nahi)" in out[-1].text and "₹6,717.50*" in out[-1].text


async def test_purchase_rejects_selling_price_and_cannot_become_a_sale(setup):
    engine, llm, _, _ = setup
    llm.script([("propose_purchase_bill", {"supplier_id": "SUP-0001", "lines": [
        {"item_code": "CEM-50", "qty": 10, "rate_source": "price_list"}]})] + bill("SUP-0001")
               + [("edit_draft", {"kind": "invoice"}), ("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 12, "rate_source": "document"}]})],
               "Preview")
    out = await engine.handle(say("Shree ka bill"))
    first, _, switch, qty = llm.results[-1]
    assert "rate on the supplier's bill" in first["error"] and "can't become a sales" in switch["error"]
    assert "error" not in qty and "12 Bag × ₹380.50" in out[-1].text  # the bill's rate stays


async def test_expense_head_is_checked_and_shows_in_profit(setup):
    engine, llm, erp, _ = setup
    llm.script([("propose_expense", {"account": "Petrol", "amount": 500, "mode": "Cash"}),
                ("propose_expense", {"account": "travel expenses", "amount": 500, "mode": "Cash", "note": "Petrol"})], "Kharcha.")
    out = await engine.handle(say("500 ka petrol dala cash me"))
    bad, good = llm.results[-1]
    assert "Travel Expenses" in bad["error"] and "error" not in good
    assert "Expense — Preview" in out[-1].text and "Travel Expenses" in out[-1].text and "₹500" in out[-1].text
    await approve(engine, out)
    assert erp.documents[-1]["doctype"] == "Journal Entry" and erp.documents[-1]["docstatus"] == 1
    profit = await build(erp, "profit", "this_month")
    assert profit["expenses"] == "₹500" and "note" not in profit


async def test_new_supplier_on_approve(setup):
    engine, llm, erp, _ = setup
    llm.script([("find_supplier", {"name": "Ambuja Depot"}), ("propose_new_supplier", {"name": "Ambuja Depot", "gstin": "27AAFCS4321M1Z6"})],
               "Naya supplier.")
    out = await engine.handle(say("Ambuja Depot naya supplier hai"))
    assert "New Supplier — Preview" in out[-1].text and "add the supplier" in out[-1].text
    await approve(engine, out)
    assert any(s.name == "Ambuja Depot" for s in erp.suppliers.values())


async def test_paying_a_supplier_settles_oldest_bills_first(setup):
    engine, llm, erp, _ = setup
    for qty in (10, 4):  # two final bills from Shree Cement
        llm.script([("propose_purchase_bill", {"supplier_id": "SUP-0001", "lines": [
            {"item_code": "CEM-50", "qty": qty, "rate": 380, "rate_source": "document"}]})], "Preview")
        await approve(engine, await engine.handle(say(f"Shree ka bill {qty}", qty)))
    first, second = [d for d in erp.documents if d["doctype"] == "Purchase Invoice"]
    llm.script([("propose_supplier_payment", {"supplier_id": "SUP-0001", "amount": 5000, "mode": "UPI"})], "Preview")
    out = await engine.handle(say("Shree Cement ko 5000 UPI se diye", 20))
    card = out[-1].text
    assert "Supplier ko Payment" in card and f"{first['name']}: ₹" in card and second["name"] in card
    await approve(engine, out)
    owed = {b.name: b.outstanding for b in await erp.supplier_outstanding("SUP-0001")}
    assert first["name"] not in owed  # the older bill is fully paid
    assert owed[second["name"]] == Decimal(first["total"]) + Decimal(second["total"]) - 5000


async def test_goods_back_to_the_supplier_make_a_debit_note(setup):
    engine, llm, erp, _ = setup
    await approve(engine, (llm.script(bill("SUP-0001"), "Preview"), await engine.handle(say("Shree ka bill")))[1])
    pinv = next(d for d in erp.documents if d["doctype"] == "Purchase Invoice")
    llm.script([("propose_purchase_return", {"invoice": pinv["name"], "lines": [{"item_code": "CEM-50", "qty": 11}]}),
                ("propose_purchase_return", {"invoice": pinv["name"], "lines": [{"item_code": "CEM-50", "qty": 3}]})], "Preview")
    out = await engine.handle(say("Shree ko 3 bag wapas bheje", 2))
    too_many, ok = llm.results[-1]
    assert "only 10" in too_many["error"] and "error" not in ok
    card = out[-1].text
    assert "Debit Note" in card and "3 Bag × ₹380.50" in card and pinv["name"] in card
    await approve(engine, out)
    ret = erp.documents[-1]
    assert ret["is_return"] == 1 and ret["return_against"] == pinv["name"] and ret["docstatus"] == 1
    left = {x.item_code: x.qty for x in (await erp.map_purchase_return(pinv["name"])).lines}
    assert left["CEM-50"] == 7  # 3 of 10 went back


async def test_stock_from_bills_when_no_warehouse_stock(setup):
    from munim.core.erp import DraftLine

    _, _, erp, _ = setup
    await erp.create_purchase_invoice("SUP-0001", [DraftLine("CEM-50", Decimal(50), Decimal(380))], None, None, None, "x", final=True)
    await erp.create_sales_draft("Sales Invoice", "CUST-0001", [DraftLine("CEM-50", Decimal(20), Decimal(450))], "y", final=True)
    report = await build(erp, "stock", "today")
    assert report["stock"] == [{"item": "Cement Bag 50 KG", "qty": "30"}] and "bills" in report["basis"]
