# sidviz_u64

Real-time waveform visualizer for the Commodore 64, driven by a Mac or Linux machine over the [Ultimate 64 (U64)](https://ultimate64.com) network API. Plays SID files, MP3s/audio files, and streams from YouTube, SoundCloud, and Spotify — and renders the live waveform on a real C64 screen with a scrolling metadata ticker.

```
+------------------------------------------------------+
|  sidviz_u64  v1.6.5  build 2026-04-24                |
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
| `ffplay` | Audio playback (MP3 / streams) |
| `ffprobe` | Audio file metadata |
| `sidplayfp` | SID emulation and playback |
| `yt-dlp` | YouTube, SoundCloud, and Spotify streaming (optional) |
| `64tass` | Assembles `sidviz.prg` — build time only |

#### macOS

```bash
brew install ffmpeg sidplayfp yt-dlp
brew install 64tass  # build time only
```

#### Linux (Debian / Ubuntu)

```bash
sudo apt install ffmpeg sidplayfp 64tass
pip install yt-dlp          # or: pipx install yt-dlp
```

> **Note:** Package names and availability vary by distro and release. If `sidplayfp` or `64tass` are not in your repos, see the build-from-source notes below.

#### Linux (Fedora / RHEL)

```bash
sudo dnf install ffmpeg      # may require RPM Fusion
sudo dnf install 64tass
pip install yt-dlp
# sidplayfp: build from source (see below)
```

### Linux (Arch)
```
sudo pacman -S --needed ffmpeg sidplayfp yt-dlp 64tass
```

#### IDUN Cartridge Linux (Arch 33-bit)

```bash
sudo pacman -S --needed sidplayfp yt-dlp 64tass
# ffmpeg: 32-bit ARM version needed
wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-armhf-static.tar.xz
tar xvf ffmpeg-release-armhf-static.tar.xz
cd ffmpeg-*-armhf-static/
# verify version
./ffmpeg -version
sudo cp ffmpeg ffprobe /usr/local/bin
```

#### Building sidplayfp from source (any Linux)

```bash
sudo apt install libsidplayfp-dev   # or equivalent for your distro
# or build from: https://github.com/libsidplayfp/sidplayfp
```

#### Building 64tass from source (any Linux)

```bash
# https://sourceforge.net/projects/tass64/
./configure && make && sudo make install
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
                  [--yt-search QUERY] [--yt-max YT_MAX]
                  [--version]
                  [file]

SID/audio waveform visualizer for C64 via U64 API

positional arguments:
  file             Audio/SID file, or YouTube / SoundCloud / Spotify URL

options:
  --ip IP          U64 IP address (default: 192.168.2.64)
  --color          Start with rainbow color mode
  --no-color       Start with white density color mode
  --sid            Force SID file mode
  --audio          Force audio file mode
  --c64audio       Play SID audio on C64 hardware (experimental)
  --macaudio       Play SID audio locally via sidplayfp (default)
  --fps FPS        Waveform frame rate (default: 10)
  --save FILE.mp3  Save stream to MP3 while playing (streaming modes)
  --yt-search QUERY
                   Search YouTube by title/artist and choose a result
  --yt-max YT_MAX  Max YouTube search results (default: 10)
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

When prompted, you can choose to play audio **locally via sidplayfp** (default) or on real **C64 hardware** (see below).

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

### Streaming (YouTube / SoundCloud / Spotify)

Pass any supported URL and the mode is detected automatically. `yt-dlp` is required for all streaming.

> **Note:** Always quote URLs in the terminal — characters like `?`, `&`, and `=` are interpreted by zsh/bash as glob or special characters and will cause a "no matches found" error if unquoted.

#### YouTube

```bash
python3 sidviz_u64.py 'https://www.youtube.com/watch?v=...'
python3 sidviz_u64.py 'https://youtu.be/...'
python3 sidviz_u64.py --yt-search "artist - title"
```

Two parallel `yt-dlp` processes are opened — one piped to `ffplay` for audio, one piped to `ffmpeg` for waveform generation. `yt-dlp` handles all range requests and DASH segment management internally, so the full track plays correctly.

#### SoundCloud

```bash
python3 sidviz_u64.py 'https://soundcloud.com/artist/track'
```

Works identically to YouTube. `yt-dlp` handles SoundCloud URLs natively — same pipe architecture, no extra steps.

#### Spotify

```bash
python3 sidviz_u64.py 'https://open.spotify.com/track/...'
```

Spotify audio is DRM-protected and cannot be streamed directly. Instead:

1. `yt-dlp` extracts the track title and artist from the Spotify page
2. The best match is found on YouTube using `yt-dlp ytsearch1:"Artist - Title"`
3. The stream plays from that YouTube result

The info box and ticker show the **Spotify metadata** (correct title, artist, album). Only public tracks are supported — private or region-locked tracks will fail before the C64 reboots.

#### Metadata fetched before the C64 reboots

For all streaming modes, metadata is fetched and the URL is validated before the C64 reboots, so a bad URL or missing `yt-dlp` fails fast with a clear error.

#### Save to MP3 while playing

```bash
python3 sidviz_u64.py 'https://www.youtube.com/watch?v=...' --save output.mp3
python3 sidviz_u64.py 'https://soundcloud.com/artist/track' --save output.mp3
```

A third `yt-dlp` process downloads and converts the stream to MP3 in parallel with playback. Works for YouTube and SoundCloud. For Spotify, the saved file comes from the YouTube match.

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
| Audio / Streaming | Title · Artist · Album · Date · Genre · Duration · Bitrate · Format |

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
