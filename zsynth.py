#!/usr/bin/env python3
"""ZSynth: a pocket groovebox for the M5 Cardputer Zero.

Four tracks (lead synth, bass synth, mic sampler, drum kit) and a 16-step
sequencer that records what you play. This file is the screen and keyboard
front end; the sound comes from zsynth-engine (C), driven over a pipe.

Notes:  Z X C V B N M = white keys, S D G H J = black keys (lower octave)
        Q W E R T Y U I O P = white keys, 2 3 5 6 7 9 0 = black (upper)
Plain:  Space play/stop   Enter record on/off   Bksp undo last note
        Tab help          Esc quit (everything is saved)
Ctrl or Alt held:
        1-4 track   Q/W choose setting   A/S change it   Z/X octave
        B/N tempo   R record sample      L load next WAV  Bksp clear track
"""
import glob
import json
import mmap
import os
import signal
import struct
import subprocess
import sys
import time

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
STATE = os.path.join(DATA, "state.json")
PATTERN = os.path.join(DATA, "pattern.txt")
SAMPLE = os.path.join(DATA, "sample.wav")
SHM = "/dev/shm/zsynth.state"
W, H = 320, 170

# ---- keyboard (Linux key codes from the Cardputer Zero keymap) ----
K_ESC, K_BKSP, K_TAB, K_ENTER, K_SPACE = 1, 14, 15, 28, 57
K_CTRL, K_ALT = 29, 56
K_1, K_2, K_3, K_4 = 2, 3, 4, 5
K_Q, K_W, K_A, K_S, K_Z, K_X = 16, 17, 30, 31, 44, 45
K_B, K_N, K_R, K_L = 48, 49, 19, 38
NOTE_KEYS = {
    44: 0, 31: 1, 45: 2, 32: 3, 46: 4, 47: 5, 34: 6, 48: 7, 35: 8, 49: 9,
    36: 10, 50: 11,
    16: 12, 3: 13, 17: 14, 4: 15, 18: 16, 19: 17, 6: 18, 20: 19, 7: 20,
    21: 21, 8: 22, 22: 23, 23: 24, 10: 25, 24: 26, 11: 27, 25: 28,
}
KEY_LABEL = {v: k for k, v in {
    "Z": 0, "S": 1, "X": 2, "D": 3, "C": 4, "V": 5, "G": 6, "B": 7, "H": 8,
    "N": 9, "J": 10, "M": 11}.items()}

# ---- tracks and their settings ----
TRACKS = [("LEAD", "LD", (80, 200, 255)), ("BASS", "BS", (255, 140, 60)),
          ("SAMPLE", "SP", (200, 120, 255)), ("DRUMS", "DR", (90, 220, 120))]
WAVES = ["SINE", "TRI", "SAW", "SQUARE"]
MODES = ["ONE-SHOT", "GATE"]
DRUM_NAMES = ["KICK", "RIM", "SNARE", "CLAP", "C-HAT", "LO TOM", "O-HAT",
              "HI TOM", "BELL", "CRASH", "O-HAT", "CLAP"]

