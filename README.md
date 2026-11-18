# ZSynth

A pocket groovebox for the M5Stack **Cardputer Zero**: two synths, a mic
sampler and a drum kit, eight 16-step patterns per song, a song chain, and a
library of songs and samples.

![Main screen](docs/drums.png) ![Song screen](docs/song.png)
![Files screen](docs/files.png)
![Drum grid](docs/edit-drums.png) ![Step entry](docs/edit-roll.png)

## Features

- **Four tracks**: Lead synth, Bass synth, Sampler, Drums. Each track has its
  own sound settings and pattern.
- **Synths**: sine, triangle, saw and square oscillators (band-limited), a
  detuned second oscillator, an ADSR envelope, and a resonant low-pass filter
  with envelope modulation. Polyphonic.
- **Sampler**: records up to 5 s from the built-in mic, trims the silence and
  normalises it, then plays it across the keyboard. Choose one-shot or gate
  mode. You can also load your own 16-bit WAV files.
- **Drums**: ten synthesised sounds: kick, snare, clap, rim, closed and open
  hats, low and high toms, cowbell and crash.
- **Sequencer**: a 16-step loop. Arm recording and play along; notes snap to
  16ths, note lengths are kept, and a metronome clicks while you record.
- **Step editing**: the Edit screen turns the drum track into a classic drum
  machine grid (10 drums × 16 steps, with accents). On the other tracks it
  becomes a piano roll: play notes or chords and they're written into the
  step under the cursor. You can also set note lengths, enter rests, delete
  steps and shift a whole track left or right.
- **Pattern banks**: 8 patterns per song. Switching while the loop plays
  waits until the end of the bar. You can copy and paste whole patterns.
- **Song mode**: chain patterns (for example 1 1 2 3) and play them in order.
- **Songs**: save, save as, new, open and delete named songs. A song keeps its
  patterns, chain, tempo, sound settings and which sample it uses.
- **Sample library**: every mic recording is saved as its own WAV. Each
  sample has a menu to play it, put it in the sampler, rename it (songs that
  use it follow the new name), duplicate it or delete it.
- **Live scope**, step grid and settings on the 320×170 screen.
- The open song is saved automatically when you quit or switch songs.

## Screens

`Tab` cycles through four screens:

- **Main**: play, record, the scope and sound settings, and the current
  pattern's step grid.
- **Edit**: edit the current track of the current pattern step by step: a
  drum grid for the drums, a piano roll for the others.
- **Song**: the eight pattern slots, with a preview of each, and the chain.
- **Files**: songs on the left, samples on the right.

## Keys

`Fn` + a key uses the Cardputer's function layer: `Fn`+number row gives
F1–F10, `Fn`+`F` `Z` `X` `C` gives the arrow keys, and `Fn`+`H` gives Help.

| Key | Action |
| --- | --- |
| `Z X C V B N M` + `S D G H J` | notes, lower octave (white + black keys) |
| `Q W E R T Y U I O P` + `2 3 5 6 7 9 0` | notes, upper octave |
| `Space` | play / stop the loop |
| `Enter` | record on/off: play while it loops |
| `Backspace` | undo the last recorded note |
| `Tab` | next screen: Main → Edit → Song → Files |
| `Fn` + `H` | help |
| `Fn` + `1`–`8` | choose a pattern (while playing, it switches at the end of the loop) |
| `Fn` + `0` | song mode on/off |
| `Esc` | back to Main / quit (the song is saved) |
| `Ctrl` + `1`–`4` | choose a track: lead, bass, sample, drums |
| `Ctrl` + `Q` / `W` | choose a setting |
| `Ctrl` + `A` / `S` | change it (hold to repeat) |
| `Ctrl` + `Z` / `X` | octave down / up |
| `Ctrl` + `B` / `N` | tempo down / up |
| `Ctrl` + `C` / `V` | copy the current pattern / paste it into the current pattern |
| `Ctrl` + `R` | start / stop recording a sample (saved to the library) |
| `Ctrl` + `L` | load the next sample from the library |
| `Ctrl` + `Backspace` | clear this track in the current pattern (on the Song screen: the whole pattern, after you press it twice) |

