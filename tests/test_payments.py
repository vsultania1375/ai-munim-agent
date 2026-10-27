"""Online payments per business: own Razorpay keys (encrypted), owner-only, demo-safe, paid links found without a webhook."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from munim.core.tenants import DEMO_SITE, Tenant
from munim.payments.razorpay import PaymentAccounts, RazorpayLinks, clean_keys
from munim.payments.service import PaymentService

KEY, SECRET = "rzp_test_ABCD1234567890", "s3cretS3cretS3cret123456"


def razorpay(state: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if state.get("reject"):
            return httpx.Response(401, json={"error": {"description": "auth"}})
        if request.method == "POST":
            state["n"] = state.get("n", 0) + 1
            return httpx.Response(200, json={"id": f"plink_{state['n']}", "short_url": f"https://rzp.io/i/{state['n']}"})
        if request.url.path.endswith("/payment_links"):
            return httpx.Response(200, json={"items": []})
        link_id = request.url.path.rsplit("/", 1)[-1]
        if link_id in state.get("paid", set()):
            return httpx.Response(200, json={"id": link_id, "status": "paid", "amount_paid": 50000, "payments": [
                {"payment_id": "pay_1", "amount": 50000, "method": "upi", "status": "captured"}]})
        return httpx.Response(200, json={"id": link_id, "status": "created"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def svc(tmp_path: Path):
    state: dict = {}
    links = RazorpayLinks(tmp_path / "r.db", http=razorpay(state))
    return PaymentService(PaymentAccounts(tmp_path / "p.db", "server-secret"), links, "aimunim.example"), state, tmp_path


OWNER = Tenant("919800000001", role="owner", status="active", site="tenant-1", company="Sharma Hardware")
STAFF = Tenant("919800000002", role="staff", status="active", site="tenant-1")
VISITOR = Tenant("919800000003", role="owner", status="demo")
TESTER = Tenant("919800000004", role="tester", status="active", site=DEMO_SITE)


def test_keys_are_checked_for_shape():
    assert clean_keys(f" {KEY} ", SECRET) == (KEY, SECRET)
    for key_id, secret in (("pk_live_x", SECRET), (KEY, "short"), (KEY, "has spaces in it ok?")):
        with pytest.raises(ValueError):
            clean_keys(key_id, secret)


async def test_owner_connects_own_keys_encrypted_and_switches(svc):
    service, state, tmp = svc
    assert service.status(STAFF) is None and service.status(VISITOR) is None  # not theirs to set up
    with pytest.raises(PermissionError):
        await service.connect(STAFF, KEY, SECRET)
    state["reject"] = True
    with pytest.raises(ValueError, match="did not accept"):
        await service.connect(OWNER, KEY, SECRET)
    state["reject"] = False
    status = await service.connect(OWNER, KEY, SECRET)
    assert status["connected"] and status["enabled"] and status["mode"] == "test" and SECRET not in json.dumps(status)
    assert status["webhook_url"] == "https://aimunim.example/razorpay/webhook/tenant-1"
    assert SECRET.encode() not in (tmp / "p.db").read_bytes()  # encrypted at rest
    assert PaymentAccounts(tmp / "p.db", "other-secret").get("tenant-1") is None  # useless without the server secret
    assert service.account_for_bill(STAFF, "tenant-1") is not None  # the business's bills, whoever made them
    service.set_enabled(OWNER, False)
    assert service.account_for_bill(OWNER, "tenant-1") is None
    service.set_enabled(OWNER, True)
    assert service.disconnect(OWNER) and service.status(OWNER) == {"connected": False}


async def test_demo_links_only_for_testers(svc):
    service, _, _ = svc
    await service.connect(TESTER, KEY, SECRET)
    assert service.account_for_bill(TESTER, DEMO_SITE) is not None
    assert service.account_for_bill(VISITOR, DEMO_SITE) is None  # a stranger's demo bill never uses the tester's account


async def test_paid_link_found_without_webhook_once(svc):
    service, state, _ = svc
    await service.connect(OWNER, KEY, SECRET)
    account = service.accounts.get("tenant-1")
    await service.links.create(account, invoice="SINV-1", customer_id="CUST-1", customer_name="Gupta Traders",
                               amount=Decimal(500), description="x")
    assert await service.links.check_open(account) == []
    state["paid"] = {"plink_1"}
    [paid] = await service.links.check_open(account)
    assert (paid.invoice, paid.amount, paid.method, paid.payment_id) == ("SINV-1", Decimal(500), "upi", "pay_1")
    assert await service.links.check_open(account) == []  # recorded once


async def test_agent_sends_the_setup_link_as_is_and_staff_cannot(tmp_path: Path):
    from datetime import date

    from munim.core.engine import AgentEngine, Inbound
    from munim.core.erp import DemoERP
    from munim.core.state import StateStore
    from tests.test_agent import ScriptedLLM

    asked = []

    async def payments(action, sender):
        asked.append((action, sender))
        return {"send": "Connect here: https://x/portal/magic/T0K", "connected": False}

    llm = ScriptedLLM()
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=DemoERP(artifact_dir=tmp_path / "d", today=date(2026, 10, 27)),
                         llm=llm, media_dir=tmp_path / "m", audit_path=tmp_path / "a", payments=payments)
    llm.script([("online_payments", {"action": "on"})], "Link bhej diya.")
    out = await engine.handle(Inbound("m1", "919800000001", "text", text="online payment chalu karo"))
    assert asked == [("on", "919800000001")] and "setup_link_sent" in llm.results[-1][0]
    assert any(o.text == "Connect here: https://x/portal/magic/T0K" for o in out)  # exact, not retyped by the model
    llm.script([("online_payments", {"action": "status"})], "...")
    await engine.handle(Inbound("m2", "919800000002", "text", text="payment status"), can_submit=False)
    assert "only the owner" in llm.results[-1][0]["error"] and len(asked) == 1
