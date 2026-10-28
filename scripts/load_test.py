"""Load test. Two parts, both free (no model calls):

  bot  - the whole WhatsApp pipeline in-process: signed webhooks from many chats at once, per-chat queues, state,
         a scripted model turn that really runs tools (find a customer, prepare a bill on the demo ERP), and replies.
         python scripts/load_test.py bot --chats 50 --messages 4
  erp  - ERPNext itself, read-only: what agent turns call most (find customer, dues, item) and PDF printing, at
         rising concurrency. Run where ERPNEXT_URL and the API keys are set (inside the bot container).
         python scripts/load_test.py erp --seconds 15
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))] if ordered else 0.0


# ---------------------------------------------------------------- bot pipeline
def bot(chats: int, messages: int) -> None:
    from unittest import mock

    from fastapi.testclient import TestClient

    secret = "load-test"
    os.chdir(tempfile.mkdtemp(prefix="munim-load-"))
    os.environ.update({
        "WA_PHONE_NUMBER_ID": "P1", "WA_APP_SECRET": secret, "WA_ACCESS_TOKEN": "x", "ANTHROPIC_API_KEY": "x",
        "MUNIM_V2_TESTERS": ",".join(f"9198{n:08d}" for n in range(chats)),  # testers: demo shop, no trial limit
        "MUNIM_STT": "none", "MUNIM_TTS": "none", "ERPNEXT_URL": "", "MUNIM_V2_POOL_DIR": "pool", "MUNIM_BOT_NUMBER": "",
        "JEV_API_KEY": "", "MUNIM_SUMMARY": "off", "MUNIM_DAILY_BUDGET_USD": "1000",
    })
    from munim.core import llm as llm_module
    from munim.whatsapp import client as wa_client

    sent_at: dict[str, list[float]] = {}

    async def send(self, to, text, *rest, **kw):
        sent_at.setdefault(to, []).append(time.perf_counter())
        return {"messages": [{"id": f"wamid.{to}.{len(sent_at[to])}"}]}

    async def nothing(self, *a, **k):
        return {}

    async def run_turn(self, *, system, messages, tools, execute):  # what a real bill turn does, minus the model
        found = await execute("find_customer", {"name": "Gupta"})
        customer = (found.get("candidates") or [{}])[0].get("customer_id", "CUST-0001")
        await execute("find_item", {"name": "cement"})
        await execute("propose_sales_document", {"kind": "invoice", "customer_id": customer,
                                                 "lines": [{"item_code": "CEM-50", "qty": 20, "rate_source": "price_list"}]})
        return llm_module.AgentResult("Preview bhej diya.", "end_turn", 2, {"input_tokens": 1})

    patches = [mock.patch.object(wa_client.WhatsAppClient, name, new) for name, new in
               (("send_text", send), ("send_buttons", send), ("send_list", send), ("start_typing", nothing))] + [mock.patch.object(llm_module.ClaudeAgentLLM, "run_turn", run_turn)]
    for p in patches:
        p.start()
    from munim.core.server import create_app

    posted: dict[str, list[float]] = {}
    with TestClient(create_app(env_file=Path("none.env"))) as client:
        started = time.perf_counter()
        for round_ in range(messages):
            for n in range(chats):
                phone = f"9198{n:08d}"
                body = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "P1"}, "messages": [{
                    "from": phone, "id": f"wamid.in.{phone}.{round_}", "timestamp": str(int(time.time())), "type": "text",
                    "text": {"body": f"Gupta ji ko 20 cement bag ka bill {round_}"}}]}}]}]}).encode()
                sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                posted.setdefault(phone, []).append(time.perf_counter())
                assert client.post("/wa/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
            time.sleep(2.0)  # a person waits for the answer before writing again (and the 1.5 s burst window passes)
        ingest = time.perf_counter() - started
        deadline = time.time() + 300
        while time.time() < deadline and sum(len(v) for v in sent_at.values()) < chats * messages * 2:
            time.sleep(0.2)
        total = time.perf_counter() - started
    latencies = []
    for phone, times in posted.items():
        replies = sent_at.get(phone, [])[1::2]  # each turn sends the reply text and the preview card
        latencies += [r - p for p, r in zip(times, replies, strict=False)]
    done = sum(len(v) for v in sent_at.values()) // 2
    print(json.dumps({
        "part": "bot", "chats": chats, "messages_per_chat": messages, "turns_answered": done, "of": chats * messages,
        "wall_seconds": round(total, 1), "ingest_seconds": round(ingest, 1),
        "turns_per_second": round(done / max(total - 2.0 * (messages - 1), 0.001), 1),
        "latency_p50_s": round(pct(latencies, 50), 2), "latency_p95_s": round(pct(latencies, 95), 2),
        "note": "latency includes the 1.5 s burst window every text waits for; the model call is not included",
    }, indent=2))


# ---------------------------------------------------------------- ERPNext
async def _worker(job, first: int, step: int, stop: float, took: list[float], errors: list[str]) -> None:
    i = first
    while time.perf_counter() < stop:
        t0 = time.perf_counter()
        try:
            await job(i)
            took.append(time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001 - counted, not fatal
            errors.append(type(exc).__name__)
        i += step


async def erp(seconds: float) -> None:
    from munim.core.server import _erp_from_env

    port = _erp_from_env()
    customers = [c["customer_id"] for c in (await port.find_customer("a")).candidates][:5] or ["CUST-0001"]
    bills = await port.client.list_documents("Sales Invoice", filters={"docstatus": 1}, fields=["name"], limit_page_length=3)

    async def read_turn(i: int) -> None:  # the reads of a typical turn
        await port.find_customer("Gupta")
        await port.customer_outstanding(customers[i % len(customers)])
        await port.find_item("cement")

    async def pdf(i: int) -> None:
        await port.document_pdf("Sales Invoice", bills[i % len(bills)]["name"])

    results = []
    for name, job, levels in (("turn reads (customer/item lists cached by the bot, dues read live)", read_turn, (1, 4, 8, 16)), ("pdf", pdf, (1, 2, 4))):
        if name == "pdf" and not bills:
            continue
        for level in levels:
            took: list[float] = []
            errors: list[str] = []
            stop = time.perf_counter() + seconds
            await asyncio.gather(*(_worker(job, w, level, stop, took, errors) for w in range(level)))
            results.append({"job": name, "concurrency": level, "per_second": round(len(took) / seconds, 1),
                            "p50_s": round(statistics.median(took), 3) if took else None, "p95_s": round(pct(took, 95), 3),
                            "errors": len(errors)})
            print(json.dumps(results[-1]))
            await asyncio.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="part", required=True)
    b = sub.add_parser("bot")
    b.add_argument("--chats", type=int, default=50)
    b.add_argument("--messages", type=int, default=4)
    e = sub.add_parser("erp")
    e.add_argument("--seconds", type=float, default=15)
    args = parser.parse_args()
    if args.part == "bot":
        bot(args.chats, args.messages)
    else:
        asyncio.run(erp(args.seconds))


if __name__ == "__main__":
    main()
