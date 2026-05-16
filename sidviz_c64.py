#!/usr/bin/env python3
"""
sidviz_u64.py -- SID/audio waveform + live camera visualizer -> C64 via U64 API
Experimental fork: plays SID audio on real C64 hardware via PSID player.
Camera mode: streams webcam footage as PETSCII character art on the C64 screen.

version 1.9.1 (2026-05-11)

Memory protocol:
  $C000     = frame flag  (Python writes 1, ASM clears to 0)
  $C001     = color flag  (2=rainbow, 1=white density, 3=fire density)
  $C002     = sid_ready   (Python sets 1 after uploading SID code)
  $C003     = quit_flag   (Python writes 1 to stop SID and return C64 to BASIC)
  $C620/$C621 = SID play vector saved post-init (play_addr=0 SIDs)
  $C100     = frame buffer, 680 bytes PETSCII (rows 8-24, $C100-$C3A7)
  $C3A8     = white density color table, 128 bytes (screen_code → C64 color)
  $C428     = fire  density color table, 128 bytes (screen_code → C64 color)
  $C500     = ticker buffer, up to 253 PETSCII chars
  $C5FC     = color_mode  (ASM owns: 0=rainbow, 1=white, 2=fire)
  $C5FD     = irq_tick    (ASM owns)
  $C5FE     = ticker length (Python writes)
  $C5FF     = ticker read position (ASM owns)
  $C600     = JMP initAddress trampoline (Python writes)
  $C610     = JMP playAddress trampoline (Python writes)

Usage:
  1. Assemble: 64tass -a -B -o sidviz.prg sidviz.asm
  2. Run: python3 sidviz_u64.py [file]
"""

VERSION = "1.9.5"
BUILD   = "2026-05-16"

import os, sys, time, subprocess, urllib.request, urllib.parse
import argparse, threading, termios, tty, re, json, select as _select, struct, queue

# Populated in main() from --cookies-from-browser / --cookies CLI flags;
# injected into every yt-dlp subprocess call.
_YTDLP_COOKIE_ARGS: list = []

FIFO_PATH    = "/tmp/sidpipe.wav"
WIDTH        = 40
HEIGHT       = 17              # rows 8-24 — protects SID driver at $0400-$04FF
VIDEO_EXTS   = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".ts", ".wmv", ".3gp"}
FRAME_BUF    = 0xC100
FRAME_FLAG   = 0xC000
COLOR_FLAG   = 0xC001
WAVE_COL_ADDR = 0xD940   # color RAM rows 8-24 ($D940-$DBE7)
C64_AUDIO_FLAG = 0xC002  # 0=off, 1=waiting, 2=SID ready
QUIT_FLAG      = 0xC003  # Python writes 1 → C64 silences SID and JMPs to BASIC
TICKER_BUF   = 0xC500
TICKER_LEN   = 0xC5FE
TICKER_ROW   = 0x0428          # screen RAM row 1
PRG_LOCAL    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidviz.prg")
PRG_REMOTE   = "sidviz.prg"
TIMEOUT      = 5.0
# C64 colors: 0=black 1=white 2=red 7=yellow 8=orange 9=brown 10=ltred 11=dkgray 12=mdgray 15=ltgray
WHITE_CTABLE_ADDR = 0xC3A8   # 128-byte table written by Python: screen_code → C64 color
FIRE_CTABLE_ADDR  = 0xC428   # 128-byte table written by Python: screen_code → C64 color
PETSCII_BLOCK   = 16   # output pixels per char cell in saved PETSCII video (640×400 for 40×25)
_REC_BLOCK      = 8    # render at native C64 resolution (320×200); ffmpeg scales up
C64_PALETTE_RGB = [    # standard C64 VICE palette (R, G, B)
    (  0,  0,  0), (255,255,255), (136,  0,  0), (170,255,238),
    (204, 68,204), (  0,204, 85), (  0,  0,170), (238,238,119),
    (221,136, 85), (102, 68,  0), (255,119,119), ( 51, 51, 51),
    (119,119,119), (170,255,102), (  0,136,255), (187,187,187),
]
SCROLL_RATE = 6   # IRQ ticks per ticker scroll step (matches sidviz.asm)
RAINBOW_TAB = [2,2,8,8,7,7,7,7,5,5,5,5,13,13,14,14,
               6,6,6,6,4,4,4,4,10,10,2,2,8,8,7,7,
               5,5,13,13,14,14,6,6]

# ---------------------------------------------------------------------------
# C64 character ROM — load from VICE install if present, else use fallback.
# Each char is 8 bytes: MSB = leftmost pixel, rows top→bottom.
# ---------------------------------------------------------------------------

def _load_c64_chargen():
    for p in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "chargen"),
        os.path.expanduser("~/Library/Application Support/VICE/C64/chargen"),
        "/opt/homebrew/share/vice/C64/chargen",
        "/usr/local/share/vice/C64/chargen",
        "/usr/share/vice/C64/chargen",
        "/usr/lib/vice/C64/chargen",
    ]:
        try:
            d = open(p, "rb").read()
            if len(d) >= 2048:
                print(f"[*] C64 chargen ROM loaded: {p}")
                return d[:2048]
        except OSError:
            pass
    return None

_C64_CHARGEN = _load_c64_chargen()

# Hardcoded 8×8 bitmaps for chars used by sidviz (CHARS_CAMERA + ticker A-Z/0-9).
# Used when VICE chargen ROM is not installed.
_C64_CHAR_FALLBACK: dict = {
    32: b'\x00\x00\x00\x00\x00\x00\x00\x00',  # space
    33: b'\x18\x18\x18\x18\x18\x00\x18\x00',  # !
    34: b'\x66\x66\x22\x00\x00\x00\x00\x00',  # "
    35: b'\x66\x66\xff\x66\xff\x66\x66\x00',  # #
    37: b'\x62\x66\x0c\x18\x30\x66\x46\x00',  # %
    42: b'\x00\x66\x3c\xff\x3c\x66\x00\x00',  # *
    43: b'\x00\x18\x18\x7e\x18\x18\x00\x00',  # +
    44: b'\x00\x00\x00\x00\x00\x18\x18\x30',  # ,
    45: b'\x00\x00\x00\x7e\x00\x00\x00\x00',  # -
    46: b'\x00\x00\x00\x00\x00\x00\x18\x00',  # .
    47: b'\x02\x06\x0c\x18\x30\x60\x40\x00',  # /
    58: b'\x00\x18\x18\x00\x18\x18\x00\x00',  # :
    61: b'\x00\x00\x7e\x00\x7e\x00\x00\x00',  # =
    63: b'\x3c\x66\x06\x1c\x18\x00\x18\x00',  # ?
    64: b'\xff\xff\xff\xff\xff\xff\xff\xff',   # screen code 64 (dense graphic block)
   102: b'\xcc\xcc\x33\x33\xcc\xcc\x33\x33',  # screen code 102 (2×2 checker ▒)
    # A-Z (screen codes 1-26)
     1: b'\x3c\x66\x66\x7e\x66\x66\x66\x00',  # A
     2: b'\x7c\x66\x66\x7c\x66\x66\x7c\x00',  # B
     3: b'\x3c\x66\x60\x60\x60\x66\x3c\x00',  # C
     4: b'\x78\x6c\x66\x66\x66\x6c\x78\x00',  # D
     5: b'\x7e\x60\x60\x7c\x60\x60\x7e\x00',  # E
     6: b'\x7e\x60\x60\x7c\x60\x60\x60\x00',  # F
     7: b'\x3c\x66\x60\x6e\x66\x66\x3c\x00',  # G
     8: b'\x66\x66\x66\x7e\x66\x66\x66\x00',  # H
     9: b'\x3c\x18\x18\x18\x18\x18\x3c\x00',  # I
    10: b'\x1e\x0c\x0c\x0c\x0c\x6c\x38\x00',  # J
    11: b'\x66\x6c\x78\x70\x78\x6c\x66\x00',  # K
    12: b'\x60\x60\x60\x60\x60\x60\x7e\x00',  # L
    13: b'\x63\x77\x7f\x6b\x63\x63\x63\x00',  # M
    14: b'\x66\x76\x7e\x6e\x66\x66\x66\x00',  # N
    15: b'\x3c\x66\x66\x66\x66\x66\x3c\x00',  # O
    16: b'\x7c\x66\x66\x7c\x60\x60\x60\x00',  # P
    17: b'\x3c\x66\x66\x66\x6e\x3c\x0e\x00',  # Q
    18: b'\x7c\x66\x66\x7c\x6c\x66\x66\x00',  # R
    19: b'\x3c\x66\x60\x3c\x06\x66\x3c\x00',  # S
    20: b'\x7e\x18\x18\x18\x18\x18\x18\x00',  # T
    21: b'\x66\x66\x66\x66\x66\x66\x3c\x00',  # U
    22: b'\x66\x66\x66\x66\x66\x3c\x18\x00',  # V
    23: b'\x63\x63\x63\x6b\x7f\x77\x63\x00',  # W
    24: b'\x66\x66\x3c\x18\x3c\x66\x66\x00',  # X
    25: b'\x66\x66\x66\x3c\x18\x18\x18\x00',  # Y
    26: b'\x7e\x06\x0c\x18\x30\x60\x7e\x00',  # Z
    # 0-9 (screen codes 48-57)
    48: b'\x3c\x66\x6e\x76\x66\x66\x3c\x00',  # 0
    49: b'\x18\x38\x18\x18\x18\x18\x3c\x00',  # 1
    50: b'\x3c\x66\x06\x0c\x18\x30\x7e\x00',  # 2
    51: b'\x3c\x66\x06\x1c\x06\x66\x3c\x00',  # 3
    52: b'\x0c\x1c\x3c\x6c\x7e\x0c\x0c\x00',  # 4
    53: b'\x7e\x60\x7c\x06\x06\x66\x3c\x00',  # 5
    54: b'\x3c\x60\x60\x7c\x66\x66\x3c\x00',  # 6
    55: b'\x7e\x06\x0c\x18\x18\x18\x18\x00',  # 7
    56: b'\x3c\x66\x66\x3c\x66\x66\x3c\x00',  # 8
    57: b'\x3c\x66\x66\x3e\x06\x06\x3c\x00',  # 9
}
_char_cell_cache: dict = {}

