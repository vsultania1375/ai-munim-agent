"""Inbound media handled on the VM before (or instead of) any model call.

- Type comes from the bytes (magic numbers), never from the filename or declared MIME.
- Digital PDFs become text (cheap); scanned PDFs become two small page images.
- Photos are downscaled: a 4000 px phone photo costs ~3x the tokens of 1200 px for the
  same reading accuracy on bills.
- Voice notes are transcribed locally (faster-whisper); the model only sees text.
- Every file is hashed so a re-sent document reuses facts already extracted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

MAX_DOC_BYTES = 10 * 1024 * 1024
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_IMAGE_EDGE = 1200
PDF_TEXT_MIN_CHARS = 200
PDF_TEXT_MAX_CHARS = 8000
PDF_MAX_PAGES = 3
PDF_SCAN_PAGES = 2


def sniff(data: bytes) -> str:
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"OggS"):
        return "ogg"
    if data[4:8] == b"ftyp":
        return "mp4"  # m4a/aac voice or video; caller decides by message type
    if head.startswith(b"ID3") or head[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "mp3"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx" if b"xl/" in data[:4096] or b"[Content_Types].xml" in data[:4096] else "zip"
    text = data[:2048].decode("utf-8", errors="ignore").lstrip("﻿").lstrip()
    if text.upper().startswith("BEGIN:VCARD"):
        return "vcard"
    if text and "," in text.splitlines()[0] and "\x00" not in text:
        return "csv"
    return "unknown"


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def downscale_image(data: bytes, max_edge: int = MAX_IMAGE_EDGE) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80, optimize=True)
        return out.getvalue()


@dataclass
class PreparedDoc:
    blocks: list[dict[str, Any]]  # content blocks for the model
    note: str  # what was done, for logs/audit
    pages: int = 0


def prepare_pdf(data: bytes) -> PreparedDoc:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = len(reader.pages)
    text_parts = []
    for page in reader.pages[:PDF_MAX_PAGES]:
        try:
            text_parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one broken page must not stop the rest
            text_parts.append("")
    text = re.sub(r"[ \t]+", " ", "\n".join(text_parts)).strip()
    if len(text) >= PDF_TEXT_MIN_CHARS:
        shown = text[:PDF_TEXT_MAX_CHARS]
        header = f"[PDF text extracted on server: pages 1-{min(pages, PDF_MAX_PAGES)} of {pages}]"
        return PreparedDoc([{"type": "text", "text": f"{header}\n{shown}"}], "pdf_text", pages)
    return PreparedDoc(_scan_pages(data, pages), "pdf_scan_images", pages)


def _scan_pages(data: bytes, pages: int) -> list[dict[str, Any]]:
    import pypdfium2 as pdfium

    blocks: list[dict[str, Any]] = [{"type": "text", "text": f"[Scanned PDF: first {min(pages, PDF_SCAN_PAGES)} of {pages} pages as images]"}]
    pdf = pdfium.PdfDocument(data)
    try:
        for index in range(min(len(pdf), PDF_SCAN_PAGES)):
            page = pdf[index]
            width = page.get_width()
            bitmap = page.render(scale=max(0.5, min(2.0, MAX_IMAGE_EDGE / max(width, 1))))
            buf = io.BytesIO()
            bitmap.to_pil().convert("RGB").save(buf, format="JPEG", quality=80)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(buf.getvalue())}})
    finally:
        pdf.close()
    return blocks


def prepare_image(data: bytes) -> PreparedDoc:
    small = downscale_image(data)
    return PreparedDoc([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(small)}}],
                       f"image_{len(data)//1024}kb_to_{len(small)//1024}kb", 1)


def raw_block(data: bytes, mime: str) -> dict[str, Any]:
    """Fallback: the original file as a document/image block."""
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}}
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}}


def prepare_any(data: bytes) -> PreparedDoc | None:
    """Model-ready blocks for a PDF or photo, or None if it is neither."""
    kind = sniff(data)
    try:
        if kind == "pdf":
            return prepare_pdf(data)
        if kind in {"jpeg", "png", "webp"}:
            return prepare_image(data)
    except Exception:  # noqa: BLE001 - fall back to the original bytes
        mime = "application/pdf" if kind == "pdf" else f"image/{kind}"
        return PreparedDoc([raw_block(data, mime)], "raw_fallback")
    return None


def parse_vcard(text: str) -> dict[str, str | None]:
    name = phone = None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.split(";")[0].upper()
        if key == "FN" and value.strip():
            name = value.strip()
        elif key == "TEL" and value.strip() and not phone:
            phone = re.sub(r"[^0-9+]", "", value)
    return {"name": name, "phone": phone}


# ---- voice -------------------------------------------------------------------
class Transcriber(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...


class NoTranscriber:
    async def transcribe(self, audio: bytes) -> str:
        return ""


def decode_audio_16k(audio: bytes):
    """Any WhatsApp voice format (opus/ogg, m4a, mp3) -> mono 16 kHz float32 via system ffmpeg."""
    import subprocess

    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-t", "180",
         "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
        input=audio, capture_output=True, timeout=60, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


class WhisperTranscriber:
    """faster-whisper on CPU (int8). The model loads on first use and stays in memory."""

    def __init__(self, model: str = "small", language: str | None = "hi", cache_dir: str = ".run/models") -> None:
        self.model_name = model
        self.language = language
        self.cache_dir = cache_dir
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8", download_root=self.cache_dir)
        return self._model

    def _run(self, audio: bytes) -> str:
        model = self._load()
        samples = decode_audio_16k(audio)
        segments, _ = model.transcribe(samples, language=self.language, vad_filter=True, beam_size=1)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, audio: bytes) -> str:
        async with self._lock:  # one transcription at a time on a 2-core VM
            started = time.time()
            text = await asyncio.to_thread(self._run, audio)
            log.info("transcribed %d bytes in %.1fs", len(audio), time.time() - started)
            return text


def cleanup_old_files(folder: Path, max_age_days: int = 30) -> int:
    """Delete uploaded media older than the retention window. Returns files removed."""
    if not folder.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in folder.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


PIPER_VOICES = {  # rhasspy/piper-voices on Hugging Face; downloaded once into the models folder
    "hi": "hi/hi_IN/priyamvada/medium/hi_IN-priyamvada-medium",
    "en": "en/en_US/lessac/medium/en_US-lessac-medium",
}
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


class NoSpeaker:
    available = False

    async def speak(self, text: str, lang: str | None) -> bytes | None:
        return None


class PiperSpeaker:
    """Local text-to-speech (Piper, CPU). Returns an OGG/Opus voice note, or None if anything fails."""

    available = True

    def __init__(self, cache_dir: str = ".run/models/piper") -> None:
        self.cache_dir = Path(cache_dir)
        self._voices: dict[str, object] = {}
        self._lock = asyncio.Lock()

    def _voice(self, lang: str):
        if lang not in self._voices:
            import httpx
            from piper import PiperVoice

            name = PIPER_VOICES[lang]
            model = self.cache_dir / (name.rsplit("/", 1)[1] + ".onnx")
            for path, url in ((model, f"{PIPER_BASE}/{name}.onnx"), (Path(str(model) + ".json"), f"{PIPER_BASE}/{name}.onnx.json")):
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    part = path.with_suffix(path.suffix + ".part")
                    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                        r.raise_for_status()
                        with part.open("wb") as fh:
                            for chunk in r.iter_bytes():
                                fh.write(chunk)
                    part.rename(path)
            self._voices[lang] = PiperVoice.load(str(model))
        return self._voices[lang]

    def _run(self, text: str, lang: str) -> bytes:
        import subprocess
        import wave

        voice = self._voice(lang)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            voice.synthesize_wav(text, wav)
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "48000",
             "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1"],
            input=buf.getvalue(), capture_output=True, timeout=60, check=True,
        )
        return proc.stdout

    async def speak(self, text: str, lang: str | None) -> bytes | None:
        text = (text or "").strip()[:600]
        if not text:
            return None
        async with self._lock:  # one at a time on a 2-core VM
            try:
                started = time.time()
                audio = await asyncio.to_thread(self._run, text, "en" if lang == "en" else "hi")
                log.info("spoke %d chars in %.1fs", len(text), time.time() - started)
                return audio
            except Exception:
                log.exception("voice reply failed")
                return None
