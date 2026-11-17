#!/usr/bin/env python3
"""Headless check: drive ZSynth with fake key presses and dump frames.

Run on the device:  python3 test_ui.py /tmp/zs   (writes /tmp/zs-*.raw)
Uses a temp data dir and a file instead of the LCD; plays quietly.
"""
import os
import sys
import time

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/zs"
os.environ["ZSYNTH_FB"] = out + "-fb.raw"
os.environ["ZSYNTH_KBD"] = "/nonexistent"
os.environ["HOME"] = "/tmp/zsynth-test-home"
open(os.environ["ZSYNTH_FB"], "wb").write(b"\0" * 320 * 170 * 2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zsynth  # noqa: E402

app = zsynth.App()
for t in range(4):  # quiet
    app.vals[t][9] = 0.08
    app.push(t, 9)


def key(code, hold=0.12, mod=False):
    if mod:
        app.on_key(29, 1)
    app.on_key(code, 1)
    time.sleep(hold)
    app.on_key(code, 0)
    if mod:
        app.on_key(29, 0)


def snap(name):
    app.render()
    with open(os.environ["ZSYNTH_FB"], "rb") as f, \
            open(f"{out}-{name}.raw", "wb") as g:
        g.write(f.read())


# record a drum beat and a bass line while looping
app.on_key(57, 1); app.on_key(57, 0)          # play
app.on_key(28, 1); app.on_key(28, 0)          # rec
key(5, mod=True)                              # Ctrl+4 drums
sps = 60 / 110 / 4
t0 = time.time()
for s in range(16):
    while time.time() < t0 + s * sps:
        time.sleep(0.002)
    if s % 4 == 0:
        key(44, 0.02)                         # kick
    if s % 8 == 4:
        key(45, 0.02)                         # snare
    if s % 2 == 1:
        key(46, 0.02)                         # hat
key(2, mod=True)                              # Ctrl+1 lead
for code in (44, 46, 48):
    key(code, 0.3)
time.sleep(0.2)
snap("lead")
key(4, mod=True)                              # Ctrl+3 sampler
snap("sample")
key(5, mod=True)
snap("drums")
key(3, mod=True)                              # bass, adjust cutoff
key(17, mod=True)
for _ in range(5):
    key(31, 0.01, mod=True)
snap("bass")
app.on_key(15, 1); app.on_key(15, 0)          # help
snap("help")
app.on_key(15, 1); app.on_key(15, 0)
st = app.status()
print("playing", st["playing"], "rec", st["rec"], "voices", st["voices"],
      "grid", [int(st["grid"][r].sum()) for r in range(4)])
app.running = False
app.save_state()
app.eng.close()
print(open("/tmp/zsynth-test-home/.local/share/zsynth/pattern.txt").read()[:300])
