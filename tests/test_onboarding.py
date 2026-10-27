from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from munim.core.engine import Inbound
from munim.core.gstin import check_gstin, find_gstin
from munim.core.llm import AgentResult
from munim.core.migration import detect_columns, parse_amount, parse_gst, plan_from_file, run_import
from munim.core.onboarding import Onboarding
from munim.core.provisioning import SiteCreds, company_abbr
from munim.core.tenants import TenantStore

PHONE = "919811111111"
TESTER = "919800000099"


class ToolLLM:
    """Answers every run_turn by calling one tool with fixed args."""

    def __init__(self, args: dict[str, Any] | None = None) -> None:
        self.args = args or {}
        self.calls = 0

    async def run_turn(self, *, system, messages, tools, execute) -> AgentResult:
        self.calls += 1
        await execute(tools[0]["name"], self.args)
        return AgentResult("", "end_turn", 1)


class FakeSiteAPI:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, dict]] = []

    async def exists(self, doctype: str, name: str) -> bool:
        return doctype == "UOM" and name in {"Nos", "Bag"}

    async def insert(self, doctype: str, doc: dict) -> dict:
        self.inserted.append((doctype, doc))
        return {"name": f"{doctype[:3].upper()}-0001", **doc}


class FakeProvisioner:
    def __init__(self) -> None:
        self.pool = {"tenant-1": SiteCreds("tenant-1", "k", "s")}
        self.site_api = FakeSiteAPI()
        self.setups: list[dict] = []

    def api(self, site: str) -> FakeSiteAPI:
        return self.site_api

    async def setup_business(self, site: str, profile: dict) -> dict[str, str]:
        self.setups.append(profile)
        return {"site": site, "company": profile.get("trade_name") or "Biz", "abbr": "SH"}


@pytest.fixture
def ob(tmp_path: Path):
    tenants = TenantStore(tmp_path / "t.db", testers={TESTER})
    prov = FakeProvisioner()
    llm = ToolLLM()
    return Onboarding(tenants=tenants, provisioner=prov, llm=llm), tenants, prov, llm


async def run(onb, tenants, msg: Inbound, sent: list | None = None):
    tenant = tenants.get(msg.sender)
    notes = sent if sent is not None else []

    async def notify(item):
        notes.append(item)

    return await onb.handle(msg, tenant, notify), tenants.get(msg.sender)


def txt(body: str, n: int = 1) -> Inbound:
    return Inbound(f"m{n}", PHONE, "text", text=body)


def tap(bid: str) -> Inbound:
    return Inbound(f"b-{bid}", PHONE, "button", button_id=bid)


def test_gstin_checksum_state_and_extraction():
    info, reason = check_gstin("27AAPFU0939F1ZV")
    assert reason is None and info.state == "Maharashtra" and info.pan == "AAPFU0939F"
    assert check_gstin("27AAPFU0939F1ZX")[0] is None  # checksum
    assert check_gstin("1234")[0] is None
    assert find_gstin("mera gst 27aapfu0939f1zv hai") == "27AAPFU0939F1ZV"


def test_tester_number_skips_onboarding(tmp_path):
    tenants = TenantStore(tmp_path / "t.db", testers={TESTER})
    tester = tenants.get(TESTER)
    assert tester.role == "tester" and tester.status == "active" and tester.erp_site == "frontend"
    assert tenants.get(PHONE).status == "new"


async def test_full_happy_path_with_gstin(ob):
    onb, tenants, prov, _ = ob
    out, t = await run(onb, tenants, txt("Hi"))
    assert t.status == "onboarding" and [b["id"] for b in out[0].buttons] == ["ob:start", "ob:demo"]

    out, t = await run(onb, tenants, tap("ob:start"))
    assert t.step == "identity"

    out, t = await run(onb, tenants, txt("27AAPFU0939F1ZV", 2))
    assert t.step == "name" and "Maharashtra" in out[0].text

    onb.llm.args = {"trade_name": "Sharma Hardware", "city": "Pune"}
    out, t = await run(onb, tenants, txt("Sharma Hardware, Pune", 3))
    assert t.step == "confirm" and "Sharma Hardware" in out[0].text and "27AAPFU0939F1ZV" in out[0].text

    out, t = await run(onb, tenants, tap("ob:ok"))
    assert t.step == "btype" and out[0].kind == "list"

    sent: list = []
    out, t = await run(onb, tenants, tap("bt:hardware"), sent)
    assert "30-60 second" in sent[0].text
    assert t.site == "tenant-1" and t.step == "migrate" and t.company == "Sharma Hardware"
    assert prov.setups[0]["gstin"] == "27AAPFU0939F1ZV"

    out, t = await run(onb, tenants, tap("mg:skip"))
    assert t.status == "active" and t.erp_site == "tenant-1"
    assert "is live" in out[0].text and "ERP login" in out[0].text


