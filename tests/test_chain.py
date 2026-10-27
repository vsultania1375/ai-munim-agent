"""Document chain: Quotation -> Sales Order -> Challan -> Bill, the ERPNext way (fake LLM, no cost)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP, DraftLine
from munim.core.erp_erpnext import ERPNextPort, _align, _clean
from munim.core.state import StateStore
from munim.erpnext.client import ERPDocument, _frappe_message
from tests.test_agent import QUOTE_GUPTA, ScriptedLLM

ME = "919800000001"


@pytest.fixture
def setup(tmp_path: Path):
    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "docs", today=date(2026, 10, 24))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m",
                         audit_path=tmp_path / "a.jsonl")
    return engine, llm, erp


def say(text: str, n: int) -> Inbound:
    return Inbound(f"m{n}", ME, "text", text=text)


async def approve(engine, out, **kw):
    card = next(o for o in out if o.kind == "buttons")
    return await engine.handle(Inbound(f"tap-{card.buttons[0]['id']}", ME, "button", button_id=card.buttons[0]["id"]), **kw)


async def make_quotation(engine, llm) -> str:
    llm.script(QUOTE_GUPTA, "Preview")
    done = await approve(engine, await engine.handle(say("Gupta ji ko 20 cement ka quotation", 1)))
    return done[0].meta["doc"]


def convert(source_doctype: str, name: str, target: str) -> list:
    return [("propose_from_document", {"source_doctype": source_doctype, "source_name": name, "target_kind": target})]


async def test_quotation_becomes_bill_with_its_lines_and_link(setup):
    engine, llm, erp = setup
    qtn = await make_quotation(engine, llm)
    llm.script(convert("Quotation", qtn, "invoice"), "Bill ka preview bheja.")
    out = await engine.handle(say("iska bill bana do", 2))
    card = out[-1].text
    assert f"Quotation {qtn} se" in card and "20 Bag × ₹450" in card
    done = await approve(engine, out)
    bill = erp.documents[-1]
    assert bill["doctype"] == "Sales Invoice" and bill["docstatus"] == 1 and ("Quotation", qtn) in bill["origins"]
    assert f"Quotation {qtn} se" in done[0].text
    llm.script(convert("Quotation", qtn, "invoice"), "Ye pehle hi bill ho chuka hai.")
    await engine.handle(say("iska bill fir se", 3))
    assert "already" in llm.results[-1][0]["error"]


async def test_partial_challan_leaves_the_rest_pending(setup):
    engine, llm, erp = setup
    qtn = await make_quotation(engine, llm)
    llm.script(convert("Quotation", qtn, "sales_order"), "Order preview.")
    await approve(engine, await engine.handle(say("order confirm ho gaya", 2)))
    order = erp.documents[-1]["name"]
    llm.script(convert("Sales Order", order, "delivery_note")
               + [("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 12, "rate_source": "price_list"}]})], "12 ka challan.")
    out = await engine.handle(say("abhi 12 bag bhej rahe hain, challan bana do", 3))
    assert "12 Bag" in out[-1].text
    await approve(engine, out)
    llm.script(convert("Sales Order", order, "delivery_note"), "Baaki ka challan.")
    out = await engine.handle(say("baaki ka challan", 4))
    assert "8 Bag" in out[-1].text  # only what is still pending


async def test_quotation_to_challan_goes_through_an_order(setup):
    engine, llm, erp = setup
    qtn = await make_quotation(engine, llm)
    llm.script(convert("Quotation", qtn, "delivery_note"), "Challan preview.")
    done = await approve(engine, await engine.handle(say("is quotation ka challan", 2)))
    kinds = [d["doctype"] for d in erp.documents]
    assert kinds == ["Quotation", "Sales Order", "Delivery Note"]
    assert "Sales Order" in done[0].text and erp.documents[1]["docstatus"] == 1


async def test_part_challan_from_quotation_orders_the_whole_quotation(setup):
    engine, llm, erp = setup
    qtn = await make_quotation(engine, llm)
    llm.script(convert("Quotation", qtn, "delivery_note")
               + [("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 5, "rate_source": "price_list"}]})], "5 ka challan.")
    await approve(engine, await engine.handle(say("abhi 5 bag ka challan", 2)))
    order, challan = erp.documents[1], erp.documents[2]
    assert order["lines"][0][1] == "20" and challan["lines"][0][1] == "5"
    llm.script(convert("Sales Order", order["name"], "delivery_note"), "Baaki.")
    out = await engine.handle(say("baaki ka challan", 3))
    assert "15 Bag" in out[-1].text


async def test_staff_and_draft_sources_are_refused_clearly(setup):
    engine, llm, erp = setup
    llm.script(QUOTE_GUPTA, "Preview")
    await approve(engine, await engine.handle(say("Gupta ji ko 20 cement ka quotation", 1), can_submit=False), can_submit=False)
    qtn = erp.documents[-1]["name"]
    llm.script(convert("Quotation", qtn, "invoice"), "Pehle quotation final karni hogi.")
    await engine.handle(say("iska bill", 2))
    assert "draft" in llm.results[-1][0]["error"]
    llm.script(convert("Quotation", qtn, "delivery_note"), "...")
    await engine.handle(say("iska challan", 3), can_submit=False)
    assert "staff" in llm.results[-1][0]["error"]


async def test_sourced_preview_cannot_switch_type_or_customer(setup):
    engine, llm, _ = setup
    qtn = await make_quotation(engine, llm)
    llm.script(convert("Quotation", qtn, "invoice") + [("edit_draft", {"kind": "quotation"})], "...")
    await engine.handle(say("iska bill", 2))
    assert "propose_from_document" in llm.results[-1][1]["error"]


async def test_recently_created_tells_model_the_doctype_and_state(setup):
    engine, llm, _ = setup
    qtn = await make_quotation(engine, llm)
    llm.script([], "ok")
    await engine.handle(say("kya bana abhi?", 2))
    block = llm.calls[-1]["messages"][-1]["content"][-1]["text"]
    assert f"Quotation {qtn} (Gupta Traders, final)" in block


# ---- ERPNext adapter: links kept, temp names dropped, order step for challans ----
class FakeClient:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, dict]] = []
        self.docs = {("Quotation", "QTN-1"): {"docstatus": 1, "party_name": "CUST-1"},
                     ("Company", "Demo"): {"abbr": "DEM", "gstin": "27AAICA1234B1ZA"},
                     ("Customer", "CUST-1"): {"gstin": "08AABCG5678K1Z4"},  # Rajasthan customer: IGST
                     ("Sales Taxes and Charges Template", "Output GST Out-state - DEM"): {"taxes": [
                         {"name": "t1", "charge_type": "On Net Total", "account_head": "Output Tax IGST - DEM", "rate": 18}]}}

    async def get_document(self, doctype: str, name: str) -> ERPDocument:
        data = self.docs.get((doctype, name)) or {"docstatus": 0}
        return ERPDocument(doctype=doctype, name=name, docstatus=data.get("docstatus"), data={**data, "name": name, "doctype": doctype})

    async def call_method(self, method: str, payload: dict[str, Any]) -> ERPDocument:
        self.calls.append((method, payload))
        if method == "frappe.client.submit":
            return ERPDocument(doctype=payload["doc"]["doctype"], name=payload["doc"]["name"], docstatus=1, data=payload["doc"])
        source = payload["source_name"]
        link = "prevdoc_docname" if "quotation" in method else "against_sales_order"
        return ERPDocument(doctype="", name="new-1", docstatus=0, data={
            "__islocal": 1, "name": "new-doc-1", "customer": "CUST-1", "customer_name": "Gupta Traders", "docstatus": 0,
            "items": [{"name": "row-tmp", "item_code": "CEM-50", "qty": 20, "rate": 450, link: source, "parent": "new-doc-1"}],
            "taxes": [{"name": "tax-tmp", "charge_type": "On Net Total", "rate": 28}],
        })

    async def create_draft_document(self, doctype: str, payload: dict[str, Any]) -> ERPDocument:
        self.inserted.append((doctype, payload))
        name = f"{doctype[:3].upper()}-{len(self.inserted)}"
        self.docs[(doctype, name)] = {"docstatus": 0, "grand_total": 11520}
        return ERPDocument(doctype=doctype, name=name, docstatus=0, data={"name": name, "grand_total": 11520})

    async def download_pdf(self, doctype: str, name: str) -> bytes:
        return b"%PDF-1.4"


async def test_erpnext_quotation_to_challan_inserts_order_then_challan_with_links(tmp_path):
    client = FakeClient()
    port = ERPNextPort(client, company="Demo", artifact_dir=tmp_path)
    mapped = await port.map_document("Quotation", "QTN-1", "Delivery Note")
    assert mapped.customer_id == "CUST-1" and mapped.lines[0].qty == Decimal(20)
    made = await port.create_from_source("Quotation", "QTN-1", "Delivery Note", [DraftLine("CEM-50", Decimal(12), Decimal(450))],
                                         "ref", final=True)
    (order_type, order), (dn_type, dn) = client.inserted
    assert (order_type, dn_type) == ("Sales Order", "Delivery Note")
    assert order["items"][0]["prevdoc_docname"] == "QTN-1" and order["delivery_date"] and order["items"][0]["qty"] == 20
    assert dn["items"][0]["against_sales_order"] == made.via and dn["items"][0]["qty"] == 12
    assert "name" not in dn and "name" not in dn["items"][0]
    assert dn["taxes_and_charges"] == "Output GST Out-state - DEM" and dn["taxes"] == [
        {"charge_type": "On Net Total", "account_head": "Output Tax IGST - DEM", "rate": 18}]
    assert made.submitted and made.via == "SAL-1" and made.pdf_path


async def test_erpnext_draft_source_is_refused(tmp_path):
    client = FakeClient()
    client.docs[("Quotation", "QTN-1")]["docstatus"] = 0
    with pytest.raises(Exception, match="draft"):
        await ERPNextPort(client, company="Demo", artifact_dir=tmp_path).map_document("Quotation", "QTN-1", "Sales Invoice")


def test_align_splits_qty_over_repeated_rows_and_adds_new_items():
    doc = {"items": [{"item_code": "A", "qty": 5, "rate": 1, "so_detail": "r1"}, {"item_code": "A", "qty": 5, "rate": 1, "so_detail": "r2"}]}
    out = _align(doc, [DraftLine("A", Decimal(7), Decimal(2)), DraftLine("B", Decimal(1), Decimal(3))])
    assert [(r["item_code"], r["qty"], r.get("so_detail")) for r in out["items"]] == [("A", 5.0, "r1"), ("A", 2.0, "r2"), ("B", 1.0, None)]
    assert _clean({"name": "x", "items": [{"name": "y", "qty": 1}]}) == {"items": [{"qty": 1}]}


def test_erpnext_validation_message_is_extracted():
    body = {"_server_messages": json.dumps([json.dumps({"message": "<b>Validity period</b> of this quotation has ended."})])}
    response = httpx.Response(417, json=body)
    assert _frappe_message(response) == "Validity period of this quotation has ended."


async def test_qty_change_on_converted_line_keeps_the_quotation_rate(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA[:2] + [("propose_sales_document", {"kind": "quotation", "customer_id": "CUST-0001", "lines": [
        {"item_code": "CEM-50", "qty": 10, "rate": 430, "rate_source": "user_stated"}]})], "Preview")
    done = await approve(engine, await engine.handle(say("Gupta ji ko 10 cement 430 pe", 1)))
    qtn = done[0].meta["doc"]
    llm.script(convert("Quotation", qtn, "invoice")
               + [("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 6, "rate_source": "document"}]})], "6 ka bill.")
    out = await engine.handle(say("isme se 6 ka bill", 2))
    assert "error" not in llm.results[-1][1]
    assert "6 Bag × ₹430" in out[-1].text  # discount from the quotation survives the qty change


async def test_several_documents_are_queued_and_follow_each_approve(setup):
    engine, llm, erp = setup
    qtn = await make_quotation(engine, llm)
    llm.script(convert("Quotation", qtn, "sales_order"), "Order.")
    await approve(engine, await engine.handle(say("order", 2)))
    order = erp.documents[-1]["name"]
    llm.script(convert("Sales Order", order, "delivery_note")
               + [("edit_draft", {"set_lines": [{"item_code": "CEM-50", "qty": 12, "rate_source": "price_list"}]})], "12.")
    await approve(engine, await engine.handle(say("12 ka challan", 3)))
    llm.script(convert("Sales Order", order, "delivery_note"), "8.")
    await approve(engine, await engine.handle(say("baaki ka challan", 4)))
    dn1, dn2 = [d["name"] for d in erp.documents if d["doctype"] == "Delivery Note"]
    llm.script(convert("Delivery Note", dn1, "invoice") + [("queue_next_steps", {"steps": [f"Sales Invoice from Delivery Note {dn2}"]})],
               "Pehla bill; approve ke baad doosra.")
    out = await engine.handle(say("dono ka alag alag bill", 5))
    assert engine.store.load("demo", ME).next_steps == [f"Sales Invoice from Delivery Note {dn2}"]
    llm.script(convert("Delivery Note", dn2, "invoice"), "Ab doosre challan ka bill.")
    after = await approve(engine, out)
    assert after[0].kind == "document" and f"Delivery Note {dn2} se" in after[-1].text  # next preview came by itself
    assert "queued from my earlier request" in str(engine.store.load("demo", ME).turns)
    assert engine.store.load("demo", ME).next_steps == []


async def test_cancel_clears_the_queue(setup):
    engine, llm, _ = setup
    llm.script(QUOTE_GUPTA + [("queue_next_steps", {"steps": ["quotation for Sharma ji, 5 cement"]})], "Pehla.")
    out = await engine.handle(say("Gupta aur Sharma dono ko quotation", 1))
    card = next(o for o in out if o.kind == "buttons")
    await engine.handle(Inbound("c1", ME, "button", button_id=card.buttons[2]["id"]))
    assert engine.store.load("demo", ME).next_steps == []
