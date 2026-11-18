#!/usr/bin/env python3
"""Draw the ZSynth icon: piano keys under a waveform.
Usage: make_icon.py [out.png] [size]   (90 for the launcher, 256 for the store)"""
import math
import sys
from PIL import Image, ImageDraw

S = 4
im = Image.new("RGBA", (90 * S, 90 * S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((6 * S, 8 * S, 84 * S, 82 * S), radius=12 * S,
                    fill=(36, 30, 58, 255))
# waveform
pts = [((14 + i * 0.5) * S,
        (30 - 11 * math.sin(i * 0.5 * 2 * math.pi / 22)
         * (0.6 + 0.4 * math.cos(i / 40))) * S) for i in range(125)]
d.line(pts, fill=(200, 120, 255, 255), width=3 * S, joint="curve")
# keys
x0, y0, kw, kh = 14, 46, 62 / 7, 28
for k in range(7):
    d.rounded_rectangle(((x0 + k * kw + 0.6) * S, y0 * S,
                         (x0 + (k + 1) * kw - 0.6) * S, (y0 + kh) * S),
                        radius=2 * S, fill=(236, 236, 244, 255))
for k in (0, 1, 3, 4, 5):
    cx = x0 + (k + 1) * kw
    d.rounded_rectangle(((cx - 2.6) * S, y0 * S, (cx + 2.6) * S,
                         (y0 + kh * 0.6) * S), radius=1 * S,
                        fill=(36, 30, 58, 255))
size = int(sys.argv[2]) if len(sys.argv) > 2 else 90
im.resize((size, size), Image.LANCZOS).save(sys.argv[1] if len(sys.argv) > 1
                                            else "zsynth.png")
