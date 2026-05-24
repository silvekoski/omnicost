#!/usr/bin/env python3
"""One-shot recolor: cyan+pink neon theme -> 'Graphite & Clay' (warm, detuned).
Theme/semantic/chrome colors are remapped; the 6-service categorical palette is
replaced with a muted set; the 3D premium heat scale and map heat ramps are detuned."""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["index.html", "cost_city.html", "geo_distribution.html", "customers.html"]

# --- targeted (structure-aware) replacements, applied before the literal sweep ---
SPECIAL = [
    # geo: 6-service categorical palette -> muted, still-distinct hues
    ('"serviceColors":{"database-relational":"#7367f0","streaming":"#eb3d63",'
     '"search":"#ffab1d","database-nosql":"#00bad1","observability":"#28c76f",'
     '"analytics":"#2092ec"}',
     '"serviceColors":{"database-relational":"#7ba78f","streaming":"#c8795a",'
     '"search":"#d6b06a","database-nosql":"#7e8fb0","observability":"#b079a0",'
     '"analytics":"#5f97a6"}'),
    # cost_city: 3D building premium heat scale  (cyan->pink)  ==>  (sage->clay)
    ('0.519 + t*0.444, 0.80, 0.42 + t*0.14); // cyan #00bad1 -> pink #eb3d63',
     '0.409 - t*0.364, 0.22 + t*0.30, 0.55 - t*0.03); // sage -> clay'),
    # cost_city: per-cloud floor districts — drop saturation so they read as earthy
    ('setHSL(h, 0.35, 0.13)', 'setHSL(h, 0.18, 0.12)'),
    # cost_city: decorative 4-stop accent bar
    ('linear-gradient(90deg,#19d3c5,#3ec4c8,#9a7fd0,#ff3db0)',
     'linear-gradient(90deg,#7ba78f,#a7a06f,#c89a6a,#c87f59)'),
]

# --- rgba(...) prefix swaps (alpha + ")" tail preserved) ---
RGBA = {
    "rgba(47,51,73": "rgba(31,33,39", "rgba(13,19,32": "rgba(20,21,26",
    "rgba(10,12,20": "rgba(19,20,25", "rgba(8,11,20": "rgba(15,16,20",
    "rgba(4,6,12": "rgba(10,10,13",
    "rgba(45,196,182": "rgba(123,167,143", "rgba(46,196,182": "rgba(123,167,143",
    "rgba(90,209,255": "rgba(123,167,143", "rgba(94,234,208": "rgba(132,184,156",
    "rgba(111,177,255": "rgba(139,148,160",
    "rgba(255,61,176": "rgba(200,127,89", "rgba(255,107,157": "rgba(200,127,89",
    "rgba(235,61,99": "rgba(200,127,89", "rgba(255,77,77": "rgba(194,90,58",
    "rgba(255,180,84": "rgba(212,163,90", "rgba(255,176,32": "rgba(212,163,90",
    "rgba(115,103,240": "rgba(94,141,119",
}

# --- hex / 0x literal map (lowercase keys). White light 0xffffff intentionally absent. ---
HEX = {
    # neutrals / backgrounds
    "#25293c": "#17181c", "0x25293c": "0x17181c", "#2f3349": "#1f2127",
    "0x2f3349": "0x1f2127", "#282c40": "#1c1d22", "#23263a": "#1a1b20",
    "#0a0e16": "#17181c", "#0c111e": "#1f2127", "#070b14": "#131419",
    "#0d1320": "#14151a", "#0b1322": "#131419",
    "#3a3d53": "#2e3138", "0x3a3d53": "0x2e3138",
    # greys / text
    "#808390": "#8c8a84", "#8a9ab2": "#8c8a84", "#8b93ad": "#8c8a84",
    "#e8edf6": "#e8e7e3", "#c6d3e6": "#cfcdc6", "#c4c8d4": "#cfcdc6",
    "#b9c6da": "#cfcdc6", "#aab2c8": "#b6b4ad",
    "#ffffff": "#f4f3f0", "#fff": "#f4f3f0",
    # cyan / teal / green -> sage family
    "#00bad1": "#7ba78f", "#2ec4b6": "#7ba78f", "#19d3c5": "#84b89c",
    "#5fe0d2": "#84b89c", "#5eead0": "#84b89c", "#28c76f": "#7ba78f",
    "#5ad1ff": "#5e8d77", "#3ec4c8": "#7ba78f", "#bdf6ff": "#cfe0d6",
    "#2092ec": "#5e8d77", "#9cc5ff": "#aeb4ba", "#6fb1ff": "#8b94a0",
    # pink / red -> clay / burnt ; errors -> brick
    "#eb3d63": "#c87f59", "#ff7a9c": "#d4905f", "0xff7a9c": "0xd4905f",
    "#ff6b9d": "#c87f59", "#ff63c6": "#d4905f", "#ff3db0": "#d4905f",
    "#ff6b3d": "#c87f59", "#ff9f1c": "#d4a35a", "#ffd166": "#e0b878",
    "#ff4d4d": "#cf6244", "#ff3b3b": "#b15c34", "#ff2d2d": "#cf6244",
    "#ff8585": "#d98b6b", "#ff7a7a": "#d98b6b",
    # purple -> sage / warm fill
    "#7367f0": "#5e8d77", "#9a7fd0": "#7ba78f",
    "0xa99bff": "0xcdb79a", "0x6878a0": "0x6f6c6a",
    # amber warnings
    "#ffab1d": "#d4a35a", "#ffb454": "#d4a35a", "#ffc564": "#e0b878",
    "#ffb020": "#d4a35a",
    # dark text-on-bright buttons
    "#1a0512": "#201912", "#08101a": "#14151a", "#06121b": "#14151a",
}

hex_re = re.compile(r"(0x[0-9a-fA-F]{6}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})(?![0-9a-fA-F])")

def sub_hex(m):
    tok = m.group(0)
    return HEX.get(tok.lower(), tok)

total = 0
for name in FILES:
    p = HERE / name
    s = p.read_text()
    before = s
    for a, b in SPECIAL:
        s = s.replace(a, b)
    for a, b in RGBA.items():
        s = s.replace(a, b)
    s = hex_re.sub(sub_hex, s)
    if s != before:
        p.write_text(s)
        total += 1
        print(f"  recolored {name}")
    else:
        print(f"  unchanged {name}")
print(f"done: {total} files updated")
