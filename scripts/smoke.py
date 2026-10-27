"""Run scripted conversations through the real v2 agent (Claude + Jev) without WhatsApp.

Usage: python scripts/v2_smoke.py
Buttons are written as "[tap:Approve]" / "[tap:Edit]" / "[tap:<option title>]".
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.llm import ClaudeAgentLLM
from munim.core.resolver import JevTurnResolver, NullResolver
from munim.core.server import load_env
from munim.core.state import StateStore

HAIKU_PER_MTOK = {"input_tokens": 0.10, "output_tokens": 0.50, "cache_read_input_tokens": 0.01, "cache_creation_input_tokens": 0.125}


def print_cost(audit: Path) -> None:
    import json

    totals: dict[str, int] = {}
    for line in audit.read_text(encoding="utf-8").splitlines():
        for key, value in json.loads(line).get("usage", {}).items():
            totals[key] = totals.get(key, 0) + value
    dollars = sum(HAIKU_PER_MTOK.get(k, 0) * v / 1_000_000 for k, v in totals.items())
    print(f"\n=== usage {totals} ~ ${dollars:.4f} (Haiku 5.5 list price) ===")


CONVERSATIONS: dict[str, list[str]] = {
    "quotation_edit_approve": [
        "Gupta ji ko 20 cement bag ka quotation bana do",
        "rate 450 nahi 470 kar do",
        "Quantity 22 kardo",
        "[tap:Approve]",
    ],
    "vague_then_slots": ["Mujhe quotation banana hai", "Sharma ji, 10 cctv aur 1 dvr"],
    "unknown_customer_add": ["Amit ko 20 cctv ka bill bnado", "Haan add kar do, naam Amit Electronics"],
    "short_answers": ["Gupta ka outstanding", "[tap:Gupta Traders]", "unko naya maal dena chahiye?"],
    "offtopic_and_greeting": ["Hello baby", "Array me x search karne ka code likho", "Mera horoscope batao"],
    "typed_approve": ["Verma ji se 5000 UPI payment aaya", "approve"],
    "unsupported": ["is mahine ki salary bana do"],
}


async def run(name: str, script: list[str], tmp: Path) -> None:
    engine = AgentEngine(
        store=StateStore(tmp / f"{name}.db"),
        erp=DemoERP(artifact_dir=tmp / "docs"),
        llm=ClaudeAgentLLM(model=os.environ.get("MUNIM_V2_MODEL", "claude-haiku-5-5")),
        resolver=JevTurnResolver(os.environ["JEV_API_KEY"]) if os.environ.get("JEV_API_KEY") else NullResolver(),
        media_dir=tmp / "media",
        audit_path=tmp / "audit.jsonl",
    )
    print(f"\n===== {name} =====")
    last_buttons: list[dict[str, str]] = []
    for n, line in enumerate(script):
        if line.startswith("[tap:"):
            title = line[5:-1]
            button = next((b for b in last_buttons if title.lower() in b["title"].lower()), None)
            if button is None:
                print(f"  !! no button '{title}' available")
                continue
            inbound = Inbound(f"{name}-{n}", "919800000001", "button", button_id=button["id"])
        else:
            inbound = Inbound(f"{name}-{n}", "919800000001", "text", text=line)
        started = time.time()
        out = await engine.handle(inbound)
        print(f"\nUSER: {line}   ({time.time() - started:.1f}s)")
        for item in out:
            if item.kind == "buttons":
                last_buttons = item.buttons
                print("BOT [buttons " + " | ".join(b["title"] for b in item.buttons) + "]:")
            else:
                print(f"BOT [{item.kind}]:")
            print("   " + item.text.replace("\n", "\n   "))


async def main() -> None:
    load_env(Path(".env"))
    only = set(sys.argv[1:])
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for name, script in CONVERSATIONS.items():
            if not only or name in only:
                await run(name, script, Path(tmp))
        audit = Path(tmp) / "audit.jsonl"
        if audit.exists():
            print_cost(audit)


if __name__ == "__main__":
    asyncio.run(main())
