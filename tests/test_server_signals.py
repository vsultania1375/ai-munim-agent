"""Webhook → WA log → delivery status, through the real FastAPI app (WhatsApp sends are faked; no model call)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from munim.core.tenants import TenantStore
from munim.core.walog import WALog
from munim.whatsapp import client as wa_client

TESTER = "919800000099"  # any number set as a tester below
SECRET = "test-secret"


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "WA_PHONE_NUMBER_ID": "P1", "WA_APP_SECRET": SECRET, "WA_ACCESS_TOKEN": "x", "ANTHROPIC_API_KEY": "x",
        "MUNIM_V2_TESTERS": TESTER, "MUNIM_STT": "none", "ERPNEXT_URL": "", "MUNIM_V2_POOL_DIR": str(tmp_path / "pool"),
        "MUNIM_BOT_NUMBER": "", "JEV_API_KEY": "", "MUNIM_SUMMARY": "off", "MUNIM_DEMO_LIMIT": "5",
    }.items():
        monkeypatch.setenv(key, value)
    sent: list[tuple[str, str]] = []

    async def send_text(self, to, text):
        sent.append((to, text))
        return {"messages": [{"id": f"wamid.OUT{len(sent)}"}]}

    async def noop(self, *a, **k):
        return {}

    async def send_buttons(self, to, text, buttons):
        return await send_text(self, to, text)

    monkeypatch.setattr(wa_client.WhatsAppClient, "send_text", send_text)
    monkeypatch.setattr(wa_client.WhatsAppClient, "send_buttons", send_buttons)  # nothing reaches Meta from tests
    monkeypatch.setattr(wa_client.WhatsAppClient, "start_typing", noop)
    from munim.core.server import create_app

    with TestClient(create_app(env_file=tmp_path / "none.env")) as client:
        yield client, sent, tmp_path


def post(client: TestClient, value: dict) -> None:
    body = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "P1"}, **value}}]}]}).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/wa/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200


def wait_for(check, seconds: float = 5.0):
    end = time.time() + seconds
    while time.time() < end:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_inbound_outbound_and_status_are_all_logged(app):
    client, sent, root = app
    post(client, {
        "contacts": [{"wa_id": TESTER, "profile": {"name": "Amit"}}],
        "messages": [{"from": TESTER, "id": "wamid.IN1", "timestamp": str(int(time.time())), "type": "text",
                      "text": {"body": "hi"}, "referral": {"source_type": "ad", "headline": "Try AI Munim"}}],
    })
    wait_for(lambda: sent)
    log = WALog(root / ".run/v2/walog.db")
    inbound = log.get("wamid.IN1")
    assert inbound.direction == "in" and inbound.meta["referral"]["headline"] == "Try AI Munim"
    out = wait_for(lambda: (row := log.get("wamid.OUT1")) and row.status == "sent" and row)  # logged, then marked sent
    assert out.direction == "out" and out.chat == TESTER and out.status == "sent"
    tenant = TenantStore(root / ".run/v2/tenants.db").get(TESTER)
    assert tenant.data["referral"]["source_type"] == "ad" and tenant.data["profile_name"] == "Amit"

    post(client, {"statuses": [{"id": "wamid.OUT1", "status": "read", "recipient_id": TESTER, "timestamp": "1"}]})
    assert wait_for(lambda: log.get("wamid.OUT1").status == "read")
    post(client, {"statuses": [{"id": "wamid.OUT1", "status": "delivered", "recipient_id": TESTER}]})
    time.sleep(0.2)
    assert log.get("wamid.OUT1").status == "read"


def test_number_change_moves_the_account(app):
    client, sent, root = app
    tenants = TenantStore(root / ".run/v2/tenants.db")
    owner = tenants.get("919800000009")
    owner.status, owner.site, owner.company, owner.data = "active", "demo", "Demo", {"lang": "en"}
    tenants.save(owner)
    post(client, {"messages": [{"from": "919800000009", "id": "wamid.SYS", "timestamp": str(int(time.time())), "type": "system",
                                "system": {"type": "user_changed_number", "wa_id": "919800000010", "body": "changed"}}]})
    wait_for(lambda: sent)
    assert sent[0][0] == "919800000010" and "number" in sent[0][1]
    assert TenantStore(root / ".run/v2/tenants.db").get("919800000010").status == "active"


def test_a_new_number_chooses_then_tries_the_demo_up_to_a_limit(app, monkeypatch):
    from munim.core import llm as llm_module

    client, sent, root = app
    asked = []

    async def run_turn(self, *, system, messages, tools, execute):
        asked.append(messages)
        return llm_module.AgentResult("Verma Hardware pe ₹2,300 baaki hai.", "end_turn", 1)

    monkeypatch.setattr(llm_module.ClaudeAgentLLM, "run_turn", run_turn)
    visitor = "919800000077"
    tenants = TenantStore(root / ".run/v2/tenants.db")

    def say(n: int, words: str, kind: str = "text") -> None:
        body = {"from": visitor, "id": f"wamid.V{n}", "timestamp": str(int(time.time())), "type": kind}
        body.update({"text": {"body": words}} if kind == "text" else
                    {"interactive": {"type": "button_reply", "button_reply": {"id": words, "title": "x"}}})
        post(client, {"messages": [body]})

    say(1, "Hi")
    wait_for(lambda: sent)
    assert "setup" in sent[0][1].lower() and not asked  # a greeting: the choice (own setup or demo), no model call
    say(2, "ob:demo", "interactive")
    wait_for(lambda: len(sent) >= 2)
    assert tenants.get(visitor).status == "demo"
    for n in range(3, 3 + 5):  # MUNIM_DEMO_LIMIT=5 here
        say(n, f"Verma ka outstanding dikhao {n}")
        wait_for(lambda n=n: len(asked) >= n - 2)
    assert any("3 demo messages left" in text for _, text in sent)  # warned with 3 left
    say(99, "aur ek baar")
    wait_for(lambda: any("that was the demo" in text.lower() for _, text in sent))  # the trial is over: own setup next
    assert len(asked) == 5
    assert tenants.get(visitor).data["demo_used"] == 5


def test_first_message_with_a_question_is_answered_at_once(app, monkeypatch):
    from munim.core import llm as llm_module

    client, sent, _ = app

    async def run_turn(self, *, system, messages, tools, execute):
        return llm_module.AgentResult("Main bill, quotation, challan aur hisaab sambhalta hoon.", "end_turn", 1)

    monkeypatch.setattr(llm_module.ClaudeAgentLLM, "run_turn", run_turn)
    post(client, {"messages": [{"from": "919800000078", "id": "wamid.W1", "timestamp": str(int(time.time())), "type": "text",
                                "text": {"body": "Hello bhai kya kya kar sakta hai tu"}}]})
    wait_for(lambda: len(sent) >= 2)
    assert "demo" in sent[0][1].lower() and sent[1][1].startswith("Main bill")  # intro, then the answer
