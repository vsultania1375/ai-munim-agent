"""A business's brand from its logo, for the bill design: colours picked in code (no model), background left out.

The background is what the logo sits on: transparent pixels, or the colour that fills most of the image's
border. Those, and near-white / near-black pixels, are ignored; the remaining colours are clustered and the
most present saturated one becomes `primary`, a clearly different one `accent`. Primary is darkened until
white text on it is readable (WCAG contrast >= 4.5). A black-and-white logo gets a neutral charcoal theme.
"""
from __future__ import annotations

import base64
import colorsys
import html
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

NEUTRAL = "#2f3640"


@dataclass(frozen=True)
class Brand:
    primary: str  # header band / table heads, white text readable on it
    accent: str  # thin rules, highlights
    tint: str  # very light primary for zebra rows
    primary_name: str  # "blue", "saffron"... for the WhatsApp card
    background: str | None  # what was left out, for the card
    logo_png_b64: str | None = None  # resized logo embedded in the print format

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast_with_white(rgb: tuple[int, int, int]) -> float:
    return 1.05 / (_luminance(rgb) + 0.05)


def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


def _hls(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    return colorsys.rgb_to_hls(*(c / 255 for c in rgb))


def _from_hls(h: float, lightness: float, s: float) -> tuple[int, int, int]:
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, lightness, s))  # type: ignore[return-value]