async def test_invalid_gstin_explains_and_stays(ob):
    onb, tenants, _, _ = ob
    await run(onb, tenants, txt("Hi"))
    await run(onb, tenants, tap("ob:start"))
    out, t = await run(onb, tenants, txt("27AAPFU0939F1ZX", 2))
    assert t.step == "identity" and "last character" in out[0].text


async def test_no_gst_path_and_edit(ob):
    onb, tenants, _, _ = ob
    await run(onb, tenants, txt("Hello"))
    await run(onb, tenants, tap("ob:start"))
    out, t = await run(onb, tenants, tap("ob:nogst"))
    assert t.step == "name"
    onb.llm.args = {"trade_name": "Amit Electronics", "city": "Rohtak"}
    out, t = await run(onb, tenants, txt("Amit Electronics Rohtak", 2))
    assert "GSTIN: none" in out[0].text and t.step == "confirm"
    out, t = await run(onb, tenants, tap("ob:edit"))
    assert t.step == "edit"
    onb.llm.args = {"trade_name": "Amit CCTV House"}
    out, t = await run(onb, tenants, txt("naam Amit CCTV House hai", 3))
    assert "Amit CCTV House" in out[0].text


async def test_first_message_with_gstin_jumps_ahead(ob):
    onb, tenants, _, _ = ob
    out, t = await run(onb, tenants, txt("mera GST 06EXBPK8917C1ZJ hai"))
    assert t.step == "name" and "Haryana" in out[0].text


async def test_demo_mode_and_back_to_setup(ob):
    onb, tenants, _, _ = ob
    await run(onb, tenants, txt("Hi"))
    _out, t = await run(onb, tenants, tap("ob:demo"))
    assert t.status == "demo" and t.erp_site == "frontend"
    assert onb.wants_setup(txt("setup"))
    _out, t = await run(onb, tenants, txt("setup", 2))
    assert t.status == "onboarding" and t.step == "identity"


async def test_pool_exhausted_falls_back_to_demo(ob):
    onb, tenants, prov, _ = ob
    tenants.claim_site("919800000000", list(prov.pool))
    await run(onb, tenants, txt("Hi"))
    await run(onb, tenants, tap("ob:start"))
    onb.llm.args = {"trade_name": "X Traders"}
    await run(onb, tenants, txt("X Traders Delhi", 2))
    await run(onb, tenants, tap("ob:ok"))
    _out, t = await run(onb, tenants, tap("bt:other"))
    assert t.status == "demo" and t.data["waitlist"] is True


def test_column_detection_and_amount_parsing():
    cols = detect_columns(("Party Name", "Mobile No", "Closing Balance"))
    assert cols == {"customer": "Party Name", "phone": "Mobile No", "balance": "Closing Balance"}
    assert parse_amount("₹1,234.50 Dr") == Decimal("1234.50")
    assert parse_amount("500 Cr") == Decimal(-500)
    assert parse_gst("18%") == Decimal(18) and parse_gst("0.28") == Decimal(28) and parse_gst("17") is None


def test_plan_from_tally_style_csv():
    csv = "Party Name,Mobile No,Closing Balance\nGupta Traders,98100 00001,15000 Dr\nSharma Ent,,0\nAdvance Party,,200 Cr\n,,\nTotal,,15200\n"
    plan = plan_from_file(csv.encode(), "debtors.csv")
    assert [c["name"] for c in plan.customers] == ["Gupta Traders", "Sharma Ent", "Advance Party"]
    assert plan.balances == [{"customer": "Gupta Traders", "amount": Decimal(15000)}]
    assert plan.customers[0]["phone"] == "9810000001"
    assert plan.skipped_rows == 2


def test_plan_from_item_list_csv():
    csv = "Item Name,Unit,Rate,GST %\nCement Bag,Bag,450,28\nWire 1.5mm,Meter,22,18%\nPaint,Litre,350,\n"
    plan = plan_from_file(csv.encode(), "items.csv")
    assert [i["name"] for i in plan.items] == ["Cement Bag", "Wire 1.5mm", "Paint"]
    assert plan.items[0]["rate"] == Decimal(450) and plan.items[1]["gst"] == Decimal(18)
    assert plan.items_without_gst == 1


def test_item_list_hsn_column_is_read_and_cleaned():
    csv = "Item Name,HSN/SAC,GST %\nCement Bag,2523,28\nWire,8544 49,18\nBad,12,18\n"
    plan = plan_from_file(csv.encode(), "items.csv")
    assert [i["hsn"] for i in plan.items] == ["2523", "854449", None]