`Alt` works the same as `Ctrl`. On the drum track, each key plays one drum;
the key map is shown on screen.

**Edit screen, drums:** `F`/`X` pick a drum and `Z`/`C` pick a step (the
arrows printed on those keys; `Fn` isn't needed here). `Enter` adds or
removes a hit, `A` turns an accent on or off, and `Backspace` pressed twice
clears that drum's row.

**Edit screen, other tracks:** play a note key to write that note into the
step under the cursor. The cursor then moves on by the note length. Hold
several keys to write a chord, and play the same note on a step again to
remove it. `Fn`+`Z`/`C` move the cursor, `Enter` skips a step (a rest),
`Backspace` clears the step, and `-` / `=` (or `+`) make notes shorter or
longer (also changing the notes already on the step). `Ctrl`+`Z`/`X` change
the octave, as on the main screen.

**Edit screen, both:** `[` / `]` shift the whole track one step left or
right. The symbol keys (`[ ] - = +`) are typed the usual way on the
Cardputer, through its symbol layer. `Space` plays and stops, so you can edit while the loop plays; the
playhead shows on the grid. `Ctrl`+`1`–`4` switches track and `Fn`+`1`–`8`
switches pattern, as everywhere.

**Song screen:** `1`–`8` insert a pattern into the chain at the cursor,
`Backspace` removes the one before the cursor, `Z`/`C` move the cursor, and
`Enter` turns song mode on or off.

**Files screen:** `F`/`X` move up and down, `Z`/`C` switch between Songs and
Samples, `Enter` opens a song (or runs *new song* / *save as…*, which ask for
a name), `S` saves the open song, and `Backspace`/`Del` pressed twice deletes
the selected file. In the Samples column, `P` plays the selected sample as
recorded (press again to stop) and `Enter` opens its menu:

![Sample menu](docs/sample-menu.png)

| Key | Action |
| --- | --- |
| `P` | play / stop, without loading it |
| `L` | use it in the sampler track |
| `R` | rename it: every song that uses it is updated |
| `U` | duplicate it (as *name*-copy) |
| `D` | delete it (press twice) |

`F`/`X` and `Enter` work too, and `Esc` closes the menu. The menu shows the
sample's length and how many songs use it.

## How it works

- `engine.c` is the real-time audio engine. It handles synthesis, the
  sequencer clock, sample playback and mic capture, and talks to PipeWire
  through the PulseAudio API. The ALSA default device on the stock image
  points at the HDMI port, so it doesn't use ALSA directly. It takes text
  commands on stdin and publishes its state (playhead, scope, pattern grid)
  in `/dev/shm/zsynth.state`.
- `zsynth.py` is the front end. It reads the keyboard from evdev, draws with
  pygame straight to the LCD framebuffer (`/dev/fb0`, RGB565), and drives the
  engine over a pipe. It is launched from the stock APPLaunch launcher like
  the built-in apps.

Data lives in `~/.local/share/zsynth/`:

- `songs/<name>.zsong` is a plain-text song file. It holds the sample name,
  the sound settings, the tempo, the chain, and every note as
  `ev pattern track step note velocity length`.
- `samples/*.wav` is the sample library. Mic recordings are named
  `rec-YYYYMMDD-HHMMSS.wav`. You can copy in your own 16-bit WAVs at any
  sample rate, mono or stereo.
- `state.json` remembers which song was open last.

A single-pattern song from an earlier version is moved into
`songs/my-first-song.zsong` the first time you open this version.

## Install

You need a Cardputer Zero on the official 64-bit Debian 13 image, reachable
over SSH with passwordless `sudo`. From this folder on your computer, run:

```sh
./install.sh user@cardputer-ip
```

This copies the sources, installs `libpulse-dev` if needed, compiles the
engine on the device, installs it to `/opt/zsynth`, and adds **ZSynth** to the
launcher. Then restart the launcher.

`test_ui.py` runs the app headless with simulated key presses, checks
pattern banks, the drum grid, step entry, the chain, song mode and
saving/opening songs, and saves
screenshots. Run it on the device with `python3 test_ui.py /tmp/zs`.

## Requirements

The stock image already has Python 3 with pygame and numpy. The engine
needs `libpulse-dev` to build, and the install script adds it.
