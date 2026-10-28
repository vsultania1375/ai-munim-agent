"""WhatsApp webhook server for Agent Core v2.

Run: python -m munim.core.server   (reads .env from the repo root)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from munim.core.automation import (
    SUMMARY_OFF,
    SUMMARY_ON,
    SUMMARY_WORDS,
    describe_erp_event,
    hook_token,
    summary_text,
    window_open,
)
from munim.core.clock import IST, today_ist
from munim.core.engine import AgentEngine, Inbound, Outbound, render_speech
from munim.core.erp import DemoERP
from munim.core.erp_erpnext import ERPNextPort
from munim.core.guards import BudgetedLLM, UsageLedger
from munim.core.i18n import detect_lang, t
from munim.core.llm import ClaudeAgentLLM
from munim.core.media import NoSpeaker, NoTranscriber, PiperSpeaker, WhisperTranscriber, cleanup_old_files
from munim.core.onboarding import Onboarding
from munim.core.portal import PortalStore, parse_command, wa_link
from munim.core.portal_web import mount_portal
from munim.core.provisioning import Provisioner, load_pool
from munim.core.resolver import JevTurnResolver, NullResolver
from munim.core.secretbox import SecretBox
from munim.core.sso import SSOStore, erp_session, mount_sso, site_from_host
from munim.core.state import StateStore
from munim.core.tenants import DEMO_SITE, TenantStore
from munim.core.walog import WALog
from munim.core.web_access import (
    OWNER_ROLES,
    STAFF_ROLES,
    ensure_owner_login,
    ensure_user,
    login_message,
    user_email,
    wants_login,
    wants_reset,
    web_host,
)
from munim.erpnext.client import ERPNextClient
from munim.payments.razorpay import PaidLink, PaymentAccounts, RazorpayLinks
from munim.payments.service import PaymentService
from munim.whatsapp.client import WhatsAppClient, WhatsAppError
from munim.whatsapp.webhook import (
    IncomingMessage,
    parse_statuses,
    parse_webhook_payload,
    verify_meta_signature,
)

log = logging.getLogger("munim.v2")

DEBOUNCE_SECONDS = 1.5
RATE_LIMIT_PER_HOUR = 60


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.split(" #")[0].strip().strip('"').strip("'"))


class SenderQueues:
    """One worker per chat: messages from one sender are handled strictly in order."""

    def __init__(self, process) -> None:
        self.process = process
        self.queues: dict[str, asyncio.Queue[IncomingMessage]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def put(self, message: IncomingMessage) -> None:
        queue = self.queues.setdefault(message.sender, asyncio.Queue())
        queue.put_nowait(message)
        task = self.tasks.get(message.sender)
        if task is None or task.done():
            self.tasks[message.sender] = asyncio.create_task(self._run(message.sender))

    async def _run(self, sender: str) -> None:
        queue = self.queues[sender]
        while not queue.empty():
            first = queue.get_nowait()
            batch = [first]
            if first.kind == "text":
                await asyncio.sleep(DEBOUNCE_SECONDS)
                while not queue.empty() and queue._queue[0].kind == "text":
                    batch.append(queue.get_nowait())
            try:
                await self.process(batch)
            except Exception:
                log.exception("processing failed")


TRIAL_KINDS = {"text", "audio", "image", "document"}  # button taps (Approve...) don't use up the demo
DEFAULT_TESTERS = ""  # set MUNIM_V2_TESTERS in the environment
TEAM_WORDS = {"team", "my team", "staff", "add staff", "staff add", "team add", "add member", "team member", "meri team"}


def _erp_client(base_url: str, key: str, secret: str, site: str) -> ERPNextClient:
    # One bench serves many sites; nginx picks the site from the Host header.
    http = httpx.AsyncClient(timeout=30, headers={"Host": site})
    return ERPNextClient(base_url, key, secret, http=http)


def _erp_from_env():
    url = os.environ.get("ERPNEXT_URL", "")
    if not url:
        return DemoERP()
    client = _erp_client(url, os.environ["ERPNEXT_API_KEY"], os.environ["ERPNEXT_API_SECRET"], DEMO_SITE)
    return ERPNextPort(client, company=os.environ.get("ERPNEXT_COMPANY", "AI Munim Demo Traders"))


def create_app(env_file: Path | None = None) -> FastAPI:
    load_env(env_file or Path(".env"))
    phone_id = os.environ["WA_PHONE_NUMBER_ID"]
    app_secret = os.environ["WA_APP_SECRET"]
    verify_token = os.environ.get("WA_VERIFY_TOKEN", "")
    wa = WhatsAppClient(
        phone_id,
        os.environ["WA_ACCESS_TOKEN"],
        outbound_enabled=True,
        graph_api_version=os.environ.get("WA_GRAPH_API_VERSION", "v25.0"),
        read_receipts_enabled=True,
        typing_enabled=True,
    )
    jev_key = os.environ.get("JEV_API_KEY", "")
    allowed = {s.strip() for s in os.environ.get("MUNIM_V2_ALLOWED_SENDERS", "").split(",") if s.strip()}
    testers = {s.strip() for s in os.environ.get("MUNIM_V2_TESTERS", DEFAULT_TESTERS).split(",") if s.strip()}
    model = os.environ.get("MUNIM_V2_MODEL", "claude-haiku-5-5")
    ledger = UsageLedger(Path(".run/v2/usage.db"))
    llm = BudgetedLLM(
        ClaudeAgentLLM(model=model, effort=os.environ.get("MUNIM_V2_EFFORT", "low")),
        ledger,
        daily_usd=float(os.environ.get("MUNIM_DAILY_BUDGET_USD", "2")),
        model=model,
    )
    resolver = JevTurnResolver(jev_key) if jev_key else NullResolver()
    store = StateStore(Path(".run/v2/state.db"))
    tenants = TenantStore(Path(".run/v2/tenants.db"), testers=testers)
    erp_url = os.environ.get("ERPNEXT_URL", "")
    pool_dir = Path(os.environ.get("MUNIM_V2_POOL_DIR", "/app/pool"))
    pool = load_pool(pool_dir)
    hook_secret = os.environ.get("MUNIM_PORTAL_SECRET") or app_secret
    box = SecretBox.from_env(fallback=hook_secret)  # stored secrets: ERP sign-in passwords, Razorpay keys
    provisioner = (Provisioner(erp_url, pool, hook_secret=hook_secret,
                               bot_url=os.environ.get("MUNIM_INTERNAL_URL", "http://aimunim-bot-1:8800"))
                   if erp_url else None)  # shares `pool`; refreshed on demand
    nudge_template = os.environ.get("MUNIM_TEMPLATE_NUDGE", "")  # approved utility template, no parameters
    summary_hour = int(os.environ.get("MUNIM_SUMMARY_HOUR", "9"))
    demo_limit = int(os.environ.get("MUNIM_DEMO_LIMIT", "25"))  # messages a visitor can try on the demo shop
    tenant_daily_usd = float(os.environ.get("MUNIM_TENANT_DAILY_USD", "0.5"))  # model spend per business per day
    demo_daily_usd = float(os.environ.get("MUNIM_DEMO_DAILY_USD", "1"))  # the shared demo shop, all visitors together
    onboarding = Onboarding(
        tenants=tenants, provisioner=provisioner, llm=llm.for_feature("onboarding"), pool_dir=pool_dir,
        max_new_sites_per_day=int(os.environ.get("MUNIM_MAX_NEW_SITES_PER_DAY", "3")),
    )
    base_domain = os.environ.get("MUNIM_PUBLIC_DOMAIN", "aimunim.duckdns.org")
    bot_number = os.environ.get("MUNIM_BOT_NUMBER", "")
    portal = PortalStore(Path(".run/v2/portal.db"), os.environ.get("MUNIM_PORTAL_SECRET") or app_secret)
    # each business connects its own Razorpay on the portal; keys stored encrypted per site
    pay_links = RazorpayLinks(Path(".run/v2/razorpay.db"))
    payments = PaymentService(PaymentAccounts(Path(".run/v2/payments.db"), box),
                              pay_links, base_domain)
    sso = SSOStore(Path(".run/v2/sso.db"))
    walog = WALog(Path(".run/v2/walog.db"))
    engines: dict[str, AgentEngine] = {}
    transcriber = (
        WhisperTranscriber(os.environ.get("MUNIM_STT_MODEL", "small"), os.environ.get("MUNIM_STT_LANGUAGE", "hi") or None)
        if os.environ.get("MUNIM_STT", "whisper") == "whisper" and _has_module("faster_whisper")
        else NoTranscriber()
    )

    speaker = (PiperSpeaker() if os.environ.get("MUNIM_TTS", "piper") == "piper" and _has_module("piper") else NoSpeaker())

    def engine_for(site: str, company: str | None) -> AgentEngine:
        if site not in engines:
            if site == DEMO_SITE:
                erp = _erp_from_env()
            else:
                creds = pool[site]
                erp = ERPNextPort(
                    _erp_client(erp_url, creds.api_key, creds.api_secret, site), company=company or "", label=""
                )
            engines[site] = AgentEngine(
                store=store, erp=erp, llm=llm.for_tenant(site, demo_daily_usd if site == DEMO_SITE else tenant_daily_usd),
                resolver=resolver, tenant=site,
                welcome_key="welcome" if site == DEMO_SITE else "welcome_business",
                transcriber=transcriber, walog=walog, speaker=speaker, payments=payments_tool,
                brand=((lambda s=site: provisioner.brand_of(s)), (lambda b, d, s=site: provisioner.apply_brand(s, b, d)))
                if provisioner is not None and site in pool else None,
            )
        return engines[site]

    sent_per_sender: dict[str, deque[float]] = defaultdict(deque)
    profile_names: dict[str, str] = {}

    def roles_for(tenant) -> tuple[str, ...]:
        return STAFF_ROLES if tenant.role == "staff" else OWNER_ROLES

    def erp_password(tenant) -> str | None:
        """The bot-held ERP password for one-tap sign-in, kept encrypted (older plain ones are sealed on first use)."""
        plain = tenant.data.pop("erp_pw", None)
        if plain:
            tenant.data["erp_pw_sealed"] = box.seal(plain)
            tenants.save(tenant)
            return plain
        return box.open(tenant.data.get("erp_pw_sealed"))

    def keep_erp_password(tenant, password: str) -> None:
        tenant.data.pop("erp_pw", None)
        tenant.data["erp_pw_sealed"] = box.seal(password)
        tenants.save(tenant)

    async def reset_sso_password(tenant, site: str) -> str:
        password = secrets.token_urlsafe(24)  # never shown; the bot signs in on the person's behalf
        await ensure_user(provisioner.api(site), phone=tenant.phone, roles=roles_for(tenant), password=password,
                          name=tenant.data.get("owner_name") or profile_names.get(tenant.phone))
        keep_erp_password(tenant, password)
        return password

    async def erp_link(tenant) -> str | None:
        site = tenant.erp_site
        if not site or provisioner is None or site not in pool:
            return None
        if not erp_password(tenant):
            await reset_sso_password(tenant, site)
        return f"https://{host_for(site)}/munim-sso?t={sso.mint(tenant.phone, site)}"

    async def start_session(phone: str, site: str) -> dict[str, str] | None:
        tenant = tenants.get(phone)
        if tenant.erp_site != site or provisioner is None or site not in pool:
            return None
        user = user_email(phone)
        cookies = await erp_session(erp_url, site, user, erp_password(tenant) or "-")
        if cookies is None:  # password changed in the ERP or never set: rotate and retry once
            cookies = await erp_session(erp_url, site, user, await reset_sso_password(tenant, site))
        return cookies

    async def web_login(tenant, *, first_time: bool) -> list[Outbound]:
        lang = tenant.data.get("lang") or store.load(tenant.site or DEMO_SITE, tenant.phone).lang
        site = tenant.erp_site
        if not site or provisioner is None or site not in pool:
            return [Outbound("text", t("web.not_ready", lang))]
        url = f"https://{host_for(site)}"
        try:
            if first_time:
                return [Outbound("text", t("web.sso_link", lang, url=await erp_link(tenant)))]
            login = await ensure_owner_login(
                provisioner.api(site), phone=tenant.phone, name=tenant.data.get("owner_name") or profile_names.get(tenant.phone), url=url,
                roles=STAFF_ROLES if tenant.role == "staff" else OWNER_ROLES,
            )
        except Exception:
            log.exception("web login failed site=%s", site)
            return [Outbound("text", t("web.failed", lang))]
        keep_erp_password(tenant, login.password)  # keep one-tap sign-in working after a manual reset
        return [Outbound("text", login_message(login, first_time=first_time, lang=lang))]

    async def to_inbound(batch: list[IncomingMessage]) -> Inbound:
        first, last = batch[0], batch[-1]
        signals = {
            "sent_at": first.timestamp.timestamp() if first.timestamp else None,
            "reply_to": next((m.context_message_id for m in reversed(batch) if m.context_message_id), None),
            "forwarded": any(m.forwarded for m in batch),
            "frequently_forwarded": any(m.frequently_forwarded for m in batch),
        }
        if first.kind == "text":
            joined = "\n".join(m.text or "" for m in batch).strip()
            return Inbound(last.message_id, first.sender, "text", text=joined, **signals)
        if first.kind == "interactive":
            return Inbound(first.message_id, first.sender, "button", button_id=first.button_payload or first.list_id, **signals)
        if first.kind == "button":  # template quick-reply buttons
            return Inbound(first.message_id, first.sender, "text", text=first.text or "", **signals)
        if first.kind in {"contacts", "location"}:
            return Inbound(first.message_id, first.sender, "contact" if first.kind == "contacts" else "location",
                           text=_contact_or_location_text(first.kind, first.extra), **signals)
        if first.kind == "video":
            return Inbound(first.message_id, first.sender, "video", **signals)
        if first.kind == "reaction":
            return Inbound(first.message_id, first.sender, "reaction", text=first.reaction_emoji or "",
                           sent_at=signals["sent_at"], reply_to=first.reaction_to)
        if first.kind in {"sticker", "unsupported", "system"}:
            return Inbound(first.message_id, first.sender, "ignore")
        if first.kind in {"document", "image", "audio"} and first.media_id:
            common = dict(text=first.caption, filename=first.filename, voice=first.voice, media_sha256=first.media_sha256, **signals)
            if first.kind != "audio" and seen_before(first):  # same file read before: skip the download too
                return Inbound(first.message_id, first.sender, first.kind, mime=first.declared_mime, **common)
            meta = await wa.get_media_metadata(first.media_id)
            data = await wa.download_media(meta["url"])
            return Inbound(first.message_id, first.sender, first.kind, media=data,
                           mime=meta.get("mime_type") or first.declared_mime, **common)
        return Inbound(first.message_id, first.sender, "other", **signals)

    def seen_before(message: IncomingMessage) -> bool:
        tenant = tenants.get(message.sender)
        if not message.media_sha256 or tenant.status not in {"active", "demo"}:
            return False  # onboarding and imports always need the bytes
        return store.media_get(tenant.erp_site or DEMO_SITE, f"meta:{message.media_sha256}") is not None

    async def send(to: str, item: Outbound) -> None:
        result: dict[str, Any] = {}
        if item.kind == "text":
            result = await wa.send_text(to, item.text)
        elif item.kind == "buttons":
            result = await wa.send_buttons(to, item.text, [{"id": b["id"], "title": b["title"][:20]} for b in item.buttons])
        elif item.kind == "list":
            result = await wa.send_list(to, item.text, item.buttons, button_text="📋 Options")
        elif item.kind == "audio" and item.path:
            result = await wa.upload_and_send_audio(to, item.path.read_bytes(), filename=item.filename or item.path.name)
        elif item.kind == "document" and item.path:
            result = await wa.upload_and_send_document(
                to, item.path.read_bytes(), filename=item.filename or item.path.name,
                content_type="application/pdf", caption=item.text[:1024],
            )
        sent_id = ((result or {}).get("messages") or [{}])[0].get("id")
        if sent_id:
            walog.record(message_id=sent_id, direction="out", chat=to, kind=item.kind, text=item.text,
                         meta=item.meta or {"kind": item.kind})
            walog.status(sent_id, "sent")

    async def route(inbound: Inbound) -> list[Outbound]:
        sender = inbound.sender
        tenant = tenants.get(sender)

        async def notify(item: Outbound) -> None:
            try:
                await send(sender, item)
            except WhatsAppError as exc:
                log.error("notify failed: %s", type(exc).__name__)

        if inbound.kind == "reaction" and tenant.status not in {"active", "demo"}:
            return []  # mid-onboarding a reaction carries no step
        command = parse_command(inbound.text) if inbound.kind == "text" else None
        lang = detect_lang(inbound.text, tenant.data.get("lang"))
        if command and command[0] == "login":
            ok = portal.verify_login(command[1], sender)
            return [Outbound("text", t("portal.login_ok" if ok else "portal.login_bad", lang))]
        if command and command[0] == "join":
            return await join_team(tenant, command[1], lang)
        if command and command[0] == "code":  # bare code: only acted on when it matches something open
            if portal.verify_login(command[1], sender):
                return [Outbound("text", t("portal.login_ok", lang))]
            if portal.invite_open(command[1]):
                return await join_team(tenant, command[1], lang)
        words = (inbound.text or "").strip().lower().rstrip("?.!") if inbound.kind == "text" else ""
        if tenant.status == "active" and tenant.erp_site and words in SUMMARY_ON | SUMMARY_OFF:
            tenant.data["summary"] = "off" if words in SUMMARY_OFF else "on"
            tenants.save(tenant)
            return [Outbound("text", t("sum.off" if words in SUMMARY_OFF else "sum.on", lang))]
        if tenant.status == "active" and tenant.erp_site and words in SUMMARY_WORDS:  # answered from the books, no model call
            return [Outbound("text", await summary_text(engine_for(tenant.erp_site, tenant.company).erp, lang))]
        if inbound.kind == "button" and (inbound.button_id or "").startswith(("own:", "ownno:")):
            return await owner_decision(tenant, inbound.button_id or "", lang)
        if inbound.kind == "text" and (inbound.text or "").strip().lower() in TEAM_WORDS and tenant.status == "active":
            return team_invite(tenant, lang)
        intro: list[Outbound] = []
        # A greeting gets the choice (own setup or demo). A real request, typed or spoken, is answered by the demo shop
        # at once, so nobody is stuck at a menu; their own setup (with GST) is one tap away.
        if inbound.kind != "button" and not onboarding.just_greeting(inbound) and onboarding.opens_in_demo(inbound, tenant):
            if inbound.text:
                tenant.data["lang"] = detect_lang(inbound.text, tenant.data.get("lang"))
            if profile_names.get(sender):
                tenant.data.setdefault("owner_name", profile_names[sender])
            tenant.status, tenant.step = "demo", "welcome"
            tenants.save(tenant)
            intro = [onboarding.demo_intro(tenant)]
        if tenant.status in {"new", "onboarding"} and tenant.role != "tester":
            out = await onboarding.handle(inbound, tenant, notify, profile_names.get(sender))
            if tenant.status == "active" and tenant.site:  # just finished onboarding
                out += await web_login(tenant, first_time=True)
            return out
        if inbound.kind == "text" and (wants_login(inbound.text) or wants_reset(inbound.text)):
            if tenant.status == "demo":
                return [Outbound("text", t("web.demo_only", detect_lang(inbound.text, tenant.data.get("lang"))))]
            return await web_login(tenant, first_time=wants_login(inbound.text))
        if tenant.status == "demo" and (onboarding.wants_setup(inbound) or (inbound.button_id or "").startswith("ob:")):
            return await onboarding.handle(inbound, tenant, notify, profile_names.get(sender))
        if tenant.status == "active" and tenant.site and tenant.site != DEMO_SITE and onboarding.is_migration(inbound):
            out = await onboarding.migration(inbound, tenant)
            tenants.save(tenant)
            return out
        site = tenant.erp_site or DEMO_SITE
        trial_note: list[Outbound] = []
        if tenant.status == "demo" and tenant.role != "tester" and inbound.kind in TRIAL_KINDS:
            used = int(tenant.data.get("demo_used", 0)) + 1
            lang = tenant.data.get("lang") or detect_lang(inbound.text or "", None)
            if used > demo_limit:  # the trial is over: their own setup is the way on
                return [Outbound("buttons", t("demo.limit", lang, n=demo_limit),
                                 buttons=[{"id": "ob:start", "title": t("ob.btn_own_setup", lang)}])]
            tenant.data["demo_used"] = used
            tenants.save(tenant)
            if demo_limit - used == 3:
                trial_note = [Outbound("buttons", t("demo.few_left", lang, left=3),
                                       buttons=[{"id": "ob:start", "title": t("ob.btn_own_setup", lang)}])]
        out = await engine_for(site, tenant.company).handle(inbound, can_submit=tenant.role != "staff", early=notify)
        if tenant.role == "staff":
            await tell_owner(tenant, out)
        else:
            out += await payment_links(tenant, site, out)
        return intro + out + trial_note

    # ---- ERP -> WhatsApp ------------------------------------------------
    async def reach(phone: str, items: list[Outbound]) -> bool:
        """A message we start: free-form inside WhatsApp's 24 h window, else one approved nudge template a day."""
        try:
            if window_open(walog.last_inbound_at(phone)):
                for item in items:
                    await send(phone, item)
                return True
            person = tenants.get(phone)
            today = today_ist().isoformat()
            if nudge_template and person.data.get("nudged") != today:
                await wa.send_template(phone, nudge_template, os.environ.get("MUNIM_TEMPLATE_LANG", "en"))
                person.data["nudged"] = today
                tenants.save(person)
        except WhatsAppError as exc:
            log.warning("proactive message failed: %s", type(exc).__name__)
        return False

    def lang_of(person) -> str | None:
        """The language they chat in (kept on the conversation; the tenant record may not have it)."""
        return person.data.get("lang") or (store.load(person.erp_site, person.phone).lang if person.erp_site else None)

    def site_owners(site: str) -> list:
        found = [m for m in tenants.members(site) if m.role == "owner"]
        if site == DEMO_SITE:
            found += [tenants.get(p) for p in testers]
        return found

    async def tell_owner(staff, out: list[Outbound]) -> None:
        """A staff draft waits for the owner: send it to them with a Finalise button."""
        owner_phone = staff.data.get("owner_phone")
        if not owner_phone:
            return
        owner = tenants.get(owner_phone)
        lang = lang_of(owner)
        for item in out:
            meta = item.meta or {}
            if meta.get("kind") != "document" or meta.get("submitted") or not meta.get("doc"):
                continue
            total = f", ₹{meta['total']}" if meta.get("total") else ""
            who = profile_names.get(staff.phone) or f"+{staff.phone}"
            card = Outbound("buttons", t("staff.draft_waiting", lang, who=who, doctype=meta["doctype"], name=meta["doc"],
                                          customer=meta.get("customer") or "", total=total),
                            buttons=[{"id": f"own:{meta['doctype']}:{meta['doc']}", "title": t("staff.btn_final", lang)},
                                     {"id": f"ownno:{meta['doctype']}:{meta['doc']}", "title": t("staff.btn_later", lang)}],
                            meta={"kind": "staff_draft", "doc": meta["doc"], "doctype": meta["doctype"]})
            await reach(owner_phone, [card])

    async def owner_decision(tenant, button_id: str, lang: str | None) -> list[Outbound]:
        action, _, rest = button_id.partition(":")
        doctype, _, name = rest.partition(":")
        if tenant.role not in {"owner", "tester"} or not tenant.erp_site:
            return [Outbound("text", t("owner_only", lang))]
        if action == "ownno":
            return [Outbound("text", t("staff.later", lang))]
        try:  # the owner's own site credentials: a button can't reach another business
            await engine_for(tenant.erp_site, tenant.company).erp.submit_document(doctype, name)
        except Exception:  # noqa: BLE001 - ERPNext validation; it stays a draft
            log.warning("owner finalise failed for %s", doctype)
            return [Outbound("text", t("submit_failed", lang, name=name))]
        return [Outbound("text", t("staff.finalised", lang, name=name),
                         meta={"kind": "document", "doc": name, "doctype": doctype, "submitted": True})]

    async def payments_tool(action: str, sender: str) -> dict[str, Any]:
        """The agent's online_payments tool: status, on/off, and a sign-in link to connect keys on the portal."""
        tenant = tenants.get(sender)
        status = payments.status(tenant)
        if status is None:
            return {"error": "only the owner of a set-up business can manage online payments (the shared demo: testers only)"}
        if action == "connect" or (action == "on" and not status["connected"]):
            link = f"https://{base_domain}/portal/magic/{portal.magic_link(tenant.phone)}"
            return {"send": t("pay.setup_link", lang_of(tenant), url=link), "connected": status["connected"]}
        if action in ("on", "off"):
            payments.set_enabled(tenant, action == "on")
            status = payments.status(tenant) or {}
        return {k: v for k, v in status.items() if k in ("connected", "enabled", "mode")}

    async def payment_links(tenant, site: str, out: list[Outbound]) -> list[Outbound]:
        from decimal import Decimal

        account = payments.account_for_bill(tenant, site)
        if account is None:
            return []
        extra = []
        for item in out:
            meta = item.meta or {}
            if not (meta.get("doctype") == "Sales Invoice" and meta.get("submitted") and meta.get("total")
                    and Decimal(meta["total"]) > 0):
                continue
            try:
                url = await pay_links.create(account, invoice=meta["doc"], customer_id=meta.get("customer_id") or "",
                                             customer_name=meta.get("customer") or "", amount=Decimal(meta["total"]),
                                             description=f"{tenant.company or 'Invoice'} {meta['doc']}")
            except Exception:  # noqa: BLE001 - the bill is made; a missing link must not hide that
                log.warning("razorpay link failed")
                continue
            extra.append(Outbound("text", t("pay.link", lang_of(tenant), name=meta["doc"], url=url,
                                          test=t("pay.test_mode", lang_of(tenant)) if account.mode == "test" else "")))
        return extra

    def site_company(site: str) -> str | None:
        return next((m.company for m in tenants.members(site) if m.company), None)

    def host_for(site: str) -> str:
        """The business's web address: demo.<domain>, or <its-name>.<domain>."""
        label = None if site == DEMO_SITE else tenants.label_for(site, site_company(site))
        return web_host(site, base_domain, DEMO_SITE, label)

    async def join_team(tenant, code: str, lang: str) -> list[Outbound]:
        if tenant.status == "active" and tenant.site and tenant.role in {"owner", "tester"}:
            return [Outbound("text", t("team.already", lang, company=tenant.company or tenant.site))]
        invite = portal.redeem_invite(code, tenant.phone)
        if invite is None:
            return [Outbound("text", t("team.bad_code", lang))]
        tenant.role, tenant.status, tenant.step = "staff", "active", "done"
        tenant.site, tenant.company = invite.site, invite.company
        tenant.data["owner_phone"] = invite.created_by
        tenant.data["lang"] = lang
        tenants.save(tenant)
        try:  # best effort: only reaches the owner inside WhatsApp's 24 h window
            await send(invite.created_by, Outbound("text", t("team.member_joined", tenants.get(invite.created_by).data.get("lang"), phone=tenant.phone)))
        except WhatsAppError:
            pass
        return [Outbound("text", t("team.joined", lang, company=invite.company))]

    def team_invite(tenant, lang: str) -> list[Outbound]:
        if tenant.role != "owner" or not tenant.site or tenant.site == DEMO_SITE:
            return [Outbound("text", t("team.owner_only", lang))]
        code = portal.create_invite(site=tenant.site, company=tenant.company or "", created_by=tenant.phone)
        others = [m for m in tenants.members(tenant.site) if m.phone != tenant.phone]
        members = "\n".join(f"• +{m.phone}" for m in others) or t("team.none", lang)
        link = wa_link(bot_number, f"JOIN {code}")
        return [Outbound("text", t("team.invite", lang, members=members, link=link, code=code, domain=base_domain))]

    async def send_password(tenant) -> bool:
        replies = await web_login(tenant, first_time=False)
        try:
            for item in replies:
                await send(tenant.phone, item)
        except WhatsAppError:
            return False
        return bool(replies) and "🔒" in replies[0].text

    async def process(batch: list[IncomingMessage]) -> None:
        sender = batch[0].sender
        window = sent_per_sender[sender]
        now = time.time()
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= RATE_LIMIT_PER_HOUR:
            log.warning("rate limited sender")
            return
        window.append(now)
        last_id = batch[-1].message_id
        if batch[0].kind == "system":
            await number_changed(batch[0])
            store.inbox_done([m.message_id for m in batch])
            return
        if batch[0].kind != "reaction":
            try:
                await wa.start_typing(last_id)
            except WhatsAppError:
                pass
        started = time.time()
        inbound = await to_inbound(batch)
        replies = await route(inbound)
        for item in replies:
            try:
                if item.kind == "speech":  # the voice reply is made after the text above has been sent
                    item = await render_speech(speaker, Path(".run/v2/media"), item)
                    if item is None:
                        continue
                await send(sender, item)
            except WhatsAppError as exc:
                log.error("send failed: %s", type(exc).__name__)
        store.inbox_done([m.message_id for m in batch])
        log.info("turn kind=%s replies=%d seconds=%.1f", inbound.kind, len(replies), time.time() - started)

    async def number_changed(message: IncomingMessage) -> None:
        new = message.new_wa_id
        if not new or new == message.sender:
            return
        moved = tenants.move(message.sender, new)
        store.move_sender(message.sender, new)
        walog.move_chat(message.sender, new)
        log.warning("customer changed WhatsApp number; account moved=%s", bool(moved))
        if moved:
            try:
                await send(new, Outbound("text", t("number_changed", moved.data.get("lang"))))
            except WhatsAppError:
                pass

    def remember_inbound(message: IncomingMessage) -> None:
        meta: dict[str, Any] = {}
        for key in ("forwarded", "frequently_forwarded", "voice", "context_message_id", "media_sha256", "filename",
                    "reaction_to", "new_wa_id", "reply_title"):
            value = getattr(message, key)
            if value:
                meta[key] = value
        if message.referral:
            meta["referral"] = json.loads(message.referral)
        walog.record(message_id=message.message_id, direction="in", chat=message.sender, kind=message.kind,
                     text=message.text or message.caption or message.reply_title or message.reaction_emoji or "",
                     meta=meta, at=message.timestamp.timestamp() if message.timestamp else None)
        tenant = tenants.get(message.sender)
        changed = False
        if message.referral and not tenant.data.get("referral"):  # first ad/post that brought them
            tenant.data["referral"] = meta["referral"]
            changed = True
        if message.profile_name and tenant.data.get("profile_name") != message.profile_name[:60]:
            tenant.data["profile_name"] = message.profile_name[:60]
            changed = True
        if changed and (tenant.status != "new" or tenant.data.get("referral")):
            tenants.save(tenant)

    def remember_status(event) -> None:
        rec = walog.status(event.message_id, event.status, event.timestamp.timestamp() if event.timestamp else None,
                           f"{event.error_code} {event.error_title}" if event.status == "failed" else None)
        if event.status == "failed":
            log.error("delivery failed kind=%s code=%s %s", rec.meta.get("kind") if rec else "?", event.error_code,
                      event.error_title)

    queues = SenderQueues(process)
    app = FastAPI(title="AI Munim v2", docs_url=None, redoc_url=None, openapi_url=None)
    if bot_number:
        from munim.core.mcp_server import OAuthStore

        mcp_store = OAuthStore(Path(".run/v2/mcp_oauth.db"))  # shared: the portal lists and disconnects AI apps
        mount_portal(app, portal=portal, tenants=tenants, bot_number=bot_number, base_domain=base_domain,
                     send_password=send_password, open_erp=erp_link, connected_apps=mcp_store.apps_for,
                     disconnect_app=lambda phone, client_id: mcp_store.revoke_subject(phone, client_id), payments=payments,
                     host_for=host_for)
    mount_sso(app, sso=sso, base_domain=base_domain, demo_site=DEMO_SITE, start_session=start_session,
              resolve_label=tenants.site_for_label)

    async def replay_pending() -> None:
        pending = store.inbox_pending()
        for _, body in pending:
            queues.put(_decode(body))
        if pending:
            log.warning("replaying %d unfinished messages after restart", len(pending))

    app.router.on_startup.append(replay_pending)

    async def media_janitor() -> None:
        async def loop() -> None:
            while True:
                removed = await asyncio.to_thread(cleanup_old_files, Path(".run/v2/media"), 30)
                if removed:
                    log.info("deleted %d media files older than 30 days", removed)
                await asyncio.sleep(24 * 3600)

        app.state.janitor = asyncio.create_task(loop())

    app.router.on_startup.append(media_janitor)

    async def reseal_secrets() -> None:
        """Key rotation: everything stored is re-encrypted with the newest data key at start."""
        sealed = payments.accounts.reseal_all()
        for person in tenants.everyone():
            value = person.data.get("erp_pw_sealed")
            plain = person.data.get("erp_pw")
            if plain or value:
                fresh = box.seal(plain) if plain else box.reseal(value)
                if fresh:
                    person.data.pop("erp_pw", None)
                    person.data["erp_pw_sealed"] = fresh
                    tenants.save(person)
                    sealed += 1
        if sealed:
            log.info("re-sealed %d stored secrets with the newest key", sealed)

    app.router.on_startup.append(reseal_secrets)

    async def warm_models() -> None:
        async def load() -> None:  # in the background: the first voice note shouldn't wait ~10 s for a model load
            for model in (transcriber, speaker):
                if hasattr(model, "warm"):
                    try:
                        await model.warm()
                    except Exception:  # noqa: BLE001 - it loads on first use instead
                        log.warning("model warm-up failed: %s", type(model).__name__)

        app.state.warm = asyncio.create_task(load())

    app.router.on_startup.append(warm_models)

    async def morning_summary() -> None:
        async def loop() -> None:
            from datetime import datetime

            while True:
                now = datetime.now(IST)
                today = now.date().isoformat()
                if summary_hour <= now.hour < summary_hour + 3:  # a morning message; a late start skips the day
                    people = {p.phone: p for p in tenants.all_active() if p.role == "owner"}
                    people.update({p: tenants.get(p) for p in testers})
                    for person in people.values():
                        site = person.erp_site
                        if not site or person.data.get("summary") == "off" or person.data.get("summary_sent") == today:
                            continue
                        try:
                            body = await summary_text(engine_for(site, person.company).erp, lang_of(person))
                            await reach(person.phone, [Outbound("text", body)])
                        except Exception:
                            log.exception("morning summary failed")
                        person.data["summary_sent"] = today
                        tenants.save(person)
                await asyncio.sleep(300)

        app.state.morning = asyncio.create_task(loop())

    if os.environ.get("MUNIM_SUMMARY", "on") == "on":
        app.router.on_startup.append(morning_summary)

    @app.post("/erp/hook/{site}")
    async def erp_hook(site: str, request: Request) -> Response:
        """ERPNext webhook: a document was finalised in the web ERP (or by a recurring schedule)."""
        import hmac as _hmac

        if not _hmac.compare_digest(request.headers.get("x-munim-token", ""), hook_token(hook_secret, site)):
            return Response("forbidden", status_code=403)
        data = await request.json()
        if not isinstance(data, dict) or walog.knows_doc(str(data.get("name") or "")):
            return Response("ok")  # the bot made it; the person already knows
        for owner in site_owners(site):
            body = describe_erp_event(data, lang_of(owner))
            if body:
                await reach(owner.phone, [Outbound("text", body, meta={"kind": "erp_event", "doc": data.get("name"),
                                                                       "doctype": data.get("doctype")})])
        return Response("ok")

    async def record_paid(paid: PaidLink) -> None:
        """A verified payment: the Payment Entry against that bill (money already in the business's account), then tell the owner."""
        from munim.core.drafts import fmt_rs

        erp = engine_for(paid.site, site_company(paid.site)).erp
        try:
            entry = await erp.create_payment_draft(paid.customer_id, paid.amount, "Razorpay", f"razorpay-{paid.payment_id}",
                                                   [(paid.invoice, paid.amount)], final=True)
        except Exception:
            log.exception("razorpay payment entry failed site=%s", paid.site)
            return
        for owner in site_owners(paid.site):
            await reach(owner.phone, [Outbound("text", t("pay.received", lang_of(owner), customer=paid.customer_name,
                                                          amount=fmt_rs(paid.amount), invoice=paid.invoice, entry=entry.name,
                                                          method=(paid.method or "online").upper()))])

    @app.post("/razorpay/webhook/{site}")
    async def razorpay_webhook(site: str, request: Request) -> Response:
        account = payments.accounts.get(site)
        body = await request.body()
        if account is None or not RazorpayLinks.verify(account, body, request.headers.get("x-razorpay-signature")):
            return Response("bad signature", status_code=401)
        paid = pay_links.paid(site, body)
        if paid is not None:
            await record_paid(paid)
        return Response("ok")

    async def payment_checker() -> None:
        async def loop() -> None:
            while True:  # payments for businesses without a webhook show up within ~2 minutes
                for site in payments.accounts.sites():
                    account = payments.accounts.get(site)
                    if account is None:
                        continue
                    try:
                        for paid in await pay_links.check_open(account):
                            await record_paid(paid)
                    except Exception:  # noqa: BLE001 - Razorpay unreachable; next round
                        log.warning("payment check failed site=%s", site)
                await asyncio.sleep(120)

        app.state.payments = asyncio.create_task(loop())

    app.router.on_startup.append(payment_checker)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        free = tenants.free_sites([x for x in pool if x != DEMO_SITE]) if pool else 0
        return {"ok": True, "jev": bool(jev_key), "erp": "erpnext" if erp_url else "demo", "free_sites": free,
                "spent_today_usd": round(ledger.spent_today(), 4)}

    @app.get("/caddy/site")
    async def caddy_site(request: Request) -> Response:
        """Caddy forward_auth for the web ERP: which ERPNext site serves this business's address."""
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        site = site_from_host(host, base_domain, DEMO_SITE, tenants.site_for_label)
        if site is None or (site != DEMO_SITE and site not in pool):
            return Response("unknown", status_code=404)
        return Response("ok", headers={"X-Erp-Site": site})

    @app.get("/caddy/ask")
    async def caddy_ask(domain: str = "") -> Response:
        """Caddy on-demand TLS: only issue certificates for hosts we actually serve."""
        names = tenants.claimed_sites() | tenants.web_labels()
        allowed = {base_domain, f"demo.{base_domain}"} | {f"{name}.{base_domain}" for name in names}
        return Response("ok" if domain in allowed else "no", status_code=200 if domain in allowed else 404)

    @app.get("/wa/webhook")
    async def verify(request: Request) -> Response:
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and verify_token and params.get("hub.verify_token") == verify_token:
            return Response(params.get("hub.challenge", ""), media_type="text/plain")
        return Response("forbidden", status_code=403)

    @app.post("/wa/webhook")
    async def receive(request: Request) -> Response:
        body = await request.body()
        if not verify_meta_signature(body, request.headers.get("x-hub-signature-256"), app_secret):
            return Response("bad signature", status_code=401)
        for name_sender, name in _profile_names(body):
            profile_names[name_sender] = name
        for event in parse_statuses(body):
            remember_status(event)
        for message in parse_webhook_payload(body):
            if message.business_phone_number_id and message.business_phone_number_id != phone_id:
                continue
            if allowed and message.sender not in allowed:
                continue
            if not store.inbox_add(message.message_id, message.sender, _encode(message)):
                continue  # webhook replay
            remember_inbound(message)
            queues.put(message)
        return Response("ok")

    if bot_number:  # your own Claude/ChatGPT: login is the WhatsApp code, like the portal
        from munim.core.mcp_server import mount_mcp
        from munim.core.portal_web import page

        def mcp_audit(event: str, data: dict[str, Any]) -> None:
            try:
                with Path(".run/v2/mcp_audit.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"at": time.time(), "event": event, **data}, ensure_ascii=False) + "\n")
            except OSError:
                log.warning("mcp audit write failed")

        start_mcp = mount_mcp(app, engine_for=engine_for, store=store, tenants=tenants, portal=portal, bot_number=bot_number,
                              base_domain=base_domain, db_path=Path(".run/v2/mcp_oauth.db"), audit=mcp_audit, page=page,
                              secret=hook_secret, oauth_store=mcp_store)
        app.router.on_startup.append(start_mcp)

    return app


def _has_module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _contact_or_location_text(kind: str, extra: str | None) -> str:
    import json

    data = json.loads(extra) if extra else None
    if kind == "contacts":
        first = (data or [{}])[0]
        name = (first.get("name") or {}).get("formatted_name") or ""
        phones = first.get("phones") or [{}]
        phone = phones[0].get("wa_id") or phones[0].get("phone") or ""
        return f"BEGIN:VCARD\nFN:{name}\nTEL:{phone}\nEND:VCARD"
    data = data or {}
    return ", ".join(str(data[k]) for k in ("name", "address", "latitude", "longitude") if data.get(k))


def _encode(message: IncomingMessage) -> str:
    import json
    from dataclasses import asdict

    data = asdict(message)
    data["timestamp"] = message.timestamp.isoformat() if message.timestamp else None
    return json.dumps(data)


def _decode(body: str) -> IncomingMessage:
    import json
    from datetime import datetime

    data = json.loads(body)
    data["timestamp"] = datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else None
    return IncomingMessage(**data)


def _profile_names(body: bytes) -> list[tuple[str, str]]:
    import json

    try:
        payload = json.loads(body)
    except ValueError:
        return []
    found = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for contact in change.get("value", {}).get("contacts", []):
                name = (contact.get("profile") or {}).get("name")
                if contact.get("wa_id") and name:
                    found.append((contact["wa_id"], str(name)[:60]))
    return found


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # access_log off: Meta's verify request carries the verify token in the query string
    uvicorn.run(
        create_app(),
        host=os.environ.get("MUNIM_V2_HOST", "127.0.0.1"),
        port=int(os.environ.get("MUNIM_V2_PORT", "8800")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