def _darken_for_white_text(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    h, lightness, s = _hls(rgb)
    while _contrast_with_white(rgb) < 4.5 and lightness > 0.05:
        lightness -= 0.03
        rgb = _from_hls(h, lightness, s)
    return rgb


def _mix_white(rgb: tuple[int, int, int], share: float) -> tuple[int, int, int]:
    return tuple(round(c + (255 - c) * share) for c in rgb)  # type: ignore[return-value]


def colour_name(rgb: tuple[int, int, int]) -> str:
    h, lightness, s = _hls(rgb)
    if s < 0.15:
        return "grey" if 0.2 < lightness < 0.8 else ("black" if lightness <= 0.2 else "white")
    deg = h * 360
    if (deg < 40 or deg >= 345) and lightness < 0.35:
        return "maroon" if deg >= 345 or deg < 15 else "brown"
    for limit, name in ((15, "red"), (40, "saffron"), (65, "yellow"), (160, "green"), (195, "teal"),
                        (255, "blue"), (290, "purple"), (345, "pink"), (361, "red")):
        if deg < limit:
            return name
    return "red"


def _background(img: Image.Image) -> tuple[int, int, int] | None:
    """The colour filling most of the border (the paper the logo sits on), if one clearly dominates."""
    w, h = img.size
    border = [img.getpixel((x, y)) for x in range(w) for y in (0, 1, h - 2, h - 1)]
    border += [img.getpixel((x, y)) for y in range(h) for x in (0, 1, w - 2, w - 1)]
    opaque = [p[:3] for p in border if p[3] >= 128]
    if len(opaque) < len(border) * 0.5:
        return None  # mostly transparent edges: there is no painted background
    buckets = Counter((r // 24, g // 24, b // 24) for r, g, b in opaque)
    (key, count), = buckets.most_common(1)
    if count < len(opaque) * 0.4:
        return None
    members = [p for p in opaque if (p[0] // 24, p[1] // 24, p[2] // 24) == key]
    return tuple(sum(c[i] for c in members) // len(members) for i in range(3))  # type: ignore[return-value]


def from_logo(data: bytes) -> Brand:
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    logo = img.copy()
    logo.thumbnail((360, 160))
    out = io.BytesIO()
    logo.save(out, "PNG", optimize=True)
    logo_b64 = base64.b64encode(out.getvalue()).decode()

    img.thumbnail((160, 160))
    bg = _background(img)
    pixels = []
    for p in getattr(img, "get_flattened_data", img.getdata)():  # Pillow 12+ name; getdata before that
        if p[3] < 128:
            continue  # transparent
        rgb = p[:3]
        if bg is not None and _distance(rgb, bg) < 60:
            continue  # the logo's background
        pixels.append(rgb)

    def usable(rgb: tuple[int, int, int]) -> bool:
        _, lightness, s = _hls(rgb)
        return s >= 0.22 and 0.12 <= lightness <= 0.88  # a real colour, not paper or ink

    coloured = [p for p in pixels if usable(p)]
    if len(coloured) < max(12, len(pixels) * 0.03):  # black/white or grey logo: a neutral theme
        primary = (47, 54, 64)
        return Brand(NEUTRAL, "#7f8c8d", _hex(_mix_white(primary, 0.92)), "charcoal",
                     _hex(bg) if bg else None, logo_b64)

    # cluster: coarse buckets, then the average of each bucket
    buckets: Counter[tuple[int, int, int]] = Counter((r // 32, g // 32, b // 32) for r, g, b in coloured)
    clusters = []
    for key, count in buckets.most_common(8):
        members = [p for p in coloured if (p[0] // 32, p[1] // 32, p[2] // 32) == key]
        avg = tuple(sum(c[i] for c in members) // len(members) for i in range(3))
        _, _, s = _hls(avg)  # type: ignore[arg-type]
        readable = 0.5 + 0.5 * min(1.0, _contrast_with_white(avg) / 4.5)  # type: ignore[arg-type]
        clusters.append((count * (0.6 + s) * readable, avg))  # prefer colours that carry white text as they are
    clusters.sort(reverse=True)
    brand_rgb = clusters[0][1]
    accent_rgb = next((c for _, c in clusters[1:] if _distance(c, brand_rgb) > 90), None)
    if accent_rgb is None:  # one-colour logo: a lighter shade of it
        h, lightness, s = _hls(brand_rgb)  # type: ignore[arg-type]
        accent_rgb = _from_hls(h, min(0.55, lightness + 0.15), s)
    primary = _darken_for_white_text(brand_rgb)  # type: ignore[arg-type]
    if _hls(brand_rgb)[1] - _hls(primary)[1] > 0.28:  # type: ignore[arg-type]
        # a light brand colour (yellow, sky) turns muddy when darkened: charcoal header, brand colour as accent
        return Brand(NEUTRAL, _hex(brand_rgb), _hex(_mix_white(brand_rgb, 0.85)), colour_name(brand_rgb),  # type: ignore[arg-type]
                     _hex(bg) if bg else None, logo_b64)
    return Brand(_hex(primary), _hex(accent_rgb), _hex(_mix_white(primary, 0.92)), colour_name(brand_rgb),  # type: ignore[arg-type]
                 _hex(bg) if bg else None, logo_b64)


def neutral() -> Brand:
    primary = (47, 54, 64)
    return Brand(NEUTRAL, "#7f8c8d", _hex(_mix_white(primary, 0.92)), "charcoal", None, None)


BRANDED_DOCTYPES = {"Sales Invoice": "Accounts", "Quotation": "Selling", "Sales Order": "Selling", "Delivery Note": "Stock"}
TEMPLATE = Path(__file__).with_name("print_template.html")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
UPI_RE = re.compile(r"^[A-Za-z0-9.\-_]{2,64}@[A-Za-z]{2,32}$")


def clean_bank(raw: dict[str, str | None]) -> tuple[dict[str, str], list[str]]:
    """Bank/UPI details to print, checked by format. Returns (details, problems)."""
    out: dict[str, str] = {}
    problems = []
    if raw.get("bank_name"):
        out["bank_name"] = str(raw["bank_name"]).strip()[:60]
    if raw.get("account_name"):
        out["account_name"] = str(raw["account_name"]).strip()[:80]
    if raw.get("account_no"):
        digits = re.sub(r"\s", "", str(raw["account_no"]))
        if re.fullmatch(r"\d{9,18}", digits):
            out["account_no"] = digits
        else:
            problems.append("account number must be 9-18 digits")
    if raw.get("ifsc"):
        ifsc = str(raw["ifsc"]).strip().upper()
        if IFSC_RE.match(ifsc):
            out["ifsc"] = ifsc
        else:
            problems.append("IFSC looks wrong (like HDFC0001234)")
    if raw.get("upi_id"):
        upi = str(raw["upi_id"]).strip()
        if UPI_RE.match(upi):
            out["upi_id"] = upi
        else:
            problems.append("UPI ID looks wrong (like shopname@okhdfc)")
    if raw.get("terms"):
        out["terms"] = str(raw["terms"]).strip()[:600]
    if raw.get("footer"):
        out["footer"] = str(raw["footer"]).strip()[:120]
    if raw.get("tagline"):
        out["tagline"] = str(raw["tagline"]).strip()[:80]
    return out, problems


def signature_from_image(data: bytes) -> str:
    """A photo of a signature -> just the ink on a transparent background, cropped and small (base64 PNG)."""
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img.thumbnail((900, 900))
    gray = img.convert("L")
    hist = gray.histogram()
    paper = max(range(256), key=lambda v: hist[v])  # the most common shade is the paper
    threshold = max(60, paper - 60)  # ink is clearly darker than the paper (shadows and lines are not)
    pixels = []
    for (r, g, b, a), level in zip(getattr(img, "get_flattened_data", img.getdata)(),
                                   getattr(gray, "get_flattened_data", gray.getdata)(), strict=True):
        if a < 128 or level > threshold:
            pixels.append((255, 255, 255, 0))
        else:  # keep the pen colour, deepen it a little so it prints crisply
            pixels.append((int(r * 0.7), int(g * 0.7), int(b * 0.7), 255))
    img.putdata(pixels)
    box = img.getbbox()
    if box is None:
        raise ValueError("no ink found in the picture")
    img = img.crop(box)
    img.thumbnail((260, 90))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return base64.b64encode(out.getvalue()).decode()


def render_print_format(brand: dict, details: dict[str, str]) -> str:
    """The Jinja print format for this business: colours, logo, bank box. Brand JSON rides along in a comment."""
    e = html.escape
    logo = (f'<img src="data:image/png;base64,{brand["logo_png_b64"]}" style="max-height:62px;max-width:230px">'
            if brand.get("logo_png_b64") else "")
    tagline = f'<div class="muted" style="font-style:italic">{e(details["tagline"])}</div>' if details.get("tagline") else ""
    rows = [(label, details[key]) for key, label in (("account_name", "Account name"), ("bank_name", "Bank"),
                                                      ("account_no", "A/c no."), ("ifsc", "IFSC"), ("upi_id", "UPI"))
            if details.get(key)]
    bank = ('<div class="box"><div class="mlabel">Pay to</div>'
            + "".join(f'<div><span class="muted">{e(k)}:</span> <b>{e(v)}</b></div>' for k, v in rows) + "</div>") if rows else ""
    sign = (f'<img src="data:image/png;base64,{brand["signature_png_b64"]}" style="max-height:56px;max-width:200px;display:block;margin-left:auto">'
            if brand.get("signature_png_b64") else '<div style="height:26px"></div>')
    terms = (f'<div class="mlabel" style="margin-top:14px">Terms</div><div class="muted">{e(details["terms"]).replace(chr(10), "<br>")}</div>'
             if details.get("terms") else "")
    footer = e(details.get("footer") or "Thank you for your business.")
    saved = {**brand, "details": details}
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in (("__BRAND_JSON__", e(json.dumps(saved))), ("__PRIMARY__", brand["primary"]), ("__ACCENT__", brand["accent"]),
                       ("__TINT__", brand["tint"]), ("__LOGO__", logo), ("__TAGLINE__", tagline), ("__BANK__", bank),
                       ("__SIGNATURE__", sign), ("__TERMS__", terms), ("__FOOTER__", footer)):
        text = text.replace(key, value)
    return text


def brand_from_print_format(text: str | None) -> dict | None:
    found = re.search(r"<!--munim-brand:(.*?)-->", text or "", re.DOTALL)
    return json.loads(html.unescape(found.group(1))) if found else None


def mask_account(number: str) -> str:
    return "••••" + number[-4:]
