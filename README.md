# sidviz_u64

Real-time waveform visualizer for the Commodore 64, driven by a Mac over the [Ultimate 64 (U64)](https://ultimate64.com) network API. Plays SID files, MP3s/audio files, and YouTube streams — and renders the live waveform on a real C64 screen with a scrolling metadata ticker.

```
+------------------------------------------------------+
|  sidviz_u64  v1.5.0  build 2026-04-23               |
+------------------------------------------------------+
|  File: Commando.sid                                  |
|  Title        Commando                               |
|  Author       Rob Hubbard                            |
|  Released     1985 Electric Dreams                   |
|  Speed        CIA                                    |
|  Length       3:30                                   |
|  Format       PSID v2                                |
+------------------------------------------------------+
```

---

## How it works

`sidviz_u64.py` runs on the Mac. It reboots the C64 via the U64 API, uploads a small 6502 program (`sidviz.prg`), and then streams waveform frames into C64 screen RAM at up to 10 fps. A scrolling PETSCII ticker on row 1 shows the track metadata. Audio plays on the Mac (or on real C64 hardware for SID files).

---

## Requirements

### Hardware
- Ultimate 64 (U64) connected to your local network

### Software
| Tool | Purpose |
|---|---|
| Python 3 | Runtime |
| `ffmpeg` | Waveform frame generation |
| `ffplay` | Audio playback (MP3 / YouTube) |
| `ffprobe` | Audio file metadata |
| `sidplayfp` | SID emulation and playback |
| `yt-dlp` | YouTube streaming (optional) |
| `64tass` | Assembles `sidviz.prg` — build time only |

Install on macOS with Homebrew:
```bash
brew install ffmpeg sidplayfp yt-dlp
brew install 64tass  # build time only
```

---

## Setup

### 1. Build the C64 program

```bash
64tass -a -B -o sidviz.prg sidviz.asm
```

This only needs to be done once (or after modifying the assembly).

### 2. Run

```bash
python3 sidviz_u64.py [file | URL] [options]
```

If no file is given the script prompts for one interactively.

---

## Usage

```
usage: sidviz_u64 [-h] [--ip IP] [--color] [--no-color] [--sid] [--audio]
                  [--c64audio] [--macaudio] [--fps FPS] [--save FILE.mp3]
                  [--version]
                  [file]

SID/audio waveform visualizer for C64 via U64 API

positional arguments:
  file             Audio/SID file or YouTube URL

options:
  --ip IP          U64 IP address (default: 192.168.2.64)
  --color          Start with rainbow color mode
  --no-color       Start with white density color mode
  --sid            Force SID file mode
  --audio          Force audio file mode
  --c64audio       Play SID audio on C64 hardware (experimental)
  --macaudio       Play SID audio on Mac via sidplayfp (default)
  --fps FPS        Waveform frame rate (default: 10)
  --save FILE.mp3  Save YouTube stream to MP3 while playing
  --version        Show version and exit
```

### Interactive controls

While streaming, the following keys are active in the terminal:

| Key | Action |
|---|---|
| `c` / `C` | Cycle color mode (rainbow → white → fire → …) |
| `q` / `Q` / Ctrl-C | Stop playback and quit |

---

## Modes

Mode is detected automatically from the file extension or URL. You can override with `--sid` or `--audio`.

### SID files (`.sid`)

```bash
python3 sidviz_u64.py tune.sid
```

`sidplayfp` emulates the SID chip and outputs audio to the Mac speaker. The waveform is generated from the same audio via a named pipe. Song length is read from the SID header and used to stop playback automatically.

When prompted, you can choose to play audio on the **Mac** (default) or on real **C64 hardware** (see below).

#### C64 hardware audio (experimental)

```bash
python3 sidviz_u64.py tune.sid --c64audio
```

The PSID binary is parsed, uploaded into C64 RAM, and the init/play addresses are wired up via trampoline routines at `$C600`/`$C610`. The C64's CIA1 timer is set to PAL 50 Hz so the SID driver runs at the correct rate. The waveform visualization still comes from `sidplayfp` on the Mac.

Constraints:
- SID code must not overlap `$0810–$0900` or `$C000–$C6FF` (used by sidviz)
- Screen RAM overlap (`$0400–$07E7`) causes artifacts on rows 0–7 only — the waveform area (rows 8–24) is unaffected
- The play routine must end with `RTS` — most PSID files do; some RSID files do not

### Audio files (MP3, FLAC, WAV, etc.)

```bash
python3 sidviz_u64.py track.mp3
```

`ffplay` handles audio playback. `ffmpeg` generates waveform frames directly from the file. Metadata (title, artist, album, duration, etc.) is read via `ffprobe`.

### YouTube streaming

```bash
python3 sidviz_u64.py 'https://www.youtube.com/watch?v=...'
```

> **Note:** Always quote the URL — the `?` in YouTube URLs is a glob character in zsh/bash and will cause a "no matches found" error if unquoted.

`yt-dlp` is required. Two parallel streams are opened (one for audio via `ffplay`, one for waveform via `ffmpeg`), both piped directly from `yt-dlp` so range requests and segmented DASH streams are handled correctly.

Metadata (title, uploader, date, duration) is fetched via `yt-dlp` before the C64 reboots, so a bad URL or missing `yt-dlp` fails fast.

#### Save to MP3 while playing

```bash
python3 sidviz_u64.py 'https://www.youtube.com/watch?v=...' --save output.mp3
```

A third `yt-dlp` process downloads and converts the stream to MP3 in parallel with playback.

---

## Color modes

Three palette modes cycle with `c`:

| Mode | Description |
|---|---|
| **Rainbow** | Each waveform density level gets a different colour, cycling through the spectrum |
| **White** | Brightness mapped to waveform density — brighter = louder |
| **Fire** | Warm fire palette from black through red, orange, and yellow |

Start in a specific mode with `--color` (rainbow) or `--no-color` (white), or choose at the prompt.

---

## Ticker

Metadata is converted to PETSCII and scrolled across row 1 of the C64 screen. Fields are separated by ` * `. Maximum 253 characters.

| Mode | Fields shown |
|---|---|
| SID | Title · Author · Released · Song Speed · Song Length · Format · Addresses |
| Audio / YouTube | Title · Artist · Album · Date · Genre · Duration · Bitrate · Format |

---

## Memory map

| Address | Owner | Description |
|---|---|---|
| `$C000` | Python | Frame flag — Python sets 1, ASM clears after display |
| `$C001` | Python | Color flag — 1=white, 2=rainbow, 3=fire |
| `$C002` | Python | C64 audio flag — 0=off, 1=waiting, 2=SID ready |
| `$C003` | ASM | SID play flag — IRQ sets 1, main loop calls play and clears |
| `$C100–$C3A7` | Python | Frame buffer — 680 bytes PETSCII (rows 8–24) |
| `$C500–$C5FB` | Python | Ticker buffer — up to 253 PETSCII chars |
| `$C5FC` | ASM | Color mode — 0=rainbow, 1=white, 2=fire |
| `$C5FD` | ASM | IRQ tick counter |
| `$C5FE` | Python | Ticker length |
| `$C5FF` | ASM | Ticker read position |
| `$C600` | Python | `JMP initAddress` trampoline (C64 audio mode) |
| `$C610` | Python | `JMP playAddress` trampoline (C64 audio mode) |
