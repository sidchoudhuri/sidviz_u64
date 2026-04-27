# sidviz_u64

Real-time waveform visualizer for the Commodore 64, driven by a Mac or Linux machine over the [Ultimate 64 (U64)](https://ultimate64.com) network API. Plays SID files, MP3s/audio files, and streams from YouTube, SoundCloud, and Spotify — and renders the live waveform on a real C64 screen with a scrolling metadata ticker.

```
+------------------------------------------------------+
|  sidviz_u64  v1.7.8  build 2026-04-25                |
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

`sidviz_u64.py` or the compiled executable found in [Releases](https://github.com/sidchoudhuri/sidviz_u64/releases) tab runs on Windows, Linux, or Mac. It reboots the C64 via the U64 API, uploads a small 6502 program (`sidviz.prg`), and then streams waveform frames into C64 screen RAM at up to 10 fps. A scrolling PETSCII ticker on row 1 shows the track metadata. Audio plays on the Mac or on real C64 hardware for SID files.

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
# sidplayfp and 64tass may need to be installed via AUR:
yay -S sidplayfp 64tass
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

### 1. Build the C64 program or use the included .prg file

```bash
64tass -a -B -o sidviz.prg sidviz.asm
```

This only needs to be done if the assembly file has changed.

### 2. Run the Python script or the platform-specific compiled binary

```bash
sidviz_u64-Linux [file | URL] [options]

sidviz_u64-macOS [file | URL] [options]

sidviz_u64-Windows.exe [file | URL] [options]
```
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
                  [--cookies-from-browser BROWSER] [--cookies FILE]
                  [--showwaves] [--showfreqs] [--avectorscope]
                  [--showspectrum] [--ahistogram]
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
  --cookies-from-browser BROWSER
                   Pull cookies from a browser for yt-dlp auth
                   (chrome, firefox, safari, edge, brave, chromium, …)
  --cookies FILE   Netscape-format cookies file for yt-dlp auth
  --showwaves      Visualization: scrolling waveform (default)
  --showfreqs      Visualization: frequency spectrum bars
  --avectorscope   Visualization: Lissajous vector scope
  --showspectrum   Visualization: scrolling spectrogram
  --ahistogram     Visualization: scrolling amplitude histogram
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

#### Age-restricted or sign-in required videos

Some YouTube videos require authentication (age-restricted content, or when YouTube returns a "Sign in to confirm" error). Pass cookies to `yt-dlp` using either flag:

```bash
# Pull cookies directly from an installed browser (no export needed):
python3 sidviz_u64.py 'https://www.youtube.com/watch?v=...' --cookies-from-browser chrome
python3 sidviz_u64.py --yt-search "artist title" --cookies-from-browser firefox

# Or point to a Netscape-format cookies file:
python3 sidviz_u64.py 'https://www.youtube.com/watch?v=...' --cookies ~/cookies.txt
```

Accepted browser names for `--cookies-from-browser`: `chrome`, `firefox`, `safari`, `edge`, `brave`, `chromium`, `opera`, `vivaldi`, `whale`.

When one of these flags is set, the cookies are forwarded to **every** `yt-dlp` call — metadata, search, audio stream, waveform stream, and `--save`.

If you hit the error without having set a cookie flag, sidviz prints a hint:

```
[!] yt-dlp metadata failed: ERROR: [youtube] …: Sign in to confirm your age. …
[!] Hint: re-run with --cookies-from-browser BROWSER (e.g. chrome, firefox, safari)
```

> **Note on exporting YouTube cookies:** YouTube rotates cookies on open browser tabs, so a naively exported file may be stale. For a stable cookie file: open a **private/incognito window**, log into YouTube, navigate to `https://www.youtube.com/robots.txt`, export only the `youtube.com` cookies with a browser extension (e.g. *Get cookies.txt LOCALLY* for Chrome, *cookies.txt* for Firefox), then **close the incognito window immediately**.

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
