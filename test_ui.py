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

# ---- edit screen: drum grid on pattern 4 ----
FK, ZK, XK, CK, AK = 33, 44, 45, 46, 30
key(F(4))
key(5, mod=True)                      # drums
key(TAB)
check("Tab opens the edit screen", app.screen == 1)
app.drum_row, app.cur_step = 0, 0


def evs(t):
    time.sleep(0.08)
    return sorted(app.events(t))


for _ in range(4):                    # kick on 1 5 9 13
    key(ENTER)
    for _ in range(4):
        key(CK)
check("Enter writes kicks on steps 1 5 9 13",
      [(e[0], e[1]) for e in evs(3)] == [(0, 36), (4, 36), (8, 36),
                                         (12, 36)])
key(XK)                               # down to snare
for _ in range(4):
    key(CK)
key(ENTER)
key(AK)
snare = [e for e in evs(3) if e[1] == 38]
check(f"snare + accent on step 5 ({snare})",
      snare == [(4, 38, 127, 1)])
key(FK)                               # back to kick, step 5
key(ENTER)
check("Enter on a hit removes it",
      [e[0] for e in evs(3) if e[1] == 36] == [0, 8, 12])
key(75)                               # ] shift right
check("] shifts the drums right",
      [e[0] for e in evs(3) if e[1] == 36] == [1, 9, 13])
snap("drums")
key(74)                               # [ shift back

# ---- edit screen: step entry on the lead ----
key(2, mod=True)                      # lead (octave 4: Z = C4 = 60)
app.cur_step = 0
key(ZK)                               # C on step 1
app.on_key(ZK, 1)                     # chord C+E on step 2
app.on_key(CK, 1)
time.sleep(0.05)
app.on_key(ZK, 0)
app.on_key(CK, 0)
key(ENTER)                            # rest on step 3
key(73)                               # = : length 2
key(XK)                               # D, 2 steps long, on step 4
lead = [(e[0], e[1], e[3]) for e in evs(0)]
check(f"step entry writes notes, chord, rest, length ({lead})",
      lead == [(0, 60, 1), (1, 60, 1), (1, 64, 1), (3, 62, 2)]
      and app.cur_step == 5)
app.cur_step = 0
key(ZK)                               # same note again removes it
check("same note on a step removes it",
      (0, 60) not in [(e[0], e[1]) for e in evs(0)])
app.cur_step = 1
key(BKSP)
check("Bksp clears the step",
      [(e[0], e[1]) for e in evs(0)] == [(3, 62)])
key(105)                              # Fn+Z (left) moves left
check("Fn+Z moves the cursor", app.cur_step == 0)
key(71)                               # - : back to length 1
for k in (ZK, 46, 48, 49, 48, 47, 46, 16):   # a little tune to look at
    key(k)
snap("roll")
key(3, mod=True)                      # back to bass for the rest

# sound screen and instrument library
UP, DOWN, LEFT, RIGHT, ESC = 103, 108, 105, 106, 1
lib = os.path.join(data, "instruments")
check("starter library made",
      {"saw-lead", "acid-bass", "sub-bass"} <=
      set(app.inst_list("synth")) and "kit" in app.inst_list("drums"))
check("new song tracks use the starter instruments",
      app.inst == ["saw-lead", "square-bass", "one-shot", "kit"])
before = dict(app.vals[1])
key(30, mod=True)                     # Ctrl+A on EDIT: no longer edits
check("Ctrl+A off the SOUND screen leaves the sound alone",
      app.vals[1] == before)
key(TAB)
check("Tab from EDIT opens SOUND", app.screen == zsynth.S_SOUND)
app.sel[1] = 0
key(DOWN)                             # Fn+X: next setting (CUTOFF)
cut = app.vals[1][1]
key(RIGHT)                            # Fn+C: raise it
check("Fn+X/C pick and change a setting, marked as changed",
      app.sel[1] == 1 and app.vals[1][1] > cut and app.inst_mod[1])
app.vals[1][9] = 0.05
app.push(1, 9)
mine = dict(app.vals[1])
snap("sound")
key(ENTER)
check("Enter opens the library on the bass's instrument",
      app.lib and app.lib_items()[app.lib["sel"]][1] == "square-bass")
key(DOWN)
heard = app.lib_items()[app.lib["sel"]][1]
check(f"moving down auditions the next one ({heard})",
      not app.same_sound(1, app.vals[1], mine))
snap("library")
key(ESC)
check("Esc puts your sound back", app.lib is None and
      app.vals[1] == mine and app.inst[1] == "square-bass" and app.inst_mod[1])
