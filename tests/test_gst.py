"""GST as India Compliance applies it (fake LLM, no cost). Numbers checked against live ERPNext 16 + IC 16.11."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from munim.core.drafts import compute_lines
from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.gst import TAX_INTER, TAX_INTRA, TAX_NONE, clean_hsn, tax_mode
from munim.core.state import StateStore
from tests.test_agent import ScriptedLLM

ME = "919800000001"
HOME = "27AAICA1234B1ZA"  # Maharashtra


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "docs", today=date(2026, 10, 24))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m",
                         audit_path=tmp_path / "a.jsonl")
    return engine, llm, erp


def say(text: str, n: int = 1) -> Inbound:
    return Inbound(f"m{n}", ME, "text", text=text)


def propose(customer: str, lines: list[tuple[str, int]], kind: str = "invoice") -> list:
    return [("propose_sales_document", {"kind": kind, "customer_id": customer,
                                        "lines": [{"item_code": c, "qty": q, "rate_source": "price_list"} for c, q in lines]})]


def test_place_of_supply_rules():
    assert tax_mode(None, "08AABCG5678K1Z4") == TAX_NONE  # unregistered business charges no GST
    assert tax_mode(HOME) == TAX_INTRA  # walk-in customer: our own state
    assert tax_mode(HOME, "27AAACB1234C1Z5") == TAX_INTRA
    assert tax_mode(HOME, "08AABCG5678K1Z4") == TAX_INTER
    assert tax_mode(HOME, None, "Rajasthan") == TAX_INTER  # unregistered customer with a billing address
    assert tax_mode(HOME, None, "maharashtra") == TAX_INTRA
    assert clean_hsn("2523") == "2523" and clean_hsn("7214 10") == "721410" and clean_hsn("252") is None


def test_split_matches_erpnext_to_the_paisa():
    erp = DemoERP()
    rows = [(erp.items["CEM-50"], Decimal(3), Decimal("452.35")), (erp.items["TMT-12"], Decimal(7), Decimal("65.33"))]
    intra = compute_lines(rows, TAX_INTRA)
    assert intra.subtotal == Decimal("1814.36")
    assert intra.taxes == (("CGST", Decimal("231.14")), ("SGST", Decimal("231.14")))  # live: 231.14 + 231.14
    assert intra.total == Decimal("2276.64")
    inter = compute_lines(rows, TAX_INTER)
    assert inter.taxes == (("IGST", Decimal("462.29")),) and inter.total == Decimal("2276.65")
    none = compute_lines(rows, TAX_NONE)
    assert none.tax == 0 and none.total == none.subtotal


def test_exact_half_paisa_rounds_like_erpnext_bankers_rounding():
    erp = DemoERP()
    rows = [(erp.items["CEM-50"], Decimal(10), Decimal("380.5")), (erp.items["TMT-12"], Decimal(50), Decimal("58.25"))]
    intra = compute_lines(rows, TAX_INTRA)  # each half is exactly 794.825; live ERPNext gave 794.82
    assert intra.taxes == (("CGST", Decimal("794.82")), ("SGST", Decimal("794.82"))) and intra.total == Decimal("8307.14")
    assert compute_lines(rows, TAX_INTER).taxes == (("IGST", Decimal("1589.65")),)


async def test_local_customer_gets_cgst_sgst_and_out_of_state_gets_igst(setup):
    engine, llm, _ = setup
    llm.script(propose("CUST-0001", [("CEM-50", 10)]), "Preview")
    out = await engine.handle(say("Gupta ji ka bill 10 cement"))
    assert "GST: ₹1,260 (CGST ₹630 + SGST ₹630)" in out[-1].text
    llm.script(propose("CUST-0002", [("CEM-50", 10)]), "Preview")
    out = await engine.handle(say("Sharma ji ka bill 10 cement", 2))
    assert "GST: ₹1,260 (IGST ₹1,260)" in out[-1].text


async def test_unregistered_business_charges_no_gst(setup):
    engine, llm, erp = setup
    erp.company_gstin = None
    llm.script(propose("CUST-0001", [("CEM-50", 10)]), "Preview")
    out = await engine.handle(say("bill 10 cement"))
    assert "GST: none (business not GST-registered)" in out[-1].text and "Total: ₹4,500*" in out[-1].text


async def test_missing_hsn_is_asked_and_saved_on_approve(setup):
    engine, llm, erp = setup
    llm.script(propose("CUST-0001", [("DVR-8", 1)]), "HSN chahiye.")
    out = await engine.handle(say("Gupta ji ko 1 DVR ka bill"))
    assert "HSN code chahiye: DVR 8 Channel" in out[-1].text
    assert llm.results[-1][0]["hsn_missing"] == ["DVR-8"]
    llm.script([("set_item_hsn", {"item_code": "DVR-8", "hsn": "12"}), ("set_item_hsn", {"item_code": "DVR-8", "hsn": "8521"})], "Save.")
    out = await engine.handle(say("8521 hai", 2))
    assert "4, 6 or 8" in llm.results[-1][0]["error"]
    assert "HSN code chahiye" not in out[-1].text and erp.items["DVR-8"].hsn is None  # not written before Approve
    card = next(o for o in out if o.kind == "buttons")
    await engine.handle(Inbound("a", ME, "button", button_id=card.buttons[0]["id"]))
    assert erp.items["DVR-8"].hsn == "8521"


async def test_hsn_not_asked_when_gst_does_not_apply(setup):
    engine, llm, erp = setup
    erp.company_gstin = None
    llm.script(propose("CUST-0001", [("DVR-8", 1)]), "Preview")
    out = await engine.handle(say("1 DVR"))
    assert "HSN" not in out[-1].text and "hsn_missing" not in llm.results[-1][0]
