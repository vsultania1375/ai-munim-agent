"""Portal pages on the main domain: open my ERP, start a new business, manage team numbers."""
from __future__ import annotations

import html
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from munim.core import pixel
from munim.core.portal import PortalStore, qr_data_uri, wa_app_link, wa_link, wa_web_link
from munim.core.tenants import DEMO_SITE, Tenant, TenantStore
from munim.payments.service import PaymentService

COOKIE = "munim_session"

STYLE = """
:root{--paper:#fff8e7;--paper2:#f3e6c4;--ink:#1b1b1b;--muted:#5b5146;--green:#25d366;--green-d:#128c7e;--deep:#075e54;
--saffron:#ff9933;--red:#c0392b;--gold:#ffd23f;--shadow:#1b1b1b;--bg:#fdf3d8;--grid:#0000000a;--card:#fffdf6}
@media (prefers-color-scheme:dark){:root{--paper:#1d2621;--paper2:#26322b;--ink:#f3ead2;--muted:#b9b09a;--bg:#121a16;
--grid:#ffffff08;--card:#1a221e;--shadow:#000}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 "DM Sans",system-ui,-apple-system,Segoe UI,sans-serif;
background-image:linear-gradient(var(--grid) 1px,transparent 1px),linear-gradient(90deg,var(--grid) 1px,transparent 1px);background-size:24px 24px}
.px{font-family:"Press Start 2P",ui-monospace,monospace;letter-spacing:.5px}
a{color:inherit}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 0}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none}.brand b{font-size:14px}
.tag{font-size:12px;color:var(--muted)}
.frame{background:var(--card);position:relative;box-shadow:0 -4px 0 var(--ink),0 4px 0 var(--ink),-4px 0 0 var(--ink),4px 0 0 var(--ink),
8px 8px 0 4px var(--shadow);margin:4px 4px 16px 4px}
.frame.paper{background:var(--paper)}
.btn{display:inline-flex;align-items:center;gap:10px;font:12px "Press Start 2P",monospace;text-decoration:none;color:#fff;background:var(--green-d);
border:0;padding:14px 16px;cursor:pointer;box-shadow:0 -4px 0 var(--ink),0 4px 0 var(--ink),-4px 0 0 var(--ink),4px 0 0 var(--ink),inset -4px -4px 0 #0003,
6px 6px 0 4px var(--shadow);transition:transform .06s steps(1)}
.btn:hover{background:var(--deep)}.btn:active{transform:translate(4px,4px);box-shadow:0 -4px 0 var(--ink),0 4px 0 var(--ink),-4px 0 0 var(--ink),
4px 0 0 var(--ink),inset -4px -4px 0 #0003,2px 2px 0 4px var(--shadow)}
.btn.alt{background:var(--saffron);color:#1b1b1b}.btn.alt:hover{background:#ffad55}
.btn.ghost{background:var(--paper);color:var(--ink)}.btn.ghost:hover{background:var(--paper2)}
.btn:focus-visible{outline:3px dashed var(--saffron);outline-offset:6px}
.hero{display:grid;grid-template-columns:1.1fr .9fr;gap:36px;align-items:center;padding:24px 0 40px}
h1{font-size:clamp(20px,4vw,38px);line-height:1.4;margin:0 0 16px}
h1 .hi{color:var(--green-d)}@media (prefers-color-scheme:dark){h1 .hi{color:var(--green)}}
.lead{font-size:18px;color:var(--muted);margin:0 0 24px;max-width:520px}
.cta{display:flex;gap:16px;flex-wrap:wrap;align-items:center}
.scene{position:relative;min-height:440px}
.phone{position:absolute;right:30px;top:6px;width:270px;padding:14px 12px 20px;background:#34495e}
.phone .top{background:var(--deep);color:#fff;font:9px "Press Start 2P",monospace;padding:10px;display:flex;gap:8px;align-items:center}
.phone .chat{background:#ece5dd;height:300px;padding:10px;display:flex;flex-direction:column;gap:8px;overflow:hidden}
@media (prefers-color-scheme:dark){.phone .chat{background:#0b141a}}
.bub{max-width:88%;padding:8px 10px;font-size:12.5px;line-height:1.35;color:#111;opacity:0;animation:pop 14s infinite;
box-shadow:0 -2px 0 #0002,0 2px 0 #0002,-2px 0 0 #0002,2px 0 0 #0002}
.bub.me{align-self:flex-end;background:#dcf8c6}.bub.bot{align-self:flex-start;background:#fff}
.bub small{display:block;color:#555}.bub .mini{display:inline-block;margin-top:6px;background:var(--green-d);color:#fff;font:8px "Press Start 2P",monospace;padding:6px}
.b1{animation-delay:0s}.b2{animation-delay:2s}.b3{animation-delay:4.5s}.b4{animation-delay:6s}
@keyframes pop{0%{opacity:0;transform:translateY(8px)}3%{opacity:1;transform:none}82%{opacity:1}90%,100%{opacity:0}}
.munim{position:absolute;left:0;bottom:6px;width:160px;height:190px}
.munim svg{position:absolute;left:0;top:0;width:160px;height:auto}
.munim .blink{animation:blink 4s steps(1) infinite}@keyframes blink{0%,92%{opacity:0}93%,97%{opacity:1}98%,100%{opacity:0}}
.say{position:absolute;left:14px;bottom:214px;font:9px "Press Start 2P",monospace;padding:10px;white-space:nowrap}
.coin{animation:bob 1.6s steps(4) infinite}.cw{position:absolute}
.c1{left:176px;bottom:4px}.c2{left:222px;bottom:22px}.c3{left:268px;bottom:2px}
.c2 svg{animation-delay:.4s}.c3 svg{animation-delay:.8s}
@keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-14px)}}
.khata{position:absolute;right:0;bottom:0;transform:rotate(-6deg)}
section.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;padding:8px 0 36px}
.tile{padding:18px}.tile h3{font-size:11px;margin:12px 0 8px;line-height:1.6}.tile p{margin:0;color:var(--muted);font-size:14px}
.grid2{display:grid;grid-template-columns:1.2fr .8fr;gap:24px;padding-bottom:40px;align-items:start}
.card{padding:22px}.card h2{font-size:13px;line-height:1.7;margin:0 0 10px}.card p{color:var(--muted);margin:0 0 16px}
.steps{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 18px}.steps span{font:9px "Press Start 2P",monospace;padding:8px;background:var(--paper2)}
.steps span.on{background:var(--gold);color:#1b1b1b}
.qrbox{display:flex;gap:22px;align-items:center;flex-wrap:wrap}.qrbox>div{flex:1;min-width:220px}
.qr{width:184px;height:184px;background:#fff;padding:6px;image-rendering:pixelated}
.code{font:16px "Press Start 2P",monospace;letter-spacing:2px;padding:12px 14px;background:var(--paper2);display:inline-block;margin:10px 0}
.status{display:flex;align-items:center;gap:10px;font-size:14px;color:var(--muted)}
.dots i{display:inline-block;width:8px;height:8px;background:var(--green-d);margin-right:4px;animation:dot 1.2s steps(1) infinite}
.dots i:nth-child(2){animation-delay:.2s}.dots i:nth-child(3){animation-delay:.4s}@keyframes dot{0%,50%{opacity:1}51%,100%{opacity:.15}}
.ok{color:var(--green-d);font:12px "Press Start 2P",monospace}
.load{height:16px;background:var(--paper2);margin-top:12px;position:relative}
.load i{position:absolute;left:0;top:0;bottom:0;width:0;background:repeating-linear-gradient(90deg,var(--green) 0 12px,var(--green-d) 12px 16px);animation:fill 1.4s steps(12) forwards}
@keyframes fill{to{width:100%}}
.toast{padding:14px 16px;margin:4px 4px 24px;background:var(--gold);color:#1b1b1b;font-size:15px}
ul.team{list-style:none;margin:0;padding:0}ul.team li{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 0;border-top:3px dotted var(--paper2)}
ul.team li:first-child{border-top:0}
details summary{cursor:pointer;color:var(--muted);font-size:14px}
footer{padding:24px 0 40px;color:var(--muted);font-size:13px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.desk{display:block}.cta.desk{display:flex}.mob{display:none}
@media (pointer:coarse){.desk,.cta.desk{display:none}.mob{display:inline-flex}}
@media (max-width:860px){.hero,.grid2{grid-template-columns:1fr}.scene{min-height:400px}.phone{right:0;width:250px}
section.tiles{grid-template-columns:repeat(2,1fr)}}
@media (max-width:480px){.scene{min-height:380px}.phone{width:220px}.munim{transform:scale(.8);transform-origin:left bottom}.say{display:none}
section.tiles{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important}.bub{opacity:1}}
"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Munim — your accounts on WhatsApp</title><meta name="color-scheme" content="light dark">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600;700&family=Press+Start+2P&display=swap" rel="stylesheet">
<style>__STYLE__</style></head><body><div class="wrap">
<header><a class="brand" href="/">__LOGO__<span><b class="px">AI MUNIM</b><br><span class="tag">bahi-khata · WhatsApp · ERPNext</span></span></a>
__HEADER_RIGHT__</header>
__BODY__
<footer><span>Made for India's shops · nothing is written until you tap Approve</span><span>Powered by ERPNext</span></footer>
</div></body></html>"""


