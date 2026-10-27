"""Tiny pixel-art sprites for the portal, drawn from ASCII maps into crisp inline SVG."""
from __future__ import annotations

PALETTE: dict[str, str] = {
    "K": "#1b1b1b",  # outline
    "S": "#e0a36c",  # skin
    "s": "#c4834f",  # skin shade
    "T": "#ff9933",  # saffron turban
    "t": "#e07b12",  # turban shade
    "Y": "#ffd23f",  # gold
    "O": "#d99a00",  # gold shade
    "W": "#fffaf0",  # white kurta / paper
    "w": "#e9e1cf",  # paper shade
    "G": "#2b2b2b",  # glasses
    "g": "#9fd3ff",  # glass shine
    "M": "#3a2416",  # mustache
    "R": "#c0392b",  # khata red
    "r": "#8e2a20",  # khata shade
    "N": "#25d366",  # whatsapp green
    "n": "#128c7e",  # whatsapp dark
    "D": "#075e54",  # whatsapp deep
    "B": "#dcf8c6",  # outgoing bubble
    "L": "#ffffff",  # incoming bubble
    "P": "#34495e",  # phone body
    "p": "#5d6d7e",  # phone edge
}

MUNIM = """
.....KKKKKK.....
....KTTTTTTK....
...KTTYTTTTTK...
...KTTTTTttTK...
...KKKKKKKKKK...
...KSSSSSSSSK...
...KGGGSSGGGK...
...KGgGKKGgGK...
...KSSSsSSSSK...
...KSMMMMMMSK...
....KSSSSSSK....
..KKWWWWWWWWKK..
.KWWWRRRRRRWWWK.
.KWWWRYYYYRWWWK.
.KSWWRRRRRRWWSK.
..KWWRrrrrRWWK..
..KWWWWWWWWWWK..
..KWWK....KWWK..
..KKK......KKK..
"""

MUNIM_BLINK = MUNIM.replace("...KGgGKKGgGK...", "...KGGGKKGGGK...")

COIN = """
..KKKK..
.KYYYYK.
KYWYYYOK
KYYKKYOK
KYYKYYOK
KYYYKYOK
.KOOOOK.
..KKKK..
"""

KHATA = """
.KKKKKKKKKKKK.
KRRRRRRRRRRRRK
KRrRRRRRRRRRRK
KRrRYYYYYYRRRK
KRrRYRRRRYRRRK
KRrRYYYYYYRRRK
KRrRRRRRRRRRRK
KRrRRRRRRRRRRK
KRRRRRRRRRRRRK
.KKKKKKKKKKKK.
"""

PHONE = """
.KKKKKKKKKKKKKK.
KPPPPPPPPPPPPPPK
KPDDDDDDDDDDDDPK
KPDNNDDDDDDDDDPK
KPwwwwwwwwwwwwPK
KPwwwwwBBBBBBwPK
KPwwwwwBBBBBBwPK
KPwwwwwwwwwwwwPK
KPwLLLLLLLwwwwPK
KPwLLLLLLLwwwwPK
KPwLLLLLLLwwwwPK
KPwwwwwwwwwwwwPK
KPwwwwwBBBBBBwPK
KPwwwwwwwwwwwwPK
KPwwwwwwwwwwwwPK
KPPPPPPPPPPPPPPK
KPPPPPPKKPPPPPPK
.KKKKKKKKKKKKKK.
"""

LOCK = """
..KKKK..
.KK..KK.
.K....K.
KKKKKKKK
KYYYYYYK
KYYKKYYK
KYYYYYYK
KKKKKKKK
"""

MIC = """
...KK...
..KNNK..
..KNNK..
..KNNK..
.KKNNKK.
.K.KK.K.
..KKKK..
...KK...
"""

BILL = """
KKKKKKK.
KWWWWWKK
KWKKKWWK
KWWWWWWK
KWKKKKWK
KWWWWWWK
KWKKKWWK
KKKKKKKK
"""


def svg(art: str, *, px: int = 6, title: str = "", cls: str = "") -> str:
    """ASCII map -> inline SVG with one rect per run of same-colour pixels (keeps it small)."""
    rows = [r for r in art.strip("\n").splitlines()]
    width = max(len(r) for r in rows)
    rects = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            ch = row[x]
            if ch == "." or ch not in PALETTE:
                x += 1
                continue
            run = 1
            while x + run < len(row) and row[x + run] == ch:
                run += 1
            rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{PALETTE[ch]}"/>')
            x += run
    label = f' role="img" aria-label="{title}"' if title else ' aria-hidden="true"'
    return (
        f'<svg class="{cls}" viewBox="0 0 {width} {len(rows)}" width="{width * px}" height="{len(rows) * px}" '
        f'shape-rendering="crispEdges"{label}>{"".join(rects)}</svg>'
    )
