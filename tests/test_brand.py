"""Bill design from the logo: colours by code with the background left out, bank box, preview -> Approve."""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from munim.core.brand import brand_from_print_format, clean_bank, from_logo, render_print_format
from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.state import DocumentContext, StateStore
from tests.test_agent import ScriptedLLM

ME = "919800000001"


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def test_logo_background_is_left_out():
    navy = Image.new("RGB", (300, 150), (12, 24, 70))
    draw = ImageDraw.Draw(navy)
    draw.rectangle((40, 40, 140, 110), fill=(220, 40, 40))
    draw.rectangle((160, 40, 260, 110), fill=(250, 200, 30))
    brand = from_logo(png(navy))
    assert brand.background == "#0c1846" and brand.primary == "#dc2828" and brand.accent == "#fac81d"  # red leads, yellow accents

    saffron = Image.new("RGB", (300, 150), (255, 153, 51))
    ImageDraw.Draw(saffron).rectangle((100, 30, 200, 120), fill=(7, 94, 84))
    assert from_logo(png(saffron)).primary_name == "teal"  # the saffron paper is not the brand

    clear = Image.new("RGBA", (300, 150), (0, 0, 0, 0))
    ImageDraw.Draw(clear).rectangle((50, 30, 250, 120), fill=(20, 150, 70, 255))
    assert from_logo(png(clear)).primary_name == "green" and from_logo(png(clear)).background is None


def test_light_and_black_logos_stay_readable():
    yellow = Image.new("RGB", (300, 150), "white")
    ImageDraw.Draw(yellow).ellipse((90, 20, 210, 130), fill=(255, 210, 60))
    brand = from_logo(png(yellow))
    assert brand.primary == "#2f3640" and brand.primary_name == "yellow" and brand.accent.startswith("#ff")  # charcoal head, yellow accent
    text_only = Image.new("RGB", (300, 150), "white")
    ImageDraw.Draw(text_only).text((40, 60), "SHARMA HARDWARE", fill="black")
    assert from_logo(png(text_only)).primary_name == "charcoal"


def test_bank_details_are_checked_and_rendered():
    details, problems = clean_bank({"bank_name": "HDFC Bank", "account_no": "5010 0123 4567 89", "ifsc": "hdfc0001234",
                                    "upi_id": "sharma@okhdfc"})
    assert problems == [] and details["account_no"] == "50100123456789" and details["ifsc"] == "HDFC0001234"
    _, bad = clean_bank({"account_no": "12AB", "ifsc": "HDFC1234", "upi_id": "nope"})
    assert len(bad) == 3
    brand = from_logo(png(Image.new("RGB", (60, 30), (30, 100, 200)))).as_dict()
    html = render_print_format(brand, details)
    assert "__" not in html.replace("__", "", 0) or "__PRIMARY__" not in html
    assert all(token not in html for token in ("__PRIMARY__", "__ACCENT__", "__TINT__", "__LOGO__", "__BANK__", "__BRAND_JSON__"))
    assert "50100123456789" in html and "HDFC0001234" in html and "{{ doc.company }}" in html
    assert brand_from_print_format(html)["details"]["upi_id"] == "sharma@okhdfc"


@pytest.fixture
def setup(tmp_path: Path):
    applied: list = []
    saved: dict = {}

    async def brand_get():
        return saved.get("brand")

    async def brand_apply(brand, details):
        applied.append((brand, details))
        saved["brand"] = {**brand, "details": details}

    llm = ScriptedLLM()
    erp = DemoERP(artifact_dir=tmp_path / "d", today=date(2026, 10, 27))
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=erp, llm=llm, media_dir=tmp_path / "m", audit_path=tmp_path / "a",
                         brand=(brand_get, brand_apply))
    return engine, llm, applied, tmp_path


async def test_logo_to_branded_bills(setup):
    engine, llm, applied, tmp = setup
    logo = Image.new("RGB", (300, 150), "white")
    ImageDraw.Draw(logo).ellipse((100, 20, 200, 130), fill=(30, 100, 200))
    path = tmp / "logo.png"
    path.write_bytes(png(logo))
    state = engine.store.load("demo", ME)
    state.document = DocumentContext(str(path), "image/png", "logo.png", engine.clock())
    engine.store.save("demo", state)
    llm.script([("set_brand", {"ifsc": "BAD"}), ("set_brand", {"use_uploaded_logo": True, "bank_name": "HDFC Bank", "account_no": "50100123456789",
                                                                "ifsc": "HDFC0001234", "upi_id": "sharma@okhdfc"})], "Design preview.")
    out = await engine.handle(Inbound("m1", ME, "text", text="ye mera logo hai, bill pe lagao, bank HDFC"))
    assert "IFSC" in llm.results[-1][0]["error"]
    card = out[-1].text
    assert "Bill design" in card and "blue" in card and "#ffffff" in card and "••••6789" in card and "50100123456789" not in card
    button = next(o for o in out if o.kind == "buttons").buttons[0]["id"]
    done = await engine.handle(Inbound("a1", ME, "button", button_id=button))
    brand, details = applied[0]
    assert brand["primary_name"] == "blue" and brand["logo_png_b64"] and details["upi_id"] == "sharma@okhdfc"
    assert "logo aur rang" in done[0].text or "logo and colours" in done[0].text
    llm.script([("set_brand", {"upi_id": "new@okicici"})], "Sirf UPI badla.")  # no new logo: the colours stay
    await engine.handle(Inbound("m2", ME, "text", text="UPI badal do new@okicici"))
    assert engine.store.load("demo", ME).draft.brand["primary"] == brand["primary"]
    llm.script([("set_brand", {})], "...")
    await engine.handle(Inbound("m3", ME, "text", text="design"), can_submit=False)
    assert "only the owner" in llm.results[-1][0]["error"]


