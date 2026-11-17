#!/usr/bin/env python3
"""ZSynth: a pocket groovebox for the M5 Cardputer Zero.

Four tracks (lead synth, bass synth, mic sampler, drum kit), eight 16-step
patterns per song, a song chain, and a library of songs and samples. This
file is the screen and keyboard front end; the sound comes from
zsynth-engine (C), driven over a pipe.

Screens (Tab cycles): MAIN (play, record, sound settings),
SONG (pattern banks and chain), FILES (songs and samples).
Fn+H shows every key.
"""
import glob
import json
import mmap
import os
import re
import signal
import struct
import subprocess
import sys
import time
import wave

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import numpy as np  # noqa: E402
import pygame  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.environ.get("ZSYNTH_ENGINE", os.path.join(HERE, "zsynth-engine"))
FB = os.environ.get("ZSYNTH_FB", "/dev/fb0")
KBD = os.environ.get(
    "ZSYNTH_KBD", "/dev/input/by-path/platform-3f804000.i2c-event")
DATA = os.path.expanduser("~/.local/share/zsynth")
SAMPLES = os.path.join(DATA, "samples")
SONGS = os.path.join(DATA, "songs")
STATE = os.path.join(DATA, "state.json")
LEGACY_PATTERN = os.path.join(DATA, "pattern.txt")
LEGACY_SAMPLE = os.path.join(DATA, "sample.wav")
SHM = "/dev/shm/zsynth.state"
W, H = 320, 170
NPAT = 8

# ---- keyboard (Linux key codes from the Cardputer Zero keymap) ----
K_ESC, K_BKSP, K_TAB, K_ENTER, K_SPACE = 1, 14, 15, 28, 57
K_CTRL, K_ALT = 29, 56
K_1, K_2, K_3, K_4 = 2, 3, 4, 5
K_Q, K_W, K_A, K_S, K_Z, K_X, K_C, K_V, K_F = 16, 17, 30, 31, 44, 45, 46, 47, 33
K_B, K_N, K_R, K_L = 48, 49, 19, 38
# Fn layer: number row -> F1..F10, F/Z/X/C -> arrows, Bksp -> Delete, H -> Help
K_FKEYS = range(59, 69)
K_UP, K_DOWN, K_LEFT, K_RIGHT, K_DEL, K_HELP = 103, 108, 105, 106, 111, 138
NOTE_KEYS = {
    44: 0, 31: 1, 45: 2, 32: 3, 46: 4, 47: 5, 34: 6, 48: 7, 35: 8, 49: 9,
    36: 10, 50: 11,
    16: 12, 3: 13, 17: 14, 4: 15, 18: 16, 19: 17, 6: 18, 20: 19, 7: 20,
    21: 21, 8: 22, 22: 23, 23: 24, 10: 25, 24: 26, 11: 27, 25: 28,
}
KEY_LABEL = {v: k for k, v in {
    "Z": 0, "S": 1, "X": 2, "D": 3, "C": 4, "V": 5, "G": 6, "B": 7, "H": 8,
    "N": 9, "J": 10, "M": 11}.items()}
CHARS = dict(zip([16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 30, 31, 32, 33, 34,
                  35, 36, 37, 38, 44, 45, 46, 47, 48, 49, 50],
                 "qwertyuiopasdfghjklzxcvbnm"))
CHARS.update({2 + i: "1234567890"[i] for i in range(10)})
CHARS[57] = "-"

SCREENS = ["MAIN", "SONG", "FILES"]

# ---- tracks and their settings ----
TRACKS = [("LEAD", "LD", (80, 200, 255)), ("BASS", "BS", (255, 140, 60)),
          ("SAMP", "SP", (200, 120, 255)), ("DRUM", "DR", (90, 220, 120))]
WAVES = ["SINE", "TRI", "SAW", "SQUARE"]
MODES = ["ONE-SHOT", "GATE"]
DRUM_NAMES = ["KICK", "RIM", "SNARE", "CLAP", "C-HAT", "LO TOM", "O-HAT",
              "HI TOM", "BELL", "CRASH", "O-HAT", "CLAP"]

# (label, engine index, kind, lo, hi)  kind: choice list / log / lin / ms
SYNTH_PARAMS = [("WAVE", 0, WAVES, 0, 3), ("CUTOFF", 1, "log", 60, 12000),
                ("RESO", 2, "lin", 0, 0.95), ("ATTACK", 3, "ms", 1, 2000),
                ("DECAY", 4, "ms", 10, 3000), ("SUSTAIN", 5, "lin", 0, 1),
                ("RELEASE", 6, "ms", 5, 4000), ("FILT ENV", 7, "lin", 0, 1),
                ("DETUNE", 8, "lin", 0, 50), ("VOLUME", 9, "lin", 0, 1)]
SAMP_PARAMS = [("MODE", 0, MODES, 0, 1), ("CUTOFF", 1, "log", 60, 12000),
               ("RESO", 2, "lin", 0, 0.95), ("ATTACK", 3, "ms", 1, 2000),
               ("RELEASE", 6, "ms", 5, 4000), ("VOLUME", 9, "lin", 0, 1)]
