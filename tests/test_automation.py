"""ERP -> WhatsApp automation, recurring bills and the (off by default) Razorpay module. Fake LLM/HTTP, no cost."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from munim.core.automation import describe_erp_event, hook_token, summary_text, window_open
from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.state import StateStore
from munim.core.tools import _next_date
from munim.payments.razorpay import RazorpayLinks
from tests.test_agent import ScriptedLLM
from tests.test_server_signals import TESTER, app, post, wait_for  # noqa: F401 - pytest fixture

ME = "919800000001"


async def test_summary_is_built_from_the_books():
    erp = DemoERP(today=date(2026, 10, 24))
    body = await summary_text(erp, "hi")
    assert body.startswith("☀️ *Suprabhat — 24 Oct*") and "Kal ki bikri: ₹0 (0 bill)" in body
    assert "Customers pe baaki: ₹18,260 (2 customer)" in body and "Gupta Traders: ₹15,000 — 20 din se late" in body
    assert "summary band" in body


def test_window_events_and_tokens():
    now = time.time()
    assert window_open(now - 3600, now) and not window_open(now - 24 * 3600, now) and not window_open(None, now)
    assert hook_token("s", "tenant-1") != hook_token("s", "tenant-2")
    made = describe_erp_event({"doctype": "Sales Invoice", "name": "SINV-26-00012", "party": "Gupta Traders", "total": "1180.0"}, "en")
    assert made == "🖥️ In the ERP: Sales Invoice *SINV-26-00012* finalised — Gupta Traders ₹1,180"
    assert "recurring" in describe_erp_event({"doctype": "Sales Invoice", "name": "X", "total": "1", "auto_repeat": "AUT-AR-1"}, "en")
    assert describe_erp_event({"doctype": "Customer", "name": "X"}, "en") is None


def test_first_copy_date_clamps_to_month_end():
    assert _next_date(date(2026, 1, 31), "Monthly") == date(2026, 2, 28)
    assert _next_date(date(2026, 10, 26), "Quarterly") == date(2027, 1, 26)
    assert _next_date(date(2026, 10, 26), "Weekly") == date(2026, 11, 2)


async def test_recurring_bill_from_a_final_invoice(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "d", today=date(2026, 10, 24))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m", audit_path=tmp_path / "a")
    llm.script([("propose_sales_document", {"kind": "invoice", "customer_id": "CUST-0002", "lines": [
        {"item_code": "CCTV-2MP", "qty": 1, "rate": 2000, "rate_source": "user_stated"}]})], "Preview")
    out = await engine.handle(Inbound("m1", ME, "text", text="Sharma ji ka maintenance bill 2000"))
    card = next(o for o in out if o.kind == "buttons")
    bill = (await engine.handle(Inbound("a1", ME, "button", button_id=card.buttons[0]["id"])))[0].meta["doc"]
    llm.script([("propose_repeat", {"invoice": bill, "start_date": "2026-01-01"}),
                ("propose_repeat", {"invoice": bill, "frequency": "Monthly"})], "Har mahine.")
    out = await engine.handle(Inbound("m2", ME, "text", text="ye bill har mahine banana hai"))
    assert "past" in llm.results[-1][0]["error"]
    assert "Har baar ka bill" in out[-1].text and "Pehla naya bill: 2026-11-24" in out[-1].text
    card = next(o for o in out if o.kind == "buttons")
    done = await engine.handle(Inbound("a2", ME, "button", button_id=card.buttons[0]["id"]))
    repeat = erp.documents[-1]
    assert repeat["doctype"] == "Auto Repeat" and repeat["reference"] == bill and repeat["submit_on_creation"] == 1
    assert "har mahine" in done[0].text and "2026-11-24" in done[0].text


# ---- server: summary command, ERP webhooks, owner buttons ----
def test_summary_word_answers_without_the_model(app):  # noqa: F811
    client, sent, _ = app
    post(client, {"messages": [{"from": TESTER, "id": "wamid.S1", "timestamp": str(int(time.time())), "type": "text",
                                "text": {"body": "summary"}}]})
    wait_for(lambda: sent)
    assert "Good morning" in sent[0][1] or "Suprabhat" in sent[0][1]


def test_erp_webhook_needs_token_skips_bot_docs_and_respects_the_window(app):  # noqa: F811
    from munim.core.walog import WALog

    client, sent, root = app
    event = {"doctype": "Sales Invoice", "name": "SINV-26-00099", "party": "Gupta Traders", "total": "1180"}
    assert client.post("/erp/hook/frontend", json=event, headers={"x-munim-token": "wrong"}).status_code == 403
    token = hook_token("test-secret", "frontend")
    assert client.post("/erp/hook/frontend", json=event, headers={"x-munim-token": token}).status_code == 200
    time.sleep(0.2)
    assert sent == []  # the owner hasn't written in 24 h and no template is configured: say nothing
    post(client, {"messages": [{"from": TESTER, "id": "wamid.H1", "timestamp": str(int(time.time())), "type": "text",
                                "text": {"body": "hi"}}]})
    wait_for(lambda: sent)
    client.post("/erp/hook/frontend", json=event, headers={"x-munim-token": token})
    wait_for(lambda: len(sent) == 2)
    assert "SINV-26-00099" in sent[1][1] and "Gupta Traders" in sent[1][1]
    WALog(root / ".run/v2/walog.db").record(message_id="out-x", direction="out", chat=TESTER, kind="document",
                                            meta={"kind": "document", "doc": "SINV-26-00100"})
    client.post("/erp/hook/frontend", json={**event, "name": "SINV-26-00100"}, headers={"x-munim-token": token})
    time.sleep(0.2)
    assert len(sent) == 2  # the bot sent that one itself


def test_owner_buttons(app):  # noqa: F811
    client, sent, _ = app
    post(client, {"messages": [{"from": TESTER, "id": "wamid.B1", "timestamp": str(int(time.time())), "type": "interactive",
                                "interactive": {"type": "button_reply", "button_reply": {"id": "ownno:Sales Invoice:X", "title": "Later"}}}]})
    wait_for(lambda: sent)
    assert "draft" in sent[0][1].lower()


# ---- Razorpay: each business's own account ----
async def test_razorpay_link_and_paid_webhook_once(tmp_path: Path):
    from munim.payments.razorpay import PaymentAccounts

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/abc"})

    accounts = PaymentAccounts(tmp_path / "p.db", "server-secret")
    shop = accounts.connect("tenant-1", "rzp_test_SHOP1234567890", "secretsecret1234567890ab", by="919800000001")
    links = RazorpayLinks(tmp_path / "r.db", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    url = await links.create(shop, invoice="SINV-26-00001", customer_id="CUST-1", customer_name="Gupta Traders",
                             amount=Decimal("1180.50"), description="Shop SINV-26-00001")
    auth, sent = calls[0]
    assert url == "https://rzp.io/i/abc" and sent["amount"] == 118050 and sent["notes"]["invoice"] == "SINV-26-00001"
    assert auth == "Basic " + base64.b64encode(b"rzp_test_SHOP1234567890:secretsecret1234567890ab").decode()  # the shop's own keys
    again = await links.create(shop, invoice="SINV-26-00001", customer_id="CUST-1", customer_name="Gupta Traders",
                               amount=Decimal("1180.50"), description="again")
    assert again == url and len(calls) == 1  # one open link per bill
    body = json.dumps({"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"id": "plink_1", "amount_paid": 118050}},
        "payment": {"entity": {"id": "pay_9", "method": "upi", "amount": 118050}}}}).encode()
    sig = hmac.new(shop.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert links.verify(shop, body, sig) and not links.verify(shop, body, "bad")
    assert links.paid("tenant-2", body) is None  # another business's webhook can't claim this link
    paid = links.paid("tenant-1", body)
    assert (paid.site, paid.invoice, paid.customer_name, paid.amount, paid.method) == (
        "tenant-1", "SINV-26-00001", "Gupta Traders", Decimal("1180.5"), "upi")
    assert links.paid("tenant-1", body) is None  # a retried webhook records nothing twice
