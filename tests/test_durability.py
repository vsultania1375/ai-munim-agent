from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.erp_erpnext import ERPNextPort
from munim.core.server import _decode, _encode
from munim.core.state import Draft, StateStore
from munim.whatsapp.webhook import IncomingMessage


def test_inbox_keeps_unfinished_messages_for_replay(tmp_path: Path):
    store = StateStore(tmp_path / "s.db")
    assert store.inbox_add("wamid.1", "911", "{}")
    assert not store.inbox_add("wamid.1", "911", "{}")  # webhook replay is ignored
    store.inbox_add("wamid.2", "911", "{}")
    store.inbox_done(["wamid.1"])
    assert [mid for mid, _ in store.inbox_pending()] == ["wamid.2"]


def test_incoming_message_roundtrip():
    msg = IncomingMessage("wamid.9", "911", "text", "pid", datetime(2026, 10, 26, tzinfo=UTC), text="hi")
    assert _decode(_encode(msg)) == msg


async def test_day_old_preview_cannot_be_approved(tmp_path: Path):
    now = [1_000_000.0]
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=DemoERP(artifact_dir=tmp_path), llm=None,
                         media_dir=tmp_path / "m", audit_path=tmp_path / "a.jsonl", clock=lambda: now[0])
    state = engine.store.load("demo", "911")
    state.draft = Draft(kind="payment", customer_id="CUST-0001", customer_name="Gupta Traders",
                        payment_amount="500", payment_mode="Cash", created_at=now[0])
    engine.store.save("demo", state)
    now[0] += 25 * 3600
    out = await engine.handle(Inbound("b1", "911", "button", button_id=f"approve:{state.draft.id}:1"))
    assert "no longer open" in out[0].text or "open nahi" in out[0].text


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_all_documents(self, doctype, fields=None, filters=None):
        self.calls.append(doctype)
        if doctype == "Item":
            return [{"name": "CEM-50", "item_name": "Cement Bag 50 KG", "stock_uom": "Bag"},
                    {"name": "WIRE", "item_name": "Wire 1.5mm", "stock_uom": "Meter"}]
        return [{"item_code": "CEM-50", "price_list_rate": 450}]

    async def list_child_rows(self, child, parent, fields):
        self.calls.append(child)
        return [{"parent": "CEM-50", "item_tax_template": "GST 28% - AMD"}]

    async def get_document(self, doctype, name):  # must not be called per item any more
        raise AssertionError("per-item fetch")


async def test_item_cache_uses_three_calls():
    client = FakeClient()
    port = ERPNextPort(client, company="X")
    cem = await port.get_item("CEM-50")
    wire = await port.get_item("WIRE")
    assert cem.gst_rate == Decimal(28) and cem.rate == Decimal("450.00")
    assert wire.gst_rate == Decimal(0) and wire.rate == Decimal("0.00")
    assert client.calls == ["Item", "Item Price", "Item Tax"]
    assert port.today <= date.today()