#                             code       white       fire
CHARS_DEF = [               # showwaves  (least → most dense)
    (32,   0,   0),         # space      black       black
    (46,  11,   9),         # .          dark gray   brown
    (58,  12,  10),         # :          med gray    light red
    (42,  15,   8),         # *          light gray  orange
    (35,   1,   2),         # #          white       red
    (160,  1,   2),         # █ solid    white       red
]
CHARS_FREQ_DEF = [          # showfreqs  (least → most dense)
    (32,   0,   0),         # space      black       black
    (46,  11,   7),         # .          dark gray   yellow
    (58,  12,  10),         # :          med gray    light red
    (33,  15,   8),         # !          light gray  orange
    (43,   1,   9),         # +          white       brown
    (34,   1,   2),         # "          white       red
    (35,   1,   2),         # #          white       red
    (42,   1,   2),         # *          white       red
]
CHARS_SCOPE_DEF = [         # avectorscope (least → most dense)
    (32,   0,   0),         # space      black       black
    (46,  11,   7),         # .          dark gray   yellow
    (58,  12,  10),         # :          med gray    light red
    (42,  15,   8),         # *          light gray  orange
    (35,   1,   2),         # #          white       red
]
CHARS_SPECTRUM_DEF = [      # showspectrum (least → most dense)
    (32,   0,   0),         # space      black       black
    (46,  11,   9),         # .          dark gray   brown
    (58,  12,   2),         # :          med gray    red
    (33,  15,  10),         # !          light gray  light red
    (43,   1,   8),         # +          white       orange
]
CHARS_HIST_DEF = [          # ahistogram (least → most dense)
    (32,   0,   0),         # space      black       black
    (46,  11,   9),         # .          dark gray   brown
    (58,  12,  10),         # :          med gray    light red
    (42,  15,   8),         # *          light gray  orange
    (35,   1,   2),         # #          white       red
]
CHARS_CAMERA_DEF = [        # camera — 10 density levels for photo-like detail
    (32,   0,   0),         # space      black       black
    (46,  11,   9),         # .          dark gray   brown
    (45,  11,   9),         # -          dark gray   brown
    (58,  12,  10),         # :          med gray    light red
    (43,  15,   8),         # +          light gray  orange
    (61,  15,   8),         # =          light gray  orange
    (42,   1,   2),         # *          white       red
    (37,   1,   2),         # %          white       red
    (35,   1,   2),         # #          white       red
    (160,  1,   2),         # █ solid    white       red
]
CHARS      = [t[0] for t in CHARS_DEF]
CHARS_FREQ = [t[0] for t in CHARS_FREQ_DEF]
CHARS_SCOPE = [t[0] for t in CHARS_SCOPE_DEF]
CHARS_SPECTRUM = [t[0] for t in CHARS_SPECTRUM_DEF]
CHARS_HIST = [t[0] for t in CHARS_HIST_DEF]
CHARS_CAMERA = [t[0] for t in CHARS_CAMERA_DEF]
SID_EXTS     = {".sid"}
U64          = ""
FPS          = 10
VIZ_MODE     = "showwaves"
VIZ_ORDER    = ["none", "showwaves", "showfreqs", "avectorscope", "showspectrum", "ahistogram"]
VIZ_LABELS   = {
    "none":         "off",
    "showwaves":    "waveform",
    "showfreqs":    "spectrum",
    "avectorscope": "scope",
    "showspectrum": "spectrogram",
    "ahistogram":   "histogram",
}

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        prog="sidviz_u64",
        description=f"SID/audio waveform visualizer for C64 via U64 API  v{VERSION} build {BUILD}"
    )
    p.add_argument("file",       nargs="?",              help="Audio/SID file")
    p.add_argument("--ip",       default="192.168.2.64", help="U64 IP address")
    p.add_argument("--color",    action="store_true",    help="Start with rainbow color")
    p.add_argument("--no-color", action="store_true",    help="Start with flat white")
    p.add_argument("--sid",      action="store_true",    help="Force sidplayfp mode")
    p.add_argument("--audio",    action="store_true",    help="Force ffmpeg audio mode")
    p.add_argument("--c64audio", action="store_true",    help="Play SID audio on C64 hardware")
    p.add_argument("--macaudio", action="store_true",    help="Play SID audio locally via sidplayfp (default)")
    p.add_argument("--fps",      type=int, default=10,   help="Frame rate (default 10)")
    p.add_argument("--save",     metavar="FILE.mp3",     help="Save YouTube stream to MP3 (YouTube mode only)")
    p.add_argument("--save-petscii", metavar="FILE.mp4",
                   help="Save the PETSCII rendering itself as an MP4 video (camera and video modes)")
    p.add_argument("--yt-search", metavar="QUERY",        help="Search YouTube by title/artist and choose a result")
    p.add_argument("--yt-max",   type=int, default=10,    help="Max YouTube search results (default 10)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="Browser to pull cookies from for yt-dlp auth (chrome, firefox, safari, edge, …)")
    p.add_argument("--cookies",  metavar="FILE",
                   help="Netscape-format cookies file for yt-dlp auth")
    p.add_argument("--showwaves",     action="store_true", help="Force waveform visualization")
    p.add_argument("--showfreqs",     action="store_true", help="Force frequency spectrum visualization")
    p.add_argument("--avectorscope",  action="store_true", help="Force vectorscope (oscilloscope) visualization")
    p.add_argument("--showspectrum",  action="store_true", help="Force scrolling spectrogram visualization")
    p.add_argument("--ahistogram",    action="store_true", help="Force amplitude histogram visualization")
    p.add_argument("--video",         action="store_true", help="Video file/URL mode: display as PETSCII art on C64")
    p.add_argument("--camera",        action="store_true", help="Live camera mode: stream webcam as PETSCII art on C64")
    p.add_argument("--camera-device", default="0",         metavar="DEV",
                   help="Camera device: index (0,1,…) or path (/dev/video0). Default: 0")
    p.add_argument("--list-cameras",  action="store_true", help="List available camera devices and exit")
    p.add_argument("--version",  action="store_true",    help="Show version and exit")
    p.add_argument("--save-d64",     metavar="FILE.d64",
                   help="Save SID + pre-rendered frames to a bootable D64 disk image")
    p.add_argument("--d64-fps",      type=int, default=10,
                   help="Frame rate for D64 recording (default 10; 50 must be divisible)")
    p.add_argument("--d64-duration", type=int, default=None, metavar="SECS",
                   help="Capture duration in seconds for D64 (default: auto from SID length)")
    p.add_argument("--d64-viz",      default=None,
                   choices=["showwaves","showfreqs","avectorscope","showspectrum","ahistogram"],
                   help="Visualization type for D64 frames (prompt if omitted)")
    p.add_argument("--d64-color",    default=None, type=int, choices=[0,1,2],
                   help="Color mode for D64 frames: 0=rainbow 1=white 2=fire (prompt if omitted)")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def ascii_to_petscii(s):
    """Convert ASCII string to C64 PETSCII screen codes (uppercase)."""
    result = []
    for ch in s.upper():
        c = ord(ch)
        if 64 <= c <= 95:
            result.append(c - 64)
        elif 32 <= c <= 63:
            result.append(c)
        else:
            result.append(32)
    return result

def get_sid_info(filepath):
    """Run sidplayfp -v, read header, kill immediately, parse metadata."""
    # sidplayfp opens /dev/tty directly and sets raw mode for its interactive UI.
    # SIGKILL skips its cleanup handlers, leaving the terminal in raw mode.
    # Save and restore settings so subsequent input() prompts work normally.
    _tty_fd, _tty_saved = None, None
    try:
        _tty_fd = sys.stdin.fileno()
        _tty_saved = termios.tcgetattr(_tty_fd)
    except Exception:
        pass
    try:
        proc = subprocess.Popen(
            ["sidplayfp", "-v", filepath],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        output = ""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            r, _, _ = _select.select([proc.stdout, proc.stderr], [], [], 0.1)
            for fd in r:
                chunk = fd.read(512)
                if chunk:
                    output += chunk.decode(errors="replace")
            if "Song Length" in output:
                break
        proc.kill()
        proc.wait()
    except Exception as e:
        print(f"[!] sidplayfp -v failed: {e}")
        return {}
    finally:
        if _tty_fd is not None and _tty_saved is not None:
            try:
                termios.tcsetattr(_tty_fd, termios.TCSADRAIN, _tty_saved)
            except Exception:
                pass

    info = {}
    fields = ["Title", "Author", "Released", "File format",
              "Song Speed", "Song Length", "Addresses", "Condition"]
    for field in fields:
        pattern = rf"\|\s*{re.escape(field)}\s*:\s*(.+?)(?:\s*\|)?\s*$"
        for line in output.splitlines():
            m = re.search(pattern, line)
            if m:
                info[field] = m.group(1).strip()
                break
    return info

def get_audio_info(filepath):
    """Use ffprobe to extract metadata from audio files."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", filepath
        ], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout)
        fmt  = data.get("format", {})
        tags = fmt.get("tags", {})
        info = {}
        tag_map = {"title": "Title", "artist": "Artist", "album": "Album",
                   "date": "Date", "genre": "Genre"}
        for k, v in tags.items():
            mapped = tag_map.get(k.lower())
            if mapped:
                info[mapped] = v
        dur = float(fmt.get("duration", 0))
        if dur:
            m, s = divmod(int(dur), 60)
            info["Duration"] = f"{m}:{s:02d}"
        br = int(fmt.get("bit_rate", 0))
        if br:
            info["Bitrate"] = f"{br // 1000} kbps"
        fname = fmt.get("format_long_name", "")
        if fname:
            info["Format"] = fname
        return info
    except Exception:
        return {}

def _spotify_info(url):
    """Fetch Spotify track metadata: page og-tags → oEmbed fallback.

    og:description contains 'Artist · Song · Year · duration', giving us
    the artist name that oEmbed alone does not provide.
    """
    info = {"Format": "Spotify", "filename": url}

    def _unescape(s):
        return (s.replace("&amp;", "&").replace("&quot;", '"')
                 .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))

    # 1. Scrape the track page — og:description includes the artist name
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="replace")

        m = re.search(r'property="og:title"\s+content="([^"]*)"', page)
        if m:
            info["Title"] = _unescape(m.group(1))

        m = re.search(r'property="og:description"\s+content="([^"]*)"', page)
        if m:
            desc = _unescape(m.group(1))
            # "Listen to Track on Spotify. Artist · Song · Year · N min M sec"
            desc = re.sub(r"(?i)listen to .+? on spotify\.\s*", "", desc)
            parts = [p.strip() for p in desc.split("·")]
            if parts and parts[0]:
                info["Artist"] = parts[0]

        if info.get("Title"):
            return info
    except Exception as e:
        print(f"[!] Spotify page fetch failed: {e}")

    # 2. oEmbed fallback — title only (sometimes "Track · Artist")
    try:
        oembed_url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url)
        with urllib.request.urlopen(oembed_url, timeout=10) as resp:
            data = json.loads(resp.read())
        title = data.get("title", "")
        if "·" in title:
            parts = title.split("·", 1)
            info["Title"]  = parts[0].strip()
            info.setdefault("Artist", parts[1].strip())
        elif title:
            info["Title"] = title
        if info.get("Title"):
            return info
    except Exception as e:
        print(f"[!] Spotify oEmbed fallback failed: {e}")

    return None

def _is_cookie_error(text):
    return "cookies-from-browser" in text or "Sign in to confirm" in text

def get_stream_info(url):
    """Use yt-dlp to extract metadata from any supported streaming URL.
    For Spotify, falls back to the public oEmbed API if yt-dlp fails."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist"] + _YTDLP_COOKIE_ARGS + [url],
            capture_output=True, text=True, timeout=30
        )
        if not r.stdout.strip():
            raise ValueError(r.stderr.strip() or "no output from yt-dlp")
        data = json.loads(r.stdout)
    except Exception as e:
        print(f"[!] yt-dlp metadata failed: {e}")
        if not _YTDLP_COOKIE_ARGS and _is_cookie_error(str(e)):
            print("[!] Hint: re-run with --cookies-from-browser BROWSER (e.g. chrome, firefox, safari)")
        if get_service(url) == "spotify":
            print("[*] Trying Spotify metadata fallback...")
            return _spotify_info(url)
        return None
    info = {}
    if data.get("title"):   info["Title"] = data["title"]
    # Artist: field name varies by service
    artist = (data.get("artist") or
              (", ".join(data["artists"]) if data.get("artists") else None) or
              data.get("uploader") or data.get("creator") or "")
    if artist:              info["Artist"] = artist
    if data.get("album"):   info["Album"]  = data["album"]
    if data.get("upload_date"):
        d = data["upload_date"]
        info["Date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    dur = data.get("duration")
    if dur:
        m, s = divmod(int(dur), 60)
        info["Duration"] = f"{m}:{s:02d}"
    service_labels = {"youtube": "YouTube", "soundcloud": "SoundCloud", "spotify": "Spotify"}
    info["Format"]   = service_labels.get(get_service(url), "Stream")
    info["filename"] = url
    return info

def resolve_stream_url(url, info):
    """For Spotify URLs: find the best YouTube match and return that URL.
    For all other services: return the URL unchanged."""
    if get_service(url) != "spotify":
        return url
    title  = info.get("Title", "")
    artist = info.get("Artist", "")
    query  = f"{artist} - {title}".strip(" -") if artist else title
    if not query:
        print("[!] Spotify: no metadata to search with")
        return None
    print(f"[*] Spotify: searching YouTube for: {query}")
    try:
        r = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist"] + _YTDLP_COOKIE_ARGS + [f"ytsearch1:{query}"],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(r.stdout)
        yt_url = data.get("webpage_url") or data.get("url")
        if not yt_url:
            raise ValueError("no URL in search result")
        print(f"[*] Spotify: matched '{data.get('title', 'unknown')}'")
        return yt_url
    except Exception as e:
        print(f"[!] Spotify YouTube search failed: {e}")
        return None

def youtube_search(query, max_results=10):
    """Search YouTube via yt-dlp and return a list of candidate videos."""
    max_results = max(1, int(max_results or 10))
    try:
        r = subprocess.run(
            ["yt-dlp", "--dump-json", "--flat-playlist", "--no-playlist"]
            + _YTDLP_COOKIE_ARGS + [f"ytsearch{max_results}:{query}"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0 or not r.stdout.strip():
            raise ValueError(r.stderr.strip() or "no output from yt-dlp")
    except Exception as e:
        print(f"[!] YouTube search failed: {e}")
        return []

    results = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = data.get("id", "")
        url = (data.get("webpage_url") or data.get("url") or
               (f"https://www.youtube.com/watch?v={vid}" if vid else ""))
        if not url:
            continue
        dur = data.get("duration")
        if isinstance(dur, (int, float)):
            m, s = divmod(int(dur), 60)
            dur_s = f"{m}:{s:02d}"
        else:
            dur_s = "?:??"
        results.append({
            "title": data.get("title", "Untitled"),
            "uploader": data.get("uploader", "Unknown"),
            "duration": dur_s,
            "url": url,
        })
    return results

def choose_youtube_result(results):
    """Prompt for selection from yt search results and return chosen URL."""
    if not results:
        return None
    print("\n[*] YouTube search results:")
    for i, item in enumerate(results, start=1):
        print(f"    {i:2d}. {item['title']}  [{item['duration']}]  -  {item['uploader']}")
    while True:
        ans = input(f"Select video [1-{len(results)}] (default 1, q=cancel): ").strip().lower()
        if ans in ("", "1"):
            return results[0]["url"]
        if ans in ("q", "quit", "n", "no"):
            return None
        if ans.isdigit():
            idx = int(ans)
            if 1 <= idx <= len(results):
                return results[idx - 1]["url"]
        print("[!] Invalid selection.")

def start_ffmpeg_waveform_stream(url, height=HEIGHT):
    """Stream audio via yt-dlp piped to ffmpeg for waveform generation."""
    yt_proc = subprocess.Popen(
        ["yt-dlp", "-f", "bestaudio", "-o", "-", "-q", "--no-playlist"] + _YTDLP_COOKIE_ARGS + [url],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    # -re: read at 1x speed — yt-dlp delivers compressed audio faster than
    # real-time; without -re ffmpeg races through the whole stream in seconds,
    # generating all waveform frames at once before the song even starts.
    cmd = ["ffmpeg", "-loglevel", "quiet", "-re", "-i", "pipe:0",
           "-filter_complex", _build_viz_filter(height),
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdin=yt_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    yt_proc.stdout.close()  # let ffmpeg own the pipe; yt_proc gets SIGPIPE if ffmpeg exits early
    print("[*] ffmpeg waveform (stream) started.")
    return yt_proc, p

def start_ffplay_stream(url):
    """Stream audio via yt-dlp piped to ffplay."""
    yt_proc = subprocess.Popen(
        ["yt-dlp", "-f", "bestaudio", "-o", "-", "-q", "--no-playlist"] + _YTDLP_COOKIE_ARGS + [url],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    p = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-i", "pipe:0"],
        stdin=yt_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    yt_proc.stdout.close()
    print("[*] ffplay audio (stream) started.")
    return yt_proc, p

def build_ticker_string(info, mode):
    """Build scrolling ticker — values only, no labels, separated by *."""
    if mode == "sid":
        order = ["Title", "Author", "Released", "Song Speed",
                 "Song Length", "File format", "Addresses"]
    else:
        order = ["Title", "Artist", "Album", "Date",
                 "Genre", "Duration", "Bitrate", "Format"]

    parts = [info[k] for k in order if k in info]
    if not parts:
        parts = [os.path.basename(info.get("filename", "UNKNOWN"))]

    ticker = "   *   ".join(parts) + "        "
    if len(ticker) > 253:
        ticker = ticker[:253]
    return ticker

def show_info_header(info, mode, filepath):
    width = 54
    print("+" + "-" * width + "+")
    print(f"|  sidviz_u64  v{VERSION}  build {BUILD}".ljust(width + 1) + "|")
    print("+" + "-" * width + "+")
    if is_url(filepath):
        label = filepath if len(filepath) <= 47 else filepath[:44] + "..."
    else:
        label = os.path.basename(filepath)
    print(f"|  File: {label}".ljust(width + 1) + "|")
    if mode == "sid":
        show_fields = [("Title", "Title"), ("Author", "Author"),
                       ("Released", "Released"), ("Song Speed", "Speed"),
                       ("Song Length", "Length"), ("File format", "Format"),
                       ("Addresses", "Addresses")]
    else:
        show_fields = [("Title", "Title"), ("Artist", "Artist"),
                       ("Album", "Album"), ("Date", "Date"),
                       ("Genre", "Genre"), ("Duration", "Duration"),
                       ("Bitrate", "Bitrate"), ("Format", "Format")]
    for key, label in show_fields:
        if key in info:
            val = info[key]
            max_val = width - 15  # 39 chars: box width minus |  label(12) space prefix
            if len(val) > max_val:
                val = val[:max_val - 3] + "..."
            print(f"|  {label:<12} {val}".ljust(width + 1) + "|")
    print("+" + "-" * width + "+")
    print()

# ---------------------------------------------------------------------------
# U64 API
# ---------------------------------------------------------------------------

def u64_get(path):
    try:
        with urllib.request.urlopen(f"{U64}/v1/{path}", timeout=TIMEOUT) as r:
            return r.read()
    except Exception as e:
        print(f"[!] GET {path} failed: {e}"); return None

def u64_put(path, params=None):
    qs  = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{U64}/v1/{path}{qs}"
    req = urllib.request.Request(url, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except Exception as e:
        print(f"[!] PUT {path} failed: {e}"); return None

def write_mem(addr, data):
    data = bytes(data)
    for i in range(0, len(data), 128):
        chunk    = data[i:i + 128]
        data_hex = "".join(f"{b:02X}" for b in chunk)
        u64_put("machine:writemem", {"address": f"{addr + i:X}", "data": data_hex})

def write_byte(addr, val):
    u64_put("machine:writemem", {"address": f"{addr:X}", "data": f"{val:02X}"})

def write_color_tables():
    white = [0] * 128
    fire  = [0] * 128
    if VIZ_MODE == "showwaves":
        defs = CHARS_DEF
    elif VIZ_MODE == "showfreqs":
        defs = CHARS_FREQ_DEF
    elif VIZ_MODE == "avectorscope":
        defs = CHARS_SCOPE_DEF
    elif VIZ_MODE == "showspectrum":
        defs = CHARS_SPECTRUM_DEF
    elif VIZ_MODE == "camera":
        defs = CHARS_CAMERA_DEF
    else:
        defs = CHARS_HIST_DEF
    for code, wcol, fcol in defs:
        white[code] = wcol
        fire[code]  = fcol
    write_mem(WHITE_CTABLE_ADDR, white)
    write_mem(FIRE_CTABLE_ADDR,  fire)

def ftp_upload(local_path, remote_name):
    ip  = U64.replace("http://", "")
    url = f"ftp://{ip}/Temp/{remote_name}"
    r   = subprocess.run(
        ["curl", "-s", "--ftp-port", "-", "-T", local_path, url],
        capture_output=True, text=True)
    if r.returncode not in (0, 8):
        print(f"[!] FTP failed (code {r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
        return False
    return True

def run_prg_from_temp(name):
    return u64_put("runners:run_prg", {"file": f"Temp/{name}"}) == 200

def smoke_test():
    print("[*] Smoke test...")
    r = u64_get("info")
    if r:
        print(f"[*] U64 OK: {r.decode(errors='replace').strip()}"); return True
    print("[!] U64 not responding."); return False

def send_ticker(ticker_str):
    petscii = ascii_to_petscii(ticker_str)
    length  = len(petscii)
    print(f"[*] Ticker: {length} chars")
    write_mem(TICKER_BUF, petscii)
    write_byte(TICKER_LEN, length)

# ---------------------------------------------------------------------------
# Audio mode detection
# ---------------------------------------------------------------------------

def is_url(s):
    return s.startswith(("http://", "https://"))

def get_service(url):
    if "spotify.com"   in url: return "spotify"
    if "soundcloud.com" in url: return "soundcloud"
    if "youtube.com"   in url or "youtu.be" in url: return "youtube"
    return "stream"

def detect_mode(filepath, force_sid=False, force_audio=False):
    if is_url(filepath):
        print(f"[*] Detected mode: stream ({get_service(filepath)})")
        return "stream"
    ext = os.path.splitext(filepath)[1].lower()
    if force_sid:   return "sid"
    if force_audio: return "audio"
    detected = "sid" if ext in SID_EXTS else "audio"
    print(f"[*] Detected mode: {detected} (extension: {ext})")
    ans = input(f"    Use {detected} mode? [Y/n]: ").strip().lower()
    if ans in ("n", "no"):
        detected = "audio" if detected == "sid" else "sid"
        print(f"[*] Switched to: {detected}")
    return detected

# ---------------------------------------------------------------------------
# FIFO + processes
# ---------------------------------------------------------------------------

def make_fifo(path):
    if os.path.exists(path): os.remove(path)
    os.mkfifo(path)
    print(f"[*] FIFO created: {path}")

def _build_viz_filter(height=HEIGHT):
    if VIZ_MODE == "showfreqs":
        return (f"[0:a]showfreqs=s={WIDTH}x{height}:mode=bar"
                f":ascale=log:fscale=log:colors=#ffffff,format=gray")
    if VIZ_MODE == "avectorscope":
        # avectorscope lissajous mode: X=L, Y=R.  SID is mono so L=R → diagonal.
        # Use aformat to reliably mix to mono, split into two copies, delay one
        # by 8ms, amerge as stereo → L≠R → ellipses that change shape with pitch.
        filt = (f"[0:a]aformat=channel_layouts=mono,asplit=2[La][Ra];"
                f"[Ra]adelay=2[Rd];"
                f"[La][Rd]amerge=inputs=2[S];"
                f"[S]avectorscope=s={WIDTH}x{height}:zoom=1.8:draw=dot:scale=log"
                f",format=gray")
        print(f"[*] avectorscope filter: {filt}")
        return filt
    if VIZ_MODE == "showspectrum":
        return (f"[0:a]showspectrum=s={WIDTH}x{height}:slide=scroll"
                f":scale=log:color=intensity,format=gray")
    if VIZ_MODE == "ahistogram":
        return (f"[0:a]ahistogram=s={WIDTH}x{height}:scale=log:slide=scroll"
                f",format=gray")
    return (f"[0:a]showwaves=s={WIDTH}x{height}:mode=cline"
            f":rate={FPS}:colors=#ffffff,format=gray")

def start_ffmpeg_waveform_fifo(realtime=False, height=HEIGHT):
    # -re: read at 1x speed so sidplayfp can't race ahead of C64 real-time playback
    re_flag = ["-re"] if realtime else []
    cmd = ["ffmpeg", "-loglevel", "quiet"] + re_flag + ["-f", "wav", "-i", FIFO_PATH,
           "-filter_complex", _build_viz_filter(height),
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print("[*] ffmpeg waveform (FIFO) started."); return p

def start_ffmpeg_waveform_file(filepath, height=HEIGHT, seek_secs=0.0):
    # -re: read at 1x (native) speed so viz stays synchronized with real-time
    # audio playback.  Without it ffmpeg races through the whole file in seconds,
    # last_viz_frame ends up at the end-of-song (silent) frame, then ffmpeg exits
    # and the blend drops to camera-only.
    seek_flags = ["-ss", str(int(seek_secs))] if seek_secs > 0.5 else []
    cmd = ["ffmpeg", "-loglevel", "quiet", "-re"] + seek_flags + [
           "-i", filepath,
           "-filter_complex", _build_viz_filter(height),
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print("[*] ffmpeg waveform (file) started."); return p

def start_ffmpeg_camera(device="0", height=HEIGHT):
    """Capture live camera frames, scale to C64 screen size, output as raw gray pixels."""
    if sys.platform == "darwin":
        # macOS: AVFoundation. Must specify -framerate 30 explicitly:
        # without it ffmpeg auto-selects 29.97 (NTSC drop-frame) which AVFoundation
        # rejects — cameras advertise integer rates like 15/30/60, not 29.97.
        # Output -r downsamples from 30 to our target FPS.
        input_flags = ["-f", "avfoundation", "-framerate", "30", "-i", str(device)]
    else:
        # Linux: v4l2 — accept bare index ("0") or full path ("/dev/video0").
        dev = device if device.startswith("/") else f"/dev/video{device}"
        input_flags = ["-f", "v4l2", "-i", dev]
    # min(iw, ih*W/H) × min(ih, iw*H/W): correct crop regardless of source AR.
    # Old formula crop=iw:iw*H/W fails when H/W > source AR (e.g. 40:23 with 16:9
    # source: 1920*23/40=1104 > 1080).  min() picks the axis that needs cropping.
    vf = (f"crop=min(iw\\,ih*{WIDTH}/{height}):min(ih\\,iw*{height}/{WIDTH}),"
          f"scale={WIDTH}:{height},"
          f"eq=contrast=1.3")
    cmd = (["ffmpeg", "-loglevel", "error"] + input_flags +
           ["-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"])
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    # Give ffmpeg a moment to open the device; if it exits immediately the camera failed.
    time.sleep(0.5)
    if p.poll() is not None:
        err = p.stderr.read().decode(errors="replace").strip()
        print(f"[!] ffmpeg camera failed to open device '{device}'")
        if err:
            print(f"[!] ffmpeg: {err}")
        return None
    print(f"[*] ffmpeg camera started (device: {device})")
    return p

def start_ffmpeg_video_frames(source, height=HEIGHT):
    """Extract grayscale video frames from a file at target FPS."""
    vf = (f"fps={FPS},scale={WIDTH}:{height}:flags=lanczos,"
          f"eq=contrast=1.3,format=gray")
    # -re: pace output to real-time so the pipe doesn't overflow and ffmpeg
    # doesn't race through the whole file before the main loop can consume frames.
    cmd = ["ffmpeg", "-loglevel", "quiet", "-re", "-i", source,
           "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(f"[*] ffmpeg video frames started: {os.path.basename(source)}")
    return p

def start_yt_video_frames(url, height=HEIGHT):
    """Stream YouTube/URL video frames via yt-dlp piped to ffmpeg."""
    yt_proc = subprocess.Popen(
        ["yt-dlp", "-f", "bestvideo[height<=480]/bestvideo", "-o", "-",
         "-q", "--no-playlist"] + _YTDLP_COOKIE_ARGS + [url],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    vf = (f"fps={FPS},scale={WIDTH}:{height}:flags=lanczos,"
          f"eq=contrast=1.3,format=gray")
    # -re: read input at native speed — yt-dlp delivers compressed video faster
    # than real-time; without it ffmpeg races through all frames at once.
    cmd = ["ffmpeg", "-loglevel", "quiet", "-re", "-i", "pipe:0",
           "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    p = subprocess.Popen(cmd, stdin=yt_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    yt_proc.stdout.close()
    print("[*] yt-dlp + ffmpeg video frames started.")
    return yt_proc, p

def start_ffplay_audio(filepath):
    p = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", filepath],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] ffplay audio started."); return p

def start_ffplay_video_audio(filepath):
    """Play audio track from a video file without showing a video window."""
    p = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", filepath],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] ffplay video audio started."); return p

def start_sidplayfp_fifo(filepath, duration_secs=None):
    cmd = ["sidplayfp"]
    if duration_secs:
        cmd += [f"-t{duration_secs}"]
    cmd += [f"-w{FIFO_PATH}", filepath]
    print(f"[*] sidplayfp FIFO cmd: {' '.join(cmd)}")
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] sidplayfp -> FIFO started."); return p

def start_sidplayfp_audio(filepath, duration_secs=None):
    cmd = ["sidplayfp"]
    if duration_secs:
        cmd += [f"-t{duration_secs}"]
    cmd += [filepath]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] sidplayfp -> audio started."); return p

# ---------------------------------------------------------------------------
# PSID parser and C64 audio uploader (EXP)
# ---------------------------------------------------------------------------

def _psid_row_overlap(filepath):
    """Return a warning string if the PSID binary overlaps screen RAM rows 2-7 ($0450-$053F), else None."""
    try:
        with open(filepath, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if raw[0:4] not in (b"PSID", b"RSID"):
        return None
    data_offset = struct.unpack_from(">H", raw, 6)[0]
    load_addr   = struct.unpack_from(">H", raw, 8)[0]
    sid_data    = raw[data_offset:]
    if load_addr == 0:
        if len(sid_data) < 2:
            return None
        load_addr = struct.unpack_from("<H", sid_data, 0)[0]
        sid_data  = sid_data[2:]
    end_addr = load_addr + len(sid_data) - 1
    # Screen RAM rows 2-7 = $0450-$053F
    if load_addr <= 0x053F and end_addr >= 0x0450:
        return (f"${load_addr:04X}–${end_addr:04X} overlaps screen RAM rows 2–7 ($0450–$053F)")
    return None


def parse_psid(filepath):
    """Parse PSID/RSID header, return dict with load/init/play addresses and data."""
    with open(filepath, "rb") as f:
        raw = f.read()

    magic = raw[0:4]
    if magic not in (b"PSID", b"RSID"):
        print(f"[!] Not a PSID/RSID file (magic: {magic})")
        return None

    version     = struct.unpack_from(">H", raw, 4)[0]
    data_offset = struct.unpack_from(">H", raw, 6)[0]
    load_addr   = struct.unpack_from(">H", raw, 8)[0]
    init_addr   = struct.unpack_from(">H", raw, 10)[0]
    play_addr   = struct.unpack_from(">H", raw, 12)[0]
    sid_data    = raw[data_offset:]

    # v2+ flags word at 0x76, bits 0-1: clock (0=unknown, 1=PAL, 2=NTSC, 3=both)
    clock = 0
    if version >= 2 and len(raw) > 0x77:
        clock = struct.unpack_from(">H", raw, 0x76)[0] & 0x03

    # Read title from header to confirm we're parsing the right file
    title = raw[0x16:0x36].rstrip(b"\x00").decode(errors="replace")
    author = raw[0x36:0x56].rstrip(b"\x00").decode(errors="replace")
    print(f"[*] PSID title: {title!r}  author: {author!r}")

    # If load_addr is 0, first 2 bytes of data are the load address (little-endian)
    if load_addr == 0:
        load_addr = struct.unpack_from("<H", sid_data, 0)[0]
        sid_data  = sid_data[2:]

    clock_str = {0: "unknown", 1: "PAL", 2: "NTSC", 3: "PAL+NTSC"}.get(clock, "?")
    print(f"[*] PSID v{version}: load=${load_addr:04X} init=${init_addr:04X} play=${play_addr:04X} "
          f"size={len(sid_data)} bytes  clock={clock_str}")

    if play_addr == 0:
        print(f"[*] play_addr=0 — SID installs play via IRQ vector during init")
        print(f"[*] ASM will save post-init $0314 to $C620; Python reads $C620 for play address")

    if load_addr <= 0x07E7 and (load_addr + len(sid_data)) >= 0x0400:
        print(f"[!] PSID driver overlaps screen RAM ($0400-$07E7) — rows 0-7 may show driver artifacts")

    return {"load_addr": load_addr, "init_addr": init_addr,
            "play_addr": play_addr, "data": sid_data, "clock": clock}

def upload_sid_to_c64(psid):
    """Upload PSID code to C64 RAM, write trampolines, signal PRG to proceed."""
    load_addr = psid["load_addr"]
    init_addr = psid["init_addr"]
    play_addr = psid["play_addr"]
    data      = psid["data"]

    print(f"[*] Uploading SID code ({len(data)} bytes) to ${load_addr:04X}...")
    write_mem(load_addr, data)

    # JMP initAddress trampoline at $C600 ($4C = JMP absolute opcode)
    print(f"[*] Writing init trampoline at $C600 -> ${init_addr:04X}...")
    write_mem(0xC600, [0x4C, init_addr & 0xFF, (init_addr >> 8) & 0xFF])

    if play_addr != 0:
        # JMP playAddress trampoline at $C610
        print(f"[*] Writing play trampoline at $C610 -> ${play_addr:04X}...")
        write_mem(0xC610, [0x4C, play_addr & 0xFF, (play_addr >> 8) & 0xFF])
        # Signal PRG: $02 = SID ready, call init then play via $C610
        write_byte(C64_AUDIO_FLAG, 2)
        print(f"[*] SID uploaded and patched — signalling PRG ($C002=2).")
    else:
        # play_addr == 0: SID installs its play address into $0314/$0315 during init
        # Write RTS at $C610 as placeholder — safe no-op until we patch the real address
        write_mem(0xC610, [0x60])  # RTS placeholder
        # Signal PRG: $02 = SID ready, call init
        write_byte(C64_AUDIO_FLAG, 2)
        print(f"[*] SID uploaded — signalling PRG to run init (play_addr=0, $C002=2)...")
        # Give PRG time to call init; ASM saves SID's post-init $0314/$0315
        # to $C620/$C621 before installing irq_handler, so we read the real
        # SID play address rather than irq_handler's address.
        time.sleep(0.3)
        vec = u64_get("machine:readmem?address=C620&length=2")
        if vec and len(vec) == 2:
            real_play = vec[0] | (vec[1] << 8)
            print(f"[*] SID installed play address: ${real_play:04X} — patching $C610...")
            write_mem(0xC610, [0x4C, real_play & 0xFF, (real_play >> 8) & 0xFF])
        else:
            print(f"[!] Could not read $C620 — play may not work")

# ---------------------------------------------------------------------------
# Keypress toggle
# ---------------------------------------------------------------------------

def make_keypress_listener(state):
    def _listen():
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        state["_term_fd"]  = fd
        state["_term_old"] = old
        try:
            tty.setraw(fd)
            while not state["quit"]:
                ch = sys.stdin.read(1)
                if not ch: break
                if ch in ("c", "C"):
                    state["cam_color"]     = (state["cam_color"] + 1) % 3
                    state["color_pending"] = True
                    label = ["rainbow", "white", "fire"][state["cam_color"]]
                    sys.stdout.write(f"\r\n[*] Camera color -> {label}\r\n")
                    sys.stdout.flush()
                elif ch in ("v", "V"):
                    state["viz_color"]     = (state["viz_color"] + 1) % 3
                    label = ["rainbow", "white", "fire"][state["viz_color"]]
                    sys.stdout.write(f"\r\n[*] Viz color -> {label}\r\n")
                    sys.stdout.flush()
                elif ch in ("w", "W"):
                    cur = VIZ_MODE
                    idx = VIZ_ORDER.index(cur) if cur in VIZ_ORDER else 0
                    new_mode = VIZ_ORDER[(idx + 1) % len(VIZ_ORDER)]
                    state["viz_pending"] = new_mode
                    sys.stdout.write(f"\r\n[*] Viz -> {VIZ_LABELS.get(new_mode, new_mode)}\r\n")
                    sys.stdout.flush()
                elif ch == "0":
                    state["viz_pending"] = "none"
                    sys.stdout.write(f"\r\n[*] Viz -> off\r\n")
                    sys.stdout.flush()
                elif ch in ("1", "2", "3", "4", "5"):
                    new_mode = VIZ_ORDER[int(ch)]  # VIZ_ORDER[0]="none", so 1→showwaves, etc.
                    state["viz_pending"] = new_mode
                    sys.stdout.write(f"\r\n[*] Viz -> {VIZ_LABELS.get(new_mode, new_mode)}\r\n")
                    sys.stdout.flush()
                elif ch in ("q", "Q", "\x03"):
                    state["quit"] = True
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    t = threading.Thread(target=_listen, daemon=True)
    return t

# ---------------------------------------------------------------------------
# Frame conversion
# ---------------------------------------------------------------------------

def pixel_to_char(val, chars=CHARS):
    return chars[val * (len(chars) - 1) // 255]

def _build_color_luts(chars_def):
    return (
        {code: wcol for code, wcol, fcol in chars_def},
        {code: fcol for code, wcol, fcol in chars_def},
    )

def _pixel_color(pixel, chars, color_mode, col_x, white_lut, fire_lut, rainbow_tab):
    if color_mode == 0:
        return rainbow_tab[col_x]
    char_code = pixel_to_char(pixel, chars)
    if color_mode == 1:
        return white_lut.get(char_code, 1)
    return fire_lut.get(char_code, 8)

def start_petscii_recorder(filepath, num_cols, num_rows, block=PETSCII_BLOCK,
                           audio_source=None, audio_fd=None):
    """Start ffmpeg PETSCII video recorder (all 25 C64 rows = 640×400).
    Frames are piped at _REC_BLOCK (8px/cell = 320×200) and scaled up to
    block×block (16px/cell = 640×400) inside ffmpeg — 4× less pipe data.
    audio_source: local file path.  audio_fd: open fd from a yt-dlp pipe."""
    rw, rh = num_cols * _REC_BLOCK, num_rows * _REC_BLOCK   # pipe input size
    out_w, out_h = num_cols * block, num_rows * block        # output file size
    cmd = ["ffmpeg", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{rw}x{rh}", "-r", str(FPS),
           "-i", "pipe:0"]
    pass_fds = ()
    if audio_source:
        cmd += ["-i", audio_source]
    elif audio_fd is not None:
        cmd += ["-i", f"/dev/fd/{audio_fd}"]
        pass_fds = (audio_fd,)
    has_audio = bool(audio_source) or (audio_fd is not None)
    vf = f"scale={out_w}:{out_h}:flags=neighbor"
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "23", "-preset", "fast", "-tune", "animation",
            "-r", str(FPS), "-vf", vf]
    if has_audio:
        cmd += ["-c:a", "aac", "-map", "0:v", "-map", "1:a"]
    cmd += [filepath]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         pass_fds=pass_fds)
    atag = (f" + audio: {os.path.basename(audio_source)}" if audio_source
            else " + audio: stream" if audio_fd is not None else "")
    print(f"[*] PETSCII recorder: {filepath} ({out_w}x{out_h} @ {FPS}fps){atag}")
    return p

def _cached_char_cell(screen_code, fg_color, block=PETSCII_BLOCK):
    """Render one C64 character cell as block×block RGB bytes; result is cached."""
    key = (screen_code, fg_color & 0x0F)
    cached = _char_cell_cache.get(key)
    if cached is not None:
        return cached
    if _C64_CHARGEN is not None:
        bm = _C64_CHARGEN[screen_code * 8 : screen_code * 8 + 8]
    else:
        bm = _C64_CHAR_FALLBACK.get(screen_code, b'\x00' * 8)
    r, g, b = C64_PALETTE_RGB[fg_color & 0x0F]
    fg = bytes([r, g, b])
    bg = b'\x00\x00\x00'
    scale = block // 8   # 2× for block=16
    cell = bytearray(block * block * 3)
    for row8 in range(8):
        byte = bm[row8] if row8 < len(bm) else 0
        for col8 in range(8):
            pix = fg if (byte >> (7 - col8)) & 1 else bg
            for dr in range(scale):
                for dc in range(scale):
                    off = ((row8 * scale + dr) * block + col8 * scale + dc) * 3
                    cell[off:off + 3] = pix
    result = bytes(cell)
    _char_cell_cache[key] = result
    return result

def _render_petscii_frame(screen_codes, colors, num_cols, num_rows, block=PETSCII_BLOCK):
    """Render a full PETSCII frame as raw RGB24 bytes using cached C64 char bitmaps.
    screen_codes: PETSCII screen code per cell; colors: C64 color index per cell."""
    bstride = block * 3
    lines = []
    for row in range(num_rows):
        base = row * num_cols
        cells = [_cached_char_cell(screen_codes[base + col], colors[base + col], block)
                 for col in range(num_cols)]
        for br in range(block):
            off = br * bstride
            end = off + bstride
            lines.append(b''.join(c[off:end] for c in cells))
    return b''.join(lines)

def _screen_code_color(screen_code, color_mode, col_x, white_lut, fire_lut, rainbow_tab):
    """Map a screen code to a C64 color index for the recorder (mirrors C64 ASM logic)."""
    if color_mode == 0:
        return rainbow_tab[col_x]
    if color_mode == 1:
        return white_lut.get(screen_code, 1)
    return fire_lut.get(screen_code, 8)

def _apply_freq_gradient(raw, color_mode, height=HEIGHT):
    """Apply a per-mode vertical gradient to showfreqs frames so density/fire
    color modes see a range of character values instead of flat solid white.

    Row 0 = top of frame (tip of tallest bars).
    Row height-1 = bottom of frame (base of all bars).

    white        (1): dim at tip, bright at base
    rainbow+fire (0,2): tent — sparse at tip, dense near base, lighter fringe at base
    """
    buf = bytearray(raw)
    for row in range(height):
        if color_mode == 1:  # white: dim at tip, bright at base
            scale = 0.2 + 0.8 * row / (height - 1)
        else:                # rainbow + fire: tent peak near base, lighter fringe at base
            if row >= height - 3:    # bottom fringe
                scale = 1.0 - (row - (height - 3)) / 2 * 0.5
            else:                    # tip→body: 0.2 at row 0, 1.0 at row height-3
                scale = 0.2 + row / (height - 3) * 0.8
        start = row * WIDTH
        for i in range(start, start + WIDTH):
            if buf[i] > 0:
                buf[i] = max(1, int(buf[i] * scale))
    return bytes(buf)

# ---------------------------------------------------------------------------
# D64 save — pack SID + pre-rendered frames onto a bootable D64 disk image
# ---------------------------------------------------------------------------

def _rle_compress(data: bytes) -> bytes:
    """RLE compress a frame for the D64 player.

    Token format:
      $00-$7F  count byte — the next (count+1) bytes are literals (1..128)
      $80-$FF  run marker — repeat the next byte (value-$7E) times (2..129)
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        run = 1
        while i + run < n and data[i + run] == b and run < 129:
            run += 1

        if run >= 3:
            consumed = 0
            while consumed < run:
                chunk = min(run - consumed, 129)
                out.append(0x7e + chunk)   # $80=2 reps ... $FF=129 reps
                out.append(b)
                consumed += chunk
            i += run
        else:
            # Gather consecutive non-run bytes as a literal batch (up to 128)
            lits = bytearray()
            while i < n and len(lits) < 128:
                c = data[i]
                rlen = 1
                while i + rlen < n and data[i + rlen] == c and rlen < 3:
                    rlen += 1
                if rlen >= 3:
                    break
                lits.append(c)
                i += 1
            if lits:
                out.append(len(lits) - 1)   # $00=1 literal, $7F=128 literals
                out.extend(lits)
    return bytes(out)


def save_to_d64(sid_file, output_d64, fps=10, viz="showwaves", color_mode=0, duration=None):
    """
    Generate a bootable D64 disk image from a SID file.

    The image contains two files:
      "*"       — autostart player PRG (pre-rendered PETSCII frames + player code)
      "SIDDATA" — raw SID binary (player loads this at boot via KERNAL LOAD)

    sid_file   : path to a .sid / .psid file
    output_d64 : output path for the .d64 image
    fps        : visualization frame rate (default 10; must divide 50 evenly)
    viz        : ffmpeg viz filter: "showwaves" | "showfreqs" | "avectorscope" |
                 "showspectrum" | "ahistogram"
    duration   : capture duration in seconds (None = auto from SID length)
    """
    import tempfile, struct as _struct
    from d64 import build_d64

    # ── locate player binary ────────────────────────────────────────────────
    player_prg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "sidviz_d64.prg")
    if not os.path.isfile(player_prg):
        print(f"[!] player binary not found: {player_prg}")
        print("[!] Run:  64tass -a -B -o sidviz_d64.prg sidviz_d64.asm")
        return False

    # ── parse SID ────────────────────────────────────────────────────────────
    psid = parse_psid(sid_file)
    if psid is None:
        return False

    # ── determine capture duration ───────────────────────────────────────────
    if duration is None:
        info = get_sid_info(sid_file)
        raw_len = info.get("Song Length", "")
        if raw_len:
            try:
                parts = raw_len.split(".")[0].split(":")
                duration = int(parts[0]) * 60 + int(parts[1])
            except Exception:
                pass
        if duration is None:
            duration = 180  # default 3 minutes

    fps_div = max(1, 50 // fps)
    actual_fps = 50 // fps_div
    frame_size = WIDTH * HEIGHT  # 40 × 17 = 680 bytes per frame

    print(f"[d64] SID: {os.path.basename(sid_file)}")
    print(f"[d64] Duration: {duration}s  FPS: {actual_fps}  "
          f"Viz: {viz}  Est frames: {duration * actual_fps}")

    # ── generate audio WAV via sidplayfp ─────────────────────────────────────
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="sidviz_d64_")
    os.close(wav_fd)
    try:
        sid_cmd = ["sidplayfp", f"-t{duration}", f"-w{wav_path}", sid_file]
        print(f"[d64] Rendering audio: {' '.join(sid_cmd)}")
        r = subprocess.run(sid_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.isfile(wav_path) or os.path.getsize(wav_path) == 0:
            print("[!] sidplayfp failed to produce WAV")
            return False

        # ── generate waveform frames via ffmpeg ──────────────────────────────
        viz_filters = {
            # mode=fill draws from zero to the waveform tip (bar style) — far
            # more pixels lit than mode=line's single-pixel trace.
            "showwaves":
                f"[0:a]showwaves=s={WIDTH}x{HEIGHT}:mode=fill"
                f":rate={actual_fps}:colors=#ffffff,format=gray",
            "showfreqs":
                f"[0:a]showfreqs=s={WIDTH}x{HEIGHT}:mode=bar"
                f":ascale=log:fscale=log:colors=#ffffff,format=gray",
            "avectorscope":
                (f"[0:a]aformat=channel_layouts=mono,asplit=2[La][Ra];"
                 f"[Ra]adelay=50[Rd];[La][Rd]amerge=inputs=2[S];"
                 f"[S]avectorscope=s={WIDTH}x{HEIGHT}:zoom=1.5:draw=line"
                 f":scale=sqrt,format=gray"),
            "showspectrum":
                f"[0:a]showspectrum=s={WIDTH}x{HEIGHT}:slide=scroll"
                f":scale=sqrt:color=intensity,format=gray",
            "ahistogram":
                f"[0:a]ahistogram=s={WIDTH}x{HEIGHT}:scale=sqrt:slide=scroll"
                f",format=gray",
        }
        filt = viz_filters.get(viz, viz_filters["showwaves"])
        chars_map = {
            "showwaves": CHARS, "showfreqs": CHARS_FREQ,
            "avectorscope": CHARS_SCOPE, "showspectrum": CHARS_SPECTRUM,
            "ahistogram": CHARS_HIST,
        }
        chars = chars_map.get(viz, CHARS)

        ff_cmd = [
            "ffmpeg", "-loglevel", "quiet",
            "-i", wav_path,
            "-filter_complex", filt,
            "-f", "rawvideo", "-pix_fmt", "gray",
            "-r", str(actual_fps), "pipe:1",
        ]
        # ── C64 RAM budget ────────────────────────────────────────────────────
        # The player PRG is loaded entirely into C64 RAM ($0801-$CFFF).
        # Frame data must also land above the SID's own load range, because
        # KERNAL LOAD of SIDDATA overwrites whatever is in that range.
        #
        # PRG layout:
        #   [player binary  449 bytes  $0801-$09BF]
        #   [metadata       16 bytes   $09C0-$09CF]
        #   [TRAM_INIT       3 bytes   $09D0-$09D2]
        #   [TRAM_PLAY       3 bytes   $09D3-$09D5]
        #   [fdat_page       1 byte    $09D6      ]
        #   [frame index  2*N bytes   $09D7-...  ]
        #   [pad to page boundary                ]
        #   [compressed frames  (must end < $D000)]

        FRAME_IDX_C64  = 0x09E7   # C64 addr where frame index starts
        IDX_FILE_OFF   = 488      # same expressed as file byte offset
        MAX_C64_END    = 0xD000   # exclusive — stop before I/O at $D000

        sid_load  = psid["load_addr"]
        sid_end   = sid_load + len(psid["data"])
        sid_end_page = (sid_end + 255) >> 8   # first page fully above SID

        # Player code lives at $0810-$09CF.  If the SID loads into that range,
        # KERNAL LOAD of SIDDATA will overwrite the running player → crash.
        PLAYER_CODE_START = 0x0810
        PLAYER_CODE_END   = 0x09D0  # exclusive
        if sid_load < PLAYER_CODE_END and sid_end > PLAYER_CODE_START:
            print(f"[!] Error: SID loads at ${sid_load:04X}-${sid_end-1:04X}, "
                  f"which overlaps the player code at $0810-$09CF.")
            print(f"[!] This SID cannot be played from a D64 image.")
            return False

        if sid_load < FRAME_IDX_C64 < sid_end:
            print(f"[!] Warning: SID occupies ${sid_load:04X}-${sid_end-1:04X} "
                  f"which overlaps the frame-index area — playback may be garbled")

        # Frame data base = max(page after index, first page above SID)
        # Computed per-iteration during capture as frame_count grows.
        def _fdat_c64(n_frames):
            """Return the page-aligned C64 address where frame data would start
            if we have n_frames (each index entry = 2 bytes)."""
            after_idx = FRAME_IDX_C64 + n_frames * 2
            page = max((after_idx + 255) >> 8, sid_end_page)
            return page << 8

        # Pre-flight: how much space is available for frame data at all?
        min_fdat_start = _fdat_c64(0)   # best case: 0 frames, so smallest idx
        available_bytes = MAX_C64_END - min_fdat_start
        if available_bytes <= 0:
            print(f"[!] SID loads too high (${sid_load:04X}-${sid_end-1:04X}) — "
                  f"no room for frame data below I/O ($D000)")
            return False
        print(f"[d64] SID range: ${sid_load:04X}-${sid_end-1:04X}  "
              f"frame data starts at ${min_fdat_start:04X}+  "
              f"budget ≤{available_bytes//1024}KB")

        print("[d64] Generating frames (stopping when C64 RAM fills)...")
        ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # colour lookup tables for color_mode 1 (white) and 2 (fire)
        chars_def_map = {
            "showwaves": CHARS_DEF, "showfreqs": CHARS_FREQ_DEF,
            "avectorscope": CHARS_SCOPE_DEF, "showspectrum": CHARS_SPECTRUM_DEF,
            "ahistogram": CHARS_HIST_DEF,
        }
        _wlut, _flut = _build_color_luts(chars_def_map.get(viz, CHARS_DEF))

        def _frame_color(frame_idx):
            if color_mode == 1:   return 1   # white
            if color_mode == 2:   return 8   # orange (fire)
            return RAINBOW_TAB[frame_idx % len(RAINBOW_TAB)]  # rainbow

        # ── read + compress frames — stop at C64 RAM limit ───────────────────
        compressed_frames = []
        running_compressed = 0
        frame_num = 0
        ram_capped = False
        while True:
            raw = ff.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            screen_codes = bytes(pixel_to_char(p, chars) for p in raw)
            color_byte   = _frame_color(frame_num)
            # color byte is stored raw (1 byte) before the RLE stream;
            # the player reads it, fills color RAM, then decompresses screen
            c_frame = bytes([color_byte]) + _rle_compress(screen_codes)

            # Would adding this frame push frame data end past $D000?
            n_after = len(compressed_frames) + 1
            fdat_start = _fdat_c64(n_after)
            fdat_end   = fdat_start + running_compressed + len(c_frame)
            if fdat_end >= MAX_C64_END:
                ram_capped = True
                break

            compressed_frames.append(c_frame)
            running_compressed += len(c_frame)
            frame_num += 1
            if frame_num % 50 == 0:
                ratio = running_compressed / (frame_num * frame_size)
                print(f"\r[d64] Frames: {frame_num}  "
                      f"Compressed: {running_compressed//1024}KB  "
                      f"Ratio: {ratio:.0%}", end="", flush=True)

        ff.stdout.close()
        ff.wait()
        print()

        if not compressed_frames:
            print("[!] No frames generated — check sidplayfp and ffmpeg are installed")
            return False

        frame_count = len(compressed_frames)
        total_compressed = running_compressed
        avg_frame = total_compressed // frame_count
        loop_secs = frame_count / actual_fps
        if ram_capped:
            print(f"[d64] C64 RAM full — captured {frame_count} frames "
                  f"({loop_secs:.0f}s loop @ {actual_fps}fps)  "
                  f"Visualization will loop every {loop_secs:.0f}s")
        else:
            print(f"[d64] Captured {frame_count} frames ({loop_secs:.0f}s)")
        print(f"[d64] Compressed: {total_compressed//1024}KB  "
              f"Avg/frame: {avg_frame}B  "
              f"(uncompressed: {frame_count*frame_size//1024}KB)")

        # ── build player PRG ─────────────────────────────────────────────────
        player_bin = bytearray(open(player_prg, "rb").read())

        sid_disk_name    = "SIDDATA"
        sid_name_petscii = sid_disk_name.encode("ascii")[:8]
        sid_name_petscii = sid_name_petscii + b"\xa0" * (8 - len(sid_name_petscii))

        init_addr = psid["init_addr"]
        play_addr = psid["play_addr"]

        # Metadata (16 bytes → $09C0-$09CF)
        meta = bytearray(16)
        meta[0]  = init_addr & 0xFF
        meta[1]  = (init_addr >> 8) & 0xFF
        meta[2]  = play_addr & 0xFF
        meta[3]  = (play_addr >> 8) & 0xFF
        meta[4]  = frame_count & 0xFF
        meta[5]  = (frame_count >> 8) & 0xFF
        meta[6]  = fps_div
        meta[7]  = len(sid_disk_name)
        meta[8:16] = sid_name_petscii

        tram_init = bytes([0x4C, init_addr & 0xFF, (init_addr >> 8) & 0xFF])
        tram_play = bytes([0x4C, play_addr & 0xFF, (play_addr >> 8) & 0xFF])

        # Compute exact frame data base address
        fdat_c64_addr = _fdat_c64(frame_count)
        fdat_page_val = fdat_c64_addr >> 8

        # Padding from end of frame index to fdat_c64_addr
        idx_end_c64 = FRAME_IDX_C64 + frame_count * 2
        pad = fdat_c64_addr - idx_end_c64
        assert pad >= 0 and fdat_c64_addr % 256 == 0

        # Frame index: 2-byte LE offsets from fdat_c64_addr (all fit in uint16
        # because total_compressed ≤ MAX_C64_END − fdat_c64_addr < 53248 < 65536)
        frame_index = bytearray()
        offset = 0
        for f in compressed_frames:
            frame_index += _struct.pack("<H", offset)
            offset += len(f)

        frame_data = b"".join(compressed_frames)

        prg_data = (
            player_bin               # 465 bytes: 2-byte header + code to $09CF
            + meta                   # 16 bytes  ($09D0-$09DF)
            + tram_init              # 3 bytes   ($09E0-$09E2)
            + tram_play              # 3 bytes   ($09E3-$09E5)
            + bytes([fdat_page_val]) # 1 byte    ($09E6)
            + frame_index            # 2*N bytes ($09E7-...)
            + bytes(pad)             # pad to page boundary
            + frame_data             # compressed frames (each: 1-byte color + RLE screen)
        )

        prg_end_c64 = 0x0801 + len(prg_data) - 2
        print(f"[d64] Player PRG: {len(prg_data)//1024}KB  "
              f"Frame data: ${fdat_c64_addr:04X}-${prg_end_c64:04X}  "
              f"(page ${fdat_page_val:02X})")

        # ── build SIDDATA PRG ─────────────────────────────────────────────────
        sid_prg = _struct.pack("<H", psid["load_addr"]) + psid["data"]
        print(f"[d64] SIDDATA: {len(sid_prg)} bytes  "
              f"load ${psid['load_addr']:04X}  "
              f"init ${psid['init_addr']:04X}  "
              f"play ${psid['play_addr']:04X}")

        # ── D64 capacity check ────────────────────────────────────────────────
        total_bytes = len(prg_data) + len(sid_prg)
        D64_CAPACITY = 664 * 254
        if total_bytes > D64_CAPACITY:
            print(f"[!] Data ({total_bytes//1024}KB) exceeds D64 capacity "
                  f"({D64_CAPACITY//1024}KB) — this shouldn't happen; "
                  f"C64 RAM limit should have caught this first")
            return False

        # ── create D64 image ──────────────────────────────────────────────────
        sid_base = os.path.splitext(os.path.basename(sid_file))[0].upper()[:8]
        build_d64(
            output_d64,
            [
                ("SIDVIZ",   prg_data),   # autostart: LOAD"*",8,1
                ("SIDDATA",  sid_prg),    # SID binary with embedded load addr
            ],
            disk_name=sid_base,
            disk_id="SV",
        )
        print(f"[d64] Done → {output_d64}")
        print(f"[d64] On C64: LOAD\"*\",8,1  then  RUN")
        return True

    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global U64, FPS, VIZ_MODE, _YTDLP_COOKIE_ARGS  # noqa: PLW0603

    def _kill_proc(p):
        if p is None:
            return
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=2.0)
        except Exception:
            pass

    args = parse_args()

    if args.cookies_from_browser:
        _YTDLP_COOKIE_ARGS = ["--cookies-from-browser", args.cookies_from_browser]
    elif args.cookies:
        _YTDLP_COOKIE_ARGS = ["--cookies", args.cookies]

    if args.version:
        print(f"sidviz_u64  v{VERSION}  build {BUILD}")
        sys.exit(0)

    if args.save_d64:
        if not args.file:
            print("[!] --save-d64 requires a .sid file argument")
            sys.exit(1)
        sid_path = os.path.expanduser(args.file)
        if not os.path.isfile(sid_path):
            print(f"[!] File not found: {sid_path}")
            sys.exit(1)
        d64_viz = args.d64_viz
        if d64_viz is None:
            ans = input("Visualization? [0=waveform, 1=spectrum, 2=scope, 3=spectrogram, 4=histogram] (default 0): ").strip()
            d64_viz = {"1":"showfreqs","2":"avectorscope","3":"showspectrum","4":"ahistogram"}.get(ans, "showwaves")
        d64_color = args.d64_color
        if d64_color is None:
            ans = input("Color mode? [0=rainbow, 1=white, 2=fire] (default 0): ").strip()
            d64_color = int(ans) if ans in ("0","1","2") else 0
        ok = save_to_d64(
            sid_path,
            args.save_d64,
            fps=args.d64_fps,
            viz=d64_viz,
            color_mode=d64_color,
            duration=args.d64_duration,
        )
        sys.exit(0 if ok else 1)

    if args.list_cameras:
        if sys.platform == "darwin":
            subprocess.run(["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                           stderr=None)
        else:
            print("Linux: available video devices:")
            subprocess.run(["ls", "-1", "/dev/video*"], shell=False)
        sys.exit(0)

    # -------------------------------------------------------------------------
    # Camera mode: camera = visual source; optional file/URL = audio source
    # -------------------------------------------------------------------------
    if args.camera:
        U64 = f"http://{args.ip}"
        FPS = args.fps
        VIZ_MODE = "camera"
        camera_device = args.camera_device

        # Optional audio + file (mirrors normal mode filepath resolution)
        if args.yt_search:
            query = args.yt_search.strip()
            if not query:
                print("[!] --yt-search requires a non-empty query"); sys.exit(1)
            print(f"[*] Searching YouTube: {query} (max {args.yt_max})")
            candidates = youtube_search(query, args.yt_max)
            if not candidates:
                print("[!] No YouTube results found"); sys.exit(1)
            chosen = choose_youtube_result(candidates)
            if not chosen:
                print("[*] Search cancelled."); sys.exit(0)
            filepath = chosen
            print(f"[*] Selected: {filepath}")
            # Save candidate metadata — fallback if get_stream_info is blocked by YouTube
            _yt_chosen = next((c for c in candidates if c["url"] == chosen), None)
        elif args.file:
            filepath = os.path.expanduser(args.file)
            _yt_chosen = None
        else:
            filepath = None
            _yt_chosen = None
        if filepath and not is_url(filepath) and not os.path.isfile(filepath):
            print(f"[!] File not found: {filepath}"); sys.exit(1)

        if args.color:      color_mode_init = 0
        elif args.no_color: color_mode_init = 1
        else:
            ans = input("Color mode? [0=rainbow, 1=white, 2=fire] (default 0): ").strip()
            color_mode_init = int(ans) if ans in ("0","1","2") else 0

        print(f"[*] Viz mode: camera  device: {camera_device}")

        # --- Blend viz: which audio visualization to overlay on camera frames ---
        blend_viz_mode = None
        if filepath:
            if args.showwaves:      blend_viz_mode = "showwaves"
            elif args.showfreqs:    blend_viz_mode = "showfreqs"
            elif args.avectorscope: blend_viz_mode = "avectorscope"
            elif args.showspectrum: blend_viz_mode = "showspectrum"
            elif args.ahistogram:   blend_viz_mode = "ahistogram"
            else:
                ans = input(
                    "Blend audio viz? [0=none, 1=waveform, 2=spectrum, 3=scope, "
                    "4=spectrogram, 5=histogram] (default 0): "
                ).strip()
                blend_viz_mode = {
                    "1": "showwaves", "2": "showfreqs", "3": "avectorscope",
                    "4": "showspectrum", "5": "ahistogram",
                }.get(ans)
            if blend_viz_mode:
                print(f"[*] Blend viz: {blend_viz_mode}")

        # --- Viz color (only meaningful in blend mode) ---
        if blend_viz_mode:
            if args.color:      viz_color_mode_init = 0
            elif args.no_color: viz_color_mode_init = 1
            else:
                ans = input("Viz color mode? [0=rainbow, 1=white, 2=fire] (default 2): ").strip()
                viz_color_mode_init = int(ans) if ans in ("0","1","2") else 2
        else:
            viz_color_mode_init = 2

        # --- Color lookup tables for per-pixel dual-color blend ---
        _cam_wlut, _cam_flut = _build_color_luts(CHARS_CAMERA_DEF)
        if blend_viz_mode:
            _viz_chars_def = {
                "showwaves":    CHARS_DEF,
                "showfreqs":    CHARS_FREQ_DEF,
                "avectorscope": CHARS_SCOPE_DEF,
                "showspectrum": CHARS_SPECTRUM_DEF,
                "ahistogram":   CHARS_HIST_DEF,
            }.get(blend_viz_mode, CHARS_DEF)
            _viz_chars = [t[0] for t in _viz_chars_def]
            _viz_wlut, _viz_flut = _build_color_luts(_viz_chars_def)
        else:
            _viz_chars, _viz_wlut, _viz_flut = None, {}, {}

        # --- Audio mode detection ---
        audio_mode        = None
        c64_audio         = False
        sid_duration_secs = None
        psid              = None
        stream_url        = filepath
        info              = {}

        if filepath:
            audio_mode = detect_mode(filepath, force_sid=args.sid, force_audio=args.audio)

            if audio_mode == "sid":
                info = get_sid_info(filepath)
                raw_len = info.get("Song Length", "")
                if raw_len:
                    try:
                        parts = raw_len.split(".")[0].split(":")
                        sid_duration_secs = int(parts[0]) * 60 + int(parts[1])
                    except Exception:
                        pass
                if args.c64audio:
                    c64_audio = True
                elif args.macaudio:
                    c64_audio = False
                else:
                    ans = input("Audio output? [m=local/sidplayfp (default), c=C64]: ").strip().lower()
                    c64_audio = ans in ("c", "c64")
                print(f"[*] SID audio: {'C64 hardware' if c64_audio else 'local (sidplayfp)'}")
                if c64_audio:
                    psid = parse_psid(filepath)
                    if not psid:
                        print("[!] PSID parse failed, falling back to local audio")
                        c64_audio = False

            elif audio_mode == "stream":
                info = get_stream_info(filepath)
                if info is None:
                    if get_service(filepath) == "spotify":
                        print("[!] Failed to fetch stream metadata"); sys.exit(1)
                    # YouTube: metadata is display-only; stream URL is enough to proceed.
                    # Fall back to yt-search candidate data if available.
                    if _yt_chosen:
                        info = {"Title":  _yt_chosen.get("title", ""),
                                "Artist": _yt_chosen.get("uploader", "")}
                        print("[!] yt-dlp full extraction blocked — using search result metadata.")
                    else:
                        info = {}
                        print("[!] Metadata unavailable — continuing without track info.")
                if get_service(filepath) == "spotify":
                    stream_url = resolve_stream_url(filepath, info)
                    if not stream_url:
                        print("[!] Could not find YouTube match for Spotify track"); sys.exit(1)
            else:
                info = get_audio_info(filepath)

            display_mode = "sid" if audio_mode == "sid" else "audio"
            show_info_header(info, display_mode, filepath)
            ticker_str = build_ticker_string(info, display_mode)
        else:
            ticker_str = f"CAMERA LIVE   *   SIDVIZ U64 V{VERSION}   *   DEVICE {camera_device}        "
            if len(ticker_str) > 253:
                ticker_str = ticker_str[:253]

        # --- C64 setup ---
        if not os.path.isfile(PRG_LOCAL):
            print(f"[!] {PRG_REMOTE} not found at {PRG_LOCAL}")
            print(f"    Build: 64tass -a -B -o sidviz.prg sidviz.asm")
            sys.exit(1)

        if filepath and audio_mode == "sid":
            _ow = _psid_row_overlap(filepath)
            if _ow:
                print(f"\n[!] WARNING: SID binary {_ow}.")
                print("[!] The visualizer will still write rows 2-7, but those rows may show")
                print("[!] SID driver bytes rendered as PETSCII artifacts.")
                ans = input("Continue anyway? [y/N]: ").strip().lower()
                if ans != "y":
                    print("[*] Aborted.")
                    sys.exit(0)

        if not smoke_test(): sys.exit(1)

        print("[*] Rebooting C64...")
        u64_put("machine:reboot")
        time.sleep(4.0)

        print(f"[*] Uploading {PRG_REMOTE}...")
        if not ftp_upload(PRG_LOCAL, PRG_REMOTE): sys.exit(1)

        print(f"[*] Running {PRG_REMOTE}...")
        if not run_prg_from_temp(PRG_REMOTE): sys.exit(1)

        if c64_audio:
            print("[*] Signalling C64 audio mode to PRG ($C002=1)...")
            write_byte(C64_AUDIO_FLAG, 1)

        time.sleep(1.0)
        write_color_tables()
        print("[*] Color tables written ($C3A8/$C428).")

        if c64_audio:
            if psid.get("clock") == 2:
                print("[*] NTSC SID — setting CIA1 timer for 60Hz...")
                write_mem(0xDC04, [0x95, 0x42])
            else:
                print("[*] Setting CIA1 timer for PAL 50Hz...")
                write_mem(0xDC04, [0xF8, 0x4C])
            write_byte(0xDC0E, 0x11)

        _cflag_map = {0: 2, 1: 1, 2: 3}
        if blend_viz_mode:
            write_byte(COLOR_FLAG, 5)  # manual mode — Python writes color RAM per-pixel
        else:
            write_byte(COLOR_FLAG, _cflag_map[color_mode_init])
        send_ticker(ticker_str)

        # --- Camera display dimensions (needed by viz ffmpeg start calls below) ---
        cam_ext_rows   = 6
        cam_height     = HEIGHT + cam_ext_rows   # 23 rows total (rows 2-24)
        cam_frame_size = WIDTH * cam_height      # 920
        viz_frame_size = WIDTH * cam_height      # 920 — blend covers all 23 rows

        # --- Start audio + optional blend viz processes ---
        procs          = []
        sid_audio_proc = None
        ffplay_proc    = None
        yt_audio_proc  = None
        viz_ffmpeg_proc = None
        sid_fifo_proc  = None
        yt_viz_proc    = None
        sid_end_time   = (time.time() + sid_duration_secs) if sid_duration_secs else None

        if audio_mode == "sid":
            if c64_audio:
                # C64 plays audio via SID chip; sidplayfp → FIFO only if blending.
                # Upload SID first so INIT runs, then do a post-INIT check: some SIDs
                # (especially multi-SID arrangements) copy a runtime player into
                # $0450-$053F (screen RAM rows 2-7) during INIT.  Writing camera
                # pixels there every frame would overwrite the player and crash the C64.
                # Read back $0450-$053F after INIT; if any non-space byte is found,
                # restrict the camera to rows 8-24 only.
                upload_sid_to_c64(psid)
                time.sleep(0.25)   # give INIT time to finish
                _post = u64_get("machine:readmem?address=450&length=F0")  # $0450, 240 bytes
                if _post and any(b != 0x20 for b in _post):
                    print("[!] SID INIT placed code at $0450-$053F (rows 2-7) — "
                          "restricting camera to rows 8-24 to avoid corrupting SID driver")
                    cam_ext_rows   = 0
                    cam_height     = HEIGHT
                    cam_frame_size = WIDTH * HEIGHT
                    viz_frame_size = WIDTH * HEIGHT
                if blend_viz_mode:
                    make_fifo(FIFO_PATH)
                    VIZ_MODE = blend_viz_mode
                    viz_ffmpeg_proc = start_ffmpeg_waveform_fifo(realtime=True, height=cam_height)
                    VIZ_MODE = "camera"
                    sid_fifo_proc = start_sidplayfp_fifo(filepath, sid_duration_secs)
                    procs = [sid_fifo_proc, viz_ffmpeg_proc]
            else:
                if blend_viz_mode:
                    make_fifo(FIFO_PATH)
                    VIZ_MODE = blend_viz_mode
                    viz_ffmpeg_proc = start_ffmpeg_waveform_fifo(realtime=True, height=cam_height)
                    VIZ_MODE = "camera"
                    time.sleep(0.3)
                    sid_fifo_proc  = start_sidplayfp_fifo(filepath, sid_duration_secs)
                    sid_audio_proc = start_sidplayfp_audio(filepath, sid_duration_secs)
                    procs = [sid_fifo_proc, sid_audio_proc, viz_ffmpeg_proc]
                else:
                    sid_audio_proc = start_sidplayfp_audio(filepath, sid_duration_secs)
                    procs = [sid_audio_proc]

        elif audio_mode == "audio":
            ffplay_proc = start_ffplay_audio(filepath)
            if blend_viz_mode:
                VIZ_MODE = blend_viz_mode
                viz_ffmpeg_proc = start_ffmpeg_waveform_file(filepath, height=cam_height)
                VIZ_MODE = "camera"
                procs = [ffplay_proc, viz_ffmpeg_proc]
            else:
                procs = [ffplay_proc]

        elif audio_mode == "stream":
            yt_audio_proc, ffplay_proc = start_ffplay_stream(stream_url)
            if blend_viz_mode:
                VIZ_MODE = blend_viz_mode
                yt_viz_proc, viz_ffmpeg_proc = start_ffmpeg_waveform_stream(stream_url, height=cam_height)
                VIZ_MODE = "camera"
                procs = [yt_audio_proc, ffplay_proc, yt_viz_proc, viz_ffmpeg_proc]
            else:
                procs = [yt_audio_proc, ffplay_proc]
            if args.save:
                print(f"[*] Saving stream to: {args.save}")
                save_proc = subprocess.Popen(
                    ["yt-dlp", "-q", "-x", "--audio-format", "mp3"]
                    + _YTDLP_COOKIE_ARGS + ["-o", args.save, stream_url],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                procs.append(save_proc)

        # --- Camera display area ---
        _cam_ext_scr  = 0x0400 + 2 * WIDTH                     # $0450 (row 2)
        _cam_ext_col  = 0xD800 + 2 * WIDTH                     # $D850 (row 2 color RAM)
        def _top_colors(cmode, screen=None):
            """6-row rainbow/density color stripe for rows 2-7."""
            if cmode == 0:
                return bytes(RAINBOW_TAB[col] for _ in range(6) for col in range(WIDTH))
            lut, default = (_cam_wlut, 1) if cmode == 1 else (_cam_flut, 8)
            if screen is None:
                return [default] * (6 * WIDTH)
            return bytes(lut.get(sc, default) for sc in screen)

        # --- Start camera ---
        cam_proc = start_ffmpeg_camera(camera_device, height=cam_height)
        if cam_proc is None:
            print("[!] Cannot open camera — check device index and permissions.")
            print(f"    Linux: ls /dev/video*  |  try --camera-device 1")
            print(f"    macOS: check System Settings → Privacy → Camera")
            for p in procs:
                try: p.terminate()
                except: pass
            sys.exit(1)

        # Clear waveform zone (rows 8-24) via frame buffer + direct write.
        # Must use WIDTH*HEIGHT (680), not viz_frame_size (920) — FRAME_BUF
        # is only 680 bytes; writing 920 would overflow into color tables at $C3A8.
        write_mem(FRAME_BUF, [0x20] * (WIDTH * HEIGHT))
        write_mem(0x0540,    [0x20] * (WIDTH * HEIGHT))
        write_byte(FRAME_FLAG, 1)
        # If extended: also clear rows 2-7 screen + color RAM.
        # Write color RAM 3× with small delays: the C64's color RAM SRAM is shared
        # with the VIC-II on every raster cycle; a single U64 API write can lose
        # to a bus conflict and be silently ignored.
        if cam_ext_rows:
            write_mem(_cam_ext_scr, [0x20] * (cam_ext_rows * WIDTH))
            for _ in range(3):
                write_mem(_cam_ext_col, _top_colors(color_mode_init))
                time.sleep(0.05)
        print("[*] Camera zone cleared.")

        petscii_recorder  = None
        petscii_queue     = None
        petscii_thread    = None
        yt_petscii_audio  = None
        _prec_ticker_petscii = None
        _prec_ticker_t0   = None
        _prec_tmp_video   = None
        _prec_tmp_audio   = None
        if args.save_petscii:
            _prec_ticker_petscii = bytes(ascii_to_petscii(ticker_str))[:253] or b'\x20'
            _prec_ticker_t0 = time.time()
            _prec_audio_src = None
            if audio_mode == "audio" and filepath and not is_url(filepath):
                # Local file: pass audio directly to ffmpeg (no timing issues)
                _prec_audio_src = filepath
                _rec_dst = args.save_petscii
            elif audio_mode == "stream":
                # Stream: download audio to temp file; mux after recording ends.
                # Avoids the fast-download / muxer-interleave problem that cut
                # video short when audio was piped directly into ffmpeg.
                _prec_tmp_video = args.save_petscii + ".video.tmp"
                _prec_tmp_audio = args.save_petscii + ".audio.tmp.m4a"
                yt_petscii_audio = subprocess.Popen(
                    ["yt-dlp", "-f", "bestaudio/best", "-o", _prec_tmp_audio,
                     "-q", "--no-playlist"] + _YTDLP_COOKIE_ARGS + [stream_url],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _rec_dst = _prec_tmp_video
            else:
                _rec_dst = args.save_petscii
            petscii_recorder = start_petscii_recorder(
                _rec_dst, WIDTH, 25,  # 25 rows = full C64 screen (640×400)
                audio_source=_prec_audio_src)
            petscii_queue = queue.Queue(maxsize=10)
            def _petscii_worker():
                # Write at a fixed wall-clock rate (FPS), repeating the last
                # frame when no new one is available.  This decouples recording
                # duration from the HTTP-limited main-loop frame rate.
                interval   = 1.0 / FPS
                last_item  = None
                next_write = time.monotonic()
                while True:
                    wait = next_write - time.monotonic()
                    if wait > 0:
                        try:
                            item = petscii_queue.get(timeout=wait)
                            if item is None:
                                break
                            last_item = item
                            # Drain any extras, keep only the latest
                            while True:
                                try:
                                    i2 = petscii_queue.get_nowait()
                                    if i2 is None:
                                        if last_item is not None:
                                            sc, col = last_item
                                            try:
                                                petscii_recorder.stdin.write(
                                                    _render_petscii_frame(sc, col, WIDTH, 25, block=_REC_BLOCK))
                                            except Exception:
                                                pass
                                        return
                                    last_item = i2
                                except queue.Empty:
                                    break
                            continue  # re-check next_write
                        except queue.Empty:
                            pass  # timed out → fall through to write
                    if last_item is not None:
                        sc, col = last_item
                        try:
                            petscii_recorder.stdin.write(
                                _render_petscii_frame(sc, col, WIDTH, 25, block=_REC_BLOCK))
                        except Exception:
                            break
                    next_write += interval
                    if time.monotonic() > next_write + interval:
                        next_write = time.monotonic()  # reset if too far behind
            petscii_thread = threading.Thread(target=_petscii_worker, daemon=True)
            petscii_thread.start()

        # Background thread keeps the latest viz frame for blending (17-row)
        last_viz_frame = bytearray(viz_frame_size)
        viz_lock = threading.Lock()
        viz_frame_count = [0]  # diagnostic: count frames received from viz ffmpeg

        if viz_ffmpeg_proc:
            def _read_viz():
                # read1() drains whatever is available (including Python's internal
                # BufferedReader buffer); select() on a BufferedReader fd is unreliable
                # because Python's buffer may have already consumed the bytes from the fd.
                buf = bytearray()
                while not state["quit"]:
                    try:
                        chunk = viz_ffmpeg_proc.stdout.read1(max(viz_frame_size * 4, 65536))
                        if not chunk:
                            break
                        buf.extend(chunk)
                        while len(buf) >= viz_frame_size:
                            with viz_lock:
                                last_viz_frame[:] = buf[:viz_frame_size]
                            viz_frame_count[0] += 1
                            del buf[:viz_frame_size]
                    except Exception as e:
                        print(f"\r\n[!] _read_viz error: {e}")
                        break

        # Background thread continuously drains the camera pipe to prevent
        # deadlock: if the main loop falls behind (slow U64 HTTP writes), the
        # 64 KB pipe buffer fills, ffmpeg blocks writing, Python blocks in
        # read() — display freezes.  The thread always reads ahead; the main
        # loop picks up the latest complete frame without ever blocking on I/O.
        latest_cam_frame = [None]
        cam_frame_lock   = threading.Lock()

        def _read_cam():
            buf = bytearray()
            while not state["quit"]:
                try:
                    r, _, _ = _select.select([cam_proc.stdout], [], [], 0.1)
                    if not r:
                        continue
                    chunk = cam_proc.stdout.read1(max(cam_frame_size * 4, 65536))
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while len(buf) >= cam_frame_size:
                        with cam_frame_lock:
                            latest_cam_frame[0] = bytes(buf[:cam_frame_size])
                        del buf[:cam_frame_size]
                except Exception:
                    break

        _blend_capable = blend_viz_mode is not None  # True if started with blend; never changes
        VIZ_MODE = blend_viz_mode if blend_viz_mode else "none"  # sync for w-key cycling

        state   = {
            "cam_color":     color_mode_init,
            "viz_color":     viz_color_mode_init,
            "color_pending": False,
            "viz_pending":   None,
            "quit":          False,
        }
        kthread = make_keypress_listener(state)
        kthread.start()

        if viz_ffmpeg_proc:
            vt = threading.Thread(target=_read_viz, daemon=True)
            vt.start()

        cam_read_thread = threading.Thread(target=_read_cam, daemon=True)
        cam_read_thread.start()

        frame_num = 0
        blend_tag = f"+{blend_viz_mode}" if blend_viz_mode else ""
        if blend_viz_mode:
            controls = "[c] cam-color, [v] viz-color, [w/0-5] viz, [q] quit"
        else:
            controls = "[c] color, [q] quit"
        print(f"[*] Camera{blend_tag} streaming to C64 at {FPS}fps\n    {controls}\n")

        # Streams need time to buffer; don't declare "Song ended" until this many
        # seconds have elapsed — prevents false early-exit if yt-dlp is slow to start.
        loop_start = time.time()
        STREAM_GRACE = 12  # seconds

        def _stream_err(proc, label):
            """Read and print stderr from a process if it died early."""
            if proc is None or proc.stderr is None:
                return
            try:
                err = proc.stderr.read(4096).decode(errors="replace").strip()
                if err:
                    print(f"\r\n[!] {label}: {err[:300]}")
            except Exception:
                pass

        try:
            while not state["quit"]:
                elapsed = time.time() - loop_start
                # SID player (local): exit is genuine whenever it happens
                if sid_audio_proc is not None and sid_audio_proc.poll() is not None:
                    print("\r\n[*] Song ended."); break
                # Stream player: give yt-dlp time to buffer before treating exit as fatal
                if ffplay_proc is not None and ffplay_proc.poll() is not None:
                    if elapsed < STREAM_GRACE:
                        # Died during grace period — likely a yt-dlp error; show it
                        _stream_err(ffplay_proc,   "ffplay")
                        _stream_err(yt_audio_proc, "yt-dlp audio")
                        print("\r\n[!] Audio stream failed (see above). Camera continues without audio.")
                        ffplay_proc = None  # suppress further checks; camera keeps running
                    else:
                        print("\r\n[*] Song ended."); break
                if sid_end_time is not None and time.time() >= sid_end_time:
                    print("\r\n[*] Song ended."); break

                if cam_proc.poll() is not None:
                    err = cam_proc.stderr.read().decode(errors="replace").strip()
                    print("\r\n[*] Camera stream ended.")
                    if err:
                        print(f"[!] ffmpeg camera: {err}")
                    break

                if viz_ffmpeg_proc is not None and viz_ffmpeg_proc.poll() is not None:
                    try:
                        verr = viz_ffmpeg_proc.stderr.read(4096).decode(errors="replace").strip()
                    except Exception:
                        verr = ""
                    print(f"\r\n[!] Viz ffmpeg exited (viz={viz_frame_count[0]} frames received).")
                    if verr:
                        print(f"[!] ffmpeg viz: {verr[:300]}")
                    viz_ffmpeg_proc = None  # suppress further checks
                    # Revert C64 to single-color mode (camera color)
                    write_byte(COLOR_FLAG, _cflag_map[state["cam_color"]])
                    if cam_ext_rows:
                        write_mem(_cam_ext_col, _top_colors(state["cam_color"]))

                if state["color_pending"]:
                    state["color_pending"] = False
                    if not viz_ffmpeg_proc:
                        # Non-blend: let C64 IRQ handle color RAM
                        write_byte(COLOR_FLAG, _cflag_map[state["cam_color"]])
                        if cam_ext_rows:
                            write_mem(_cam_ext_col, _top_colors(state["cam_color"]))

                if state["viz_pending"] is not None and _blend_capable:
                    new_viz = state["viz_pending"]
                    state["viz_pending"] = None
                    _cur_blend = blend_viz_mode or "none"
                    if new_viz != _cur_blend:
                        sys.stdout.write(
                            f"\r\n[*] Switching viz: {VIZ_LABELS.get(_cur_blend, _cur_blend)}"
                            f" -> {VIZ_LABELS.get(new_viz, new_viz)}\r\n")
                        sys.stdout.flush()

                        if viz_ffmpeg_proc is not None:
                            _old_viz = viz_ffmpeg_proc
                            _kill_proc(_old_viz)
                            try: _old_viz.stdout.close()
                            except: pass
                            time.sleep(0.1)  # let _read_viz thread see EOF and exit

                        if new_viz == "none":
                            viz_ffmpeg_proc = None
                            blend_viz_mode  = None
                            VIZ_MODE        = "none"
                            write_byte(COLOR_FLAG, _cflag_map[state["cam_color"]])
                            if cam_ext_rows:
                                write_mem(_cam_ext_col, _top_colors(state["cam_color"]))
                        else:
                            blend_viz_mode = new_viz
                            VIZ_MODE = new_viz  # _build_viz_filter() reads this; stays set
                            _viz_chars_def = {
                                "showwaves":    CHARS_DEF,
                                "showfreqs":    CHARS_FREQ_DEF,
                                "avectorscope": CHARS_SCOPE_DEF,
                                "showspectrum": CHARS_SPECTRUM_DEF,
                            }.get(blend_viz_mode, CHARS_HIST_DEF)
                            _viz_chars = [t[0] for t in _viz_chars_def]
                            _viz_wlut, _viz_flut = _build_color_luts(_viz_chars_def)

                            if audio_mode == "sid":
                                _kill_proc(sid_fifo_proc)
                                make_fifo(FIFO_PATH)
                                viz_ffmpeg_proc = start_ffmpeg_waveform_fifo(
                                    realtime=True, height=cam_height)
                                time.sleep(0.3)
                                sid_fifo_proc = start_sidplayfp_fifo(filepath, sid_duration_secs)
                            elif audio_mode == "stream":
                                _kill_proc(yt_viz_proc)
                                yt_viz_proc, viz_ffmpeg_proc = start_ffmpeg_waveform_stream(
                                    stream_url, height=cam_height)
                            else:
                                viz_ffmpeg_proc = start_ffmpeg_waveform_file(
                                    filepath, height=cam_height)

                            write_byte(COLOR_FLAG, 5)  # re-enable per-pixel color mode
                            threading.Thread(target=_read_viz, daemon=True).start()

                            time.sleep(0.1)
                            if viz_ffmpeg_proc.poll() is not None:
                                try:
                                    err = viz_ffmpeg_proc.stderr.read(512).decode(
                                        errors="replace").strip()
                                except Exception:
                                    err = ""
                                sys.stdout.write(
                                    f"\r\n[!] New viz ffmpeg exited immediately: {err}\r\n")
                                sys.stdout.flush()

                with cam_frame_lock:
                    raw = latest_cam_frame[0]
                    latest_cam_frame[0] = None
                if raw is None:
                    time.sleep(0.010)
                    continue

                # Split into top (rows 2-7, direct) and bottom (rows 8-24, frame buf)
                if cam_ext_rows:
                    top_raw = raw[:cam_ext_rows * WIDTH]
                    bot_raw = raw[cam_ext_rows * WIDTH:]
                else:
                    top_raw = b""
                    bot_raw = raw

                # Blend viz (all 23 rows) with camera then split for writing paths.
                if viz_ffmpeg_proc:
                    with viz_lock:
                        vz_top = last_viz_frame[:cam_ext_rows * WIDTH]
                        vz_bot = last_viz_frame[cam_ext_rows * WIDTH:]
                    blended_top = bytes(max(c, v) for c, v in zip(top_raw, vz_top)) if top_raw else b""
                    blended_bot = bytes(max(c, v) for c, v in zip(bot_raw, vz_bot))

                    # Per-pixel color: camera color when cam wins, viz color when viz wins
                    cam_cmode = state["cam_color"]
                    viz_cmode = state["viz_color"]
                    if cam_ext_rows:
                        color_top = bytearray(cam_ext_rows * WIDTH)
                        for i in range(cam_ext_rows * WIDTH):
                            col_x = i % WIDTH
                            cp, vp = top_raw[i], vz_top[i]
                            if cp >= vp:
                                color_top[i] = _pixel_color(cp, CHARS_CAMERA, cam_cmode, col_x,
                                                            _cam_wlut, _cam_flut, RAINBOW_TAB)
                            else:
                                color_top[i] = _pixel_color(vp, _viz_chars, viz_cmode, col_x,
                                                            _viz_wlut, _viz_flut, RAINBOW_TAB)
                    color_bot = bytearray(HEIGHT * WIDTH)
                    for i in range(HEIGHT * WIDTH):
                        col_x = i % WIDTH
                        cp, vp = bot_raw[i], vz_bot[i]
                        if cp >= vp:
                            color_bot[i] = _pixel_color(cp, CHARS_CAMERA, cam_cmode, col_x,
                                                        _cam_wlut, _cam_flut, RAINBOW_TAB)
                        else:
                            color_bot[i] = _pixel_color(vp, _viz_chars, viz_cmode, col_x,
                                                        _viz_wlut, _viz_flut, RAINBOW_TAB)
                else:
                    blended_top = top_raw
                    blended_bot = bot_raw

                # Bottom rows (8-24): write screen chars via frame buffer path
                bot_screen = bytes(pixel_to_char(p, CHARS_CAMERA) for p in blended_bot)
                write_mem(FRAME_BUF, bot_screen)
                write_byte(FRAME_FLAG, 1)

                # Top rows (2-7): write screen RAM and color RAM every frame.
                if blended_top:
                    top_screen = bytes(pixel_to_char(p, CHARS_CAMERA) for p in blended_top)
                    write_mem(_cam_ext_scr, top_screen)
                    if viz_ffmpeg_proc:
                        write_mem(_cam_ext_col, color_top)
                    else:
                        write_mem(_cam_ext_col, _top_colors(state["cam_color"], top_screen))

                # Bottom rows color RAM: Python writes directly in blend mode
                if viz_ffmpeg_proc:
                    write_mem(WAVE_COL_ADDR, color_bot)

                if petscii_queue is not None:
                    # Rows 2-7 screen codes (already computed for C64 write above)
                    sc_top = top_screen if cam_ext_rows else bytes([0x20] * 6 * WIDTH)
                    sc_bot = bot_screen
                    if viz_ffmpeg_proc:
                        col_top = bytes(color_top) if cam_ext_rows else bytes([0] * 6 * WIDTH)
                        col_bot = bytes(color_bot)
                    else:
                        cmode = state["cam_color"]
                        col_top = bytes(_screen_code_color(sc_top[i], cmode, i % WIDTH,
                                        _cam_wlut, _cam_flut, RAINBOW_TAB) for i in range(len(sc_top)))
                        col_bot = bytes(_screen_code_color(sc_bot[i], cmode, i % WIDTH,
                                        _cam_wlut, _cam_flut, RAINBOW_TAB) for i in range(len(sc_bot)))
                    # Ticker simulation: advance at 50Hz / SCROLL_RATE chars per second
                    _tlen = len(_prec_ticker_petscii)
                    _tpos = int((time.time() - _prec_ticker_t0) * 50.0 / SCROLL_RATE) % _tlen
                    ticker_sc  = bytes(_prec_ticker_petscii[(_tpos + i) % _tlen] for i in range(WIDTH))
                    ticker_col = bytes([13] * WIDTH)   # light green
                    # Assemble all 25 rows: row0 (black) + row1 (ticker) + rows2-7 + rows8-24
                    all_sc  = bytes([0x20] * WIDTH) + ticker_sc  + sc_top  + sc_bot
                    all_col = bytes([0]    * WIDTH) + ticker_col + col_top + col_bot
                    try:
                        petscii_queue.put_nowait((all_sc, all_col))
                    except queue.Full:
                        pass   # drop frame if renderer is behind; main loop must not block

                frame_num += 1
                if blend_viz_mode and viz_ffmpeg_proc:
                    cam_ind = ["R","W","F"][state["cam_color"]]
                    viz_ind = ["R","W","F"][state["viz_color"]]
                    ind = f"cam:{cam_ind} viz:{viz_ind}"
                else:
                    ind = ["R","W","F"][state["cam_color"]]
                vfc = viz_frame_count[0] if viz_ffmpeg_proc else -1
                vsuf = f" viz={vfc:05d}" if vfc >= 0 else ""
                tsuf = f" top={len(top_raw)}" if cam_ext_rows else ""
                print(f"\r[*] Frame {frame_num:05d} [{ind}]{vsuf}{tsuf}", end="", flush=True)

        except KeyboardInterrupt:
            print("\r\n[*] Interrupted.")
        finally:
            state["quit"] = True
            if petscii_queue is not None:
                petscii_queue.put(None)
                if petscii_thread is not None:
                    petscii_thread.join(timeout=60)
            if petscii_recorder is not None:
                try:
                    petscii_recorder.stdin.close()
                    petscii_recorder.wait(timeout=120)
                except Exception:
                    try: petscii_recorder.kill()
                    except Exception: pass
            if _prec_tmp_video and _prec_tmp_audio:
                if yt_petscii_audio is not None:
                    try: yt_petscii_audio.wait(timeout=60)
                    except Exception: pass
                print("\r\n[*] Muxing PETSCII audio+video...")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", _prec_tmp_video, "-i", _prec_tmp_audio,
                     "-c:v", "copy", "-c:a", "aac", "-shortest", args.save_petscii],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _f in (_prec_tmp_video, _prec_tmp_audio):
                    try: os.unlink(_f)
                    except Exception: pass
            elif yt_petscii_audio is not None:
                try: yt_petscii_audio.terminate(); yt_petscii_audio.wait(timeout=3)
                except Exception: pass
            print("\r\n[*] PETSCII recording saved.")
            if c64_audio:
                write_byte(C64_AUDIO_FLAG, 0)
                time.sleep(0.04)
                write_mem(0xD400, [0] * 25)
                write_byte(QUIT_FLAG, 1)
                time.sleep(0.3)
            else:
                write_mem(0xD400, [0] * 25)
                write_byte(FRAME_FLAG, 0)
                write_mem(FRAME_BUF, [0x20] * (WIDTH * HEIGHT))  # FRAME_BUF = rows 8-24 only
                if cam_ext_rows:
                    write_mem(_cam_ext_scr, [0x20] * (cam_ext_rows * WIDTH))
                    write_mem(_cam_ext_col, [0x00] * (cam_ext_rows * WIDTH))
                write_mem(TICKER_ROW, [0x20] * 40)
            def _stop(proc):
                if proc is None:
                    return
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            _stop(cam_proc)
            for p in procs:
                _stop(p)
            try: os.remove(FIFO_PATH)
            except: pass
            if "_term_fd" in state and "_term_old" in state:
                try:
                    termios.tcsetattr(state["_term_fd"], termios.TCSADRAIN, state["_term_old"])
                except Exception:
                    pass
            print("[*] Done.")
        return

    # -------------------------------------------------------------------------
    # Video mode: video file or URL displayed as PETSCII art on C64
    # -------------------------------------------------------------------------
    _video_file = (args.file and not is_url(args.file) and
                   os.path.splitext(args.file)[1].lower() in VIDEO_EXTS)
    if args.video or _video_file:
        U64 = f"http://{args.ip}"
        FPS = args.fps
        VIZ_MODE = "camera"

        # --- Resolve filepath ---
        if args.yt_search:
            query = args.yt_search.strip()
            if not query:
                print("[!] --yt-search requires a non-empty query"); sys.exit(1)
            print(f"[*] Searching YouTube: {query} (max {args.yt_max})")
            candidates = youtube_search(query, args.yt_max)
            if not candidates:
                print("[!] No YouTube results found"); sys.exit(1)
            chosen = choose_youtube_result(candidates)
            if not chosen:
                print("[*] Search cancelled."); sys.exit(0)
            filepath = chosen
            _yt_chosen = next((c for c in candidates if c["url"] == chosen), None)
        else:
            _yt_chosen = None
            filepath = os.path.expanduser(args.file) if args.file else \
                       os.path.expanduser(input("Video file or URL: ").strip())

        if not is_url(filepath) and not os.path.isfile(filepath):
            print(f"[!] File not found: {filepath}"); sys.exit(1)

        stream_url = filepath

        # --- Color mode ---
        if args.color:      color_mode_init = 0
        elif args.no_color: color_mode_init = 1
        else:
            ans = input("Color mode? [0=rainbow, 1=white, 2=fire] (default 0): ").strip()
            color_mode_init = int(ans) if ans in ("0","1","2") else 0

        src_label = os.path.basename(filepath) if not is_url(filepath) else filepath
        print(f"[*] Viz mode: video  source: {src_label}")

        # --- Blend viz overlay ---
        blend_viz_mode = None
        if args.showwaves:      blend_viz_mode = "showwaves"
        elif args.showfreqs:    blend_viz_mode = "showfreqs"
        elif args.avectorscope: blend_viz_mode = "avectorscope"
        elif args.showspectrum: blend_viz_mode = "showspectrum"
        elif args.ahistogram:   blend_viz_mode = "ahistogram"
        else:
            ans = input(
                "Blend audio viz? [0=none, 1=waveform, 2=spectrum, 3=scope, "
                "4=spectrogram, 5=histogram] (default 0): "
            ).strip()
            blend_viz_mode = {
                "1": "showwaves", "2": "showfreqs", "3": "avectorscope",
                "4": "showspectrum", "5": "ahistogram",
            }.get(ans)
        if blend_viz_mode:
            print(f"[*] Blend viz: {blend_viz_mode}")

        # --- Viz color (blend mode only) ---
        if blend_viz_mode:
            if args.color:      viz_color_mode_init = 0
            elif args.no_color: viz_color_mode_init = 1
            else:
                ans = input("Viz color mode? [0=rainbow, 1=white, 2=fire] (default 2): ").strip()
                viz_color_mode_init = int(ans) if ans in ("0","1","2") else 2
        else:
            viz_color_mode_init = 2

        # --- Color lookup tables for per-pixel dual-color blend ---
        _cam_wlut, _cam_flut = _build_color_luts(CHARS_CAMERA_DEF)
        if blend_viz_mode:
            _viz_chars_def = {
                "showwaves":    CHARS_DEF,
                "showfreqs":    CHARS_FREQ_DEF,
                "avectorscope": CHARS_SCOPE_DEF,
                "showspectrum": CHARS_SPECTRUM_DEF,
                "ahistogram":   CHARS_HIST_DEF,
            }.get(blend_viz_mode, CHARS_DEF)
            _viz_chars = [t[0] for t in _viz_chars_def]
            _viz_wlut, _viz_flut = _build_color_luts(_viz_chars_def)
        else:
            _viz_chars, _viz_wlut, _viz_flut = None, {}, {}

        # --- Metadata and ticker ---
        info = {}
        if is_url(filepath):
            info = get_stream_info(filepath) or {}
            if not info and _yt_chosen:
                info = {
                    "Title":    _yt_chosen.get("title", ""),
                    "Artist":   _yt_chosen.get("uploader", ""),
                    "Duration": _yt_chosen.get("duration", ""),
                }
        else:
            info = get_audio_info(filepath)
        show_info_header(info, "audio", filepath)
        ticker_str = build_ticker_string(info, "audio")
        if not ticker_str.strip("* "):
            ticker_str = f"{src_label}   *   SIDVIZ U64 V{VERSION}        "

        # --- Reboot + upload PRG ---
        if not smoke_test(): sys.exit(1)
        print("[*] Rebooting C64...")
        u64_put("machine:reboot")
        time.sleep(4.0)
        print(f"[*] Uploading {PRG_REMOTE}...")
        if not ftp_upload(PRG_LOCAL, PRG_REMOTE): sys.exit(1)
        print(f"[*] Running {PRG_REMOTE}...")
        if not run_prg_from_temp(PRG_REMOTE): sys.exit(1)

        time.sleep(1.0)
        write_color_tables()

        _cflag_map = {0: 2, 1: 1, 2: 3}
        if blend_viz_mode:
            write_byte(COLOR_FLAG, 5)
        else:
            write_byte(COLOR_FLAG, _cflag_map[color_mode_init])
        send_ticker(ticker_str)

        # --- Display dimensions (always full 23 rows for video) ---
        cam_ext_rows   = 6
        cam_height     = HEIGHT + cam_ext_rows   # 23 rows (rows 2-24)
        cam_frame_size = WIDTH * cam_height      # 920
        viz_frame_size = WIDTH * cam_height

        _cam_ext_scr = 0x0400 + 2 * WIDTH       # $0450 row 2 screen RAM
        _cam_ext_col = 0xD800 + 2 * WIDTH       # $D850 row 2 color RAM
        def _top_colors(cmode, screen=None):
            if cmode == 0:
                return bytes(RAINBOW_TAB[col] for _ in range(6) for col in range(WIDTH))
            lut, default = (_cam_wlut, 1) if cmode == 1 else (_cam_flut, 8)
            if screen is None:
                return [default] * (6 * WIDTH)
            return bytes(lut.get(sc, default) for sc in screen)

        # --- Clear display area ---
        write_mem(FRAME_BUF, [0x20] * (WIDTH * HEIGHT))
        write_mem(0x0540,    [0x20] * (WIDTH * HEIGHT))
        write_byte(FRAME_FLAG, 1)
        write_mem(_cam_ext_scr, [0x20] * (cam_ext_rows * WIDTH))
        for _ in range(3):
            write_mem(_cam_ext_col, _top_colors(color_mode_init))
            time.sleep(0.05)
        print("[*] Video zone cleared.")

        petscii_recorder  = None
        petscii_queue     = None
        petscii_thread    = None
        yt_petscii_audio  = None
        _prec_ticker_petscii = None
        _prec_ticker_t0   = None
        _prec_tmp_video   = None
        _prec_tmp_audio   = None
        if args.save_petscii:
            _prec_ticker_petscii = bytes(ascii_to_petscii(ticker_str))[:253] or b'\x20'
            _prec_ticker_t0 = time.time()
            _prec_audio_src = None
            if not is_url(filepath):
                _prec_audio_src = filepath
                _rec_dst = args.save_petscii
            else:
                _prec_tmp_video = args.save_petscii + ".video.tmp"
                _prec_tmp_audio = args.save_petscii + ".audio.tmp.m4a"
                yt_petscii_audio = subprocess.Popen(
                    ["yt-dlp", "-f", "bestaudio/best", "-o", _prec_tmp_audio,
                     "-q", "--no-playlist"] + _YTDLP_COOKIE_ARGS + [stream_url],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _rec_dst = _prec_tmp_video
            petscii_recorder = start_petscii_recorder(
                _rec_dst, WIDTH, 25,
                audio_source=_prec_audio_src)
            petscii_queue = queue.Queue(maxsize=10)
            def _petscii_worker():
                interval   = 1.0 / FPS
                last_item  = None
                next_write = time.monotonic()
                while True:
                    wait = next_write - time.monotonic()
                    if wait > 0:
                        try:
                            item = petscii_queue.get(timeout=wait)
                            if item is None:
                                break
                            last_item = item
                            while True:
                                try:
                                    i2 = petscii_queue.get_nowait()
                                    if i2 is None:
                                        if last_item is not None:
                                            sc, col = last_item
                                            try:
                                                petscii_recorder.stdin.write(
                                                    _render_petscii_frame(sc, col, WIDTH, 25, block=_REC_BLOCK))
                                            except Exception:
                                                pass
                                        return
                                    last_item = i2
                                except queue.Empty:
                                    break
                            continue
                        except queue.Empty:
                            pass
                    if last_item is not None:
                        sc, col = last_item
                        try:
                            petscii_recorder.stdin.write(
                                _render_petscii_frame(sc, col, WIDTH, 25, block=_REC_BLOCK))
                        except Exception:
                            break
                    next_write += interval
                    if time.monotonic() > next_write + interval:
                        next_write = time.monotonic()
            petscii_thread = threading.Thread(target=_petscii_worker, daemon=True)
            petscii_thread.start()

        # --- Start processes ---
        procs        = []
        yt_vid_proc  = None
        viz_ffmpeg_proc = None
        yt_viz_proc  = None
        audio_proc   = None

        if is_url(filepath):
            yt_vid_proc, vid_proc = start_yt_video_frames(stream_url, height=cam_height)
            procs += [yt_vid_proc, vid_proc]
            yt_audio_proc, audio_proc = start_ffplay_stream(stream_url)
            procs += [yt_audio_proc, audio_proc]
        else:
            vid_proc   = start_ffmpeg_video_frames(filepath, height=cam_height)
            audio_proc = start_ffplay_video_audio(filepath)
            procs += [vid_proc, audio_proc]

        if blend_viz_mode:
            VIZ_MODE = blend_viz_mode
            if is_url(filepath):
                yt_viz_proc, viz_ffmpeg_proc = start_ffmpeg_waveform_stream(stream_url, height=cam_height)
                procs += [yt_viz_proc, viz_ffmpeg_proc]
            else:
                viz_ffmpeg_proc = start_ffmpeg_waveform_file(filepath, height=cam_height)
                procs.append(viz_ffmpeg_proc)
            VIZ_MODE = "camera"

        if args.save and is_url(filepath):
            print(f"[*] Saving video to: {args.save}")
            save_proc = subprocess.Popen(
                ["yt-dlp", "-f", "bestvideo+bestaudio/best",
                 "--merge-output-format", "mp4"]
                + _YTDLP_COOKIE_ARGS + ["-o", args.save, stream_url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(save_proc)

        # --- Background threads ---
        last_viz_frame  = bytearray(viz_frame_size)
        viz_lock        = threading.Lock()
        viz_frame_count = [0]

        if viz_ffmpeg_proc:
            def _read_viz():
                buf = bytearray()
                while True:
                    try:
                        chunk = viz_ffmpeg_proc.stdout.read1(65536)
                        if not chunk: break
                        buf.extend(chunk)
                        while len(buf) >= viz_frame_size:
                            with viz_lock:
                                last_viz_frame[:] = buf[:viz_frame_size]
                                viz_frame_count[0] += 1
                            del buf[:viz_frame_size]
                    except Exception:
                        break
            threading.Thread(target=_read_viz, daemon=True).start()

        latest_vid_frame = [None]
        vid_frame_lock   = threading.Lock()

        def _read_vid():
            buf = bytearray()
            while True:
                try:
                    chunk = vid_proc.stdout.read1(65536)
                    if not chunk: break
                    buf.extend(chunk)
                    while len(buf) >= cam_frame_size:
                        with vid_frame_lock:
                            latest_vid_frame[0] = bytes(buf[:cam_frame_size])
                        del buf[:cam_frame_size]
                except Exception:
                    break
        threading.Thread(target=_read_vid, daemon=True).start()

        # --- State + keypress ---
        _blend_capable = blend_viz_mode is not None  # True if started with blend; never changes
        VIZ_MODE = blend_viz_mode if blend_viz_mode else "none"  # sync for w-key cycling

        state = {
            "cam_color":     color_mode_init,
            "viz_color":     viz_color_mode_init,
            "color_pending": False,
            "viz_pending":   None,
            "quit":          False,
        }
        kthread = make_keypress_listener(state)
        kthread.start()

        frame_num = 0
        blend_tag = f"+{blend_viz_mode}" if blend_viz_mode else ""
        controls  = "[c] vid-color, [v] viz-color, [w/0-5] viz, [q] quit" if blend_viz_mode else "[c] color, [q] quit"
        print(f"[*] Video{blend_tag} streaming to C64 at {FPS}fps\n    {controls}\n")

        try:
            while not state["quit"]:
                if audio_proc is not None and audio_proc.poll() is not None:
                    print("\r\n[*] Audio ended."); break
                if vid_proc.poll() is not None:
                    print("\r\n[*] Video ended."); break

                if viz_ffmpeg_proc is not None and viz_ffmpeg_proc.poll() is not None:
                    try:
                        verr = viz_ffmpeg_proc.stderr.read(4096).decode(errors="replace").strip()
                    except Exception:
                        verr = ""
                    print(f"\r\n[!] Viz ffmpeg exited.")
                    if verr: print(f"[!] ffmpeg viz: {verr[:300]}")
                    viz_ffmpeg_proc = None
                    write_byte(COLOR_FLAG, _cflag_map[state["cam_color"]])
                    write_mem(_cam_ext_col, _top_colors(state["cam_color"]))

                if state["color_pending"]:
                    state["color_pending"] = False
                    if not viz_ffmpeg_proc:
                        write_byte(COLOR_FLAG, _cflag_map[state["cam_color"]])
                        write_mem(_cam_ext_col, _top_colors(state["cam_color"]))

                if state["viz_pending"] is not None and _blend_capable:
                    new_viz = state["viz_pending"]
                    state["viz_pending"] = None
                    _cur_blend = blend_viz_mode or "none"
                    if new_viz != _cur_blend:
                        sys.stdout.write(
                            f"\r\n[*] Switching viz: {VIZ_LABELS.get(_cur_blend, _cur_blend)}"
                            f" -> {VIZ_LABELS.get(new_viz, new_viz)}\r\n")
                        sys.stdout.flush()

                        if viz_ffmpeg_proc is not None:
                            _old_viz = viz_ffmpeg_proc
                            _kill_proc(_old_viz)
                            try: _old_viz.stdout.close()
                            except: pass
                            time.sleep(0.1)  # let _read_viz thread see EOF and exit

                        if new_viz == "none":
                            viz_ffmpeg_proc = None
                            blend_viz_mode  = None
                            VIZ_MODE        = "none"
                            write_byte(COLOR_FLAG, _cflag_map[state["cam_color"]])
                            write_mem(_cam_ext_col, _top_colors(state["cam_color"]))
                        else:
                            blend_viz_mode = new_viz
                            VIZ_MODE = new_viz  # _build_viz_filter() reads this; stays set
                            _viz_chars_def = {
                                "showwaves":    CHARS_DEF,
                                "showfreqs":    CHARS_FREQ_DEF,
                                "avectorscope": CHARS_SCOPE_DEF,
                                "showspectrum": CHARS_SPECTRUM_DEF,
                            }.get(blend_viz_mode, CHARS_HIST_DEF)
                            _viz_chars = [t[0] for t in _viz_chars_def]
                            _viz_wlut, _viz_flut = _build_color_luts(_viz_chars_def)

                            if is_url(filepath):
                                _kill_proc(yt_viz_proc)
                                yt_viz_proc, viz_ffmpeg_proc = start_ffmpeg_waveform_stream(
                                    stream_url, height=cam_height)
                            else:
                                viz_ffmpeg_proc = start_ffmpeg_waveform_file(
                                    filepath, height=cam_height)

                            write_byte(COLOR_FLAG, 5)  # re-enable per-pixel color mode
                            threading.Thread(target=_read_viz, daemon=True).start()

                            time.sleep(0.1)
                            if viz_ffmpeg_proc.poll() is not None:
                                try:
                                    err = viz_ffmpeg_proc.stderr.read(512).decode(
                                        errors="replace").strip()
                                except Exception:
                                    err = ""
                                sys.stdout.write(
                                    f"\r\n[!] New viz ffmpeg exited immediately: {err}\r\n")
                                sys.stdout.flush()

                with vid_frame_lock:
                    raw = latest_vid_frame[0]
                    latest_vid_frame[0] = None
                if raw is None:
                    time.sleep(0.010)
                    continue

                top_raw = raw[:cam_ext_rows * WIDTH]
                bot_raw = raw[cam_ext_rows * WIDTH:]

                if viz_ffmpeg_proc:
                    with viz_lock:
                        vz_top = last_viz_frame[:cam_ext_rows * WIDTH]
                        vz_bot = last_viz_frame[cam_ext_rows * WIDTH:]
                    blended_top = bytes(max(c, v) for c, v in zip(top_raw, vz_top))
                    blended_bot = bytes(max(c, v) for c, v in zip(bot_raw, vz_bot))
                    cam_cmode = state["cam_color"]
                    viz_cmode = state["viz_color"]
                    color_top = bytearray(cam_ext_rows * WIDTH)
                    for i in range(cam_ext_rows * WIDTH):
                        col_x = i % WIDTH
                        cp, vp = top_raw[i], vz_top[i]
                        if cp >= vp:
                            color_top[i] = _pixel_color(cp, CHARS_CAMERA, cam_cmode, col_x,
                                                        _cam_wlut, _cam_flut, RAINBOW_TAB)
                        else:
                            color_top[i] = _pixel_color(vp, _viz_chars, viz_cmode, col_x,
                                                        _viz_wlut, _viz_flut, RAINBOW_TAB)
                    color_bot = bytearray(HEIGHT * WIDTH)
                    for i in range(HEIGHT * WIDTH):
                        col_x = i % WIDTH
                        cp, vp = bot_raw[i], vz_bot[i]
                        if cp >= vp:
                            color_bot[i] = _pixel_color(cp, CHARS_CAMERA, cam_cmode, col_x,
                                                        _cam_wlut, _cam_flut, RAINBOW_TAB)
                        else:
                            color_bot[i] = _pixel_color(vp, _viz_chars, viz_cmode, col_x,
                                                        _viz_wlut, _viz_flut, RAINBOW_TAB)
                else:
                    blended_top = top_raw
                    blended_bot = bot_raw

                bot_screen = bytes(pixel_to_char(p, CHARS_CAMERA) for p in blended_bot)
                write_mem(FRAME_BUF, bot_screen)
                write_byte(FRAME_FLAG, 1)

                top_screen = bytes(pixel_to_char(p, CHARS_CAMERA) for p in blended_top)
                write_mem(_cam_ext_scr, top_screen)
                if viz_ffmpeg_proc:
                    write_mem(_cam_ext_col, color_top)
                    write_mem(WAVE_COL_ADDR, color_bot)
                else:
                    write_mem(_cam_ext_col, _top_colors(state["cam_color"], top_screen))

                if petscii_queue is not None:
                    sc_top = top_screen
                    sc_bot = bot_screen
                    if viz_ffmpeg_proc:
                        col_top = bytes(color_top)
                        col_bot = bytes(color_bot)
                    else:
                        cmode = state["cam_color"]
                        col_top = bytes(_screen_code_color(sc_top[i], cmode, i % WIDTH,
                                        _cam_wlut, _cam_flut, RAINBOW_TAB) for i in range(len(sc_top)))
                        col_bot = bytes(_screen_code_color(sc_bot[i], cmode, i % WIDTH,
                                        _cam_wlut, _cam_flut, RAINBOW_TAB) for i in range(len(sc_bot)))
                    _tlen = len(_prec_ticker_petscii)
                    _tpos = int((time.time() - _prec_ticker_t0) * 50.0 / SCROLL_RATE) % _tlen
                    ticker_sc  = bytes(_prec_ticker_petscii[(_tpos + i) % _tlen] for i in range(WIDTH))
                    ticker_col = bytes([13] * WIDTH)
                    all_sc  = bytes([0x20] * WIDTH) + ticker_sc  + sc_top  + sc_bot
                    all_col = bytes([0]    * WIDTH) + ticker_col + col_top + col_bot
                    try:
                        petscii_queue.put_nowait((all_sc, all_col))
                    except queue.Full:
                        pass

                frame_num += 1
                if blend_viz_mode and viz_ffmpeg_proc:
                    vid_ind = ["R","W","F"][state["cam_color"]]
                    viz_ind = ["R","W","F"][state["viz_color"]]
                    ind = f"vid:{vid_ind} viz:{viz_ind}"
                else:
                    ind = ["R","W","F"][state["cam_color"]]
                vfc  = viz_frame_count[0] if viz_ffmpeg_proc else -1
                vsuf = f" viz={vfc:05d}" if vfc >= 0 else ""
                print(f"\r[*] Frame {frame_num:05d} [{ind}]{vsuf}", end="", flush=True)

        except KeyboardInterrupt:
            print("\r\n[*] Interrupted.")
        finally:
            state["quit"] = True
            if petscii_queue is not None:
                petscii_queue.put(None)
                if petscii_thread is not None:
                    petscii_thread.join(timeout=60)
            if petscii_recorder is not None:
                try:
                    petscii_recorder.stdin.close()
                    petscii_recorder.wait(timeout=120)
                except Exception:
                    try: petscii_recorder.kill()
                    except Exception: pass
            if _prec_tmp_video and _prec_tmp_audio:
                if yt_petscii_audio is not None:
                    try: yt_petscii_audio.wait(timeout=60)
                    except Exception: pass
                print("\r\n[*] Muxing PETSCII audio+video...")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", _prec_tmp_video, "-i", _prec_tmp_audio,
                     "-c:v", "copy", "-c:a", "aac", "-shortest", args.save_petscii],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _f in (_prec_tmp_video, _prec_tmp_audio):
                    try: os.unlink(_f)
                    except Exception: pass
            elif yt_petscii_audio is not None:
                try: yt_petscii_audio.terminate(); yt_petscii_audio.wait(timeout=3)
                except Exception: pass
            print("\r\n[*] PETSCII recording saved.")
            write_mem(0xD400, [0] * 25)
            write_byte(FRAME_FLAG, 0)
            write_mem(FRAME_BUF,    [0x20] * (WIDTH * HEIGHT))
            write_mem(_cam_ext_scr, [0x20] * (cam_ext_rows * WIDTH))
            write_mem(_cam_ext_col, [0x00] * (cam_ext_rows * WIDTH))
            write_mem(TICKER_ROW, [0x20] * 40)
            def _stop(proc):
                if proc is None: return
                try:
                    proc.terminate(); proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                except Exception:
                    pass
            _stop(vid_proc)
            for p in procs: _stop(p)
            try: os.remove(FIFO_PATH)
            except: pass
            if "_term_fd" in state and "_term_old" in state:
                try:
                    termios.tcsetattr(state["_term_fd"], termios.TCSADRAIN, state["_term_old"])
                except Exception:
                    pass
            print("[*] Done.")
        return

    # -------------------------------------------------------------------------
    # Normal file / stream mode
    # -------------------------------------------------------------------------
    if args.yt_search:
        query = args.yt_search.strip()
        if not query:
            print("[!] --yt-search requires a non-empty query")
            sys.exit(1)
        print(f"[*] Searching YouTube: {query} (max {args.yt_max})")
        candidates = youtube_search(query, args.yt_max)
        if not candidates:
            print("[!] No YouTube results found")
            sys.exit(1)
        chosen = choose_youtube_result(candidates)
        if not chosen:
            print("[*] Search cancelled.")
            sys.exit(0)
        filepath = chosen
        print(f"[*] Selected: {filepath}")
    else:
        filepath = os.path.expanduser(args.file) if args.file else \
                   os.path.expanduser(input("Audio/SID file path: ").strip())

    if not is_url(filepath) and not os.path.isfile(filepath):
        print(f"[!] File not found: {filepath}"); sys.exit(1)

    U64 = f"http://{args.ip}"
    FPS = args.fps

    if args.showwaves:
        VIZ_MODE = "showwaves"
    elif args.showfreqs:
        VIZ_MODE = "showfreqs"
    elif args.avectorscope:
        VIZ_MODE = "avectorscope"
    elif args.showspectrum:
        VIZ_MODE = "showspectrum"
    elif args.ahistogram:
        VIZ_MODE = "ahistogram"
    else:
        ans = input("Visualization? [0=waveform, 1=spectrum, 2=scope, 3=spectrogram, 4=histogram] (default 0): ").strip()
        if ans == "1":   VIZ_MODE = "showfreqs"
        elif ans == "2": VIZ_MODE = "avectorscope"
        elif ans == "3": VIZ_MODE = "showspectrum"
        elif ans == "4": VIZ_MODE = "ahistogram"
        else:            VIZ_MODE = "showwaves"
    print(f"[*] Viz mode: {VIZ_MODE}")

    if args.color:      color_mode_init = 0
    elif args.no_color: color_mode_init = 1
    else:
        ans = input("Color mode? [0=rainbow, 1=white, 2=fire] (default 0): ").strip()
        color_mode_init = int(ans) if ans in ("0","1","2") else 0

    mode = detect_mode(filepath, force_sid=args.sid, force_audio=args.audio)

    # Determine audio destination for SID files only
    c64_audio = False
    if mode == "sid":
        if args.c64audio:
            c64_audio = True
        elif args.macaudio:
            c64_audio = False
        else:
            ans = input("Audio output? [m=local/sidplayfp (default), c=C64]: ").strip().lower()
            c64_audio = ans in ("c", "c64")
        print(f"[*] SID audio: {'C64 hardware (PSID player)' if c64_audio else 'local (sidplayfp)'}")

    # Get and display metadata
    if mode == "sid":
        info = get_sid_info(filepath)
    elif mode == "stream":
        info = get_stream_info(filepath)
        if info is None:
            if get_service(filepath) == "spotify":
                print("[!] Failed to fetch stream metadata — is yt-dlp installed and the URL valid?")
                sys.exit(1)
            info = {}
            print("[!] Metadata unavailable — continuing without track info.")
    else:
        info = get_audio_info(filepath)
    display_mode = "sid" if mode == "sid" else "audio"
    show_info_header(info, display_mode, filepath)
    ticker_str = build_ticker_string(info, display_mode)

    # Resolve the actual stream URL (Spotify → YouTube search; others unchanged)
    stream_url = filepath
    if mode == "stream" and get_service(filepath) == "spotify":
        stream_url = resolve_stream_url(filepath, info)
        if not stream_url:
            print("[!] Could not find a YouTube match for this Spotify track")
            sys.exit(1)

    if not os.path.isfile(PRG_LOCAL):
        print(f"[!] {PRG_REMOTE} not found at {PRG_LOCAL}")
        print(f"    Build: 64tass -a -B -o sidviz.prg sidviz.asm")
        sys.exit(1)
    print(f"[*] {PRG_REMOTE}: {os.path.getsize(PRG_LOCAL)} bytes")

    if mode == "sid":
        _ow = _psid_row_overlap(filepath)
        if _ow:
            print(f"\n[!] WARNING: SID binary {_ow}.")
            print("[!] The visualizer will still write rows 2-7, but those rows may show")
            print("[!] SID driver bytes rendered as PETSCII artifacts.")
            ans = input("Continue anyway? [y/N]: ").strip().lower()
            if ans != "y":
                print("[*] Aborted.")
                sys.exit(0)

    if not smoke_test(): sys.exit(1)

    print("[*] Rebooting C64...")
    u64_put("machine:reboot")
    time.sleep(4.0)

    print(f"[*] Uploading {PRG_REMOTE}...")
    if not ftp_upload(PRG_LOCAL, PRG_REMOTE): sys.exit(1)

    # For C64 audio: parse PSID before running PRG so we're ready to upload immediately
    sid_audio_proc    = None
    ffplay_proc       = None
    sid_duration_secs = None
    psid              = None
    procs             = []

    if mode == "sid":
        raw_len = info.get("Song Length", "")
        if raw_len:
            try:
                parts = raw_len.split(".")[0].split(":")
                sid_duration_secs = int(parts[0]) * 60 + int(parts[1])
                print(f"[*] SID duration: {raw_len} = {sid_duration_secs}s")
            except Exception:
                pass

        if c64_audio:
            psid = parse_psid(filepath)
            if not psid:
                print("[!] PSID parse failed, falling back to local audio")
                c64_audio = False

    # Run PRG — PRG clears $C002 in init, so no stale values from previous runs
    print(f"[*] Running {PRG_REMOTE}...")
    if not run_prg_from_temp(PRG_REMOTE): sys.exit(1)
    # If C64 audio: write $C002=$01 IMMEDIATELY after run_prg_from_temp
    # PRG clears $C002 at start of init, then spends ~300ms on display setup
    # before checking it — plenty of time for us to set it
    # local/MP3 modes: never write $C002, it stays $00 after PRG clears it
    if c64_audio:
        print("[*] Signalling C64 audio mode to PRG ($C002=1)...")
        write_byte(C64_AUDIO_FLAG, 1)

    time.sleep(1.0)  # wait for PRG to finish init

    write_color_tables()
    print("[*] Color tables written ($C3A8/$C428).")

    # Force PAL 50Hz CIA1 timer A — only needed in C64 audio mode
    # where the SID play routine expects PAL timing.
    # For local/MP3 modes the KERNAL timer is already correct.
    if c64_audio:
        if psid.get("clock") == 2:  # NTSC-only: 1022727 / 60 = 17045 = $4295
            print("[*] NTSC SID — setting CIA1 timer for 60Hz...")
            write_mem(0xDC04, [0x95, 0x42])
        else:                       # PAL / unknown / both: 985248 / 50 = 19704 = $4CF8
            print("[*] Setting CIA1 timer for PAL 50Hz...")
            write_mem(0xDC04, [0xF8, 0x4C])
        write_byte(0xDC0E, 0x11)           # start timer A continuous, force reload

    # Send initial color mode and ticker
    _cflag_map = {0: 2, 1: 1, 2: 3}
    write_byte(COLOR_FLAG, _cflag_map[color_mode_init])
    send_ticker(ticker_str)

    # Rows 2-7: always extend the visualizer to fill the full screen.
    _EXT_ROWS    = 6
    _DISP_HEIGHT = HEIGHT + _EXT_ROWS   # 23 rows total (rows 2-24)
    _ext_scr     = 0x0450               # screen RAM rows 2-7
    _ext_col     = 0xD850               # color RAM rows 2-7

    # Build per-character color LUTs for the extended rows (mirrors write_color_tables defs).
    _ext_defs = {
        "showwaves": CHARS_DEF, "showfreqs": CHARS_FREQ_DEF,
        "avectorscope": CHARS_SCOPE_DEF, "showspectrum": CHARS_SPECTRUM_DEF,
    }.get(VIZ_MODE, CHARS_HIST_DEF)
    _ext_wlut, _ext_flut = _build_color_luts(_ext_defs)

    def _ext_top_colors(cmode, screen=None):
        if cmode == 0:
            return bytes(RAINBOW_TAB[col] for _ in range(_EXT_ROWS) for col in range(WIDTH))
        lut, default = (_ext_wlut, 1) if cmode == 1 else (_ext_flut, 8)
        if screen is None:
            return [default] * (_EXT_ROWS * WIDTH)
        return bytes(lut.get(sc, default) for sc in screen)

    # Initialize rows 2-7 with spaces and set color RAM (3× for SRAM reliability)
    write_mem(_ext_scr, [0x20] * (_EXT_ROWS * WIDTH))
    for _ in range(3):
        write_mem(_ext_col, _ext_top_colors(color_mode_init))
        time.sleep(0.05)

    # Start audio/waveform processes
    sid_fifo_proc = None
    yt_viz_proc   = None
    if mode == "sid":
        make_fifo(FIFO_PATH)
        if c64_audio:
            # Upload first — C64 starts playing at the end of upload_sid_to_c64.
            # Then start sidplayfp so the waveform is in sync with the C64.
            # -re flag throttles ffmpeg to real-time speed so the pipeline
            # doesn't race ahead of the C64's real-time playback.
            ffmpeg_proc   = start_ffmpeg_waveform_fifo(realtime=True, height=_DISP_HEIGHT)
            upload_sid_to_c64(psid)
            sid_fifo_proc = start_sidplayfp_fifo(filepath, sid_duration_secs)
            procs = [sid_fifo_proc, ffmpeg_proc]
        else:
            ffmpeg_proc   = start_ffmpeg_waveform_fifo(realtime=True, height=_DISP_HEIGHT)
            time.sleep(0.3)           # give ffmpeg time to open FIFO before sidplayfp writes
            sid_fifo_proc  = start_sidplayfp_fifo(filepath, sid_duration_secs)
            sid_audio_proc = start_sidplayfp_audio(filepath, sid_duration_secs)
            procs = [sid_fifo_proc, sid_audio_proc, ffmpeg_proc]
    elif mode == "stream":
        yt_viz_proc,   ffmpeg_proc = start_ffmpeg_waveform_stream(stream_url, height=_DISP_HEIGHT)
        yt_audio_proc, ffplay_proc = start_ffplay_stream(stream_url)
        procs = [yt_viz_proc, ffmpeg_proc, yt_audio_proc, ffplay_proc]
        if args.save:
            print(f"[*] Saving stream to: {args.save}")
            save_proc = subprocess.Popen(
                ["yt-dlp", "-q", "-x", "--audio-format", "mp3"] + _YTDLP_COOKIE_ARGS + ["-o", args.save, stream_url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(save_proc)
    else:
        # MP3/audio mode — $C002 stays $00, PRG already in main loop
        ffmpeg_proc = start_ffmpeg_waveform_file(filepath, height=_DISP_HEIGHT)
        ffplay_proc = start_ffplay_audio(filepath)
        procs = [ffplay_proc, ffmpeg_proc]

    playback_start = time.time()

    frame_size     = WIDTH * _DISP_HEIGHT
    sid_end_time   = (time.time() + sid_duration_secs) if mode == "sid" and sid_duration_secs else None
    state          = {"cam_color": color_mode_init, "viz_color": 2,
                      "color_pending": False, "viz_pending": None, "quit": False}
    kthread        = make_keypress_listener(state)
    kthread.start()

    frame_num = 0
    print(f"[*] Streaming to C64 at {FPS}fps -- [c] color, [w/0-5] viz (0=off), [q] quit\n")

    try:
        while not state["quit"]:
            # Stop when audio process finishes
            if sid_audio_proc is not None and sid_audio_proc.poll() is not None:
                print("\r\n[*] Song ended."); break
            if ffplay_proc is not None and ffplay_proc.poll() is not None:
                print("\r\n[*] Song ended."); break
            # Stop at parsed song duration (wall clock override for SONGLENGTHS cutoff)
            if sid_end_time is not None and time.time() >= sid_end_time:
                print("\r\n[*] Song ended."); break

            if state["color_pending"]:
                state["color_pending"] = False
                write_byte(COLOR_FLAG, _cflag_map[state["cam_color"]])
                write_mem(_ext_col, _ext_top_colors(state["cam_color"]))

            if state["viz_pending"] is not None:
                new_viz = state["viz_pending"]
                state["viz_pending"] = None
                if new_viz != VIZ_MODE:
                    sys.stdout.write(
                        f"\r\n[*] Switching viz: {VIZ_LABELS.get(VIZ_MODE, VIZ_MODE)}"
                        f" -> {VIZ_LABELS.get(new_viz, new_viz)}\r\n")
                    sys.stdout.flush()

                    _old_ffmpeg = ffmpeg_proc
                    _kill_proc(_old_ffmpeg)
                    if _old_ffmpeg is not None:
                        try: _old_ffmpeg.stdout.close()
                        except: pass

                    VIZ_MODE = new_viz

                    if new_viz == "none":
                        if mode == "sid":
                            _old_sid = sid_fifo_proc
                            _kill_proc(_old_sid)
                            sid_fifo_proc = None
                            procs = [p for p in procs if p not in (_old_ffmpeg, _old_sid)]
                        elif mode == "stream":
                            _old_yt = yt_viz_proc
                            _kill_proc(_old_yt)
                            yt_viz_proc = None
                            procs = [p for p in procs if p not in (_old_ffmpeg, _old_yt)]
                        else:
                            procs = [p for p in procs if p is not _old_ffmpeg]
                        ffmpeg_proc = None
                        write_mem(FRAME_BUF, [0x20] * (WIDTH * HEIGHT))
                        write_mem(_ext_scr,  [0x20] * (_EXT_ROWS * WIDTH))
                        write_byte(FRAME_FLAG, 1)
                    else:
                        # Rebuild LUTs for the new mode
                        _ext_defs = {
                            "showwaves":    CHARS_DEF,
                            "showfreqs":    CHARS_FREQ_DEF,
                            "avectorscope": CHARS_SCOPE_DEF,
                            "showspectrum": CHARS_SPECTRUM_DEF,
                        }.get(VIZ_MODE, CHARS_HIST_DEF)
                        _ext_wlut, _ext_flut = _build_color_luts(_ext_defs)

                        if mode == "sid":
                            _old_sid = sid_fifo_proc
                            _kill_proc(_old_sid)
                            make_fifo(FIFO_PATH)
                            ffmpeg_proc   = start_ffmpeg_waveform_fifo(
                                realtime=True, height=_DISP_HEIGHT)
                            time.sleep(0.3)
                            sid_fifo_proc = start_sidplayfp_fifo(filepath, sid_duration_secs)
                            procs = [p for p in procs
                                     if p not in (_old_ffmpeg, _old_sid)] + [ffmpeg_proc, sid_fifo_proc]
                        elif mode == "stream":
                            _old_yt = yt_viz_proc
                            _kill_proc(_old_yt)
                            yt_viz_proc, ffmpeg_proc = start_ffmpeg_waveform_stream(
                                stream_url, height=_DISP_HEIGHT)
                            procs = [p for p in procs
                                     if p not in (_old_ffmpeg, _old_yt)] + [yt_viz_proc, ffmpeg_proc]
                        else:
                            _elapsed = time.time() - playback_start
                            ffmpeg_proc = start_ffmpeg_waveform_file(
                                filepath, height=_DISP_HEIGHT, seek_secs=_elapsed)
                            procs = [p for p in procs if p is not _old_ffmpeg] + [ffmpeg_proc]

                        time.sleep(0.1)
                        if ffmpeg_proc.poll() is not None:
                            err = ffmpeg_proc.stderr.read(512).decode(errors="replace").strip()
                            sys.stdout.write(f"\r\n[!] New ffmpeg exited immediately: {err}\r\n")
                            sys.stdout.flush()

            # Viz is off — idle until user switches back
            if ffmpeg_proc is None:
                time.sleep(0.05)
                continue

            # Check if data available before blocking read (allows q to work)
            ready, _, _ = _select.select([ffmpeg_proc.stdout], [], [], 0.5)
            if not ready:
                continue

            # Blocking read — ffmpeg throttles to FPS naturally via FIFO or file rate
            raw = ffmpeg_proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                print("\r\n[*] Stream ended."); break

            top_raw = raw[:_EXT_ROWS * WIDTH]
            bot_raw = raw[_EXT_ROWS * WIDTH:]

            if VIZ_MODE == "showfreqs":
                raw_grad = _apply_freq_gradient(raw, state["cam_color"], height=_DISP_HEIGHT)
                top_raw  = raw_grad[:_EXT_ROWS * WIDTH]
                bot_raw  = raw_grad[_EXT_ROWS * WIDTH:]
                top_screen = bytes(pixel_to_char(p, CHARS_FREQ) for p in top_raw)
                bot_screen = bytes(pixel_to_char(p, CHARS_FREQ) for p in bot_raw)
            elif VIZ_MODE == "avectorscope":
                top_screen = bytes(pixel_to_char(p, CHARS_SCOPE) for p in top_raw)
                bot_screen = bytes(pixel_to_char(p, CHARS_SCOPE) for p in bot_raw)
            elif VIZ_MODE == "showspectrum":
                top_screen = bytes(pixel_to_char(p, CHARS_SPECTRUM) for p in top_raw)
                bot_screen = bytes(pixel_to_char(p, CHARS_SPECTRUM) for p in bot_raw)
            elif VIZ_MODE == "ahistogram":
                top_screen = bytes(pixel_to_char(p, CHARS_HIST) for p in top_raw)
                bot_screen = bytes(pixel_to_char(p, CHARS_HIST) for p in bot_raw)
            else:
                top_screen = bytes(pixel_to_char(p) for p in top_raw)
                bot_screen = bytes(pixel_to_char(p) for p in bot_raw)

            write_mem(_ext_scr, top_screen)
            write_mem(_ext_col, _ext_top_colors(state["cam_color"], top_screen))
            write_mem(FRAME_BUF, bot_screen)
            write_byte(FRAME_FLAG, 1)

            frame_num += 1
            ind  = ["R","W","F"][state["cam_color"]]
            vind = VIZ_LABELS.get(VIZ_MODE, VIZ_MODE)[:3].upper()
            print(f"\r[*] Frame {frame_num:05d} [{ind}|{vind}]", end="", flush=True)

    except KeyboardInterrupt:
        print("\r\n[*] Interrupted.")
    finally:
        state["quit"] = True
        if c64_audio:
            print("\r\n[*] Stopping SID and returning C64 to BASIC...")
            # Step 1: clear c64_audio_flag so the IRQ stops calling the SID
            # play routine at $C610 — without this the IRQ overwrites our
            # $D400 zeroes on every frame before do_quit can run.
            write_byte(C64_AUDIO_FLAG, 0)
            time.sleep(0.04)             # wait ~2 IRQ frames (50Hz = 20ms each)
            # Step 2: silence SID directly — IRQ is no longer touching $D400
            write_mem(0xD400, [0] * 25)
            # Step 3: signal PRG main loop to JMP $FCE2 (BASIC ready screen)
            write_byte(QUIT_FLAG, 1)
            time.sleep(0.3)
        else:
            # Local/MP3 mode: silence any residual SID output on C64 display side
            write_mem(0xD400, [0] * 25)
            write_byte(FRAME_FLAG, 0)
            write_mem(FRAME_BUF, [0x20] * (WIDTH * HEIGHT))
            write_mem(_ext_scr, [0x20] * (_EXT_ROWS * WIDTH))
            orig = u64_get("machine:readmem?address=F9&length=2")
            if orig and len(orig) == 2:
                u64_put("machine:writemem", {"address": "314",
                                              "data": f"{orig[0]:02X}{orig[1]:02X}"})
            write_mem(TICKER_ROW, [0x20] * 40)
        for p in procs:
            try: p.terminate()
            except: pass
        try: os.remove(FIFO_PATH)
        except: pass
        # Restore terminal
        if "_term_fd" in state and "_term_old" in state:
            try:
                termios.tcsetattr(state["_term_fd"], termios.TCSADRAIN, state["_term_old"])
            except Exception:
                pass
        print("[*] Done.")

if __name__ == "__main__":
    main()
