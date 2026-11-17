#!/usr/bin/env python3
"""Headless check: drive ZSynth with fake key presses and dump frames.

Run on the device:  python3 test_ui.py /tmp/zs   (writes /tmp/zs-*.raw)
Uses a temporary home folder and a file instead of the LCD, and plays
quietly. If the real data folder has a pre-pattern-bank song, a copy of it
is used to check that it gets moved over.
"""
import os
import shutil
import sys
import time

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/zs"
real = os.path.expanduser("~/.local/share/zsynth")
home = "/tmp/zsynth-test-home"
shutil.rmtree(home, ignore_errors=True)
data = os.path.join(home, ".local/share/zsynth")
os.makedirs(data)
for f in ("pattern.txt", "state.json", "sample.wav"):
    if os.path.exists(os.path.join(real, f)):
        shutil.copy(os.path.join(real, f), data)
os.environ["ZSYNTH_FB"] = out + "-fb.raw"
os.environ["ZSYNTH_KBD"] = "/nonexistent"
os.environ["HOME"] = home
open(os.environ["ZSYNTH_FB"], "wb").write(b"\0" * 320 * 170 * 2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zsynth  # noqa: E402

CTRL, TAB, SPACE, ENTER, BKSP = 29, 15, 57, 28, 14
F = lambda n: 58 + n  # Fn+n -> F-key code  # noqa: E731

app = zsynth.App()
for t in range(4):  # quiet
    app.vals[t][9] = 0.05
    app.push(t, 9)
results = []


def check(name, ok):
    results.append((name, bool(ok)))


def key(code, hold=0.05, mod=False):
    if mod:
        app.on_key(CTRL, 1)
    app.on_key(code, 1)
    time.sleep(hold)
    app.on_key(code, 0)
    if mod:
        app.on_key(CTRL, 0)
    time.sleep(0.03)


def snap(name):
    app.render()
    with open(os.environ["ZSYNTH_FB"], "rb") as f, \
            open(f"{out}-{name}.raw", "wb") as g:
        g.write(f.read())


def st():
    time.sleep(0.06)
    return app.status()


migrated = os.path.exists(os.path.join(data, "pattern.txt.old"))
check("old song moved into songs/my-first-song",
      not os.path.exists(os.path.join(data, "pattern.txt")) and
      (not migrated or app.song == "my-first-song"))
first_grid = int(st()["pgrid"][0].sum())

# pattern 2: record a drum beat while looping
key(F(2))
check("Fn+2 selects pattern 2 when stopped", st()["cur_pat"] == 1)
key(5, mod=True)                      # drums
key(SPACE)
key(ENTER)
sps = 60 / app.bpm / 4
t0 = time.time() + sps * 0.5
for s in range(16):
    while time.time() < t0 + s * sps:
        time.sleep(0.002)
    if s % 4 == 0:
        key(44, 0.01)
    if s % 8 == 4:
        key(45, 0.01)
key(ENTER)
check("drums recorded into pattern 2", st()["pgrid"][1][3].sum() >= 6)
check("pattern 1 untouched", int(st()["pgrid"][0].sum()) == first_grid)

# queue pattern 1 while playing: switch waits for the loop end
key(F(1))
s1 = st()
check("Fn+1 while playing is queued", s1["next_pat"] == 0 and
      s1["cur_pat"] == 1)
time.sleep(16 * sps + 0.1)
check("queued pattern starts at loop end", st()["cur_pat"] == 0)

# copy pattern 2 into 3
key(F(2))
time.sleep(16 * sps + 0.1)
key(46, mod=True)                     # Ctrl+C
key(SPACE)                            # stop
key(F(3))
key(47, mod=True)                     # Ctrl+V
check("Ctrl+C/V copies pattern 2 to 3",
      (st()["pgrid"][2] == st()["pgrid"][1]).all() and st()["pgrid"][2].any())
snap("main")

# song screen: chain 2 2 3 1, song mode
key(TAB)
for d in (3, 3, 4, 2):                # keys 2 2 3 1
    key(d)
check("chain entered", app.chain == [1, 1, 2, 0])
key(ENTER)
check("song mode on", st()["song_mode"] == 1 and st()["chain_len"] == 4)
key(SPACE)
time.sleep(0.2)
pats = []
for _ in range(4):
    pats.append(st()["cur_pat"])
    time.sleep(16 * sps)
check(f"song plays the chain in order (heard {[p + 1 for p in pats]})",
      pats == [1, 1, 2, 0])
snap("song")
key(SPACE)

# files: save as, new song, reopen
key(TAB)
app.files_col = 0
app.files_sel[0] = 1                  # "+ save as..."
key(ENTER)
for c in (20, 18, 31, 20):            # t e s t
    key(c)
key(ENTER)
check("save as -> songs/test.zsong", app.song == "test" and
      os.path.exists(os.path.join(data, "songs/test.zsong")))
snap("files")
app.files_sel[0] = 0                  # "+ new song"
key(ENTER)
for c in (25, 38, 30, 21):            # p l a y
    key(c)
key(ENTER)
check("new song is empty", app.song == "play" and
      st()["pgrid"].sum() == 0 and not st()["song_mode"])
names = [n for _, n in app.files_items(0)]
app.files_sel[0] = names.index("test")
key(ENTER)
s2 = st()
check("reopened song has its patterns and chain",
      app.song == "test" and s2["pgrid"][1].any() and s2["pgrid"][2].any()
      and s2["chain_len"] == 4 and app.chain == [1, 1, 2, 0])

# samples column
app.files_col = 1
items = app.files_items(1)
check(f"sample library lists {[n for _, n in items]}",
      not migrated or ("sample", "first-sample.wav") in items)
key(TAB)
key(4, mod=True)
time.sleep(0.3)
check("song's sample loads after reopening",
      not migrated or st()["sample_len"] > 0)
snap("sample")
time.sleep(0.2)
app.running = False
app.save_song()
app.eng.close()
print(open(os.path.join(data, "songs/test.zsong")).read()[:400])
print()
for name, ok in results:
    print("PASS" if ok else "FAIL", name)
print(f"{sum(ok for _, ok in results)}/{len(results)} passed")