DRUM_PARAMS = [("TONE", 1, "log", 300, 12000), ("DECAY", 4, "lin", 0.2, 3),
               ("VOLUME", 9, "lin", 0, 1)]
PARAMS = [SYNTH_PARAMS, SYNTH_PARAMS, SAMP_PARAMS, DRUM_PARAMS]

DEFAULTS = [
    {0: 2, 1: 2500, 2: 0.3, 3: 5, 4: 300, 5: 0.6, 6: 250, 7: 0.5, 8: 8,
     9: 0.55},
    {0: 3, 1: 500, 2: 0.55, 3: 2, 4: 220, 5: 0.35, 6: 90, 7: 0.7, 8: 0,
     9: 0.7},
    {0: 0, 1: 12000, 2: 0, 3: 2, 6: 150, 9: 0.8},
    {1: 12000, 4: 1.0, 9: 0.8},
]
DEFAULT_OCT = [4, 2, 4, 3]

BG = (10, 12, 16)
PANEL = (24, 28, 36)
GRID = (44, 50, 62)
TEXT = (230, 234, 240)
DIM = (130, 138, 152)
RED = (240, 70, 70)
AMBER = (245, 190, 60)
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def font(name, size):
    try:
        return pygame.font.Font(FONT_DIR + name, size)
    except (OSError, FileNotFoundError):
        return pygame.font.Font(None, size + 4)


def slug(name):
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return s[:24] or "untitled"