key(ENTER)
while app.lib["sel"]:
    key(UP)
key(ENTER)                            # "+ save this sound as..."
for _ in range(20):
    key(BKSP)
for c in (48, 30, 31, 31):            # b a s s
    key(c)
key(ENTER)
check("save as writes instruments/synth/bass.zinst",
      os.path.exists(os.path.join(lib, "synth/bass.zinst")) and
      app.inst[1] == "bass" and not app.inst_mod[1])
key(ENTER)
app.lib_select("acid-bass")
key(ENTER)
check("Enter uses acid-bass", app.inst[1] == "acid-bass" and
      abs(app.vals[1][1] - 350) < 1)
app.vals[1][9] = 0.05
app.push(1, 9)
key(ENTER)
app.lib_select("bass")
key(19)                               # R: rename
for _ in range(20):
    key(BKSP)
for c in (34, 19, 24, 22, 44, 46):    # g r o u z c -> "grouzc"
    key(c)
key(ENTER)
check("rename moves the library file",
      os.path.exists(os.path.join(lib, "synth/grouzc.zinst")) and
      not os.path.exists(os.path.join(lib, "synth/bass.zinst")))
app.lib_select("grouzc")
key(32)
key(32)                               # D twice: delete
check("D twice deletes it",
      not os.path.exists(os.path.join(lib, "synth/grouzc.zinst")))
key(ESC)
app.save_song()
check("the song remembers each track's instrument",
      "inst 1 acid-bass 0" in open(app.song_path(app.song)).read())
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
check("step-edited pattern 4 was saved too",
      s2["pgrid"][3][3].sum() >= 4 and s2["pgrid"][3][0].sum() >= 4)

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

# sample action menu: Enter on a sample opens it
import wave  # noqa: E402
samples = os.path.join(data, "samples")
with wave.open(os.path.join(samples, "rec-test.wav"), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(48000)
    w.writeframes(b"\0\0" * 24000)          # 0.5 s of silence
app.sample_name = "rec-test.wav"             # the open song uses it
app.save_song()
for _ in range(4):
    key(TAB)                                 # back round to FILES
app.files_col = 1
app.select_sample("rec-test.wav")
key(ENTER)
check("Enter on a sample opens its menu",
      app.menu and app.menu["name"] == "rec-test.wav")
key(25)                                      # P: play
check("P plays it", app.previewing() == "rec-test.wav")
snap("sample-menu")
key(25)
check("P again stops it", app.previewing() is None)
key(22)                                      # U: duplicate
check("duplicate makes rec-test-copy",
      os.path.exists(os.path.join(samples, "rec-test-copy.wav")) and
      app.menu is None)
app.select_sample("rec-test.wav")
key(ENTER)
key(19)                                      # R: rename, name is prefilled
check("rename prompt starts with the old name",
      app.prompt and app.prompt["text"] == "rec-test")
for _ in range(4):
    key(BKSP)
for c in (34, 19, 23, 25):                   # g r i p  -> "rec-grip"
    key(c)
key(ENTER)
song_txt = open(os.path.join(data, "songs", app.song + ".zsong")).read()
check("rename moves the file and the song follows",
      os.path.exists(os.path.join(samples, "rec-grip.wav")) and
      not os.path.exists(os.path.join(samples, "rec-test.wav")) and
      app.sample_name == "rec-grip.wav" and "sample rec-grip.wav" in song_txt)
app.select_sample("rec-test-copy.wav")
key(ENTER)
key(38)                                      # L: use in sampler
check("L loads it into the sampler",
      app.sample_name == "rec-test-copy.wav" and app.track == 2)
app.select_sample("rec-test-copy.wav")
key(ENTER)
key(32)                                      # D: first press only warns
check("first D only warns",
      os.path.exists(os.path.join(samples, "rec-test-copy.wav")))
key(32)
check("second D deletes and unloads it",
      not os.path.exists(os.path.join(samples, "rec-test-copy.wav")) and
      app.sample_name == "" and app.menu is None)
key(ENTER)
key(1)                                       # Esc closes the menu
check("Esc closes the menu", app.menu is None and app.screen == zsynth.S_FILES)
time.sleep(0.2)
app.running = False
app.save_song()
app.eng.close()
print(open(os.path.join(data, "songs/test.zsong")).read()[:400])
print()
for name, ok in results:
    print("PASS" if ok else "FAIL", name)
print(f"{sum(ok for _, ok in results)}/{len(results)} passed")
