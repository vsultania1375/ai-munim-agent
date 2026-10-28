from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.llm import AgentResult
from munim.core.state import StateStore


class ScriptedLLM:
    """Plays a fixed list of steps per turn: each step is a list of tool calls, then final text."""

    def __init__(self) -> None:
        self.turns: list[tuple[list[tuple[str, dict[str, Any]]], str]] = []
        self.calls: list[dict[str, Any]] = []
        self.results: list[list[dict[str, Any]]] = []

    def script(self, tool_calls: list[tuple[str, dict[str, Any]]], final_text: str) -> None:
        self.turns.append((tool_calls, final_text))

    async def run_turn(self, *, system, messages, tools, execute) -> AgentResult:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        tool_calls, final_text = self.turns.pop(0)
        outcomes = []
        for name, args in tool_calls:
            outcomes.append(await execute(name, args))
        self.results.append(outcomes)
        return AgentResult(final_text, "end_turn", 1 + bool(tool_calls))


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "docs", today=date(2026, 10, 24))
    engine = AgentEngine(
        store=StateStore(tmp_path / "state.db"),
        erp=erp,
        llm=llm,
        media_dir=tmp_path / "media",
        audit_path=tmp_path / "audit.jsonl",
    )
    return engine, llm, erp


def msg(text: str, n: int = 1) -> Inbound:
    return Inbound(message_id=f"m{n}", sender="919800000001", kind="text", text=text)


def tap(button_id: str) -> Inbound:
    return Inbound(message_id=f"b-{button_id}", sender="919800000001", kind="button", button_id=button_id)


QUOTE_GUPTA = [
    ("find_customer", {"name": "Gupta ji"}),
    ("find_item", {"name": "cement bag"}),
    (
        "propose_sales_document",
        {"kind": "quotation", "customer_id": "CUST-0001", "lines": [{"item_code": "CEM-50", "qty": 20, "rate_source": "price_list"}]},
    ),
]


async def test_greeting_on_first_message_skips_llm(setup):
    engine, llm, _ = setup
    out = await engine.handle(msg("Hii"))
    assert "AI Munim" in out[0].text
    assert llm.calls == []


