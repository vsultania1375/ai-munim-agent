"""Voice replies: a voice note gets a voice note back (fake transcriber/speaker; no model, no TTS)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from munim.core.engine import AgentEngine, Inbound
from munim.core.erp import DemoERP
from munim.core.state import StateStore
from tests.test_agent import ScriptedLLM

ME = "919800000001"


class Ears:
    async def transcribe(self, audio: bytes) -> str:
        return "Gupta ji pe kitna baaki hai"


class Mouth:
    available = True

    def __init__(self) -> None:
        self.said: list[tuple[str, str | None]] = []

    async def speak(self, text: str, lang: str | None) -> bytes | None:
        self.said.append((text, lang))
        return b"OggS-voice"


@pytest.fixture
def setup(tmp_path: Path):
    llm, mouth = ScriptedLLM(), Mouth()
    engine = AgentEngine(store=StateStore(tmp_path / "s.db"), erp=DemoERP(artifact_dir=tmp_path / "d", today=date(2026, 10, 27)),
                         llm=llm, media_dir=tmp_path / "m", audit_path=tmp_path / "a", transcriber=Ears(), speaker=mouth)
    return engine, llm, mouth


def voice(n: int = 1) -> Inbound:
    return Inbound(f"v{n}", ME, "audio", media=b"opus-bytes", mime="audio/ogg", voice=True)


async def test_voice_note_gets_a_voice_note_back(setup):
    engine, llm, mouth = setup
    llm.script([], "Gupta Traders pe ₹15,000 baaki hai.\nSPEAK: गुप्ता ट्रेडर्स पर 15000 रुपये बाकी हैं।")
    out = await engine.handle(voice())
    asked = llm.calls[-1]["messages"][-1]["content"][-1]["text"]
    assert "SPEAK:" in asked  # the model is asked for the spoken line only on a voice turn
    texts = [o.text for o in out if o.kind == "text"]
    assert texts[-1] == "Gupta Traders pe ₹15,000 baaki hai." and all("SPEAK" not in t for t in texts)
    assert out[-1].kind == "speech" and mouth.said == []  # the text goes out first; speech is made after it
    audio = await engine.render_voice(out[-1])
    assert audio.kind == "audio" and audio.path.read_bytes() == b"OggS-voice" and audio.path.suffix == ".ogg"
    assert mouth.said == [("गुप्ता ट्रेडर्स पर 15000 रुपये बाकी हैं।", "hi")]


async def test_what_we_heard_goes_out_before_the_answer(setup):
    engine, llm, _ = setup
    sent_early = []

    async def early(item):
        sent_early.append((item.text, len(llm.calls)))

    llm.script([], "Gupta Traders pe ₹15,000 baaki hai.")
    out = await engine.handle(voice(3), early=early)
    assert sent_early == [("Maine suna: _Gupta ji pe kitna baaki hai_", 0)]  # before the model was even called
    assert [o.text for o in out] == ["Gupta Traders pe ₹15,000 baaki hai."]  # not sent twice


async def test_switch_off_and_typed_turns_stay_text(setup):
    engine, llm, mouth = setup
    off = await engine.handle(Inbound("t1", ME, "text", text="voice reply band"))
    assert off[0].text.startswith("Voice replies off")  # plain: they wrote without emojis
    llm.script([], "Theek hai.")
    out = await engine.handle(voice(2))
    assert "SPEAK" not in llm.calls[-1]["messages"][-1]["content"][-1]["text"] and not any(o.kind == "speech" for o in out)
    await engine.handle(Inbound("t2", ME, "text", text="voice reply chalu"))
    llm.script([], "Haan.\nSPEAK: हाँ।")  # a typed message never gets a voice note, and SPEAK never shows as text
    out = await engine.handle(Inbound("t3", ME, "text", text="ok"))
    assert [o.text for o in out] == ["Haan."] and mouth.said == []