# (label, engine index, kind, lo, hi)  kind: choice/log/lin/ms
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
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def font(name, size):
    try:
        return pygame.font.Font(FONT_DIR + name, size)
    except (OSError, FileNotFoundError):
        return pygame.font.Font(None, size + 4)


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
        os.makedirs(SAMPLES, exist_ok=True)
        pygame.font.init()
        self.s = pygame.Surface((W, H))
        self.f_sm = font("DejaVuSansCondensed.ttf", 10)
        self.f_smb = font("DejaVuSansCondensed-Bold.ttf", 10)
        self.f_mid = font("DejaVuSans-Bold.ttf", 12)
        self.f_big = font("DejaVuSans-Bold.ttf", 18)

        self.track = 0
        self.sel = [0, 0, 0, 0]
        self.octave = list(DEFAULT_OCT)
        self.vals = [dict(d) for d in DEFAULTS]
        self.bpm = 110
        self.sample_file = 0
        self.help = False
        self.mod = False
        self.held = {}  # key code -> (track, note)
        self.last_note = [None] * 4
        self.msg, self.msg_t = "", 0
        self.load_state()

        self.eng = Engine()
        self.eng.send("samppath", SAMPLE)
        if os.path.exists(SAMPLE):
            self.eng.send("sampload", SAMPLE)
        self.eng.send("load", PATTERN)
        self.eng.send("bpm", self.bpm)
        for t in range(4):
            for _, idx, kind, _, _ in PARAMS[t]:
                self.push(t, idx)

        self.shm = None
        for _ in range(50):
            try:
                fd = os.open(SHM, os.O_RDONLY)
                if os.fstat(fd).st_size >= 48:
                    self.shm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
                    break
                os.close(fd)
            except OSError:
                pass
            time.sleep(0.05)

        self.fb = os.open(FB, os.O_RDWR)
        try:
            self.kbd = os.open(KBD, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f"zsynth: no keyboard ({e})", file=sys.stderr)
            self.kbd = None
        self.running = True

    # ---------- state ----------
    def load_state(self):
        try:
            with open(STATE) as fh:
                st = json.load(fh)
            self.bpm = int(st.get("bpm", self.bpm))
            self.octave = st.get("octave", self.octave)
            self.track = int(st.get("track", 0))
            for t, d in enumerate(st.get("vals", [])[:4]):
                for k, v in d.items():
                    self.vals[t][int(k)] = float(v)
        except (OSError, ValueError, TypeError):
            pass

    def save_state(self):
        st = {"bpm": self.bpm, "octave": self.octave, "track": self.track,
              "vals": [{str(k): v for k, v in d.items()} for d in self.vals]}
        try:
            with open(STATE, "w") as fh:
                json.dump(st, fh)
        except OSError:
            pass
        self.eng.send("save", PATTERN)

    def push(self, t, idx):
        v = self.vals[t][idx]
        kind = next(p[2] for p in PARAMS[t] if p[1] == idx)
        self.eng.send("set", t, idx, v / 1000.0 if kind == "ms" else v)

    def flash(self, text):
        self.msg, self.msg_t = text, time.time()

    # ---------- engine status ----------
    def status(self):
        if not self.shm:
            return None
        hdr = struct.unpack_from("<II6i2f2i", self.shm, 0)
        return {
            "playing": hdr[2], "rec": hdr[3], "step": hdr[4], "bpm": hdr[5],
            "sampling": hdr[6], "sample_len": hdr[7], "in": hdr[8],
            "out": hdr[9], "voices": hdr[10],
            "scope": np.frombuffer(self.shm, np.float32, 640, 48),
            "grid": np.frombuffer(self.shm, np.uint8, 64, 48 + 2560
                                  ).reshape(4, 16),
            "ov": np.frombuffer(self.shm, np.uint8, 256, 48 + 2560 + 64),
        }

    # ---------- input ----------
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
        if t == 3:  # drums: same kit on both rows
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
        if self.help and val == 1:
            self.help = False
            return
        if self.mod:
            self.command(code, val)
            return
        if val != 1:
            return
        if code in NOTE_KEYS:
            t = self.track
            n = self.note_for(t, NOTE_KEYS[code])
            self.held[code] = (t, n)
            self.last_note[t] = n
            self.eng.send("on", t, n, 110)
        elif code == K_SPACE:
            st = self.status()
            self.eng.send("stop" if st and st["playing"] else "play")
        elif code == K_ENTER:
            st = self.status()
            self.eng.send("rec", 0 if st and st["rec"] else 1)
        elif code == K_BKSP:
            self.eng.send("undo", self.track)
        elif code == K_TAB:
            self.help = True
        elif code == K_ESC:
            self.running = False

    def command(self, code, val):
        t = self.track
        plist = PARAMS[t]
        if code in (K_A, K_S):  # allow auto-repeat
            label, idx, kind, lo, hi = plist[self.sel[t] % len(plist)]
            v = self.vals[t][idx]
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
            self.bpm = max(40, min(300, self.bpm + (1 if code == K_N else -1)))
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
        elif code == K_R:
            st = self.status()
            if st and st["sampling"]:
                self.eng.send("samprec", "stop")
            else:
                self.track = 2
                self.eng.send("samprec", "start")
        elif code == K_L:
            files = sorted(glob.glob(os.path.join(SAMPLES, "*.wav")))
            if not files:
                self.flash("no WAVs in ~/.local/share/zsynth/samples")
            else:
                self.sample_file %= len(files)
                f = files[self.sample_file]
                self.sample_file += 1
                self.eng.send("sampload", f)
                try:  # keep a copy as the current sample
                    with open(f, "rb") as src, open(SAMPLE, "wb") as dst:
                        dst.write(src.read())
                except OSError:
                    pass
                self.track = 2
                self.flash("loaded " + os.path.basename(f))
        elif code == K_BKSP:
            self.eng.send("clear", t)
            self.flash(f"cleared {TRACKS[t][0].lower()}")

    # ---------- drawing ----------
    def text(self, f, s, col, pos, anchor="topleft"):
        img = f.render(s, True, col)
        r = img.get_rect(**{anchor: pos})
        self.s.blit(img, r)
        return r

    def fmt(self, t, p):
        label, idx, kind, lo, hi = p
        v = self.vals[t][idx]
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
            w = 58
            on = i == self.track
            pygame.draw.rect(self.s, col if on else PANEL, (x, 0, w - 2, 15),
                             border_radius=3)
            self.text(self.f_smb, f"{i + 1} {name}", BG if on else DIM,
                      (x + w // 2 - 1, 8), "center")
            x += w
        bx = 234
        playing = st and st["playing"]
        if playing:
            pygame.draw.polygon(self.s, (90, 220, 120),
                                [(bx, 3), (bx, 12), (bx + 8, 7)])
        else:
            pygame.draw.rect(self.s, DIM, (bx, 3, 9, 9))
        if st and st["rec"]:
            pygame.draw.circle(self.s, RED, (bx + 18, 8), 5)
        self.text(self.f_mid, f"{self.bpm}", TEXT, (W - 23, 8), "midright")
        self.text(self.f_sm, "bpm", DIM, (W - 3, 9), "midright")

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
        # trigger on a rising zero crossing so the wave stands still
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
            self.text(self.f_sm, "no sample yet", DIM,
                      (x0 + w // 2, mid - 7), "center")
            self.text(self.f_sm, "Ctrl+R records from the mic", DIM,
                      (x0 + w // 2, mid + 7), "center")
            return
        ov = st["ov"]
        for i in range(w - 4):
            a = ov[int(i * 256 / (w - 4))] / 255 * (h / 2 - 4)
            pygame.draw.line(self.s, col, (x0 + 2 + i, mid - a),
                             (x0 + 2 + i, mid + a))
        self.text(self.f_sm, f"{st['sample_len'] / 48000:.2f}s", DIM,
                  (x0 + w - 4, y0 + 2), "topright")

    def draw_params(self, rect):
        x0, y0, w, h = rect
        t = self.track
        plist = PARAMS[t]
        sel = self.sel[t] % len(plist)
        rows = 10
        top = max(0, min(sel - rows // 2, len(plist) - rows))
        for i, p in enumerate(plist[top:top + rows]):
            j = top + i
            y = y0 + i * 10
            if j == sel:
                pygame.draw.rect(self.s, PANEL, (x0, y, w, 10))
                pygame.draw.rect(self.s, TRACKS[t][2], (x0, y, 2, 10))
            f = self.f_smb if j == sel else self.f_sm
            self.text(f, p[0], TEXT if j == sel else DIM, (x0 + 5, y - 1))
            self.text(f, self.fmt(t, p), TEXT, (x0 + w - 2, y - 1),
                      "topright")
        if t == 3:  # drum key map
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

    def draw_footer(self):
        if self.msg and time.time() - self.msg_t < 2.5:
            self.text(self.f_sm, self.msg, TEXT, (W // 2, H - 1), "midbottom")
            return
        hint = ("Ctrl: 1-4 track  Q/W pick  A/S adjust  Z/X oct  B/N tempo"
                if self.mod else
                "Space play  Enter rec  Ctrl+… settings  Tab help  Esc quit")
        self.text(self.f_sm, hint, DIM, (W // 2, H - 1), "midbottom")

    def draw_help(self):
        self.s.fill(BG)
        lines = [
            ("NOTES", "Z X C V B N M + S D G H J  (low)"),
            ("", "Q W E R T Y U I O P + 2 3 5 6 7 9 0"),
            ("SPACE", "play / stop the 16-step loop"),
            ("ENTER", "record on/off (play while it loops)"),
            ("BKSP", "undo last recorded note"),
            ("CTRL 1-4", "track: lead, bass, sample, drums"),
            ("CTRL Q/W", "choose setting   CTRL A/S change"),
            ("CTRL Z/X", "octave down/up   CTRL B/N tempo"),
            ("CTRL R", "record a sample from the mic"),
            ("CTRL L", "load next WAV from samples folder"),
            ("CTRL BKSP", "clear this track's pattern"),
            ("ESC", "quit (song is saved)"),
        ]
        self.text(self.f_mid, "ZSynth keys", TEXT, (6, 2))
        for i, (k, v) in enumerate(lines):
            y = 19 + i * 12
            self.text(self.f_smb, k, TRACKS[i % 4][2], (6, y))
            self.text(self.f_sm, v, TEXT, (70, y))
        self.text(self.f_sm, "any key to close", DIM, (W - 4, H - 1),
                  "bottomright")

    def render(self):
        st = self.status()
        if st:
            self.bpm = st["bpm"] or self.bpm
        if self.help:
            self.draw_help()
            self.flush()
            return
        self.s.fill(BG)
        self.draw_top(st)
        rect = (0, 18, 150, 96)
        if self.track == 2:
            self.draw_sample(st, rect)
        else:
            self.draw_scope(st, rect)
        self.draw_params((156, 18, 162, 100))
        self.draw_grid(st, 119)
        self.draw_footer()
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
        self.save_state()
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
