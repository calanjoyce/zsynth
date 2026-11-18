#!/usr/bin/env python3
"""ZSynth: a pocket groovebox for the M5 Cardputer Zero.

Four tracks (lead synth, bass synth, mic sampler, drum kit), eight patterns
(1-32 steps, any time signature) per song, a song chain, and a library of songs and samples. This
file is the screen and keyboard front end; the sound comes from
zsynth-engine (C), driven over a pipe.

Screens (Tab cycles): MAIN (play, record, sound settings), EDIT (drum
grid, or step entry into a piano roll for the other tracks), SONG (pattern
banks and chain), FILES (songs and samples).
Fn+H shows every key.
"""
import glob
import json
import mmap
import os
import re
import signal
import struct
import shutil
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
INSTR = os.path.join(DATA, "instruments")   # instruments/<kind>/<name>.zinst
STATE = os.path.join(DATA, "state.json")
LEGACY_PATTERN = os.path.join(DATA, "pattern.txt")
LEGACY_SAMPLE = os.path.join(DATA, "sample.wav")
SHM = "/dev/shm/zsynth.state"
W, H = 320, 170
NPAT = 8
NSTEP = 32          # most steps a pattern can have (the engine's NSTEP)
# where things are in /dev/shm/zsynth.state (offsets in engine.c's Shm)
OFF_GRID, OFF_OV, OFF_EXT, OFF_PGRID = 2608, 2736, 2992, 3080
OFF_EVN, OFF_EV, OFF_PLEN, OFF_PBEAT, SHM_SIZE = 4104, 4120, 6168, 6176, 6184
# Time signatures: (label, steps, steps per beat). Steps are 16ths.
METERS = [("4/4", 16, 4), ("3/4", 12, 4), ("5/4", 20, 4), ("7/4", 28, 4),
          ("2/4", 8, 4), ("6/8", 12, 6), ("7/8", 14, 2), ("9/8", 18, 6),
          ("12/8", 24, 6)]


def meter_name(n, beat):
    """"7/8" for 14 steps in eighths, "13/16" when it isn't a tidy meter."""
    for label, ln, bt in METERS:
        if (ln, bt) == (n, beat):
            return label
    if beat == 4 and n % 4 == 0:
        return f"{n // 4}/4"
    if beat in (2, 6) and n % 2 == 0:
        return f"{n // 2}/8"
    return f"{n}/16"

# ---- keyboard (Linux key codes from the Cardputer Zero keymap) ----
K_ESC, K_BKSP, K_TAB, K_ENTER, K_SPACE = 1, 14, 15, 28, 57
K_CTRL, K_ALT = 29, 56
K_1, K_2, K_3, K_4 = 2, 3, 4, 5
K_Q, K_W, K_A, K_S, K_Z, K_X, K_C, K_V, K_F = 16, 17, 30, 31, 44, 45, 46, 47, 33
K_B, K_N, K_R, K_L = 48, 49, 19, 38
K_P, K_D, K_U, K_M = 25, 32, 22, 50
# Symbol layer: the Cardputer Zero sends keypad codes for the printed symbols
# (see /usr/share/keymaps/tca8418_keypad_m5stack_keymap.map)
K_MINUS, K_PLUS, K_EQUAL, K_LBRACE, K_RBRACE = 71, 72, 73, 74, 75
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

# Actions in a sample's menu on the FILES screen: (id, label, shortcut key)
SAMPLE_ACTIONS = [("play", "Play", K_P), ("load", "Use in sampler", K_L),
                  ("rename", "Rename", K_R), ("dup", "Duplicate", K_U),
                  ("delete", "Delete", K_D)]

KEY_NAME = {K_P: "P", K_L: "L", K_R: "R", K_U: "U", K_D: "D"}

SCREENS = ["MAIN", "EDIT", "SOUND", "SONG", "FILES"]
S_MAIN, S_EDIT, S_SOUND, S_SONG, S_FILES = range(5)

# Step editor. Arrows: Fn+F/Z/X/C everywhere; on the drum grid plain
# F/Z/X/C too (the letters aren't notes there).
ARROW = {K_UP: (0, -1), K_DOWN: (0, 1), K_LEFT: (-1, 0), K_RIGHT: (1, 0)}
GRID_ARROW = {K_F: (0, -1), K_X: (0, 1), K_Z: (-1, 0), K_C: (1, 0)}
# drum grid rows: engine drum index (see engine.c), top to bottom
DRUM_ROWS = [(0, "KICK"), (2, "SNARE"), (3, "CLAP"), (1, "RIM"),
             (4, "C-HAT"), (6, "O-HAT"), (5, "LO TOM"), (7, "HI TOM"),
             (8, "BELL"), (9, "CRASH")]
# semitone (note % 12) -> drum, as in the engine's drum_of_semi
DRUM_OF_SEMI = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 6, 3]
VEL_NORMAL, VEL_ACCENT = 100, 127
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#",
              "B"]
ROLL_ROWS = 29          # piano roll: two octaves and a bit, like the keys

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

# ---- instrument library ----
# Lead and bass share the synth, so their instruments are interchangeable.
KIND = ["synth", "synth", "sampler", "drums"]
FACTORY = {
    "synth": {
        "saw-lead": DEFAULTS[0],
        "square-bass": DEFAULTS[1],
        "pluck": {0: 2, 1: 1800, 2: 0.4, 3: 1, 4: 180, 5: 0, 6: 120, 7: 0.8,
                  8: 4, 9: 0.6},
        "soft-pad": {0: 1, 1: 1500, 2: 0.2, 3: 600, 4: 1200, 5: 0.8,
                     6: 1500, 7: 0.2, 8: 12, 9: 0.45},
        "bell": {0: 0, 1: 6000, 2: 0.1, 3: 1, 4: 900, 5: 0, 6: 900, 7: 0.3,
                 8: 3, 9: 0.5},
        "organ": {0: 1, 1: 4000, 2: 0.1, 3: 5, 4: 100, 5: 1.0, 6: 60, 7: 0,
                  8: 2, 9: 0.5},
        "acid-bass": {0: 2, 1: 350, 2: 0.85, 3: 1, 4: 180, 5: 0.2, 6: 60,
                      7: 0.9, 8: 0, 9: 0.6},
        "sub-bass": {0: 0, 1: 800, 2: 0, 3: 3, 4: 400, 5: 0.9, 6: 80, 7: 0,
                     8: 0, 9: 0.8},
    },
    "sampler": {
        "one-shot": DEFAULTS[2],
        "soft-gate": {0: 1, 1: 5000, 2: 0.2, 3: 30, 6: 400, 9: 0.8},
        "fm-piano": {0: 1, 1: 7000, 2: 0.1, 3: 2, 6: 180, 9: 0.7},
        "harpsichord": {0: 0, 1: 12000, 2: 0, 3: 1, 6: 200, 9: 0.7},
        "marimba": {0: 0, 1: 9000, 2: 0, 3: 1, 6: 200, 9: 0.8},
        "kalimba": {0: 0, 1: 9000, 2: 0.1, 3: 1, 6: 300, 9: 0.8},
        "glockenspiel": {0: 0, 1: 12000, 2: 0, 3: 1, 6: 400, 9: 0.6},
        "tenor-sax": {0: 1, 1: 6000, 2: 0.1, 3: 15, 6: 120, 9: 0.75},
        "sax-stab": {0: 0, 1: 8000, 2: 0.1, 3: 1, 6: 80, 9: 0.8},
        "harmonica": {0: 1, 1: 8000, 2: 0, 3: 20, 6: 150, 9: 0.7},
    },
    "drums": {
        "kit": DEFAULTS[3],
        "tight-kit": {1: 9000, 4: 0.6, 9: 0.8},
        "boomy-kit": {1: 3000, 4: 2.2, 9: 0.85},
    },
}
FACTORY["synth"].update({
    "disco-bass": {0: 2, 1: 650, 2: 0.5, 3: 1, 4: 140, 5: 0.25, 6: 60,
                   7: 0.75, 8: 6, 9: 0.75},
    "night-lead": {0: 2, 1: 1800, 2: 0.5, 3: 2, 4: 200, 5: 0.4, 6: 150,
                   7: 0.6, 8: 12, 9: 0.4},
})
# sampler instruments that come with a starter sample (see samples/)
FACTORY_SAMPLE = {n: n + ".wav" for n in FACTORY["sampler"]
                  if n not in ("one-shot", "soft-gate")}