def test_signature_photo_keeps_only_the_ink():
    import base64

    from munim.core.brand import signature_from_image

    paper = Image.new("RGB", (600, 300), (236, 232, 222))  # off-white paper
    draw = ImageDraw.Draw(paper)
    draw.rectangle((0, 250, 600, 300), fill=(205, 200, 190))  # a soft shadow: not ink
    draw.line((120, 160, 220, 90, 300, 170, 420, 100), fill=(20, 40, 140), width=6)  # blue pen
    sign = Image.open(io.BytesIO(base64.b64decode(signature_from_image(png(paper)))))
    assert sign.mode == "RGBA" and sign.width <= 260 and sign.height <= 90
    alphas = [px[3] for px in getattr(sign, "get_flattened_data", sign.getdata)()]
    assert 0 in alphas and 255 in alphas  # paper gone, ink kept
    inked = [px for px in getattr(sign, "get_flattened_data", sign.getdata)() if px[3] == 255]
    assert all(b > r for r, g, b, _ in inked)  # still the blue pen
    with pytest.raises(ValueError):
        signature_from_image(png(Image.new("RGB", (100, 50), "white")))


async def test_signature_terms_footer_and_noting_what_we_cannot_do(setup):
    import json

    engine, llm, applied, tmp = setup
    logo = Image.new("RGB", (300, 150), "white")
    ImageDraw.Draw(logo).ellipse((100, 20, 200, 130), fill=(30, 100, 200))
    (tmp / "logo.png").write_bytes(png(logo))
    state = engine.store.load("demo", ME)
    state.document = DocumentContext(str(tmp / "logo.png"), "image/png", "logo.png", engine.clock())
    engine.store.save("demo", state)
    llm.script([("set_brand", {"use_uploaded_logo": True})], "Logo preview.")
    out = await engine.handle(Inbound("m1", ME, "text", text="ye logo"))
    await engine.handle(Inbound("a1", ME, "button", button_id=next(o for o in out if o.kind == "buttons").buttons[0]["id"]))

    paper = Image.new("RGB", (400, 200), (240, 240, 235))
    ImageDraw.Draw(paper).line((50, 120, 150, 60, 250, 130, 350, 70), fill=(10, 10, 10), width=5)
    (tmp / "sign.jpg").write_bytes(png(paper))
    state = engine.store.load("demo", ME)
    state.document = DocumentContext(str(tmp / "sign.jpg"), "image/jpeg", "sign.jpg", engine.clock())
    engine.store.save("demo", state)
    llm.script([("set_brand", {"use_uploaded_signature": True, "terms": "Goods once sold will not be taken back.",
                               "footer": "Dhanyavaad!"})], "Sign preview.")
    out = await engine.handle(Inbound("m2", ME, "text", text="ye mera sign hai, har bill pe; terms bhi daalo"))
    card = out[-1].text
    assert "Sign: ✓" in card and "Terms: Goods once sold" in card and "Dhanyavaad!" in card and "Logo: ✓" in card
    await engine.handle(Inbound("a2", ME, "button", button_id=next(o for o in out if o.kind == "buttons").buttons[0]["id"]))
    brand, details = applied[-1]
    assert brand["signature_png_b64"] and brand["primary_name"] == "blue"  # the sign did not replace the logo
    html = render_print_format(brand, details)
    assert "Goods once sold will not be taken back." in html and "Dhanyavaad!" in html and html.count("data:image/png;base64,") == 2

    llm.script([("note_request", {"summary": "Print a QR code for UPI on every bill"})], "Noted.")
    await engine.handle(Inbound("m3", ME, "text", text="bill pe UPI ka QR bhi chahiye"))
    audit = [json.loads(line) for line in (tmp / "a").read_text(encoding="utf-8").splitlines()]
    assert any(a["event"] == "feature_request" and "QR" in a["request"] for a in audit)
