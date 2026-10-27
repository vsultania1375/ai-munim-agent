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
        "MUNIM_BOT_NUMBER": "", "JEV_API_KEY": "", "MUNIM_SUMMARY": "off",
    }.items():
        monkeypatch.setenv(key, value)
    sent: list[tuple[str, str]] = []

    async def send_text(self, to, text):
        sent.append((to, text))
        return {"messages": [{"id": f"wamid.OUT{len(sent)}"}]}

    async def noop(self, *a, **k):
        return {}

    monkeypatch.setattr(wa_client.WhatsAppClient, "send_text", send_text)
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
    out = wait_for(lambda: log.get("wamid.OUT1"))
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