DEFAULT_INST = ["saw-lead", "square-bass", "one-shot", "kit"]

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
        new_library = not os.path.isdir(INSTR)
        for k in FACTORY:
            os.makedirs(os.path.join(INSTR, k), exist_ok=True)
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
        self.menu = None    # sample action menu: dict(name, sel)
        self.preview = None  # (Popen, sample name) while one is auditioned
        self.inst = list(DEFAULT_INST)   # instrument name per track ("" = custom)
        self.inst_mod = [False] * 4      # changed since loaded / saved
        self.lib = None     # library browser: dict(sel, orig)
        self.later = []     # [(time, engine command...)] for preview notes
        self.files_col = 0
        self.files_sel = [0, 0]
        self.pending_rec = None
        self.save_seq = 0
        self.wav_info = {}
        # step editor
        self.cur_step = 0
        self.drum_row = 0
        self.step_len = 1
        self.entry_held = set()
        self.entry_at = 0

        self.eng = Engine()
        self.shm = None
        for _ in range(60):
            try:
                fd = os.open(SHM, os.O_RDONLY)
                if os.fstat(fd).st_size >= SHM_SIZE:
                    self.shm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
                    break
                os.close(fd)
            except OSError:
                pass
            time.sleep(0.05)

        self.add_factory()
        if new_library:
            self.make_library()
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
            self.name_insts({})
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
        self.inst, self.inst_mod = list(DEFAULT_INST), [False] * 4
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
        lines += [f"inst {t} {self.inst[t] or '-'} {int(self.inst_mod[t])}"
                  for t in range(4)]
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
        inst = {}
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
                    elif p[0] == "inst" and len(p) == 4:
                        inst[int(p[1]) % 4] = ("" if p[2] == "-" else p[2],
                                               p[3] == "1")
                    elif p[0] == "chain":
                        chain = [int(x) for x in p[1:] if x.isdigit()]
        except (OSError, ValueError):
            self.flash(f"can't read {name}")
            return
        self.vals, self.octave, self.sample_name = vals, octave, sample
        self.chain, self.chain_cur = chain, len(chain)
        self.name_insts(inst)
        self.song = name
        self.eng.send("stop")
        self.eng.send("load", path)
        self.apply_sound()
        self.save_state()
        self.flash(f"opened {name}")

    def name_insts(self, saved):
        """Instrument names for a song just read: the ones it saved, or
        (older songs) whichever library instrument sounds the same."""
        for t in range(4):
            if t in saved:
                self.inst[t], self.inst_mod[t] = saved[t]
            else:
                self.inst[t], self.inst_mod[t] = \
                    self.match_inst(t, self.vals[t]), False

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

    def load_sample(self, name, quiet=False):
        self.sample_name = name
        self.eng.send("sampload", os.path.join(SAMPLES, name))
        if not quiet:
            self.inst_mod[2] = bool(self.inst[2])
            self.flash(f"sample: {name}")

    # ---- instrument library ----
    def inst_path(self, kind, name):
        return os.path.join(INSTR, kind, name + ".zinst")

    def inst_list(self, kind):
        return sorted(os.path.basename(p)[:-6] for p in
                      glob.glob(os.path.join(INSTR, kind, "*.zinst")))

    def read_inst(self, kind, name):
        """(values, sample name) of a library instrument, or None."""
        vals, sample = {}, ""
        try:
            with open(self.inst_path(kind, name)) as fh:
                for ln in fh:
                    p = ln.split()
                    if len(p) == 3 and p[0] == "param":
                        vals[int(p[1])] = float(p[2])
                    elif p and p[0] == "sample":
                        sample = " ".join(p[1:])
        except (OSError, ValueError):
            return None
        return vals, sample

    def write_inst(self, kind, name, vals, sample=""):
        lines = ["zinst 1", f"kind {kind}"]
        lines += [f"param {k} {v:g}" for k, v in sorted(vals.items())]
        if sample:
            lines.append(f"sample {sample}")
        path = self.inst_path(kind, name)
        with open(path + ".tmp", "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(path + ".tmp", path)

    def track_vals(self, t, vals):
        """vals limited to (and completed with) the settings track t has."""
        return {p[1]: float(vals.get(p[1], DEFAULTS[t].get(p[1], p[3])))
                for p in PARAMS[t]}

    def same_sound(self, t, a, b):
        a, b = self.track_vals(t, a), self.track_vals(t, b)
        return all(abs(a[k] - b[k]) <= 1e-3 * max(1, abs(b[k])) for k in a)

    def match_inst(self, t, vals):
        """Name of a library instrument that sounds like vals, or ""."""
        for name in self.inst_list(KIND[t]):
            got = self.read_inst(KIND[t], name)
            if got and self.same_sound(t, vals, got[0]):
                return name
        return ""

    def add_factory(self):
        """Add starter instruments this library hasn't had yet. Each is
        added once, so one you delete or rename stays that way."""
        mark = os.path.join(INSTR, ".factory")
        try:
            with open(mark) as fh:
                had = set(fh.read().split())
        except OSError:
            had = set()
        new = []
        for kind, insts in FACTORY.items():
            for name, vals in insts.items():
                key = f"{kind}/{name}"
                if key in had:
                    continue
                if not os.path.exists(self.inst_path(kind, name)):
                    self.write_inst(kind, name, vals,
                                    FACTORY_SAMPLE.get(name, "")
                                    if kind == "sampler" else "")
                new.append(key)
        if new:
            with open(mark, "a") as fh:
                fh.write("\n".join(new) + "\n")

    def make_library(self):
        """First run: your songs' custom sounds become instruments."""
        for song in self.song_list():
            vals = [dict(d) for d in DEFAULTS]
            sample = ""
            try:
                with open(self.song_path(song)) as fh:
                    for ln in fh:
                        p = ln.split()
                        if len(p) == 4 and p[0] == "param":
                            vals[int(p[1]) % 4][int(p[2])] = float(p[3])
                        elif p and p[0] == "sample":
                            sample = " ".join(p[1:])
                        elif p and p[0] == "ev":
                            break
            except (OSError, ValueError):
                continue
            for t in range(4):
                if not self.match_inst(t, vals[t]):
                    self.write_inst(KIND[t], slug(f"{song}-{TRACKS[t][0]}"),
                                    self.track_vals(t, vals[t]),
                                    sample if t == 2 else "")

    def use_inst(self, t, name, audition=False):
        """Put library instrument `name` on track t. With audition, only
        the sound changes (the library browser undoes it on Esc)."""
        got = self.read_inst(KIND[t], name)
        if not got:
            self.flash(f"can't read {name}")
            return
        vals, sample = got
        self.vals[t] = self.track_vals(t, vals)
        for p in PARAMS[t]:
            self.push(t, p[1])
        if t == 2 and sample and sample != self.sample_name and \
                os.path.exists(os.path.join(SAMPLES, sample)):
            self.load_sample(sample, quiet=True)
        if not audition:
            self.inst[t], self.inst_mod[t] = name, False

    def save_inst(self, t, name):
        self.write_inst(KIND[t], name, self.track_vals(t, self.vals[t]),
                        self.sample_name if t == 2 else "")
        self.inst[t], self.inst_mod[t] = name, False
        self.flash(f"saved instrument {name}")

    def unique_inst(self, kind, base):
        name, n = base, 2
        while os.path.exists(self.inst_path(kind, name)):
            name = f"{base}-{n}"
            n += 1
        return name

    def preview_note(self):
        """A short phrase on the current track, to hear its sound."""
        t, now = self.track, time.time()
        if t == 3:      # kick, snare, hat
            for i, semi in enumerate((0, 2, 4)):
                n = self.note_for(3, semi)
                self.later.append((now + 0.18 * i, "on", 3, n, 110))
                self.later.append((now + 0.18 * i + 0.1, "off", 3, n))
        else:
            n = self.note_for(t, 0)
            self.eng.send("on", t, n, 110)
            self.later.append((now + 0.45, "off", t, n))

    def run_later(self):
        now = time.time()
        due = [c for c in self.later if c[0] <= now]
        if due:
            self.later = [c for c in self.later if c[0] > now]
            for c in due:
                self.eng.send(*c[1:])

    # ---- sample library: audition, rename, duplicate, delete ----
    def previewing(self):
        """Name of the sample being auditioned, or None."""
        if self.preview and self.preview[0].poll() is None:
            return self.preview[1]
        self.preview = None
        return None

    def stop_preview(self):
        if self.previewing():
            self.preview[0].terminate()
            try:
                self.preview[0].wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.preview[0].kill()
        self.preview = None

    def toggle_preview(self, name):
        """Play a saved sample as recorded (not pitched, not through the
        sampler's settings) without loading it; again to stop."""
        was = self.previewing()
        self.stop_preview()
        if was == name:
            return
        try:
            p = subprocess.Popen(["paplay", os.path.join(SAMPLES, name)],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        except OSError:
            self.flash("can't play: paplay is missing")
            return
        self.preview = (p, name)

    def unique_sample(self, base):
        name, n = base + ".wav", 2
        while os.path.exists(os.path.join(SAMPLES, name)):
            name = f"{base}-{n}.wav"
            n += 1
        return name

    def songs_using(self, name):
        """Song files whose saved sample is `name`."""
        out = []
        for path in glob.glob(os.path.join(SONGS, "*.zsong")):
            try:
                with open(path) as fh:
                    head = fh.read(512).splitlines()
            except OSError:
                continue
            if f"sample {name}" in head[:3]:
                out.append(path)
        return out

    def rename_sample(self, old, base):
        new = old if slug(base) + ".wav" == old else \
            self.unique_sample(slug(base))
        if new == old:
            return
        if self.previewing() == old:
            self.stop_preview()
        os.rename(os.path.join(SAMPLES, old), os.path.join(SAMPLES, new))
        # keep every song that uses it pointing at the new name
        for path in self.songs_using(old):
            with open(path) as fh:
                lines = fh.read().split("\n")
            for i, ln in enumerate(lines[:3]):
                if ln == f"sample {old}":
                    lines[i] = f"sample {new}"
            with open(path + ".tmp", "w") as fh:
                fh.write("\n".join(lines))
            os.replace(path + ".tmp", path)
        if self.sample_name == old:
            self.sample_name = new
        self.select_sample(new)
        self.flash(f"renamed to {new[:-4]}")

    def duplicate_sample(self, name):
        new = self.unique_sample(name[:-4] + "-copy")
        shutil.copyfile(os.path.join(SAMPLES, name), os.path.join(SAMPLES, new))
        self.select_sample(new)
        self.flash(f"copied to {new[:-4]}")

    def delete_sample(self, name):
        if self.previewing() == name:
            self.stop_preview()
        os.remove(os.path.join(SAMPLES, name))
        if name == self.sample_name:
            self.sample_name = ""
            self.eng.send("sampclear")
        n = len(self.songs_using(name))
        self.flash(f"deleted {name[:-4]}" +
                   (f" ({n} song{'s' * (n > 1)} used it)" if n else ""))
        self.files_sel[1] = min(self.files_sel[1],
                                max(0, len(self.sample_list()) - 1))

    def select_sample(self, name):
        names = self.sample_list()
        if name in names:
            self.files_sel[1] = names.index(name)

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
        ext = struct.unpack_from("<6i", self.shm, OFF_EXT)
        return {
            "playing": hdr[2], "rec": hdr[3], "step": hdr[4], "bpm": hdr[5],
            "sampling": hdr[6], "sample_len": hdr[7], "in": hdr[8],
            "out": hdr[9], "voices": hdr[10],
            "scope": np.frombuffer(self.shm, np.float32, 640, 48),
            "grid": np.frombuffer(self.shm, np.uint8, 4 * NSTEP, OFF_GRID
                                  ).reshape(4, NSTEP),
            "ov": np.frombuffer(self.shm, np.uint8, 256, OFF_OV),
            "cur_pat": ext[0], "next_pat": ext[1], "song_mode": ext[2],
            "chain_pos": ext[3], "chain_len": ext[4], "saved_seq": ext[5],
            "pgrid": np.frombuffer(self.shm, np.uint8, NPAT * 4 * NSTEP,
                                   OFF_PGRID).reshape(NPAT, 4, NSTEP),
            "plen": [max(1, int(v)) for v in
                     np.frombuffer(self.shm, np.uint8, NPAT, OFF_PLEN)],
            "pbeat": [max(1, int(v)) for v in
                      np.frombuffer(self.shm, np.uint8, NPAT, OFF_PBEAT)],
        }

    def pat_len(self, st=None, p=None):
        """(steps, steps per beat) of pattern p (default: the current one)."""
        st = st or self.status()
        if not st:
            return 16, 4
        p = st["cur_pat"] if p is None else p
        return st["plen"][p], st["pbeat"][p]

    def events(self, t):
        """Notes of the current pattern on track t: (step, note, vel, len)."""
        if not self.shm:
            return []
        n = min(128, max(0, struct.unpack_from("<i", self.shm,
                                               OFF_EVN + 4 * t)[0]))
        raw = np.frombuffer(self.shm, np.uint8, 4 * n, OFF_EV + t * 512)
        return [tuple(int(v) for v in raw[i:i + 4])
                for i in range(0, 4 * n, 4)]

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
            if code in self.entry_held:
                self.entry_held.discard(code)
                if not self.entry_held:   # chord done: move on
                    self.cur_step = ((self.entry_at + self.step_len) %
                                     self.pat_len()[0])
            return
        if self.prompt:
            if val == 1 or code == K_BKSP:
                self.prompt_key(code)
            return
        if self.menu:
            self.menu_key(code, val)
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
        if self.lib:
            self.lib_key(code, val)
            return
        if self.mod:
            self.command(code, val)
            return
        if val == 1 and code == K_TAB:
            self.screen = (self.screen + 1) % len(SCREENS)
            if self.screen == S_EDIT:
                self.eng.send("rec", 0)   # notes typed here are step entry
            return
        if val == 1 and code == K_SPACE:
            st = self.status()
            self.eng.send("stop" if st and st["playing"] else "play")
            return
        if self.screen == S_MAIN:
            self.main_key(code, val)
        elif self.screen == S_EDIT:
            self.edit_key(code, val)
        elif self.screen == S_SOUND:
            self.sound_key(code, val)
        elif self.screen == S_SONG:
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

    # ================= step editor =================
    def cur_pat(self):
        st = self.status()
        return st["cur_pat"] if st else 0

    def edit_key(self, code, val):
        t = self.track
        mv = ARROW.get(code) or (GRID_ARROW.get(code) if t == 3 else None)
        if mv:                                  # arrows repeat when held
            dx, dy = mv
            self.cur_step = (self.cur_step + dx) % self.pat_len()[0]
            if t == 3 and dy:
                self.drum_row = (self.drum_row + dy) % len(DRUM_ROWS)
            return
        if val != 1:
            return
        if code == K_ESC:
            self.screen = S_MAIN
        elif code in (K_LBRACE, K_RBRACE):
            self.eng.send("shift", self.cur_pat(), t,
                          1 if code == K_RBRACE else -1)
        elif t == 3:
            self.drum_edit_key(code)
        else:
            self.note_edit_key(code)

    def drum_hits(self, step, drum):
        return [e for e in self.events(3) if e[0] == step
                and DRUM_OF_SEMI[e[1] % 12] == drum]

    def drum_edit_key(self, code):
        p, s = self.cur_pat(), self.cur_step
        drum = DRUM_ROWS[self.drum_row][0]
        hits = self.drum_hits(s, drum)
        note = 36 + drum
        if code == K_ENTER:                     # hit on / off
            if hits:
                for e in hits:
                    self.eng.send("stepdel", p, 3, s, e[1])
            else:
                self.eng.send("stepset", p, 3, s, note, VEL_NORMAL, 1)
                self.eng.send("on", 3, note, VEL_NORMAL)
        elif code == K_A:                       # accent on / off
            accent = not (hits and hits[0][2] >= VEL_ACCENT)
            vel = VEL_ACCENT if accent else VEL_NORMAL
            for e in hits or [(s, note, 0, 1)]:
                self.eng.send("stepset", p, 3, s, e[1], vel, 1)
            self.eng.send("on", 3, note, vel)
        elif code == K_BKSP:                    # clear this drum's row
            if self.confirmed(("clearrow", p, drum),
                              f"press again to clear "
                              f"{DRUM_ROWS[self.drum_row][1].lower()}"):
                for e in self.events(3):
                    if DRUM_OF_SEMI[e[1] % 12] == drum:
                        self.eng.send("stepdel", p, 3, e[0], e[1])

    def note_edit_key(self, code):
        t, p, s = self.track, self.cur_pat(), self.cur_step
        here = [e for e in self.events(t) if e[0] == s]
        if code in NOTE_KEYS:
            n = self.note_for(t, NOTE_KEYS[code])
            if not self.entry_held:
                self.entry_at = s
            s = self.entry_at
            if any(e[0] == s and e[1] == n for e in self.events(t)):
                self.eng.send("stepdel", p, t, s, n)   # same note again: remove
            else:
                self.eng.send("stepset", p, t, s, n, VEL_NORMAL,
                              self.step_len)
                self.held[code] = (t, n)
                self.eng.send("on", t, n, VEL_NORMAL)
            self.entry_held.add(code)
        elif code == K_ENTER:                   # rest: skip ahead
            self.cur_step = (s + self.step_len) % self.pat_len()[0]
        elif code in (K_BKSP, K_DEL):
            self.eng.send("stepdel", p, t, s, -1)
        elif code in (K_MINUS, K_EQUAL, K_PLUS):    # note length
            self.step_len = max(1, min(NSTEP, self.step_len +
                                       (-1 if code == K_MINUS else 1)))
            for e in here:
                self.eng.send("stepset", p, t, s, e[1], e[2], self.step_len)

    def song_key(self, code, val):
        if code in (K_LEFT, K_Z):
            self.chain_cur = max(0, self.chain_cur - 1)
        elif code in (K_RIGHT, K_C):
            self.chain_cur = min(len(self.chain), self.chain_cur + 1)
        elif code in (K_MINUS, K_EQUAL, K_PLUS):    # length (repeats)
            p = self.cur_pat()
            n, beat = self.pat_len(p=p)
            n = max(1, min(NSTEP, n + (-1 if code == K_MINUS else 1)))
            self.set_pat_len(p, n, beat)
        elif val != 1:
            return
        elif code == K_M:                       # next time signature
            p = self.cur_pat()
            now = self.pat_len(p=p)
            i = next((k for k, m in enumerate(METERS) if m[1:] == now), -1)
            label, n, beat = METERS[(i + 1) % len(METERS)]
            self.set_pat_len(p, n, beat)
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

    def set_pat_len(self, p, n, beat):
        self.eng.send("plen", p, n, beat)
        self.flash(f"pattern {p + 1}: {meter_name(n, beat)}, {n} steps")

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
                self.menu = {"name": name, "sel": 0}
        elif code == K_P and kind == "sample":
            self.toggle_preview(name)
        elif code in (K_DEL, K_BKSP):
            if kind == "song":
                if name == self.song:
                    self.flash("can't delete the open song")
                elif self.confirmed(("del", name), f"press again to delete {name}"):
                    os.remove(self.song_path(name))
                    self.flash(f"deleted {name}")
            elif kind == "sample":
                if self.confirmed(("del", name),
                                  f"press again to delete {name[:-4]}"):
                    self.delete_sample(name)
            self.files_sel[col] = min(self.files_sel[col],
                                      len(self.files_items(col)) - 1)

    def menu_key(self, code, val):
        m = self.menu
        if code in (K_UP, K_F):
            m["sel"] = (m["sel"] - 1) % len(SAMPLE_ACTIONS)
            return
        if code in (K_DOWN, K_X):
            m["sel"] = (m["sel"] + 1) % len(SAMPLE_ACTIONS)
            return
        if val != 1:
            return
        if code == K_ESC:
            self.menu = None
            return
        if code == K_ENTER:
            self.sample_action(SAMPLE_ACTIONS[m["sel"]][0])
            return
        for i, (act, _, k) in enumerate(SAMPLE_ACTIONS):
            if code == k:
                m["sel"] = i
                self.sample_action(act)
                return

    def sample_action(self, act):
        name = self.menu["name"]
        if not os.path.exists(os.path.join(SAMPLES, name)):
            self.menu = None
            return
        if act == "play":                       # menu stays open
            self.toggle_preview(name)
        elif act == "load":
            self.menu = None
            self.load_sample(name)
            self.track = 2
        elif act == "rename":
            self.menu = None
            self.ask_name("Rename sample",
                          lambda base: self.rename_sample(name, base),
                          text=name[:-4])
        elif act == "dup":
            self.menu = None
            self.duplicate_sample(name)
        elif act == "delete":
            if self.confirmed(("del", name), "press again to delete it"):
                self.menu = None
                self.delete_sample(name)

    def files_items(self, col):
        if col == 0:
            return ([("new", "+ new song"), ("saveas", "+ save as...")] +
                    [("song", n) for n in self.song_list()])
        return [("sample", n) for n in self.sample_list()]

    def ask_name(self, title, done, text=""):
        self.prompt = {"title": title, "text": text, "done": done}

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

    def pick(self, d):
        t = self.track
        self.sel[t] = (self.sel[t] + d) % len(PARAMS[t])

    def adjust(self, up):
        t = self.track
        plist = PARAMS[t]
        label, idx, kind, lo, hi = plist[self.sel[t] % len(plist)]
        v = self.vals[t].get(idx, lo)
        if isinstance(kind, list):
            v = (int(v) + (1 if up else -1)) % len(kind)
        elif kind in ("log", "ms"):
            v = min(hi, max(lo, v * (1.12 if up else 1 / 1.12)))
        else:
            v = min(hi, max(lo, v + (hi - lo) * (0.025 if up else -0.025)))
        self.vals[t][idx] = v
        self.inst_mod[t] = True
        self.push(t, idx)

    # ================= sound screen and instrument library =================
    def sound_key(self, code, val):
        if code in (K_UP, K_DOWN):              # Fn+F/X: pick a setting
            self.pick(1 if code == K_DOWN else -1)
            return
        if code in (K_LEFT, K_RIGHT):           # Fn+Z/C: change it (repeats)
            self.adjust(code == K_RIGHT)
            return
        if val != 1:
            return
        if code == K_ENTER:
            self.open_library()
        elif code == K_ESC:
            self.screen = S_MAIN
        elif code in NOTE_KEYS:                 # play to hear the sound
            self.main_key(code, val)

    def lib_items(self):
        return [("saveas", "+ save this sound as...")] + \
            [("inst", n) for n in self.inst_list(KIND[self.track])]

    def open_library(self):
        t = self.track
        items = self.lib_items()
        names = [n for _, n in items]
        self.lib = {"sel": names.index(self.inst[t]) if self.inst[t] in names
                    else 0,
                    "orig": (dict(self.vals[t]), self.inst[t],
                             self.inst_mod[t], self.sample_name)}

    def close_library(self, keep):
        t = self.track
        if not keep:                            # undo the auditioning
            vals, name, mod, sample = self.lib["orig"]
            self.vals[t] = vals
            for p in PARAMS[t]:
                self.push(t, p[1])
            if t == 2 and sample != self.sample_name:
                if sample:
                    self.load_sample(sample, quiet=True)
                else:
                    self.sample_name = ""
                    self.eng.send("sampclear")
            self.inst[t], self.inst_mod[t] = name, mod
        self.lib = None

    def lib_audition(self):
        kind, name = self.lib_items()[self.lib["sel"]]
        if kind == "inst":
            self.use_inst(self.track, name, audition=True)
            self.preview_note()
        else:                                   # back to the sound you had
            vals, _, _, sample = self.lib["orig"]
            t = self.track
            self.vals[t] = dict(vals)
            for p in PARAMS[t]:
                self.push(t, p[1])

    def lib_key(self, code, val):
        items = self.lib_items()
        sel = min(self.lib["sel"], len(items) - 1)
        if code in (K_UP, K_F, K_DOWN, K_X):
            d = -1 if code in (K_UP, K_F) else 1
            self.lib["sel"] = max(0, min(len(items) - 1, sel + d))
            if self.lib["sel"] != sel:
                self.lib_audition()
            return
        if val != 1 or self.mod:
            return
        kind, name = items[sel]
        t = self.track
        if code == K_ESC:
            self.close_library(keep=False)
        elif code == K_SPACE:
            st = self.status()
            self.eng.send("stop" if st and st["playing"] else "play")
        elif code == K_P and kind == "inst":
            self.preview_note()
        elif code == K_ENTER:
            if kind == "saveas":
                self.close_library(keep=False)
                self.ask_name("Save instrument as",
                              lambda n: self.save_inst(
                                  t, self.unique_inst(KIND[t], n)),
                              text=self.inst[t])
            else:
                self.close_library(keep=True)
                self.use_inst(t, name)
                self.flash(f"{TRACKS[t][0].lower()}: {name}")
        elif kind != "inst":
            return
        elif code == K_S:                       # your sound -> this instrument
            if self.confirmed(("instsave", name),
                              f"press again to save your sound as {name}"):
                vals, _, _, sample = self.lib["orig"]
                self.close_library(keep=False)
                self.write_inst(KIND[t], name, self.track_vals(t, vals),
                                sample if t == 2 else "")
                self.inst[t], self.inst_mod[t] = name, False
                self.flash(f"saved instrument {name}")
        elif code == K_R:
            self.close_library(keep=False)
            self.ask_name("Rename instrument",
                          lambda n: self.rename_inst(t, name, n), text=name)
        elif code == K_U:
            new = self.unique_inst(KIND[t], name + "-copy")
            shutil.copyfile(self.inst_path(KIND[t], name),
                            self.inst_path(KIND[t], new))
            self.lib_select(new)
            self.flash(f"copied to {new}")
        elif code == K_D:
            if self.confirmed(("instdel", name), f"press again to delete {name}"):
                os.remove(self.inst_path(KIND[t], name))
                for k in range(4):       # songs keep their copy of the sound
                    if KIND[k] == KIND[t] and self.inst[k] == name:
                        self.inst_mod[k] = True
                if self.lib["orig"][1] == name:
                    o = self.lib["orig"]
                    self.lib["orig"] = (o[0], o[1], True, o[3])
                self.lib["sel"] = min(sel, len(self.lib_items()) - 1)
                self.flash(f"deleted {name}")

    def lib_select(self, name):
        names = [n for _, n in self.lib_items()]
        if self.lib and name in names:
            self.lib["sel"] = names.index(name)

    def rename_inst(self, t, old, new):
        new = slug(new)
        if new == old:
            return
        new = self.unique_inst(KIND[t], new)
        os.rename(self.inst_path(KIND[t], old), self.inst_path(KIND[t], new))
        for k in range(4):
            if KIND[k] == KIND[t] and self.inst[k] == old:
                self.inst[k] = new
        self.open_library()
        self.lib_select(new)
        self.flash(f"renamed to {new}")

    def command(self, code, val):
        t = self.track
        if code in (K_A, K_S, K_Q, K_W) and self.screen != S_SOUND:
            if val == 1:
                self.flash("sound settings are on the SOUND screen (Tab)")
            return
        if code in (K_A, K_S):  # allow auto-repeat
            self.adjust(code == K_S)
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
            self.pick(1 if code == K_W else -1)
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
            if self.screen == S_SONG and st:
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

    def draw_scope(self, st, rect, octave=True):
        x0, y0, w, h = rect
        pygame.draw.rect(self.s, PANEL, rect, border_radius=4)
        col = TRACKS[self.track][2]
        mid = y0 + h // 2
        pygame.draw.line(self.s, GRID, (x0 + 2, mid), (x0 + w - 3, mid))
        if self.track != 3 and octave:
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

    def draw_grid(self, st, y0, rh=9):
        n, beat = self.pat_len(st)
        cw, x0 = (18 if n <= 16 else max(9, 288 // n)), 30
        step = st["step"] if st else -1
        for r in range(4):
            y = y0 + r * (rh + 1)
            on = r == self.track
            self.text(self.f_smb if on else self.f_sm, TRACKS[r][1],
                      TRACKS[r][2] if on else DIM, (x0 - 6, y + rh // 2),
                      "midright")
            for c in range(n):
                x = x0 + c * cw
                hit = st["grid"][r][c] if st else 0
                col = TRACKS[r][2] if hit else (PANEL if c // beat % 2 == 0
                                                else (32, 36, 46))
                if hit and not on:
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
        rect = (0, 18, 150, 62)
        if self.track == 2:
            self.draw_sample(st, rect)
        else:
            self.draw_scope(st, rect)
        self.draw_tracks((154, 18, 164, 62))
        self.draw_grid(st, 84, rh=16)
        self.draw_footer(
            "Ctrl: 1-4 track  Z/X oct  B/N tempo  C/V copy  R/L sample"
            if self.mod else
            "Space play  Enter rec  Fn+1-8 pattern  Tab screens  Fn+H help")

    def inst_label(self, t):
        return (self.inst[t] or "custom") + (" *" if self.inst_mod[t] else "")

    def draw_tracks(self, rect):
        """Each track's instrument; the current one highlighted."""
        x0, y0, w, h = rect
        rh = h // 4
        for t, (name, _, col) in enumerate(TRACKS):
            y = y0 + t * rh
            on = t == self.track
            pygame.draw.rect(self.s, PANEL if on else BG, (x0, y, w, rh - 1),
                             border_radius=3)
            pygame.draw.rect(self.s, col if on else GRID, (x0, y, 3, rh - 1))
            self.text(self.f_smb, name, col if on else DIM, (x0 + 7, y + 2))
            self.text(self.f_smb if on else self.f_sm, self.inst_label(t)[:20],
                      TEXT if on else DIM, (x0 + 40, y + 2))
            if t != 3:
                self.text(self.f_sm, f"O{self.octave[t]}", DIM,
                          (x0 + w - 4, y + 2), "topright")

    def draw_sound(self, st):
        t = self.track
        name, _, col = TRACKS[t]
        self.text(self.f_smb, name, col, (4, 19))
        self.text(self.f_mid, self.inst_label(t), TEXT, (38, 17))
        right = f"OCT {self.octave[t]}" if t != 3 else ""
        if t == 2:
            right = (self.sample_name[:-4] or "no sample")[:20]
        self.text(self.f_sm, right, DIM, (W - 4, 19), "topright")
        if self.lib:
            self.draw_library()
            return
        # settings, two columns
        plist = PARAMS[t]
        sel = self.sel[t] % len(plist)
        for i, p in enumerate(plist):
            x = 2 + (i // 5) * 160
            y = 34 + (i % 5) * 12
            if i == sel:
                pygame.draw.rect(self.s, PANEL, (x, y - 1, 156, 12),
                                 border_radius=2)
                pygame.draw.rect(self.s, col, (x, y - 1, 2, 12))
            f = self.f_smb if i == sel else self.f_sm
            self.text(f, p[0], TEXT if i == sel else DIM, (x + 6, y))
            self.text(f, self.fmt(t, p), TEXT, (x + 150, y), "topright")
        box = (0, 96, 156, 60)
        if t == 3:
            pygame.draw.rect(self.s, PANEL, box, border_radius=4)
            names = [f"{KEY_LABEL[k]} {DRUM_NAMES[k]}"
                     for k in (0, 2, 4, 5, 7, 9, 11, 1, 3, 6, 8, 10)]
            for k, nm in enumerate(names):
                self.text(self.f_sm, nm, DIM,
                          (6 + (k % 2) * 76, 99 + (k // 2) * 9))
        else:
            self.draw_env(box)
        self.draw_scope(st, (160, 96, 158, 60), octave=False)
        self.draw_footer("Fn+F/X pick  Fn+Z/C change  notes play  "
                         "Enter library")

    def draw_env(self, rect):
        """The amplitude envelope (attack, decay, sustain, release)."""
        x0, y0, w, h = rect
        t = self.track
        v = self.vals[t]
        col = TRACKS[t][2]
        pygame.draw.rect(self.s, PANEL, rect, border_radius=4)
        a, r = v.get(3, 5), v.get(6, 100)
        d, sus = (v.get(4, 300), v.get(5, 0.6)) if t != 2 else (0, 1)
        seg = [max(1.0, x) ** 0.5 for x in (a, d, r)]
        hold = 9.0                              # sustain shown at a fixed width
        scale = (w - 12) / (sum(seg) + hold)
        top, bot = y0 + 8, y0 + h - 6
        lvl = bot - (bot - top) * sus
        xa = x0 + 6 + seg[0] * scale
        xd = xa + seg[1] * scale
        xs = xd + hold * scale
        xr = xs + seg[2] * scale
        pts = [(x0 + 6, bot), (xa, top), (xd, lvl), (xs, lvl), (xr, bot)]
        pygame.draw.lines(self.s, col, False, pts, 2)
        self.text(self.f_sm, "ENVELOPE", DIM, (x0 + w - 4, y0 + 2), "topright")

    def draw_library(self):
        t = self.track
        col = TRACKS[t][2]
        kind = KIND[t]
        items = self.lib_items()
        sel = min(self.lib["sel"], len(items) - 1)
        title = {"synth": "SYNTH INSTRUMENTS (lead and bass)",
                 "sampler": "SAMPLER INSTRUMENTS",
                 "drums": "DRUM KITS"}[kind]
        pygame.draw.rect(self.s, col, (2, 33, 316, 13), border_radius=3)
        self.text(self.f_smb, title, BG, (W // 2, 39), "center")
        rows = 8
        top = max(0, min(sel - rows // 2, len(items) - rows))
        orig_name = self.lib["orig"][1]
        for k, (kd, name) in enumerate(items[top:top + rows]):
            i = top + k
            y = 49 + k * 12
            if i == sel:
                pygame.draw.rect(self.s, PANEL, (2, y - 1, 316, 12),
                                 border_radius=2)
                pygame.draw.rect(self.s, col, (2, y - 1, 2, 12))
            if kd == "saveas":
                self.text(self.f_sm, name, AMBER, (8, y))
                continue
            mine = name == orig_name
            self.text(self.f_smb if i == sel else self.f_sm,
                      ("● " if mine else "  ") + name[:30],
                      TEXT if i == sel or mine else DIM, (8, y))
            users = [TRACKS[k2][1] for k2 in range(4)
                     if k2 != t and KIND[k2] == kind and self.inst[k2] == name]
            if users:
                self.text(self.f_sm, "on " + " ".join(users), DIM,
                          (314, y), "topright")
        self.text(self.f_sm, "S save your sound over it   R rename   "
                  "U duplicate   D delete", DIM, (W // 2, 147), "midtop")
        self.draw_footer("F/X browse (you hear each)  Enter use  P hear  "
                         "Esc undo")

    def draw_edit(self, st):
        if self.track == 3:
            self.draw_drum_grid(st)
        else:
            self.draw_roll(st)

    def edit_view(self, st):
        """The steps the Edit screen shows: up to 16 at a time, the page
        with the cursor on it. (start, count, pattern length, beat)."""
        n, beat = self.pat_len(st)
        if self.cur_step >= n:
            self.cur_step %= n
        start = self.cur_step // 16 * 16
        return start, min(16, n - start), n, beat

    def step_cols(self, x0, cw, y0, h, st, view):
        """Beat shading and playhead behind the Edit screen's steps."""
        start, cols, n, beat = view
        for c in range(cols):
            if (start + c) // beat % 2:
                pygame.draw.rect(self.s, (18, 21, 28),
                                 (x0 + c * cw, y0, cw, h))
        if st and st["playing"] and start <= st["step"] < start + cols:
            pygame.draw.rect(self.s, (52, 58, 74),
                             (x0 + (st["step"] - start) * cw, y0, cw, h))

    def view_label(self, view):
        start, cols, n, beat = view
        label = f"{meter_name(n, beat)}  {n} steps"
        if n > 16:
            label = f"steps {start + 1}-{start + cols} of {n}   " + \
                meter_name(n, beat)
        return label

    def draw_drum_grid(self, st):
        x0, cw, y0, rh = 50, 16, 18, 12
        col = TRACKS[3][2]
        view = self.edit_view(st)
        start, cols = view[0], view[1]
        self.step_cols(x0, cw, y0, rh * len(DRUM_ROWS), st, view)
        hits = {}
        for s, n, v, _ in self.events(3):
            key = (s, DRUM_OF_SEMI[n % 12])
            hits[key] = max(hits.get(key, 0), v)
        for r, (drum, name) in enumerate(DRUM_ROWS):
            y = y0 + r * rh
            on = r == self.drum_row
            self.text(self.f_smb if on else self.f_sm, name,
                      TEXT if on else DIM, (x0 - 5, y + rh // 2), "midright")
            for c in range(cols):
                x = x0 + c * cw
                v = hits.get((start + c, drum))
                if v:
                    shade = col if v >= VEL_ACCENT else \
                        tuple(int(k * 0.7) for k in col)
                    pygame.draw.rect(self.s, shade, (x + 1, y + 1, cw - 3,
                                                     rh - 3), border_radius=2)
                    if v >= VEL_ACCENT:
                        pygame.draw.rect(self.s, TEXT, (x + 1, y + 1, cw - 3,
                                                        rh - 3), 1,
                                         border_radius=2)
                else:
                    pygame.draw.rect(self.s, PANEL, (x + 5, y + 4, cw - 11,
                                                     rh - 9), border_radius=1)
        cx = x0 + (self.cur_step - start) * cw
        cy = y0 + self.drum_row * rh
        pygame.draw.rect(self.s, AMBER, (cx - 1, cy - 1, cw + 1, rh + 1), 1,
                         border_radius=3)
        self.text(self.f_sm, f"STEP {self.cur_step + 1}", col,
                  (x0, y0 + rh * len(DRUM_ROWS) + 2))
        self.text(self.f_sm, self.view_label(view), DIM,
                  (W - 4, y0 + rh * len(DRUM_ROWS) + 2), "topright")
        self.draw_footer("F/X drum  Z/C step  Enter hit  A accent  "
                         "[ ] shift  Bksp clear row")

    def draw_roll(self, st):
        t = self.track
        col = TRACKS[t][2]
        x0, cw, y0, rh = 30, 18, 18, 4
        lo = self.note_for(t, 0)                # lowest note-key pitch
        top = y0 + ROLL_ROWS * rh
        view = self.edit_view(st)
        start, cols, plen = view[0], view[1], view[2]
        self.step_cols(x0, cw, y0, ROLL_ROWS * rh, st, view)
        for r in range(ROLL_ROWS):              # octave lines and labels
            n = lo + r
            y = top - (r + 1) * rh
            if n % 12 == 0:
                pygame.draw.line(self.s, GRID, (x0, y + rh - 1),
                                 (x0 + cols * cw, y + rh - 1))
                self.text(self.f_sm, f"C{n // 12 - 1}", DIM,
                          (x0 - 4, y + rh), "bottomright")
            elif NOTE_NAMES[n % 12].endswith("#"):
                pygame.draw.rect(self.s, (14, 16, 21),
                                 (x0, y, cols * cw, rh))
        evs = self.events(t)
        for s, n, v, ln in evs:
            if s >= plen:                       # hidden past the end
                continue
            r = n - lo
            if r < 0 or r >= ROLL_ROWS:         # outside the view: edge mark
                if start <= s < start + cols:
                    y = top - 2 if r < 0 else y0
                    pygame.draw.rect(self.s, DIM,
                                     (x0 + (s - start) * cw + 1, y, cw - 3, 2))
                continue
            y = top - (r + 1) * rh
            # the steps it covers, wrapping past the loop end
            spans = [(s, min(s + ln, plen))]
            if s + ln > plen:
                spans.append((0, s + ln - plen))
            for a, b in spans:
                a, b = max(a, start), min(b, start + cols)
                if a < b:
                    pygame.draw.rect(self.s, col,
                                     (x0 + (a - start) * cw + 1, y,
                                      max(3, (b - a) * cw - 2), rh - 1),
                                     border_radius=1)
        cx = x0 + (self.cur_step - start) * cw
        pygame.draw.rect(self.s, AMBER, (cx, y0, cw - 1, ROLL_ROWS * rh), 1,
                         border_radius=2)
        # what's on the cursor step
        here = sorted(e[1] for e in evs if e[0] == self.cur_step)
        names = " ".join(f"{NOTE_NAMES[n % 12]}{n // 12 - 1}" for n in here)
        y = top + 3
        self.text(self.f_smb, f"STEP {self.cur_step + 1}", col, (4, y))
        self.text(self.f_sm, names or "rest", TEXT if here else DIM, (54, y))
        right = f"LEN {self.step_len}   OCT {self.octave[t]}"
        if plen != 16 or view[3] != 4:
            right = self.view_label(view).split("   ")[0] + "   " + right
        self.text(self.f_sm, right, DIM, (W - 4, y), "topright")
        self.draw_footer("notes write  Fn+Z/C step  Enter skip  Bksp del  "
                         "- = len  [ ] shift")

    def draw_song(self, st):
        blink = int(time.time() * 4) % 2
        cur = st["cur_pat"] if st else 0
        nxt = st["next_pat"] if st else -1
        for p in range(NPAT):
            x = 2 + (p % 4) * 79
            y = 19 + (p // 4) * 35
            g = st["pgrid"][p] if st else np.zeros((4, NSTEP), np.uint8)
            n, beat = self.pat_len(st, p)
            g = g[:, :n]
            empty = not g.any()
            pygame.draw.rect(self.s, PANEL, (x, y, 76, 32), border_radius=4)
            border = TEXT if p == cur else (AMBER if p == nxt and blink
                                            else None)
            if border:
                pygame.draw.rect(self.s, border, (x, y, 76, 32), 1,
                                 border_radius=4)
            self.text(self.f_smb, str(p + 1), TEXT if p == cur else DIM,
                      (x + 4, y + 1))
            if (n, beat) != (16, 4):
                self.text(self.f_sm, meter_name(n, beat), AMBER,
                          (x + 73, y + 1), "topright")
            if empty:
                self.text(self.f_sm, "empty", GRID, (x + 44, y + 16),
                          "center")
                continue
            cwf = 64 / n
            for r in range(4):
                for c in range(n):
                    if g[r][c]:
                        pygame.draw.rect(self.s, TRACKS[r][2],
                                         (x + 11 + int(c * cwf), y + 12 + r * 5,
                                          max(1, int(cwf) - 1), 4))
        # chain
        y0 = 92
        song = st and st["song_mode"]
        self.text(self.f_smb, "CHAIN", TEXT, (4, y0))
        self.text(self.f_sm, "song mode ON" if song else "song mode off",
                  AMBER if song else DIM, (W - 4, y0), "topright")
        n, beat = self.pat_len(st)
        self.text(self.f_sm, f"P{cur + 1}: {meter_name(n, beat)}, {n} steps",
                  TEXT, (W // 2 - 10, y0), "midtop")
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
        self.draw_footer("1-8 add  Bksp del  Z/C move  Enter song mode  "
                         "- = length  M meter")

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
                playing = kind == "sample" and name == self.previewing()
                mark = "▶ " if playing else ("● " if current else "  ")
                self.text(self.f_smb if current else self.f_sm,
                          mark + label[:22],
                          TRACKS[2][2] if playing else col_t, (x0 + 3, y - 1))
                if kind == "sample":
                    ln = self.wav_len(name)
                    if ln:
                        self.text(self.f_sm, f"{ln:.1f}s", DIM,
                                  (x0 + 153, y - 1), "topright")
            if not items:
                self.text(self.f_sm, "none yet: Ctrl+R records one", DIM,
                          (x0 + 78, 70), "center")
        if self.menu:
            self.draw_menu()
        elif self.files_col == 1:
            self.draw_footer("F/X up/down  Z/C column  Enter actions  "
                             "P play  Del delete")
        else:
            self.draw_footer("F/X up/down  Z/C column  Enter open  S save  "
                             "Del delete")

    def draw_menu(self):
        """Action menu for one sample, over the SONGS column so the sample
        list stays visible."""
        name = self.menu["name"]
        x0, y0, w = 2, 34, 156
        h = 26 + 13 * len(SAMPLE_ACTIONS)
        col = TRACKS[2][2]
        pygame.draw.rect(self.s, (0, 0, 0), (x0, y0, w, h), border_radius=5)
        pygame.draw.rect(self.s, col, (x0, y0, w, h), 1, border_radius=5)
        self.text(self.f_smb, name[:-4][:24], TEXT, (x0 + 6, y0 + 3))
        ln = self.wav_len(name)
        users = len(self.songs_using(name))
        info = (f"{ln:.1f}s" if ln else "?") + \
            (f" · in {users} song{'s' * (users > 1)}" if users else "")
        self.text(self.f_sm, info, DIM, (x0 + 6, y0 + 14))
        playing = self.previewing() == name
        for i, (act, label, k) in enumerate(SAMPLE_ACTIONS):
            y = y0 + 26 + i * 13
            sel = i == self.menu["sel"]
            if sel:
                pygame.draw.rect(self.s, PANEL, (x0 + 3, y - 1, w - 6, 13),
                                 border_radius=2)
                pygame.draw.rect(self.s, col, (x0 + 3, y - 1, 2, 13))
            if act == "play" and playing:
                label = "Stop"
            if act == "load" and name == self.sample_name:
                label = "In the sampler now"
            self.text(self.f_smb if sel else self.f_sm, label,
                      TEXT if sel else DIM, (x0 + 10, y))
            self.text(self.f_sm, KEY_NAME[k], DIM, (x0 + w - 8, y),
                      "topright")
        self.draw_footer("F/X pick  Enter do it  or press the letter  "
                         "Esc close")

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
            ("TAB", "screens: main → edit → sound → song → files"),
            ("EDIT SCREEN", "drums: grid · synths: play notes into steps"),
            ("FN 1-8 / FN 0", "pick pattern (at loop end) · song mode"),
            ("CTRL 1-4", "track: lead, bass, sample, drums"),
            ("SOUND SCREEN", "Fn+F/X pick, Fn+Z/C change, Enter library"),
            ("CTRL Z/X  B/N", "octave  ·  tempo"),
            ("CTRL C / V", "copy / paste pattern"),
            ("CTRL R / L", "record sample  ·  next sample"),
            ("BKSP / CTRL", "undo note  ·  clear track"),
            ("SONG SCREEN", "1-8 chain · Enter song mode · - = M length"),
            ("ESC", "back / quit (everything is saved)"),
        ]
        self.text(self.f_mid, "ZSynth keys", TEXT, (6, 2))
        for i, (k, v) in enumerate(lines):
            y = 18 + i * 11
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
            [self.draw_main, self.draw_edit, self.draw_sound, self.draw_song,
             self.draw_files][self.screen](st)
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
            if self.later:
                self.run_later()
            now = time.time()
            if now >= nxt:
                self.render()
                nxt = now + frame
            time.sleep(0.004)
        self.stop_preview()
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