async def test_migration_preview_import_and_opening_entry(ob):
    onb, tenants, prov, _ = ob
    t = tenants.get(PHONE)
    t.status, t.step, t.site, t.company, t.data = "onboarding", "migrate", "tenant-1", "Sharma Hardware", {"abbr": "SH"}
    tenants.save(t)
    csv = b"Party Name,Closing Balance\nGupta Traders,15000 Dr\nVerma,2500\n"
    doc = Inbound("f1", PHONE, "document", media=csv, mime="text/csv", filename="debtors.csv")
    out, t = await run(onb, tenants, doc)
    assert "2 customers" in out[0].text and [b["id"] for b in out[0].buttons] == ["mg:import", "mg:cancel"]
    out, t = await run(onb, tenants, tap("mg:import"))
    kinds = [d for d, _ in prov.site_api.inserted]
    assert kinds.count("Customer") == 2 and kinds.count("Journal Entry") == 0
    opening = [d for k, d in prov.site_api.inserted if k == "Sales Invoice"]
    assert [(o["customer"], o["items"][0]["rate"], o["is_opening"], o["docstatus"]) for o in opening] == [
        ("Gupta Traders", 15000.0, "Yes", 1), ("Verma", 2500.0, "Yes", 1)]
    assert opening[0]["items"][0]["income_account"] == "Temporary Opening - SH"
    assert "Import done" in out[0].text and "2 old dues in the books (₹17,500)" in out[0].text and "plan" not in t.data


async def test_run_import_items_get_gst_template_and_price():
    api = FakeSiteAPI()
    plan = plan_from_file(b"Item Name,Unit,Rate,GST\nCement Bag,Bag,450,28\n", "i.csv")
    result = await run_import(api, plan, company="Sharma Hardware", abbr="SH")
    item = next(doc for d, doc in api.inserted if d == "Item")
    price = next(doc for d, doc in api.inserted if d == "Item Price")
    assert item["taxes"] == [{"item_tax_template": "GST 28% - SH"}] and price["price_list_rate"] == 450.0
    assert result["items"] == 1


def test_company_abbr():
    assert company_abbr("Sharma Hardware Store") == "SHS"
    assert company_abbr("Ram Lal & Sons") == "RLS"


def test_web_access_helpers():
    from munim.core.web_access import login_id_for, new_password, wants_login, wants_reset, web_host

    assert login_id_for("919800000099") == "9800000099"
    pw = new_password()
    assert len(pw) == 11 and pw[5] == "-" and not set(pw) & set("0O1lI")
    assert web_host("frontend", "aimunim.duckdns.org", "frontend") == "demo.aimunim.duckdns.org"
    assert web_host("tenant-2", "aimunim.duckdns.org", "frontend") == "tenant-2.aimunim.duckdns.org"
    assert wants_login("ERP login") and wants_reset("password reset") and not wants_login("bill bana do")


def test_claimed_sites_for_caddy(tmp_path):
    tenants = TenantStore(tmp_path / "t.db")
    tenants.claim_site(PHONE, ["tenant-1", "tenant-2"])
    assert tenants.claimed_sites() == {"tenant-1"}


async def test_onboarding_follows_hinglish_user(ob):
    onb, tenants, _, _ = ob
    out, t = await run(onb, tenants, txt("Namaste ji, mujhe setup karna hai"))
    assert t.data["lang"] == "hi" and "Namaste" in out[0].text and out[0].buttons[0]["title"] == "🚀 Setup shuru"
    await run(onb, tenants, tap("ob:start"))
    out, t = await run(onb, tenants, txt("27AAPFU0939F1ZX", 2))
    assert "aakhri character" in out[0].text


def test_language_detection():
    from munim.core.i18n import detect_lang

    assert detect_lang("Gupta ji ko 20 cement bag ka bill bana do") == "hi"
    assert detect_lang("Please make an invoice for Gupta Traders") == "en"
    assert detect_lang("गुप्ता जी का बिल बना दो") == "hi"
    assert detect_lang("ok", "hi") == "hi" and detect_lang("Hi") == "en"
    assert detect_lang("rate 470 kar do", "en") == "hi"


async def test_daily_new_site_cap_sends_to_demo(tmp_path):
    tenants = TenantStore(tmp_path / "t.db")
    prov = FakeProvisioner()
    prov.pool["tenant-2"] = SiteCreds("tenant-2", "k", "s")
    onb = Onboarding(tenants=tenants, provisioner=prov, llm=ToolLLM({"trade_name": "Y Traders"}), max_new_sites_per_day=1)
    tenants.claim_site("919800000000", ["tenant-1"])  # someone already took today's only slot
    await run(onb, tenants, txt("Hi"))
    await run(onb, tenants, tap("ob:start"))
    await run(onb, tenants, txt("Y Traders Delhi", 2))
    await run(onb, tenants, tap("ob:ok"))
    out, t = await run(onb, tenants, tap("bt:other"))
    assert t.status == "demo" and t.site is None and "tomorrow" in out[0].text
