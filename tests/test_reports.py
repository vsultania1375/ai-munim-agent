"""Business questions: India periods resolved by code, every figure summed by code (fake LLM, no cost)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.reports import ReportError, build, period_range
from munim.core.state import StateStore
from tests.test_agent import ScriptedLLM

ME = "919800000001"
MON = date(2026, 10, 26)


def test_india_periods():
    assert period_range("this_week", MON) == (MON, MON)
    assert period_range("last_week", MON) == (date(2026, 10, 19), date(2026, 10, 25))
    assert period_range("last_month", MON) == (date(2026, 9, 1), date(2026, 9, 30))
    assert period_range("this_quarter", MON) == (date(2026, 10, 1), MON)
    assert period_range("this_fy", MON) == (date(2026, 4, 1), MON)  # financial year starts 1 April
    assert period_range("last_fy", date(2026, 2, 10)) == (date(2024, 4, 1), date(2025, 3, 31))
    assert period_range("custom", MON, "2026-10-01", "2026-10-10") == (date(2026, 10, 1), date(2026, 10, 10))
    with pytest.raises(ReportError):
        period_range("custom", MON, "2026-10-10", "2026-10-01")


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "docs", today=date(2026, 10, 24))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m",
                         audit_path=tmp_path / "a.jsonl")
    return engine, llm, erp


async def bill(engine, llm, customer: str, lines: list[tuple[str, int]], n: int) -> None:
    llm.script([("propose_sales_document", {"kind": "invoice", "customer_id": customer, "lines": [
        {"item_code": c, "qty": q, "rate_source": "price_list"} for c, q in lines]})], "Preview")
    out = await engine.handle(Inbound(f"m{n}", ME, "text", text="bill"))
    card = next(o for o in out if o.kind == "buttons")
    await engine.handle(Inbound(f"a{n}", ME, "button", button_id=card.buttons[0]["id"]))


async def test_sales_items_customers_profit_from_the_books(setup):
    engine, llm, erp = setup
    await bill(engine, llm, "CUST-0001", [("CEM-50", 10), ("TMT-12", 100)], 1)  # 4,500 @28% + 6,500 @18%
    await bill(engine, llm, "CUST-0002", [("CEM-50", 4)], 2)  # 1,800 @28% IGST
    sales = await build(erp, "sales", "this_month")
    assert sales["bills"] == 2 and sales["sales_before_gst"] == "₹12,800" and sales["gst"] == "₹2,934"
    assert sales["total_billed"] == "₹15,734" and sales["period"] == "01 Oct 2026 – 24 Oct 2026"
    items = await build(erp, "top_items", "this_month")
    assert items["items"][0] == {"item": "TMT Steel Bar 12mm", "qty": "100", "sales_before_gst": "₹6,500"}
    assert items["items"][1]["qty"] == "14"
    top = await build(erp, "top_customers", "this_month")
    assert top["customers"][0] == {"customer": "Gupta Traders", "billed": "₹13,430", "bills": 1}
    profit = await build(erp, "profit", "this_month")
    assert profit["income"] == "₹12,800" and "income only" in profit["note"]
    assert (await build(erp, "sales", "last_month"))["bills"] == 0


async def test_receivables_by_age(setup):
    _, _, erp = setup
    due = await build(erp, "receivables")
    assert due["total_due"] == "₹18,260" and due["customers_with_dues"] == 2
    assert due["by_age"] == {"not_due": "₹3,260", "1_30_days": "₹15,000"}
    assert due["top"][0] == {"customer": "Gupta Traders", "due": "₹15,000", "most_overdue_days": 20}


async def test_collections_and_pending(setup):
    engine, llm, erp = setup
    llm.script([("propose_payment", {"customer_id": "CUST-0001", "amount": 5000, "mode": "UPI"})], "Preview")
    out = await engine.handle(Inbound("p1", ME, "text", text="Gupta ji se 5000 UPI"))
    card = next(o for o in out if o.kind == "buttons")
    await engine.handle(Inbound("p2", ME, "button", button_id=card.buttons[0]["id"]))
    got = await build(erp, "collections", "today")
    assert got == {"report": "collections", "period": "24 Oct 2026", "received": "₹5,000", "payments": 1, "by_mode": {"UPI": "₹5,000"}}
    assert (await build(erp, "stock"))["note"].startswith("no stock-tracked")


async def test_model_gets_report_through_the_tool(setup):
    engine, llm, _ = setup
    llm.script([("business_report", {"report": "receivables"}), ("business_report", {"report": "sales", "period": "kal"})],
               "Kul ₹18,260 baaki hai.")
    await engine.handle(Inbound("q", ME, "text", text="kis kis pe kitna baaki hai?"))
    first, second = llm.results[-1]
    assert first["total_due"] == "₹18,260" and "period must be one of" in second["error"]