async def test_quotation_preview_has_code_computed_totals_and_buttons(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya, check karke Approve karein.")
    out = await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    assert out[0].kind == "text"
    card = out[1]
    assert card.kind == "buttons"
    assert "Gupta Traders" in card.text
    assert "20 Bag × ₹450 = ₹9,000" in card.text
    assert "GST: ₹2,520" in card.text
    assert "Total: ₹11,520" in card.text
    assert [b["title"] for b in card.buttons] == ["✅ Approve", "✏️ Edit", "❌ Cancel"]
    assert llm.results[0][0]["status"] == "resolved"


async def test_llm_rate_is_ignored_for_price_list_lines(setup):
    engine, llm, _ = setup
    llm.script(
        [("propose_sales_document", {"kind": "invoice", "customer_id": "CUST-0001",
                                     "lines": [{"item_code": "CEM-50", "qty": 2, "rate": 1, "rate_source": "price_list"}]})],
        "ok",
    )
    out = await engine.handle(msg("bill bana do"))
    assert "2 Bag × ₹450 = ₹900" in out[1].text


async def test_edit_recomputes_and_old_version_cannot_be_approved(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    first = await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    old_approve = first[1].buttons[0]["id"]

    llm.script([("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 22, "rate": 470, "rate_source": "user_stated"}]})], "Update kar diya.")
    edited = await engine.handle(msg("rate 470 aur qty 22 kar do", 2))
    card = edited[1]
    assert "22 Bag × ₹470 = ₹10,340" in card.text
    assert "Total: ₹13,235.20" in card.text

    stale = await engine.handle(tap(old_approve))
    assert "purana preview" in stale[0].text
    assert stale[1].buttons[0]["id"] == card.buttons[0]["id"]


async def test_approve_creates_draft_once_and_sends_pdf(setup):
    engine, llm, erp = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    out = await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    approve = out[1].buttons[0]["id"]

    done = await engine.handle(tap(approve))
    assert done[0].kind == "document"
    assert done[0].path and done[0].path.exists()
    assert "SAL-QTN-DEMO" in done[0].text
    assert len(erp.documents) == 1

    again = await engine.handle(tap(approve))
    assert "pehle hi ban chuka" in again[0].text
    assert len(erp.documents) == 1


async def test_typed_approve_does_not_execute(setup):
    engine, llm, erp = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    llm.script([], "Kripya preview par Approve button dabayein.")
    await engine.handle(msg("approve", 2))
    assert erp.documents == []


async def test_unknown_item_code_returns_error_to_model(setup):
    engine, llm, _ = setup
    llm.script(
        [("propose_sales_document", {"kind": "quotation", "customer_id": "CUST-0001",
                                     "lines": [{"item_code": "MADE-UP", "qty": 1, "rate_source": "price_list"}]})],
        "Ye item list me nahi mila.",
    )
    out = await engine.handle(msg("quotation"))
    assert "error" in llm.results[0][0]
    assert all(o.kind == "text" for o in out)


async def test_not_found_customer_then_add_via_approval(setup):
    engine, llm, erp = setup
    llm.script([("find_customer", {"name": "Vivek ji"})], "Vivek ji customer list me nahi mile.")
    await engine.handle(msg("Vivek ji ko 20 cement bag ka quotation bana do"))
    assert llm.results[0][0]["status"] == "not_found"

    llm.script([("propose_new_customer", {"name": "Vivek Rao"})], "Preview bhej diya.")
    out = await engine.handle(msg("Add karo Vivek Rao naam se", 2))
    added = await engine.handle(tap(out[1].buttons[0]["id"]))
    assert "add ho gaye" in added[0].text
    assert (await erp.find_customer("Vivek Rao")).status == "resolved"


async def test_ask_user_sends_buttons_and_tap_is_routed_with_context(setup):
    engine, llm, _ = setup
    llm.script([("ask_user", {"question": "Kaunse Gupta?", "options": ["Gupta Traders", "Gupta Electricals"]})], "")
    out = await engine.handle(msg("Gupta ka outstanding"))
    assert out[0].kind == "buttons"
    tap_id = out[0].buttons[1]["id"]

    llm.script([("customer_outstanding", {"customer_id": "CUST-0004"})], "Gupta Electricals ka koi outstanding nahi hai.")
    await engine.handle(tap(tap_id))
    last_user = llm.calls[-1]["messages"][-1]["content"][-1]["text"]
    assert 'User tapped "Gupta Electricals" for: Kaunse Gupta?' in last_user


async def test_outstanding_totals_and_overdue_days_from_code(setup):
    engine, llm, _ = setup
    llm.script([("customer_outstanding", {"customer_id": "CUST-0001"})], "Gupta ji ka 15,000 baaki hai.")
    await engine.handle(msg("Gupta ji ka outstanding dikhao"))
    result = llm.results[0][0]
    assert result["total_outstanding"] == "15000.00"
    assert result["open_invoices"][0]["overdue_days"] == 20


async def test_new_request_replaces_open_preview(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    llm.script([("propose_payment", {"customer_id": "CUST-0002", "amount": 5000, "mode": "Cash"})], "Payment preview bhej diya.")
    await engine.handle(msg("Sharma ji se 5000 cash mila", 2))
    assert llm.results[1][0]["replaced_previous_unapproved"] == "Quotation"
    state = engine.store.load("demo", "919800000001")
    assert state.draft.kind == "payment"


async def test_state_block_and_history_reach_the_model(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    llm.script([], "Theek hai.")
    await engine.handle(msg("haan", 2))
    messages = llm.calls[-1]["messages"]
    assert messages[0]["role"] == "user"
    assert "[preview card sent]" in messages[1]["content"]
    assert "open_preview: Quotation for Gupta Traders" in messages[-1]["content"][-1]["text"]


async def test_document_upload_sets_context_and_sends_media_block(setup):
    engine, llm, _ = setup
    llm.script(
        [("save_document_facts", {"doc_type": "sales_invoice", "readable": True, "party_name": "Gupta Traders",
                                  "lines": [{"description": "Cement Bag", "qty": 20, "rate": 450}]})],
        "Ye Gupta Traders ki invoice hai, 20 cement bag.",
    )
    pdf = b"%PDF-1.4 fake"
    await engine.handle(Inbound("d1", "919800000001", "document", media=pdf, mime="application/pdf", filename="bill.pdf"))
    content = llm.calls[0]["messages"][-1]["content"]
    assert content[0]["type"] == "document"
    state = engine.store.load("demo", "919800000001")
    assert state.document.facts["party_name"] == "Gupta Traders"

    llm.script([], "ok")
    await engine.handle(msg("Isse delivery challan banao", 2))
    assert "uploaded_document: bill.pdf" in llm.calls[1]["messages"][-1]["content"][-1]["text"]


async def test_reset_clears_state(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    out = await engine.handle(msg("reset", 2))
    assert "reset" in out[0].text.lower()
    assert engine.store.load("demo", "919800000001").draft is None


async def test_llm_crash_gives_friendly_message(setup):
    engine, llm, _ = setup

    async def boom(**_):
        raise RuntimeError("network")

    llm.run_turn = boom
    await engine.handle(msg("Hii"))
    out = await engine.handle(msg("Gupta ji ka bill", 2))
    assert "technical dikkat" in out[0].text


def test_webhook_dedupe(tmp_path):
    store = StateStore(tmp_path / "s.db")
    assert store.first_time_seen("wamid.1")
    assert not store.first_time_seen("wamid.1")


async def test_fixed_text_follows_user_language(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA, "Preview sent.")
    out = await engine.handle(msg("Please make a quotation for Gupta Traders, 20 cement bags"))
    assert "Tap Approve and it's done" in out[1].text
    llm.script([("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 25, "rate_source": "price_list"}]})], "Theek hai.")
    out = await engine.handle(msg("qty 25 kar do bhai", 2))
    assert "Approve dabate hi ban jaayega" in out[1].text


async def test_payment_settles_oldest_bill_first_and_rest_is_advance(setup):
    engine, llm, _ = setup
    llm.script([("propose_payment", {"customer_id": "CUST-0001", "amount": 20000, "mode": "UPI"})], "Preview bhej diya.")
    out = await engine.handle(msg("Gupta ji se 20000 UPI aaya", 1))
    card = out[-1].text
    assert "ACC-SINV-DEMO-0101: ₹15,000" in card and "Advance" in card and "₹5,000" in card
    assert engine.store.load("demo", "919800000001").draft.allocations == [{"invoice": "ACC-SINV-DEMO-0101", "amount": "15000.00"}]


async def test_owner_approve_is_one_tap_final(setup):
    engine, llm, erp = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    out = await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    done = await engine.handle(tap(out[1].buttons[0]["id"]))
    assert len(done) == 1 and done[0].kind == "document"  # no "submit karein?" follow-ups
    assert "ban gaya" in done[0].text and "Draft" not in done[0].text
    assert erp.documents[0]["docstatus"] == 1 and done[0].meta["submitted"] is True


async def test_staff_approve_makes_a_draft_for_the_owner(setup):
    engine, llm, erp = setup
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    out = await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"), can_submit=False)
    assert "Approve dabane par draft banega" in out[1].text
    done = await engine.handle(tap(out[1].buttons[0]["id"]), can_submit=False)
    assert "Owner ise final karenge" in done[0].text and erp.documents[0]["docstatus"] == 0


async def test_refused_submit_is_reported_as_draft(setup):
    engine, llm, erp = setup
    original = erp.create_sales_draft

    async def refusing(*args, final=False, **kw):
        from dataclasses import replace

        doc = await original(*args, final=False, **kw)
        return replace(doc, submit_failed=final)

    erp.create_sales_draft = refusing
    llm.script(QUOTE_GUPTA, "Preview bhej diya.")
    out = await engine.handle(msg("Gupta ji ko 20 cement bag ka quotation bana do"))
    done = await engine.handle(tap(out[1].buttons[0]["id"]))
    assert "final nahi hone diya" in done[0].text and erp.documents[0]["docstatus"] == 0


async def test_learned_alias_resolves_next_time(setup):
    engine, llm, _ = setup
    llm.script([("save_alias", {"kind": "item", "alias": "Sariya Sona", "target_id": "TMT-12"})], "Yaad rakh liya.")
    await engine.handle(msg("haan Sariya Sona matlab TMT bar hai", 1))
    llm.script([("find_item", {"name": "sariya   sona"})], "ok")
    await engine.handle(msg("10 sariya sona ka bill", 2))
    found = llm.results[-1][0]
    assert found["status"] == "resolved" and found["learned"] and found["candidates"][0]["item_code"] == "TMT-12"
    llm.script([("save_alias", {"kind": "item", "alias": "x", "target_id": "NOPE"})], "ok")
    await engine.handle(msg("x matlab nope", 3))
    assert "error" in llm.results[-1][0]


async def test_payment_card_tells_owner_it_is_final_and_staff_it_is_a_draft(setup):
    engine, llm, _ = setup
    llm.script([("propose_payment", {"customer_id": "CUST-0002", "amount": 500, "mode": "UPI"})], "Preview")
    owner = (await engine.handle(msg("Verma ji se 500 UPI aaya")))[-1].text
    llm.script([("propose_payment", {"customer_id": "CUST-0002", "amount": 500, "mode": "UPI"})], "Preview")
    staff = (await engine.handle(Inbound("s1", "919800000002", "text", text="Verma ji se 500 UPI aaya"), can_submit=False))[-1].text
    assert owner != staff and "draft" not in owner.lower() and "draft" in staff.lower()
