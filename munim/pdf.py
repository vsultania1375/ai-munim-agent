from __future__ import annotations

from pathlib import Path

WATERMARK = "DEMO - NOT A TAX OR STATUTORY DOCUMENT"


def write_demo_pdf(path: Path, title: str, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_lines = [WATERMARK, title, *lines]
    stream = "BT /F1 16 Tf 50 780 Td " + _pdf_text(safe_lines[0]) + " Tj "
    stream += "/F1 12 Tf 0 -32 Td " + _pdf_text(safe_lines[1]) + " Tj "
    for line in safe_lines[2:]:
        stream += "0 -18 Td " + _pdf_text(line[:100]) + " Tj "
    stream += "ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode('latin-1', errors='replace'))} >>\nstream\n{stream}\nendstream",
    ]
    content = "%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(content.encode("latin-1")))
        content += f"{index} 0 obj\n{obj}\nendobj\n"
    xref = len(content.encode("latin-1"))
    content += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        content += f"{offset:010d} 00000 n \n"
    content += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(content.encode("latin-1", errors="replace"))
    return path


def _pdf_text(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"({escaped})"