class Engine:
    def __init__(self):
        self.p = subprocess.Popen([ENGINE], stdin=subprocess.PIPE,
                                  text=True, bufsize=1)

    def send(self, *parts):
        try:
            self.p.stdin.write(" ".join(str(x) for x in parts) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def close(self):
        self.send("quit")
        try:
            self.p.stdin.close()
            self.p.wait(timeout=2)
        except Exception:
            self.p.kill()


class App:
    def __init__(self):
        for d in (SAMPLES, SONGS):
            os.makedirs(d, exist_ok=True)
        pygame.font.init()
        self.s = pygame.Surface((W, H))
        self.f_sm = font("DejaVuSansCondensed.ttf", 10)
        self.f_smb = font("DejaVuSansCondensed-Bold.ttf", 10)
        self.f_mid = font("DejaVuSans-Bold.ttf", 12)

        self.screen = 0
        self.track = 0
        self.sel = [0, 0, 0, 0]
        self.octave = list(DEFAULT_OCT)
        self.vals = [dict(d) for d in DEFAULTS]
        self.bpm = 110
        self.song = "untitled"
        self.sample_name = ""
        self.chain = []
        self.chain_cur = 0
        self.clip = None
        self.help = False
        self.mod = False
        self.held = {}
        self.msg, self.msg_t = "", 0
        self.confirm = None  # (action key, deadline)
        self.prompt = None  # dict(title, text, done)
        self.files_col = 0
        self.files_sel = [0, 0]
        self.pending_rec = None
        self.save_seq = 0
        self.wav_info = {}

        self.eng = Engine()
        self.shm = None
        for _ in range(60):
            try:
                fd = os.open(SHM, os.O_RDONLY)
                if os.fstat(fd).st_size >= 3528:
                    self.shm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
                    break
                os.close(fd)
            except OSError:
                pass
            time.sleep(0.05)

        self.startup()

        self.fb = os.open(FB, os.O_RDWR)
        try:
            self.kbd = os.open(KBD, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f"zsynth: no keyboard ({e})", file=sys.stderr)
            self.kbd = None
        self.running = True

    # ================= songs and samples =================
    def song_path(self, name):
        return os.path.join(SONGS, name + ".zsong")

    def startup(self):
        st = {}
        try:
            with open(STATE) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            pass
        self.track = int(st.get("track", 0)) % 4
        name = st.get("song")
        if name and os.path.exists(self.song_path(name)):
            self.load_song(name, autosave=False)
            return
        # first run after the pattern-bank update: bring the old song over
        if os.path.exists(LEGACY_PATTERN):
            self.vals = [dict(d) for d in DEFAULTS]
            for t, d in enumerate(st.get("vals", [])[:4]):
                for k, v in d.items():
                    self.vals[t][int(k)] = float(v)
            self.octave = st.get("octave", self.octave)
            if os.path.exists(LEGACY_SAMPLE):
                dst = os.path.join(SAMPLES, "first-sample.wav")
                if not os.path.exists(dst):
                    os.replace(LEGACY_SAMPLE, dst)
                self.sample_name = "first-sample.wav"
            self.eng.send("load", LEGACY_PATTERN)
            self.song = "my-first-song"
            self.apply_sound()
            self.save_song()
            os.replace(LEGACY_PATTERN, LEGACY_PATTERN + ".old")
            return
        self.new_song("untitled", save=True)

    def apply_sound(self):
        for t in range(4):
            for p in PARAMS[t]:
                self.push(t, p[1])
        if self.sample_name and os.path.exists(
                os.path.join(SAMPLES, self.sample_name)):
            self.eng.send("sampload", os.path.join(SAMPLES, self.sample_name))
        else:
            self.sample_name = ""
            self.eng.send("sampclear")

    def new_song(self, name, save=True):
        self.eng.send("new")
        self.eng.send("bpm", 110)
        self.vals = [dict(d) for d in DEFAULTS]
        self.octave = list(DEFAULT_OCT)
        self.chain, self.chain_cur = [], 0
        self.song = name
        self.apply_sound()
        if save:
            self.save_song()

    def save_song(self, name=None):
        name = name or self.song
        tmp = os.path.join(SONGS, ".dump.tmp")
        self.save_seq += 1
        self.eng.send("save", self.save_seq, tmp)
        deadline = time.time() + 2
        while time.time() < deadline:
            st = self.status()
            if st and st["saved_seq"] == self.save_seq:
                break
            time.sleep(0.02)
        try:
            with open(tmp) as fh:
                body = fh.read()
            os.remove(tmp)
        except OSError:
            self.flash("save failed")
            return False
        lines = ["zsong 1", f"sample {self.sample_name}"]
        lines += [f"octave {t} {o}" for t, o in enumerate(self.octave)]
        for t in range(4):
            for k, v in sorted(self.vals[t].items()):
                lines.append(f"param {t} {k} {v:g}")
        path = self.song_path(name)
        with open(path + ".tmp", "w") as fh:
            fh.write("\n".join(lines) + "\n" + body)
        os.replace(path + ".tmp", path)
        self.song = name
        self.save_state()
        return True

    def load_song(self, name, autosave=True):
        if autosave:
            self.save_song()
        path = self.song_path(name)
        vals = [dict(d) for d in DEFAULTS]
        octave = list(DEFAULT_OCT)
        sample = ""
        chain = []
        try:
            with open(path) as fh:
                for ln in fh:
                    p = ln.split()
                    if not p:
                        continue
                    if p[0] == "sample":
                        sample = " ".join(p[1:])
                    elif p[0] == "octave" and len(p) == 3:
                        octave[int(p[1]) % 4] = int(p[2])
                    elif p[0] == "param" and len(p) == 4:
                        vals[int(p[1]) % 4][int(p[2])] = float(p[3])
                    elif p[0] == "chain":
                        chain = [int(x) for x in p[1:] if x.isdigit()]
        except (OSError, ValueError):
            self.flash(f"can't read {name}")
            return
        self.vals, self.octave, self.sample_name = vals, octave, sample
        self.chain, self.chain_cur = chain, len(chain)
        self.song = name
        self.eng.send("stop")
        self.eng.send("load", path)
        self.apply_sound()
        self.save_state()
        self.flash(f"opened {name}")

    def save_state(self):
        try:
            with open(STATE, "w") as fh:
                json.dump({"song": self.song, "track": self.track}, fh)
        except OSError:
            pass

    def song_list(self):
        return sorted(os.path.basename(p)[:-6]
                      for p in glob.glob(os.path.join(SONGS, "*.zsong")))

    def sample_list(self):
        return sorted(os.path.basename(p)
                      for p in glob.glob(os.path.join(SAMPLES, "*.wav")))

    def wav_len(self, name):
        path = os.path.join(SAMPLES, name)
        try:
            key = (name, os.path.getmtime(path))
        except OSError:
            return None
        if key not in self.wav_info:
            try:
                with wave.open(path) as w:
                    self.wav_info[key] = w.getnframes() / w.getframerate()
            except (wave.Error, OSError, EOFError):
                self.wav_info[key] = None
        return self.wav_info[key]

    def load_sample(self, name):
        self.sample_name = name
        self.eng.send("sampload", os.path.join(SAMPLES, name))
        self.flash(f"sample: {name}")

    def start_sample_rec(self):
        name = time.strftime("rec-%Y%m%d-%H%M%S.wav")
        self.pending_rec = name
        self.eng.send("samppath", os.path.join(SAMPLES, name))
        self.eng.send("samprec", "start")
        self.track = 2

    def check_rec(self, st):
        if self.pending_rec and st and not st["sampling"]:
            name, self.pending_rec = self.pending_rec, None
            # the engine writes the file just after capture stops
            for _ in range(25):
                if os.path.exists(os.path.join(SAMPLES, name)):
                    self.sample_name = name
                    self.flash(f"saved {name}")
                    return
                time.sleep(0.02)
            self.flash("too quiet: nothing recorded")

    def send_chain(self):
        self.eng.send("chain", *self.chain)

    # ================= engine status =================
    def status(self):
        if not self.shm:
            return None
        hdr = struct.unpack_from("<II6i2f2i", self.shm, 0)
        ext = struct.unpack_from("<6i", self.shm, 2928)
        return {
            "playing": hdr[2], "rec": hdr[3], "step": hdr[4], "bpm": hdr[5],
            "sampling": hdr[6], "sample_len": hdr[7], "in": hdr[8],
            "out": hdr[9], "voices": hdr[10],
            "scope": np.frombuffer(self.shm, np.float32, 640, 48),
            "grid": np.frombuffer(self.shm, np.uint8, 64, 2608
                                  ).reshape(4, 16),
            "ov": np.frombuffer(self.shm, np.uint8, 256, 2672),
            "cur_pat": ext[0], "next_pat": ext[1], "song_mode": ext[2],
            "chain_pos": ext[3], "chain_len": ext[4], "saved_seq": ext[5],
            "pgrid": np.frombuffer(self.shm, np.uint8, 512, 3016
                                   ).reshape(NPAT, 4, 16),
        }

    def push(self, t, idx):
        v = self.vals[t].get(idx, DEFAULTS[t].get(idx, 0))
        kind = next(p[2] for p in PARAMS[t] if p[1] == idx)
        self.eng.send("set", t, idx, v / 1000.0 if kind == "ms" else v)

    def flash(self, text):
        self.msg, self.msg_t = text, time.time()

    def confirmed(self, key, text):
        """True on the second press within 2 s; the first press warns."""
        now = time.time()
        if self.confirm and self.confirm[0] == key and now < self.confirm[1]:
            self.confirm = None
            return True
        self.confirm = (key, now + 2)
        self.flash(text)
        return False

    # ================= input =================
    def poll_keys(self):
        if self.kbd is None:
            return
        while True:
            try:
                data = os.read(self.kbd, 24 * 32)
            except (BlockingIOError, OSError):
                return
            if not data:
                return
            for i in range(0, len(data) - 23, 24):
                _, _, typ, code, val = struct.unpack_from("llHHi", data, i)
                if typ == 1:
                    self.on_key(code, val)

    def note_for(self, t, semi):
        if t == 3:
            return 36 + semi % 12
        return 12 * (self.octave[t] + 1) + semi

    def on_key(self, code, val):
        if code in (K_CTRL, K_ALT):
            self.mod = val != 0
            return
        if val == 0:
            if code in self.held:
                t, n = self.held.pop(code)
                self.eng.send("off", t, n)
            return
        if self.prompt:
            if val == 1 or code == K_BKSP:
                self.prompt_key(code)
            return
        if self.help:
            if val == 1:
                self.help = False
            return
        if val == 1 and code == K_HELP:
            self.help = True
            return
        if val == 1 and code in K_FKEYS:
            n = code - K_FKEYS[0]
            if n < NPAT:
                self.select_pattern(n)
            elif n == 9:
                self.toggle_song_mode()
            return
        if self.mod:
            self.command(code, val)
            return
        if val == 1 and code == K_TAB:
            self.screen = (self.screen + 1) % len(SCREENS)
            return
        if val == 1 and code == K_SPACE:
            st = self.status()
            self.eng.send("stop" if st and st["playing"] else "play")
            return
        if self.screen == 0:
            self.main_key(code, val)
        elif self.screen == 1:
            self.song_key(code, val)
        else:
            self.files_key(code, val)

    def select_pattern(self, n):
        st = self.status()
        if st and st["song_mode"]:
            self.flash("song mode is on: Fn+0 turns it off")
            return
        self.eng.send("pat", n)
        if st and st["playing"] and n != st["cur_pat"]:
            self.flash(f"pattern {n + 1} next")

    def toggle_song_mode(self):
        st = self.status()
        on = not (st and st["song_mode"])
        if on and not self.chain:
            self.flash("chain is empty: add patterns on the SONG screen")
            return
        self.eng.send("songmode", 1 if on else 0)
        self.flash("song mode on" if on else "song mode off")

    def main_key(self, code, val):
        if val != 1:
            return
        if code in NOTE_KEYS:
            t = self.track
            n = self.note_for(t, NOTE_KEYS[code])
            self.held[code] = (t, n)
            self.eng.send("on", t, n, 110)
        elif code == K_ENTER:
            st = self.status()
            self.eng.send("rec", 0 if st and st["rec"] else 1)
        elif code == K_BKSP:
            self.eng.send("undo", self.track)
        elif code == K_ESC:
            self.running = False

    def song_key(self, code, val):
        if code in (K_LEFT, K_Z):
            self.chain_cur = max(0, self.chain_cur - 1)
        elif code in (K_RIGHT, K_C):
            self.chain_cur = min(len(self.chain), self.chain_cur + 1)
        elif val != 1:
            return
        elif K_1 <= code <= K_1 + 7:
            if len(self.chain) < 64:
                self.chain.insert(self.chain_cur, code - K_1)
                self.chain_cur += 1
                self.send_chain()
        elif code == K_BKSP:
            if self.chain_cur > 0:
                self.chain.pop(self.chain_cur - 1)
                self.chain_cur -= 1
                self.send_chain()
        elif code == K_ENTER:
            self.toggle_song_mode()
        elif code == K_ESC:
            self.screen = 0

    def files_key(self, code, val):
        col = self.files_col
        items = self.files_items(col)
        if code in (K_UP, K_F):
            self.files_sel[col] = max(0, self.files_sel[col] - 1)
            return
        if code in (K_DOWN, K_X):
            self.files_sel[col] = min(len(items) - 1, self.files_sel[col] + 1)
            return
        if val != 1:
            return
        if code in (K_LEFT, K_Z, K_RIGHT, K_C):
            self.files_col = 1 - col
            return
        if code == K_ESC:
            self.screen = 0
            return
        if code == K_S:
            if self.save_song():
                self.flash(f"saved {self.song}")
            return
        if not items:
            return
        kind, name = items[min(self.files_sel[col], len(items) - 1)]
        if code == K_ENTER:
            if kind == "new":
                self.ask_name("New song name", self.do_new_song)
            elif kind == "saveas":
                self.ask_name("Save song as", self.do_save_as)
            elif kind == "song" and name != self.song:
                self.load_song(name)
            elif kind == "sample":
                self.load_sample(name)
        elif code in (K_DEL, K_BKSP):
            if kind == "song":
                if name == self.song:
                    self.flash("can't delete the open song")
                elif self.confirmed(("del", name), f"press again to delete {name}"):
                    os.remove(self.song_path(name))
                    self.flash(f"deleted {name}")
            elif kind == "sample":
                if self.confirmed(("del", name), f"press again to delete {name}"):
                    os.remove(os.path.join(SAMPLES, name))
                    if name == self.sample_name:
                        self.sample_name = ""
                        self.eng.send("sampclear")
                    self.flash(f"deleted {name}")
            self.files_sel[col] = min(self.files_sel[col],
                                      len(self.files_items(col)) - 1)

    def files_items(self, col):
        if col == 0:
            return ([("new", "+ new song"), ("saveas", "+ save as...")] +
                    [("song", n) for n in self.song_list()])
        return [("sample", n) for n in self.sample_list()]

    def ask_name(self, title, done):
        self.prompt = {"title": title, "text": "", "done": done}

    def prompt_key(self, code):
        p = self.prompt
        if code == K_ESC:
            self.prompt = None
        elif code == K_BKSP:
            p["text"] = p["text"][:-1]
        elif code == K_ENTER:
            name = slug(p["text"])
            self.prompt = None
            if p["text"].strip():
                p["done"](name)
        elif code in CHARS and len(p["text"]) < 24:
            p["text"] += CHARS[code]

    def unique(self, name):
        base, n = name, 2
        while os.path.exists(self.song_path(name)):
            name = f"{base}-{n}"
            n += 1
        return name

    def do_new_song(self, name):
        self.save_song()
        name = self.unique(name)
        self.new_song(name)
        self.flash(f"new song {name}")

    def do_save_as(self, name):
        name = self.unique(name)
        if self.save_song(name):
            self.flash(f"saved as {name}")

    def command(self, code, val):
        t = self.track
        plist = PARAMS[t]
        if code in (K_A, K_S):  # allow auto-repeat
            label, idx, kind, lo, hi = plist[self.sel[t] % len(plist)]
            v = self.vals[t].get(idx, lo)
            up = code == K_S
            if isinstance(kind, list):
                v = (int(v) + (1 if up else -1)) % len(kind)
            elif kind in ("log", "ms"):
                v = min(hi, max(lo, v * (1.12 if up else 1 / 1.12)))
            else:
                v = min(hi, max(lo, v + (hi - lo) * (0.025 if up else -0.025)))
            self.vals[t][idx] = v
            self.push(t, idx)
            return
        if code in (K_B, K_N):
            st = self.status()
            bpm = (st["bpm"] if st else self.bpm) + (1 if code == K_N else -1)
            self.bpm = max(40, min(300, bpm))
            self.eng.send("bpm", self.bpm)
            return
        if val != 1:
            return
        if code in (K_1, K_2, K_3, K_4):
            self.track = code - K_1
        elif code in (K_Q, K_W):
            self.sel[t] = (self.sel[t] + (1 if code == K_W else -1)) % len(plist)
        elif code in (K_Z, K_X) and t != 3:
            self.octave[t] = max(0, min(7, self.octave[t] +
                                        (1 if code == K_X else -1)))
        elif code == K_C:
            st = self.status()
            self.clip = st["cur_pat"] if st else 0
            self.flash(f"copied pattern {self.clip + 1}")
        elif code == K_V:
            st = self.status()
            if self.clip is None:
                self.flash("nothing copied: Ctrl+C first")
            elif st:
                self.eng.send("copy", self.clip, st["cur_pat"])
                self.flash(f"pattern {self.clip + 1} pasted into "
                           f"{st['cur_pat'] + 1}")
        elif code == K_R:
            st = self.status()
            if st and st["sampling"]:
                self.eng.send("samprec", "stop")
            else:
                self.start_sample_rec()
        elif code == K_L:
            files = self.sample_list()
            if not files:
                self.flash("no samples yet: Ctrl+R records one")
            else:
                i = (files.index(self.sample_name) + 1
                     if self.sample_name in files else 0)
                self.load_sample(files[i % len(files)])
                self.track = 2
        elif code == K_BKSP:
            st = self.status()
            if self.screen == 1 and st:
                p = st["cur_pat"]
                if self.confirmed(("clearpat", p),
                                  f"press again to clear pattern {p + 1}"):
                    self.eng.send("clearpat", p)
                    self.flash(f"cleared pattern {p + 1}")
            else:
                self.eng.send("clear", t)
                self.flash(f"cleared {TRACKS[t][0].lower()}")

    # ================= drawing =================
    def text(self, f, s, col, pos, anchor="topleft"):
        img = f.render(s, True, col)
        r = img.get_rect(**{anchor: pos})
        self.s.blit(img, r)
        return r

    def fmt(self, t, p):
        label, idx, kind, lo, hi = p
        v = self.vals[t].get(idx, lo)
        if isinstance(kind, list):
            return kind[int(v) % len(kind)]
        if kind == "ms":
            return f"{v / 1000:.2f}s" if v >= 1000 else f"{v:.0f}ms"
        if kind == "log":
            return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}Hz"
        if label == "DETUNE":
            return f"{v:.0f}c"
        if label == "DECAY" and t == 3:
            return f"x{v:.2f}"
        return f"{v:.2f}"

    def draw_top(self, st):
        x = 0
        for i, (name, _, col) in enumerate(TRACKS):
            w = 50
            on = i == self.track
            pygame.draw.rect(self.s, col if on else PANEL, (x, 0, w - 2, 15),
                             border_radius=3)
            self.text(self.f_smb, f"{i + 1} {name}", BG if on else DIM,
                      (x + w // 2 - 1, 8), "center")
            x += w
        # pattern indicator
        if st:
            song = st["song_mode"]
            label = f"P{st['cur_pat'] + 1}"
            if st["next_pat"] >= 0:
                label += f">{st['next_pat'] + 1}"
            pygame.draw.rect(self.s, AMBER if song else PANEL, (202, 0, 40, 15),
                             border_radius=3)
            self.text(self.f_smb, ("S " if song else "") + label,
                      BG if song else TEXT, (222, 8), "center")
        bx = 248
        if st and st["playing"]:
            pygame.draw.polygon(self.s, (90, 220, 120),
                                [(bx, 3), (bx, 12), (bx + 8, 7)])
        else:
            pygame.draw.rect(self.s, DIM, (bx, 3, 9, 9))
        if st and st["rec"]:
            pygame.draw.circle(self.s, RED, (bx + 17, 8), 5)
        self.text(self.f_mid, f"{self.bpm}", TEXT, (W - 22, 8), "midright")
        self.text(self.f_sm, "bpm", DIM, (W - 2, 9), "midright")

    def draw_scope(self, st, rect):
        x0, y0, w, h = rect
        pygame.draw.rect(self.s, PANEL, rect, border_radius=4)
        col = TRACKS[self.track][2]
        mid = y0 + h // 2
        pygame.draw.line(self.s, GRID, (x0 + 2, mid), (x0 + w - 3, mid))
        if self.track != 3:
            self.text(self.f_sm, f"OCT {self.octave[self.track]}", DIM,
                      (x0 + 4, y0 + 2))
        if not st:
            return
        sc = st["scope"]
        start = 0
        for i in range(1, 320):
            if sc[i - 1] < 0 <= sc[i]:
                start = i
                break
        seg = sc[start:start + 2 * (w - 4):2]
        if len(seg) > 1:
            pts = [(x0 + 2 + i, mid - float(v) * (h / 2 - 3))
                   for i, v in enumerate(seg)]
            pygame.draw.lines(self.s, col, False, pts, 1)

    def draw_sample(self, st, rect):
        x0, y0, w, h = rect
        pygame.draw.rect(self.s, PANEL, rect, border_radius=4)
        mid = y0 + h // 2
        col = TRACKS[2][2]
        if st and st["sampling"]:
            lvl = min(1.0, st["in"] * 1.5)
            self.text(self.f_mid, "● RECORDING", RED, (x0 + w // 2, y0 + 14),
                      "center")
            pygame.draw.rect(self.s, GRID, (x0 + 10, mid - 4, w - 20, 10),
                             border_radius=3)
            pygame.draw.rect(self.s, RED, (x0 + 10, mid - 4,
                                           int((w - 20) * lvl), 10),
                             border_radius=3)
            self.text(self.f_sm, "Ctrl+R to stop (max 5 s)", DIM,
                      (x0 + w // 2, y0 + h - 12), "center")
            return
        if not st or not st["sample_len"]:
            self.text(self.f_sm, "no sample loaded", DIM,
                      (x0 + w // 2, mid - 7), "center")
            self.text(self.f_sm, "Ctrl+R record · Ctrl+L load", DIM,
                      (x0 + w // 2, mid + 7), "center")
            return
        ov = st["ov"]
        for i in range(w - 4):
            a = ov[int(i * 256 / (w - 4))] / 255 * (h / 2 - 8)
            pygame.draw.line(self.s, col, (x0 + 2 + i, mid + 4 - a),
                             (x0 + 2 + i, mid + 4 + a))
        name = self.sample_name[:-4] if self.sample_name else "unsaved"
        self.text(self.f_sm, name[:22], DIM, (x0 + 4, y0 + 2))
        self.text(self.f_sm, f"{st['sample_len'] / 48000:.1f}s", DIM,
                  (x0 + w - 4, y0 + 2), "topright")

    def draw_params(self, rect):
        x0, y0, w, h = rect
        t = self.track
        plist = PARAMS[t]
        sel = self.sel[t] % len(plist)
        for i, p in enumerate(plist):
            y = y0 + i * 10
            if i == sel:
                pygame.draw.rect(self.s, PANEL, (x0, y, w, 10))
                pygame.draw.rect(self.s, TRACKS[t][2], (x0, y, 2, 10))
            f = self.f_smb if i == sel else self.f_sm
            self.text(f, p[0], TEXT if i == sel else DIM, (x0 + 5, y - 1))
            self.text(f, self.fmt(t, p), TEXT, (x0 + w - 2, y - 1),
                      "topright")
        if t == 3:
            y = y0 + 34
            names = [f"{KEY_LABEL[s]} {DRUM_NAMES[s]}"
                     for s in (0, 2, 4, 5, 7, 9, 11, 1, 3, 6, 8, 10)]
            for k, nm in enumerate(names):
                self.text(self.f_sm, nm, DIM,
                          (x0 + 5 + (k % 2) * 78, y + (k // 2) * 10))

    def draw_grid(self, st, y0):
        cw, rh, x0 = 18, 9, 30
        step = st["step"] if st else -1
        for r in range(4):
            y = y0 + r * (rh + 1)
            on = r == self.track
            self.text(self.f_smb if on else self.f_sm, TRACKS[r][1],
                      TRACKS[r][2] if on else DIM, (x0 - 6, y + rh // 2),
                      "midright")
            for c in range(16):
                x = x0 + c * cw
                n = st["grid"][r][c] if st else 0
                col = TRACKS[r][2] if n else (PANEL if c // 4 % 2 == 0
                                              else (32, 36, 46))
                if n and not on:
                    col = tuple(int(v * 0.55) for v in col)
                pygame.draw.rect(self.s, col, (x, y, cw - 2, rh),
                                 border_radius=2)
        if st and st["playing"]:
            x = x0 + step * cw
            pygame.draw.rect(self.s, TEXT, (x - 1, y0 - 1, cw, 4 * (rh + 1)),
                             1, border_radius=2)

    def draw_footer(self, hint):
        if self.msg and time.time() - self.msg_t < 2.5:
            self.text(self.f_sm, self.msg, AMBER, (W // 2, H - 1),
                      "midbottom")
            return
        self.text(self.f_sm, hint, DIM, (W // 2, H - 1), "midbottom")

    def draw_main(self, st):
        rect = (0, 18, 150, 96)
        if self.track == 2:
            self.draw_sample(st, rect)
        else:
            self.draw_scope(st, rect)
        self.draw_params((156, 18, 162, 100))
        self.draw_grid(st, 119)
        self.draw_footer(
            "Ctrl: 1-4 track  Q/W pick  A/S adjust  Z/X oct  B/N tempo"
            if self.mod else
            "Space play  Enter rec  Fn+1-8 pattern  Tab screens  Fn+H help")

    def draw_song(self, st):
        blink = int(time.time() * 4) % 2
        cur = st["cur_pat"] if st else 0
        nxt = st["next_pat"] if st else -1
        for p in range(NPAT):
            x = 2 + (p % 4) * 79
            y = 19 + (p // 4) * 35
            g = st["pgrid"][p] if st else np.zeros((4, 16), np.uint8)
            empty = not g.any()
            pygame.draw.rect(self.s, PANEL, (x, y, 76, 32), border_radius=4)
            border = TEXT if p == cur else (AMBER if p == nxt and blink
                                            else None)
            if border:
                pygame.draw.rect(self.s, border, (x, y, 76, 32), 1,
                                 border_radius=4)
            self.text(self.f_smb, str(p + 1), TEXT if p == cur else DIM,
                      (x + 4, y + 1))
            if empty:
                self.text(self.f_sm, "empty", GRID, (x + 44, y + 16),
                          "center")
                continue
            for r in range(4):
                for c in range(16):
                    if g[r][c]:
                        pygame.draw.rect(self.s, TRACKS[r][2],
                                         (x + 11 + c * 4, y + 6 + r * 6, 3, 5))
        # chain
        y0 = 92
        song = st and st["song_mode"]
        self.text(self.f_smb, "CHAIN", TEXT, (4, y0))
        self.text(self.f_sm, "song mode ON" if song else "song mode off",
                  AMBER if song else DIM, (W - 4, y0), "topright")
        per_row, cw = 16, 19
        first = max(0, (self.chain_cur // per_row - 1) * per_row)
        for k in range(2 * per_row):
            i = first + k
            x = 4 + (k % per_row) * cw
            y = y0 + 14 + (k // per_row) * 21
            if i < len(self.chain):
                playing_here = (song and st["playing"]
                                and i == st["chain_pos"])
                pygame.draw.rect(self.s, AMBER if playing_here else PANEL,
                                 (x, y, cw - 2, 18), border_radius=3)
                self.text(self.f_smb, str(self.chain[i] + 1),
                          BG if playing_here else TEXT,
                          (x + (cw - 2) // 2, y + 9), "center")
            if i == self.chain_cur:
                pygame.draw.rect(self.s, TRACKS[self.track][2],
                                 (x - 2, y, 2, 18))
        if not self.chain:
            self.text(self.f_sm, "press 1-8 to add patterns to the chain",
                      DIM, (W // 2, y0 + 24), "center")
        self.draw_footer("1-8 add  Bksp remove  Z/C move  Enter song mode  "
                         "Ctrl+C/V copy")

    def draw_files(self, st):
        self.text(self.f_smb, "SONG", DIM, (4, 19))
        self.text(self.f_mid, self.song, TEXT, (36, 17))
        for col in range(2):
            x0 = 2 + col * 160
            active = col == self.files_col
            title = "SONGS" if col == 0 else "SAMPLES"
            pygame.draw.rect(self.s, TRACKS[2][2] if active else PANEL,
                             (x0, 34, 156, 13), border_radius=3)
            self.text(self.f_smb, title, BG if active else DIM,
                      (x0 + 78, 40), "center")
            items = self.files_items(col)
            sel = min(self.files_sel[col], max(0, len(items) - 1))
            rows = 9
            top = max(0, min(sel - rows // 2, len(items) - rows))
            for k, (kind, name) in enumerate(items[top:top + rows]):
                i = top + k
                y = 50 + k * 12
                if active and i == sel:
                    pygame.draw.rect(self.s, PANEL, (x0, y - 1, 156, 12),
                                     border_radius=2)
                current = ((kind == "song" and name == self.song) or
                           (kind == "sample" and name == self.sample_name))
                col_t = (AMBER if kind in ("new", "saveas") else
                         TEXT if current else
                         (TEXT if active and i == sel else DIM))
                label = name[:-4] if kind == "sample" else name
                self.text(self.f_smb if current else self.f_sm,
                          ("● " if current else "  ") + label[:22], col_t,
                          (x0 + 3, y - 1))
                if kind == "sample":
                    ln = self.wav_len(name)
                    if ln:
                        self.text(self.f_sm, f"{ln:.1f}s", DIM,
                                  (x0 + 153, y - 1), "topright")
            if not items:
                self.text(self.f_sm, "none yet: Ctrl+R records one", DIM,
                          (x0 + 78, 70), "center")
        self.draw_footer("F/X up/down  Z/C column  Enter open  S save  "
                         "Del delete")

    def draw_prompt(self):
        p = self.prompt
        pygame.draw.rect(self.s, (0, 0, 0), (20, 45, 280, 70),
                         border_radius=6)
        pygame.draw.rect(self.s, TRACKS[2][2], (20, 45, 280, 70), 1,
                         border_radius=6)
        self.text(self.f_mid, p["title"], TEXT, (32, 52))
        pygame.draw.rect(self.s, PANEL, (32, 72, 256, 18), border_radius=3)
        cursor = "_" if int(time.time() * 2) % 2 else " "
        self.text(self.f_mid, p["text"] + cursor, TEXT, (38, 74))
        self.text(self.f_sm, "Enter save · Esc cancel · Space = -", DIM,
                  (160, 104), "center")

    def draw_help(self):
        self.s.fill(BG)
        lines = [
            ("NOTES", "Z…M + S D G H J  ·  Q…P + 2 3 5 6 7 9 0"),
            ("SPACE / ENTER", "play / stop  ·  record on/off"),
            ("TAB", "screens: main → song → files"),
            ("FN 1-8 / FN 0", "pick pattern (at loop end) · song mode"),
            ("CTRL 1-4", "track: lead, bass, sample, drums"),
            ("CTRL Q/W  A/S", "pick a setting  ·  change it"),
            ("CTRL Z/X  B/N", "octave  ·  tempo"),
            ("CTRL C / V", "copy / paste pattern"),
            ("CTRL R / L", "record sample  ·  next sample"),
            ("BKSP / CTRL", "undo note  ·  clear track"),
            ("SONG SCREEN", "1-8 add to chain, Enter song mode"),
            ("ESC", "back / quit (everything is saved)"),
        ]
        self.text(self.f_mid, "ZSynth keys", TEXT, (6, 2))
        for i, (k, v) in enumerate(lines):
            y = 19 + i * 12
            self.text(self.f_smb, k, TRACKS[i % 4][2], (6, y))
            self.text(self.f_sm, v, TEXT, (98, y))
        self.text(self.f_sm, "any key to close", DIM, (W - 4, H - 1),
                  "bottomright")

    def render(self):
        st = self.status()
        if st:
            self.bpm = st["bpm"] or self.bpm
            self.check_rec(st)
        if self.help:
            self.draw_help()
        else:
            self.s.fill(BG)
            self.draw_top(st)
            [self.draw_main, self.draw_song, self.draw_files][self.screen](st)
            if self.prompt:
                self.draw_prompt()
        self.flush()

    def flush(self):
        a = pygame.surfarray.pixels3d(self.s)
        r = a[:, :, 0].astype(np.uint16)
        g = a[:, :, 1].astype(np.uint16)
        b = a[:, :, 2].astype(np.uint16)
        del a
        px = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        os.pwrite(self.fb, px.T.astype("<u2").tobytes(), 0)

    def run(self):
        frame = 1 / 15
        nxt = 0
        while self.running and self.eng.p.poll() is None:
            self.poll_keys()
            now = time.time()
            if now >= nxt:
                self.render()
                nxt = now + frame
            time.sleep(0.004)
        self.save_song()
        self.eng.close()
        self.s.fill((0, 0, 0))
        self.flush()


def main():
    app = App()

    def stop(*_):
        app.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app.run()


if __name__ == "__main__":
    main()
