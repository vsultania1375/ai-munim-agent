# AI Munim — run your business's ERP from WhatsApp

[![CI](https://github.com/vsultania1375/ai-munim-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/vsultania1375/ai-munim-agent/actions/workflows/ci.yml)

A WhatsApp agent for Indian small businesses (hardware, cement, CCTV, kirana...). The owner
types or forwards things the way they already do — *"Gupta ji ko 20 cement bag ka quotation
bana do"*, a photo of a supplier bill, an old Tally export — and AI Munim turns it into
drafts in their own **ERPNext**, with exact GST maths and an Approve button before anything
is written.

**Try it:** message [wa.me/917428051112](https://wa.me/917428051112) and say *Hi*.

## What it does

| | |
|---|---|
| **Talk, don't type forms** | Quotations, invoices, delivery challans, payments received, new customers, outstanding/overdue, item rates — in English, Hinglish or Hindi. The agent replies in the user's language. |
| **One open preview, exact money** | Every write is a preview card with ✅ Approve / ✏️ Edit / ❌ Cancel. Totals and GST are computed in code (never by the model) and match the ERPNext draft to the paisa. Stale or repeated approvals are rejected. |
| **Context that holds** | "rate 470 kar do", "haan", "isse challan banao" resolve against a single conversation state (open preview, pending question, last uploaded document). |
| **Documents** | Send a bill/invoice photo or PDF; the agent reads it and can turn it into a quotation or challan. |
| **2-minute onboarding** | New numbers send a GSTIN (offline checksum + state/PAN) or a photo of a GST certificate/visiting card, confirm, pick a business type, and get **their own ERPNext site** in ~30 s. |
| **Bring old data** | Excel/CSV from Tally, Vyapar, Khatabook or any sheet: columns are detected, previewed as counts, and imported on tap (customers, items with rates/GST, opening balances as a draft entry). |
| **Full ERP on the web too** | *ERP login* sends a login for `https://<site>.aimunim.duckdns.org`; WhatsApp is one way to run the ERP, the ERPNext desk is the other. |

## Use it from your own Claude or ChatGPT

Add `https://aimunim.duckdns.org/mcp` as a custom connector (Claude: Settings → Connectors → Add custom
connector; ChatGPT: Settings → Apps & Connectors, developer mode → create). On Connect, verify with your
business's WhatsApp number — no password. Your AI can then read reports and prepare bills that you
confirm in the chat; amounts and GST still come from AI Munim's code.

## How it works

```
WhatsApp → Meta Cloud API → Caddy (HTTPS) → bot (FastAPI)
  per-chat queue (in order, burst debounce) → route by number
    new number      → onboarding → own ERPNext site from a pre-created pool
    existing owner  → agent: ConversationState → Claude + closed tools → preview card
                      Approve (one tap) → ERPNext document, submitted for the owner + final PDF
ERPNext (frappe_docker, multi-site) on the same VM; nginx picks the site by Host header
```

Design rules that came out of a failed first version (context loops, stale previews,
a 3B local model that could not follow Hinglish):

- **One state object per chat** instead of many handlers each holding their own state.
- **The LLM understands, code calculates, the owner approves.** Tools never write.
- **Buttons for every question**, so a bare "haan" is never ambiguous.
- **One ERPNext site per business**, so data never crosses tenants.

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Stack

Python 3.11+ · FastAPI · Claude Haiku 5.5 (tool use, vision) · TypeSafe Jev (optional
turn resolver) · ERPNext v16 (frappe_docker) · Caddy (on-demand TLS) · Meta WhatsApp
Cloud API · Oracle Cloud Always Free (Ampere A1, 2 OCPU / 12 GB).

## Run locally (demo ERP, no ERPNext needed)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                             # flows with a scripted fake LLM; free
cp .env.example .env                               # fill WhatsApp + Anthropic keys
python -m munim.core.server                        # http://127.0.0.1:8800
python scripts/dev_tunnel.py                       # quick tunnel + points the Meta webhook at it
```

With `ERPNEXT_URL` empty the agent runs on an in-memory demo business (Gupta Traders,
cement, CCTV...).

## Deploy (single VM)

1. Ubuntu 24.04 VM with Docker; open 80/443.
2. ERPNext: `frappe_docker` `pwd.yml` with random passwords, `FRAPPE_SITE_NAME_HEADER: $$host`,
   frontend bound to `127.0.0.1`, running the image from `infra/erpnext.Dockerfile` (ERPNext +
   India Compliance). Add `india_compliance` to `sites/apps.txt`, `bench --site <site> install-app
   india_compliance` on existing sites, then run `scripts/gst_upgrade.py` in the bot container.
3. Demo company: `infra/erpnext_seed.py` inside the backend container.
4. Tenant pool: `bash infra/make_pool.sh 3`.
5. `docker compose -f infra/docker-compose.yml -p aimunim up -d --build`, then point the
   Meta webhook to `https://<domain>/wa/webhook`.

## Backups and health

- `infra/backup.sh` (root cron in `/etc/cron.d/aimunim`, 03:00 IST): SQL dump of every ERPNext site + bot state + pool keys,
  encrypted with `/opt/aimunim/backup.key`, uploaded to a private Oracle Object Storage bucket
  through a write-only pre-authenticated URL. Objects are named by weekday, so the last 7 days
  are kept without any delete permission.
- Restore: `openssl enc -d -aes-256-cbc -pbkdf2 -pass file:backup.key -in aimunim-backup-Mon.tar.gz.enc | tar xz`
- `infra/healthcheck.sh` (same cron file, every 5 min): restarts the bot after two failed `/health` checks and
  alerts the operator on WhatsApp (once per problem per 6 h) when the bot or ERPNext is down, disk is 85% full,
  memory is under 600 MB, or there was no successful backup in 26 h. WhatsApp delivers only within 24 h of the
  operator's last message, so pair it with an external monitor on `/health`.
- `infra/pool_topup.sh` (hourly): keeps at least two empty ERPNext sites ready for new businesses.
- Capacity: one bench, one database per business (host-routed). ERPNext runs 3 gunicorn workers × 4 threads on
  the 2-vCPU VM; CPU (PDF printing, voice) is the first limit. Sites are portable, so scaling out is moving
  sites to more benches and the database to its own server.

## Cost

Haiku with prompt caching costs roughly **$0.001–0.002 per message**; the VM and domain
are free tier. Only messages cost money.

## Status and next

Working: WhatsApp agent, previews/approval, one-tap ERPNext documents + PDF, onboarding with own
site, data import, web login, language following.
Next: voice notes (STT), live GST portal verification, automatic pool top-up and waitlist
notification, business-name subdomains, purchase bills.

*This repository is the v2 rewrite of an earlier AI Munim prototype (July 2026): new agent
core, onboarding, multi-tenant ERPNext and web access; it reuses the prototype's WhatsApp
client, ERPNext REST client and Hinglish name matching.*
