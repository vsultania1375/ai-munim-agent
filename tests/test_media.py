from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

from PIL import Image

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.media import cleanup_old_files, parse_vcard, prepare_image, prepare_pdf, sniff
from munim.core.state import StateStore
from munim.pdf import write_demo_pdf
from tests.test_agent import ScriptedLLM

SENDER = "919800000001"


def png_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def make_engine(tmp_path: Path, llm, transcriber=None) -> AgentEngine:
    return AgentEngine(store=StateStore(tmp_path / "s.db"), erp=DemoERP(artifact_dir=tmp_path / "d", today=date(2026, 10, 26)),
                       llm=llm, media_dir=tmp_path / "m", audit_path=tmp_path / "a.jsonl", transcriber=transcriber)


def test_sniff_by_bytes_not_name():
    xlsx = io.BytesIO()
    with zipfile.ZipFile(xlsx, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("xl/workbook.xml", "<x/>")
    assert sniff(b"%PDF-1.7 ...") == "pdf"
    assert sniff(png_bytes(4, 4)) == "png"
    assert sniff(b"\xff\xd8\xff\xe0abc") == "jpeg"
    assert sniff(b"OggS\x00\x02") == "ogg"
    assert sniff(b"BEGIN:VCARD\nFN:Amit\nEND:VCARD") == "vcard"
    assert sniff(b"Name,Phone\nA,1\n") == "csv"
    assert sniff(xlsx.getvalue()) == "xlsx"
    assert sniff(b"\x00\x01\x02garbage") == "unknown"


def test_digital_pdf_becomes_text_not_pages(tmp_path: Path):
    lines = [f"Cement Bag 50 KG qty {n} rate 450" for n in range(20)]
    pdf = write_demo_pdf(tmp_path / "bill.pdf", "Tax Invoice Gupta Traders", lines).read_bytes()
    prepared = prepare_pdf(pdf)
    assert prepared.note == "pdf_text"
    assert prepared.blocks[0]["type"] == "text" and "Gupta Traders" in prepared.blocks[0]["text"]


def test_photo_is_downscaled():
    prepared = prepare_image(png_bytes(4000, 3000))
    out = Image.open(io.BytesIO(__import__("base64").b64decode(prepared.blocks[0]["source"]["data"])))
    assert max(out.size) == 1200 and out.format == "JPEG"


def test_vcard_parse():
    assert parse_vcard("BEGIN:VCARD\nFN:Amit Electronics\nTEL;TYPE=CELL:+91 98100-00001\nEND:VCARD") == {
        "name": "Amit Electronics", "phone": "+919810000001"}


class FakeTranscriber:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: bytes) -> str:
        return self.text


async def test_voice_note_is_echoed_then_handled_as_text(tmp_path: Path):
    llm = ScriptedLLM()
    llm.script([], "Theek hai.")
    engine = make_engine(tmp_path, llm, FakeTranscriber("Gupta ji ka outstanding dikhao"))
    out = await engine.handle(Inbound("v1", SENDER, "audio", media=b"OggS fake"))
    assert out[0].text.startswith("Maine suna") and "outstanding" in out[0].text
    assert "User: Gupta ji ka outstanding dikhao" in llm.calls[0]["messages"][-1]["content"][-1]["text"]


async def test_unclear_voice_does_not_call_model(tmp_path: Path):
    llm = ScriptedLLM()
    engine = make_engine(tmp_path, llm, FakeTranscriber(""))
    out = await engine.handle(Inbound("v1", SENDER, "audio", media=b"OggS fake"))
    assert "voice" in out[0].text.lower() and llm.calls == []


async def test_shared_contact_becomes_customer_preview_without_model(tmp_path: Path):
    llm = ScriptedLLM()
    engine = make_engine(tmp_path, llm)
    out = await engine.handle(Inbound("c1", SENDER, "contact", text="BEGIN:VCARD\nFN:Amit Electronics\nTEL:919810000001\nEND:VCARD"))
    assert out[0].kind == "buttons" and "Amit Electronics" in out[0].text and llm.calls == []
    added = await engine.handle(Inbound("b1", SENDER, "button", button_id=out[0].buttons[0]["id"]))
    assert "Amit Electronics" in added[0].text


async def test_same_photo_twice_is_read_once(tmp_path: Path):
    llm = ScriptedLLM()
    llm.script([("save_document_facts", {"doc_type": "sales_invoice", "readable": True, "party_name": "Gupta Traders"})], "Gupta Traders ka bill hai.")
    llm.script([], "Ye wahi bill hai.")
    engine = make_engine(tmp_path, llm)
    photo = png_bytes(2000, 1500)
    await engine.handle(Inbound("p1", SENDER, "image", media=photo))
    await engine.handle(Inbound("p2", SENDER, "image", media=photo))
    first, second = (c["messages"][-1]["content"] for c in llm.calls)
    assert any(b["type"] == "image" for b in first)
    assert not any(b["type"] == "image" for b in second)
    assert "read earlier" in second[-1]["text"]


def test_old_media_cleanup(tmp_path: Path):
    import os
    import time

    old, new = tmp_path / "a" / "old.jpg", tmp_path / "a" / "new.jpg"
    old.parent.mkdir()
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    assert cleanup_old_files(tmp_path, 30) == 1 and new.exists() and not old.exists()