def page(body: str, header_right: str = "") -> HTMLResponse:
    logo = pixel.svg(pixel.MUNIM, px=2, title="AI Munim")
    doc = (PAGE.replace("__STYLE__", STYLE).replace("__LOGO__", logo)
           .replace("__HEADER_RIGHT__", header_right).replace("__BODY__", body))
    return HTMLResponse(doc, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"})


def _hero(start_link: str) -> str:
    munim = pixel.svg(pixel.MUNIM, px=10, title="Munim ji")
    blink = pixel.svg(pixel.MUNIM_BLINK, px=10, cls="blink")
    coin = pixel.svg(pixel.COIN, px=5, cls="coin")
    khata = pixel.svg(pixel.KHATA, px=8, cls="khata")
    return f"""
<section class="hero">
  <div>
    <h1 class="px">Aapka hisaab-kitaab,<br><span class="hi">WhatsApp</span> pe.</h1>
    <p class="lead">Just type or say it — <i>"Gupta ji ko 20 cement bag ka bill bana do"</i>. AI Munim drafts it in your own ERP,
    with exact GST, and waits for your Approve.</p>
    <div class="cta">
      <a class="btn" href="#open" id="heroOpen">{pixel.svg(pixel.LOCK, px=2)} Open my ERP</a>
      <a class="btn alt" href="{html.escape(start_link)}">Start free on WhatsApp</a>
    </div>
  </div>
  <div class="scene" aria-hidden="true">
    <div class="phone frame">
      <div class="top">{pixel.svg(pixel.MUNIM, px=1)} AI Munim · online</div>
      <div class="chat">
        <div class="bub me b1">Gupta ji ko 20 cement bag ka bill bana do</div>
        <div class="bub bot b2"><b>Invoice preview</b><small>20 Bag × ₹450 = ₹9,000<br>GST ₹2,520 · <b>Total ₹11,520</b></small>
          <span class="mini">✅ Approve</span></div>
        <div class="bub me b3">✅ Approve</div>
        <div class="bub bot b4">📄 Draft <b>ACC-SINV-0042</b> ban gaya. PDF attached.</div>
      </div>
    </div>
    <div class="munim">{munim}{blink}</div>
    <div class="say frame paper">Namaste ji!</div>
    <div class="cw c1">{coin}</div><div class="cw c2">{coin}</div><div class="cw c3">{coin}</div>
    {khata}
  </div>
</section>
<section class="tiles">
  <div class="tile frame">{pixel.svg(pixel.BILL, px=5)}<h3 class="px">Bills in a line</h3><p>Quotation, invoice, challan, payment — from one WhatsApp message.</p></div>
  <div class="tile frame">{pixel.svg(pixel.MIC, px=5)}<h3 class="px">Just speak</h3><p>Voice notes in Hindi or Hinglish, understood on our own server.</p></div>
  <div class="tile frame">{pixel.svg(pixel.COIN, px=5)}<h3 class="px">Paisa-perfect</h3><p>GST and totals computed by code, matched to ERPNext to the paisa.</p></div>
  <div class="tile frame">{pixel.svg(pixel.LOCK, px=5)}<h3 class="px">Your own ERP</h3><p>Every business gets its own ERPNext. Nothing moves until you tap Approve.</p></div>
</section>"""


LOGIN_SCRIPT = """<script>
const flow=document.getElementById('flow');
const step=n=>{for(let i=1;i<=3;i++){const s=document.getElementById('s'+i);if(s)s.className=i<=n?'on':''}};
const btn=document.getElementById('login');
const hero=document.getElementById('heroOpen');
if(hero&&btn)hero.onclick=e=>{e.preventDefault();document.getElementById('open').scrollIntoView({behavior:'smooth',block:'center'});btn.click();};
if(btn)btn.onclick=async()=>{
  btn.disabled=true;
  const r=await fetch('/portal/login',{method:'POST'});const d=await r.json();step(2);
  flow.innerHTML=`<div class="qrbox"><img class="qr desk" src="${d.qr}" alt="Scan to send the login message">
  <div><p class="desk"><b>WhatsApp on this computer?</b> Open it with the message typed and press Enter — keep this tab open.</p>
  <div class="cta desk"><a class="btn" href="${d.web}" target="_blank" rel="noopener">WhatsApp Web ▸</a><a class="btn ghost" href="${d.app}">WhatsApp app</a></div>
  <p class="desk" style="margin-top:14px"><b>Or scan with your phone</b> — WhatsApp opens with the message typed.</p>
  <a class="btn mob" href="${d.link}">Open WhatsApp ▸</a><div class="code">LOGIN ${d.code}</div>
  <div class="status" id="st"><span class="dots"><i></i><i></i><i></i></span> Waiting for your message · 10 min</div></div></div>`;
  const t=setInterval(async()=>{const s=await (await fetch('/portal/login/'+d.sid)).json();
    if(s.verified){clearInterval(t);step(3);
      flow.innerHTML='<p class="ok">✓ VERIFIED</p><p>Opening your khata…</p><div class="load"><i></i></div>';
      setTimeout(()=>location.replace('/portal/me/open'),1400);}
    if(s.expired){clearInterval(t);document.getElementById('st').textContent='Code expired — reload and verify again.';}},1500);
};
</script>"""

ME_SCRIPT = """<script>
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
document.getElementById('pw').onclick=async()=>{const d=await post('/portal/me/password');
  document.getElementById('pwout').innerHTML=d.ok?'<span class="ok">SENT ✓</span>':'Could not send — type <b>password reset</b> on WhatsApp.';};
const inv=document.getElementById('inv');if(inv)inv.onclick=async()=>{const d=await post('/portal/me/invite');
  document.getElementById('invout').innerHTML=`<div class="qrbox" style="margin-top:16px"><img class="qr" src="${d.qr}" alt="Scan to join the team">
  <div><p>Your staff scans this (valid 24 h, one use), or forward:</p><div class="code">JOIN ${d.code}</div><p><a href="${d.link}">Share link</a></p></div></div>`;};
async function dc(c){if(!confirm('Disconnect this app?'))return;await post('/portal/me/apps/disconnect',{client_id:c});location.reload();}
async function rzp(path,body){const d=await post('/portal/me/payments/'+path,body);
  if(d.ok){location.hash='payments';location.reload();}else{document.getElementById('rzpout').textContent=d.error||'Could not do that.';}}
const rzc=document.getElementById('rzc');if(rzc)rzc.onclick=()=>{document.getElementById('rzpout').textContent='Checking with Razorpay…';
  rzp('connect',{key_id:document.getElementById('rzk').value,key_secret:document.getElementById('rzs').value});};
async function rm(p){if(!confirm('Remove +'+p+'?'))return;await post('/portal/me/remove',{phone:p});location.reload();}
</script>"""


def payments_card(status: dict[str, Any] | None) -> str:
    """Online payments for the owner: connect their own Razorpay, switch on/off. Empty for staff and demo visitors."""
    if status is None:
        return ""
    head = ('<div class="card frame" id="payments"><h2 class="px">Online payments</h2>'
            '<p>Customers pay your bills by UPI or card through <b>your own Razorpay account</b>; the money goes to your bank '
            'and the bill is marked paid in your books. AI Munim never holds your money.</p>')
    if not status["connected"]:
        return head + """<p><small>Razorpay Dashboard → Account &amp; Settings → API Keys → Generate. Start with <b>Test mode</b>
keys (rzp_test_…) to try it; switch to Live keys after Razorpay activates your account.</small></p>
<p><input id="rzk" placeholder="Key ID  rzp_test_…" autocomplete="off" style="width:100%;padding:10px;margin:4px 0">
<input id="rzs" type="password" placeholder="Key Secret" autocomplete="off" style="width:100%;padding:10px;margin:4px 0"></p>
<div class="cta"><button class="btn alt" id="rzc">Connect Razorpay ▸</button></div><p id="rzpout"></p>
<details><summary>No Razorpay account yet?</summary><p style="margin-top:10px">Sign up at razorpay.com; Razorpay does the KYC
(PAN, bank account). Until then your UPI ID on the bill still works.</p></details></div>"""
    mode = "TEST" if status["mode"] == "test" else "LIVE"
    state = '<span class="ok">ON</span>' if status["enabled"] else "OFF"
    toggle = ("<button class=\"btn ghost\" onclick=\"rzp('off')\">Turn off</button>" if status["enabled"]
              else "<button class=\"btn alt\" onclick=\"rzp('on')\">Turn on</button>")
    return head + f"""<ul class="team"><li><span>Razorpay {mode} · {html.escape(status['key'])} · {state}</span></li></ul>
<div class="cta" style="margin-top:12px">{toggle}<button class="btn ghost" onclick="if(confirm('Disconnect Razorpay?'))rzp('disconnect')">Disconnect</button></div>
<p id="rzpout"></p><details><summary>Instant updates (optional webhook)</summary><p style="margin-top:10px"><small>Without it, payments
show up within about 2 minutes. For instant: Razorpay → Settings → Webhooks → Add: URL <b>{html.escape(status['webhook_url'])}</b>,
secret <b>{html.escape(status['webhook_secret'])}</b>, event <b>payment_link.paid</b>.</small></p></details></div>"""


def mount_portal(
    app: FastAPI,
    *,
    portal: PortalStore,
    tenants: TenantStore,
    bot_number: str,
    base_domain: str,
    send_password: Callable[[Tenant], Awaitable[bool]],
    open_erp: Callable[[Tenant], Awaitable[str | None]] | None = None,
    connected_apps: Callable[[str], list[dict[str, Any]]] | None = None,
    disconnect_app: Callable[[str, str], None] | None = None,
    payments: PaymentService | None = None,
    host_for: Callable[[str], str] | None = None,
) -> None:
    def site_url(tenant: Tenant) -> str | None:
        site = tenant.erp_site
        if not site:
            return None
        if host_for is not None:
            return f"https://{host_for(site)}"
        host = f"demo.{base_domain}" if site == DEMO_SITE else f"{site}.{base_domain}"
        return f"https://{host}"

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        return origin.startswith(f"https://{base_domain}")

    def me(request: Request) -> Tenant | None:
        phone = portal.session_phone(request.cookies.get(COOKIE))
        return tenants.get(phone) if phone else None

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        start_text = "Hi, I want to set up AI Munim for my business"
        start = wa_link(bot_number, start_text)
        start_web = wa_web_link(bot_number, start_text)
        logged_out = request.query_params.get("from") == "erp"
        tenant = None if logged_out else me(request)
        toast = '<div class="toast frame">👋 You are logged out of your ERP. Verify again any time to open it.</div>' if logged_out else ""
        if tenant and tenant.status == "active":
            open_card = f"""
<div class="card frame paper" id="open"><h2 class="px">Welcome back</h2>
<p>+{html.escape(tenant.phone)} · {html.escape(tenant.company or "")}</p>
<div class="cta"><a class="btn" href="/portal/me/open">Open my ERP ▸</a><a class="btn ghost" href="/portal/me">Team &amp; settings</a></div></div>"""
        else:
            open_card = """
<div class="card frame paper" id="open"><h2 class="px">Open my ERP</h2>
<p>No password. Prove it's your WhatsApp and your ERP opens right here, already signed in.</p>
<div class="steps"><span class="on" id="s1">1 · CODE</span><span id="s2">2 · SEND</span><span id="s3">3 · OPEN</span></div>
<div id="flow"><button class="btn" id="login">Verify with WhatsApp ▸</button></div></div>"""
        right = '<a class="btn ghost" href="/portal/me">My account</a>' if tenant else ""
        body = toast + _hero(start) + f"""
<section class="grid2">
{open_card}
<div>
<div class="card frame"><h2 class="px">New shop?</h2><p>Setup takes 2 minutes on WhatsApp. Your data gets its own ERP.</p>
<a class="btn alt mob" href="{html.escape(start)}">Start on WhatsApp</a>
<div class="cta desk" style="margin-bottom:16px"><a class="btn alt" href="{html.escape(start_web)}" target="_blank" rel="noopener">WhatsApp Web ▸</a></div>
<img class="qr desk" src="{qr_data_uri(start)}" alt="Scan to start on WhatsApp"><p class="desk" style="margin-top:12px">Scan with your phone camera.</p></div>
<div class="card frame"><h2 class="px">Joining a team?</h2><p>Scan the invite your owner shows you, or send the <b>JOIN</b> message they shared — from your own WhatsApp.</p></div>
</div>
</section>""" + LOGIN_SCRIPT
        response = page(body, right)
        if logged_out:
            response.delete_cookie(COOKIE)
        return response

    @app.post("/portal/login")
    async def login_start(request: Request) -> JSONResponse:
        if not same_origin(request):
            return JSONResponse({"error": "origin"}, status_code=403)
        sid, code = portal.start_login()
        message = f"LOGIN {code}"
        link = wa_link(bot_number, message)
        return JSONResponse({"sid": sid, "code": code, "link": link, "qr": qr_data_uri(link),
                             "web": wa_web_link(bot_number, message), "app": wa_app_link(bot_number, message)})

    @app.get("/portal/login/{sid}")
    async def login_status(sid: str) -> JSONResponse:
        phone = portal.login_phone(sid)
        if phone is None:
            return JSONResponse({"verified": False, "expired": not portal.login_pending(sid)})
        response = JSONResponse({"verified": True})
        response.set_cookie(COOKIE, portal.session_cookie(phone), max_age=12 * 3600, secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/portal/me", response_class=HTMLResponse)
    async def portal_me(request: Request) -> Response:
        tenant = me(request)
        if tenant is None:
            return Response(status_code=303, headers={"Location": "/#open"})
        url = site_url(tenant)
        logout = '<form method="post" action="/portal/logout"><button class="btn ghost">Log out</button></form>'
        if tenant.status != "active" or url is None:
            start = wa_link(bot_number, "setup")
            return page(f"""<section style="padding:30px 0"><div class="card frame paper"><h2 class="px">Almost there</h2>
<p>+{html.escape(tenant.phone)} isn't linked to a business yet.</p><a class="btn alt" href="{html.escape(start)}">Finish setup on WhatsApp</a></div></section>""", logout)
        role = {"owner": "Owner", "tester": "Tester"}.get(tenant.role, "Team member")
        team = ""
        if tenant.role == "owner" and tenant.site != DEMO_SITE:
            members = [m for m in tenants.members(tenant.site) if m.phone != tenant.phone]
            rows = "".join(
                f'<li><span>+{html.escape(m.phone)} <small>{html.escape(m.data.get("owner_name") or "")}</small></span>'
                f'<button class="btn ghost" onclick="rm(\'{html.escape(m.phone)}\')">Remove</button></li>' for m in members
            ) or "<li><span>Only you so far.</span></li>"
            team = f"""<div class="card frame"><h2 class="px">Team numbers</h2><p>Staff make bills and quotations from their own WhatsApp; only you can submit.</p>
<ul class="team">{rows}</ul><div class="cta" style="margin-top:16px"><button class="btn alt" id="inv">+ Invite a member</button></div><div id="invout"></div></div>"""
        apps = connected_apps(tenant.phone) if connected_apps else []
        app_rows = "".join(
            f'<li><span>{html.escape(a["name"])} <small>since {time.strftime("%d %b", time.localtime(a["connected_at"]))}'
            f'{" · used " + time.strftime("%d %b %H:%M", time.localtime(a["last_used"])) if a.get("last_used") else ""}</small></span>'
            f'<button class="btn ghost" onclick="dc(\'{html.escape(a["client_id"])}\')">Disconnect</button></li>' for a in apps
        ) or "<li><span>None yet. Add AI Munim as a connector in Claude or ChatGPT.</span></li>"
        apps_card = f"""<div class="card frame"><h2 class="px">Connected AI apps</h2>
<p>Your own Claude / ChatGPT using these books. Disconnect stops it at once.</p><ul class="team">{app_rows}</ul>
<p><small>Connector URL: <b>https://{html.escape(base_domain)}/mcp</b></small></p></div>"""
        pay_card = payments_card(payments.status(tenant)) if payments is not None else ""
        body = f"""
<section class="grid2" style="padding-top:20px">
<div>
<div class="card frame paper"><h2 class="px">{html.escape(tenant.company or "My business")}</h2><p>+{html.escape(tenant.phone)} · {role}</p>
<div class="cta"><a class="btn" href="/portal/me/open">Open my ERP ▸</a></div>
<details style="margin-top:18px"><summary>Using another device or the ERPNext app?</summary>
<p style="margin-top:10px">Address: <b>{html.escape(url)}</b><br>Login ID: <b>{html.escape(tenant.phone[-10:])}</b></p>
<button class="btn ghost" id="pw">Send me a password on WhatsApp</button><p id="pwout"></p>
<p><a href="{html.escape(url)}/login?manual=1">Open the ERP sign-in page</a></p></details></div>
{team}
{pay_card}
{apps_card}
</div>
<div class="card frame" style="text-align:center">{pixel.svg(pixel.MUNIM, px=9, title="Munim ji")}
<p class="px" style="font-size:10px;margin-top:14px">Sab hisaab ready hai!</p></div>
</section>""" + ME_SCRIPT
        return page(body, logout)

    @app.get("/portal/magic/{token}", response_class=HTMLResponse)
    async def magic_page(token: str) -> HTMLResponse:
        # a button, not an instant sign-in: link previews fetch GET links and must not use up the one-time token
        return page(f"""<section style="padding:30px 0"><div class="card frame paper"><h2 class="px">Continue</h2>
<p>Open your AI Munim settings.</p><form method="post" action="/portal/magic/{html.escape(token)}">
<button class="btn">Continue ▸</button></form></div></section>""")

    @app.post("/portal/magic/{token}")
    async def magic_use(token: str) -> Response:
        phone = portal.redeem_magic(token)
        if phone is None:
            return Response(status_code=303, headers={"Location": "/#open"})
        response = Response(status_code=303, headers={"Location": "/portal/me#payments", "Cache-Control": "no-store"})
        response.set_cookie(COOKIE, portal.session_cookie(phone), max_age=12 * 3600, secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/portal/me/payments/{action}")
    async def payments_action(action: str, request: Request) -> JSONResponse:
        tenant = me(request)
        if tenant is None or not same_origin(request) or payments is None or payments.site_of(tenant) is None:
            return JSONResponse({"ok": False, "error": "Only the business owner can do this."}, status_code=403)
        if action == "connect":
            body: dict[str, Any] = await request.json()
            try:
                await payments.connect(tenant, str(body.get("key_id") or ""), str(body.get("key_secret") or ""))
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)})
            except Exception:  # noqa: BLE001 - Razorpay down or unreachable
                return JSONResponse({"ok": False, "error": "Could not reach Razorpay. Try again in a minute."})
            return JSONResponse({"ok": True})
        if action in ("on", "off"):
            return JSONResponse({"ok": payments.set_enabled(tenant, action == "on")})
        if action == "disconnect":
            return JSONResponse({"ok": payments.disconnect(tenant)})
        return JSONResponse({"ok": False}, status_code=404)

    @app.post("/portal/me/apps/disconnect")
    async def apps_disconnect(request: Request) -> JSONResponse:
        tenant = me(request)
        if tenant is None or not same_origin(request) or disconnect_app is None:
            return JSONResponse({"ok": False}, status_code=403)
        disconnect_app(tenant.phone, str((await request.json()).get("client_id") or ""))
        return JSONResponse({"ok": True})

    @app.get("/portal/me/open")
    async def portal_open(request: Request) -> Response:
        tenant = me(request)
        link = await open_erp(tenant) if tenant is not None and open_erp is not None else None
        return Response(status_code=303, headers={"Location": link or "/portal/me", "Cache-Control": "no-store"})

    @app.post("/portal/me/password")
    async def portal_password(request: Request) -> JSONResponse:
        tenant = me(request)
        if tenant is None or not same_origin(request):
            return JSONResponse({"ok": False}, status_code=403)
        return JSONResponse({"ok": await send_password(tenant)})

    @app.post("/portal/me/invite")
    async def portal_invite(request: Request) -> JSONResponse:
        tenant = me(request)
        if tenant is None or not same_origin(request) or tenant.role != "owner" or not tenant.site:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        code = portal.create_invite(site=tenant.site, company=tenant.company or "", created_by=tenant.phone)
        link = wa_link(bot_number, f"JOIN {code}")
        return JSONResponse({"code": code, "link": link, "qr": qr_data_uri(link), "web": wa_web_link(bot_number, f"JOIN {code}")})

    @app.post("/portal/me/remove")
    async def portal_remove(request: Request) -> JSONResponse:
        tenant = me(request)
        body: dict[str, Any] = await request.json()
        if tenant is None or not same_origin(request) or tenant.role != "owner" or not tenant.site:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        ok = tenants.remove_member(tenant.site, str(body.get("phone", "")))
        return JSONResponse({"ok": ok})

    @app.post("/portal/logout")
    async def portal_logout() -> Response:
        response = Response(status_code=303, headers={"Location": "/?from=erp"})
        response.delete_cookie(COOKIE)
        return response
