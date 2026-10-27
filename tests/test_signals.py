"""Every WhatsApp signal is captured and reaches the agent's context (fake LLM, no cost)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.state import StateStore
from munim.core.tenants import TenantStore
from munim.core.walog import WALog
from munim.whatsapp.webhook import parse_statuses, parse_webhook_payload
from tests.test_agent import QUOTE_GUPTA, ScriptedLLM

ME = "919800000001"
T0 = 1_793_000_000.0  # a fixed instant; clock() is controlled in each test


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    clock = Clock()
    walog = WALog(tmp_path / "walog.db")
    engine = AgentEngine(
        store=StateStore(tmp_path / "state.db"), erp=DemoERP(artifact_dir=tmp_path / "docs", today=date(2026, 10, 24)),
        llm=llm, media_dir=tmp_path / "media", audit_path=tmp_path / "audit.jsonl", clock=clock, walog=walog,
    )
    return engine, llm, walog, clock


def said(text: str, n: int, **kw) -> Inbound:
    return Inbound(message_id=f"in{n}", sender=ME, kind="text", text=text, **kw)


def sent(walog: WALog, out, n: int) -> str:
    """What the server does after a successful send: log the outbound with its meta."""
    wamid = f"out{n}"
    walog.record(message_id=wamid, direction="out", chat=ME, kind=out.kind, text=out.text, meta=out.meta or {"kind": out.kind})
    return wamid


def last_user_block(llm: ScriptedLLM) -> str:
    return llm.calls[-1]["messages"][-1]["content"][-1]["text"]


# ---- webhook parsing -------------------------------------------------------
def _payload(message: dict | None = None, statuses: list | None = None) -> str:
    value = {"metadata": {"phone_number_id": "P1"}, "contacts": [{"wa_id": ME, "profile": {"name": "Amit"}}]}
    if message:
        value["messages"] = [{"from": ME, "id": "wamid.X", "timestamp": "1793000000", **message}]
    if statuses:
        value["statuses"] = statuses
    return json.dumps({"entry": [{"changes": [{"value": value}]}]})


def test_parser_reads_every_signal():
    m = parse_webhook_payload(_payload({
        "type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg", "sha256": "abc", "voice": True},
        "context": {"id": "wamid.PREV", "from": "917428051112", "forwarded": True, "frequently_forwarded": True},
        "referral": {"source_type": "ad", "headline": "Free munim"},
    }))[0]
    assert (m.voice, m.media_sha256, m.context_message_id, m.context_from) == (True, "abc", "wamid.PREV", "917428051112")
    assert m.forwarded and m.frequently_forwarded and m.profile_name == "Amit"
    assert json.loads(m.referral)["headline"] == "Free munim"
    assert m.timestamp.timestamp() == 1793000000

    r = parse_webhook_payload(_payload({"type": "reaction", "reaction": {"message_id": "wamid.CARD", "emoji": "👍"}}))[0]
    assert (r.reaction_to, r.reaction_emoji) == ("wamid.CARD", "👍")
    s = parse_webhook_payload(_payload({"type": "system", "system": {"type": "user_changed_number", "wa_id": "919811111111"}}))[0]
    assert s.new_wa_id == "919811111111"
    b = parse_webhook_payload(_payload({"type": "interactive", "interactive": {"button_reply": {"id": "approve:x:1", "title": "✅ Approve"}}}))[0]
    assert (b.button_payload, b.reply_title) == ("approve:x:1", "✅ Approve")


def test_statuses_parse_and_only_move_forward(tmp_path):
    events = parse_statuses(_payload(statuses=[
        {"id": "out1", "status": "read", "recipient_id": ME, "timestamp": "1793000100"},
        {"id": "out2", "status": "failed", "recipient_id": ME, "errors": [{"code": 131047, "title": "Re-engagement message"}]},
    ]))
    assert [(e.status, e.error_code) for e in events] == [("read", None), ("failed", 131047)]
    log = WALog(tmp_path / "w.db")
    log.record(message_id="out1", direction="out", chat=ME, kind="text")
    log.status("out1", "read")
    log.status("out1", "delivered")  # arrives late: must not move backwards
    assert log.get("out1").status == "read"


# ---- time ------------------------------------------------------------------
async def test_model_sees_india_time_turn_stamps_and_gap_marker(setup):
    engine, llm, _, clock = setup
    llm.script([], "Namaste! Kya banana hai?")
    await engine.handle(said("kal ka hisaab dekhna hai", 1, sent_at=T0))
    clock.now = T0 + 3 * 24 * 3600
    llm.script([], "Ji.")
    await engine.handle(said("aur cement ka rate?", 2, sent_at=clock.now))
    block = last_user_block(llm)
    assert "now:" in block and "IST" in block
    assert "since_last_message: 3 days" in block
    history = llm.calls[-1]["messages"][0]["content"]
    assert history.startswith("[") and "kal ka hisaab" in history
    clock.now += 60
    llm.script([], "ok")
    await engine.handle(said("theek", 3, sent_at=clock.now))
    assert any("[— 3 days later —]" in m["content"] for m in llm.calls[-1]["messages"][:-1])


async def test_late_delivered_message_is_flagged(setup):
    engine, llm, _, _ = setup
    llm.script([], "ok")
    await engine.handle(said("Gupta ji ka payment aaya", 1, sent_at=T0 - 2 * 3600))
    assert "delivered late" in last_user_block(llm)


# ---- reply context ---------------------------------------------------------
async def test_reply_to_older_preview_version_is_named(setup):
    engine, llm, walog, _ = setup
    llm.script(QUOTE_GUPTA, "Preview")
    out = await engine.handle(said("Gupta ji ko 20 cement ka quotation", 1))
    v1 = sent(walog, out[-1], 1)
    assert out[-1].meta["kind"] == "card" and out[-1].meta["version"] == 1
    llm.script([("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 25, "rate_source": "price_list"}]})], "v2")
    await engine.handle(said("25 kar do", 2))
    llm.script([], "ok")
    await engine.handle(said("isme rate kya tha?", 3, reply_to=v1))
    assert "OLDER version (v1)" in last_user_block(llm)


async def test_reply_to_created_document_names_it(setup):
    engine, llm, walog, _ = setup
    llm.script(QUOTE_GUPTA, "Preview")
    out = await engine.handle(said("Gupta ji ko 20 cement ka quotation", 1))
    card = next(o for o in out if o.kind == "buttons")
    created = await engine.handle(Inbound("b1", ME, "button", button_id=card.buttons[0]["id"]))
    pdf = sent(walog, created[0], 2)
    llm.script([], "ok")
    await engine.handle(said("isko invoice bana do", 3, reply_to=pdf))
    assert "Quotation" in last_user_block(llm) and "(submitted)" in last_user_block(llm)


async def test_reply_to_own_old_photo_reselects_it(setup, tmp_path):
    engine, llm, walog, _ = setup
    photo = tmp_path / "bill.jpg"
    photo.write_bytes(b"x")
    walog.record(message_id="in-photo", direction="in", chat=ME, kind="image",
                 meta={"media_path": str(photo), "mime": "image/jpeg", "filename": "bill.jpg", "digest": "d1"})
    llm.script([], "ok")
    await engine.handle(said("is bill ka total?", 5, reply_to="in-photo"))
    block = last_user_block(llm)
    assert "user's own earlier document bill.jpg" in block and "uploaded_document: bill.jpg" in block


async def test_forwarded_and_voice_flags_reach_model(setup):
    engine, llm, _, _ = setup
    llm.script([], "ok")
    await engine.handle(said("50 bag cement bhejo", 1, forwarded=True, frequently_forwarded=True))
    assert "forwarded: yes (frequently forwarded)" in last_user_block(llm)


# ---- reactions / delivery --------------------------------------------------
async def test_reaction_on_open_preview_never_approves(setup):
    engine, llm, walog, _ = setup
    llm.script(QUOTE_GUPTA, "Preview")
    out = await engine.handle(said("Gupta ji ko 20 cement ka quotation", 1))
    card = sent(walog, out[-1], 1)
    reply = await engine.handle(Inbound("r1", ME, "reaction", text="👍", reply_to=card))
    assert "Approve" in reply[0].text
    state = engine.store.load("demo", ME)
    assert state.draft is not None and state.last_created == []


async def test_failed_delivery_is_told_to_model(setup):
    engine, llm, walog, _ = setup
    walog.record(message_id="out9", direction="out", chat=ME, kind="document", meta={"kind": "document", "doc": "QTN-1"})
    walog.status("out9", "failed", error="131047 Re-engagement")
    llm.script([], "ok")
    await engine.handle(said("PDF nahi aaya", 1))
    assert "delivery_problem" in last_user_block(llm)


# ---- number change / same file ---------------------------------------------
def test_number_change_moves_account_and_chat(tmp_path):
    tenants = TenantStore(tmp_path / "t.db")
    owner = tenants.get("919800000001")
    owner.status, owner.site, owner.data = "active", "s1.local", {"erp_pw": "x", "lang": "hi"}
    tenants.save(owner)
    store = StateStore(tmp_path / "s.db")
    state = store.load("s1.local", "919800000001")
    state.add_turn("user", "hello")
    store.save("s1.local", state)
    moved = tenants.move("919800000001", "919811111111")
    store.move_sender("919800000001", "919811111111")
    assert moved.site == "s1.local" and "erp_pw" not in moved.data and moved.data["previous_phone"] == "919800000001"
    assert tenants.get("919800000001").status == "new"
    assert store.load("s1.local", "919811111111").turns[0]["text"] == "hello"


async def test_same_file_by_whatsapp_hash_needs_no_download_or_model_pixels(setup):
    engine, llm, _, _ = setup
    engine.store.media_put("demo", "meta:abc", {"facts": {"total": "1180"}, "path": "/x.jpg", "mime": "image/jpeg", "filename": "b.jpg"})
    llm.script([], "ok")
    await engine.handle(Inbound("in1", ME, "image", text="ye wala", media_sha256="abc"))
    content = llm.calls[-1]["messages"][-1]["content"]
    assert len(content) == 1 and '"total": "1180"' in content[0]["text"]
