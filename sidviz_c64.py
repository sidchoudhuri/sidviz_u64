#!/usr/bin/env python3
"""
sidviz_u64.py -- SID/audio waveform visualizer -> C64 via U64 API
Experimental fork: plays SID audio on real C64 hardware via PSID player.

version 1.4.0-exp (2026-04-17-exp2)

Memory protocol:
  $C000     = frame flag  (Python writes 1, ASM clears to 0)
  $C001     = color flag  (2=rainbow, 1=white density, 3=fire density)
  $C002     = sid_ready   (Python sets 1 after uploading SID code)
  $C003     = sid_play_flag (IRQ sets 1, main loop calls play and clears)
  $C100     = frame buffer, 680 bytes PETSCII (rows 8-24, $C100-$C3A7)
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

VERSION = "1.6.0"
BUILD   = "2026-04-24"

import os, sys, time, subprocess, urllib.request, urllib.parse
import argparse, threading, termios, tty, re, json, select as _select, struct

FIFO_PATH    = "/tmp/sidpipe.wav"
WIDTH        = 40
HEIGHT       = 17              # rows 8-24 — protects SID driver at $0400-$04FF
FRAME_BUF    = 0xC100
FRAME_FLAG   = 0xC000
COLOR_FLAG   = 0xC001
C64_AUDIO_FLAG = 0xC002  # 0=off, 1=waiting, 2=SID ready
TICKER_BUF   = 0xC500
TICKER_LEN   = 0xC5FE
TICKER_ROW   = 0x0428          # screen RAM row 1
PRG_LOCAL    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidviz.prg")
PRG_REMOTE   = "sidviz.prg"
TIMEOUT      = 5.0
CHARS        = [32, 46, 58, 42, 35, 64]
SID_EXTS     = {".sid"}
U64          = ""
FPS          = 10

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
    p.add_argument("--version",  action="store_true",    help="Show version and exit")
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
    try:
        proc = subprocess.Popen(
            ["sidplayfp", "-v", filepath],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
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

def _spotify_info_oembed(url):
    """Fallback Spotify metadata via the public oEmbed API (no auth needed)."""
    try:
        oembed_url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url)
        with urllib.request.urlopen(oembed_url, timeout=10) as r:
            data = json.loads(r.read())
        info = {}
        title = data.get("title", "")
        # oEmbed title is sometimes "Track · Artist"
        if "·" in title:
            parts = title.split("·", 1)
            info["Title"]  = parts[0].strip()
            info["Artist"] = parts[1].strip()
        elif title:
            info["Title"] = title
        info["Format"]   = "Spotify"
        info["filename"] = url
        return info
    except Exception as e:
        print(f"[!] Spotify oEmbed fallback failed: {e}")
        return None

def get_stream_info(url):
    """Use yt-dlp to extract metadata from any supported streaming URL.
    For Spotify, falls back to the public oEmbed API if yt-dlp fails."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if not r.stdout.strip():
            raise ValueError(r.stderr.strip() or "no output from yt-dlp")
        data = json.loads(r.stdout)
    except Exception as e:
        print(f"[!] yt-dlp metadata failed: {e}")
        if get_service(url) == "spotify":
            print("[*] Trying Spotify oEmbed fallback...")
            return _spotify_info_oembed(url)
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
            ["yt-dlp", "--dump-json", "--no-playlist", f"ytsearch1:{query}"],
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

def start_ffmpeg_waveform_stream(url):
    """Stream audio via yt-dlp piped to ffmpeg for waveform generation."""
    yt_proc = subprocess.Popen(
        ["yt-dlp", "-f", "bestaudio", "-o", "-", "-q", "--no-playlist", url],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    cmd = ["ffmpeg", "-loglevel", "quiet", "-i", "pipe:0",
           "-filter_complex",
           f"[0:a]showwaves=s={WIDTH}x{HEIGHT}:mode=cline:rate={FPS}:colors=#ffffff,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdin=yt_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    yt_proc.stdout.close()  # let ffmpeg own the pipe; yt_proc gets SIGPIPE if ffmpeg exits early
    print("[*] ffmpeg waveform (stream) started.")
    return yt_proc, p

def start_ffplay_stream(url):
    """Stream audio via yt-dlp piped to ffplay."""
    yt_proc = subprocess.Popen(
        ["yt-dlp", "-f", "bestaudio", "-o", "-", "-q", "--no-playlist", url],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    p = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0"],
        stdin=yt_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
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

def start_ffmpeg_waveform_fifo():
    cmd = ["ffmpeg", "-loglevel", "quiet", "-f", "wav", "-i", FIFO_PATH,
           "-filter_complex",
           f"[0:a]showwaves=s={WIDTH}x{HEIGHT}:mode=cline:rate={FPS}:colors=#ffffff,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    print("[*] ffmpeg waveform (FIFO) started."); return p

def start_ffmpeg_waveform_file(filepath):
    cmd = ["ffmpeg", "-loglevel", "quiet",
           "-i", filepath,
           "-filter_complex",
           f"[0:a]showwaves=s={WIDTH}x{HEIGHT}:mode=cline:rate={FPS}:colors=#ffffff,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    print("[*] ffmpeg waveform (file) started."); return p

def start_ffplay_audio(filepath):
    p = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", filepath],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] ffplay audio started."); return p

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

def parse_psid(filepath):
    """Parse PSID/RSID header, return dict with load/init/play addresses and data."""
    with open(filepath, "rb") as f:
        raw = f.read()

    magic = raw[0:4]
    if magic not in (b"PSID", b"RSID"):
        print(f"[!] Not a PSID/RSID file (magic: {magic})")
        return None

    data_offset = struct.unpack_from(">H", raw, 6)[0]
    load_addr   = struct.unpack_from(">H", raw, 8)[0]
    init_addr   = struct.unpack_from(">H", raw, 10)[0]
    play_addr   = struct.unpack_from(">H", raw, 12)[0]
    sid_data    = raw[data_offset:]

    # Read title from header to confirm we're parsing the right file
    title = raw[0x16:0x36].rstrip(b"\x00").decode(errors="replace")
    author = raw[0x36:0x56].rstrip(b"\x00").decode(errors="replace")
    print(f"[*] PSID title: {title!r}  author: {author!r}")

    # If load_addr is 0, first 2 bytes of data are the load address (little-endian)
    if load_addr == 0:
        load_addr = struct.unpack_from("<H", sid_data, 0)[0]
        sid_data  = sid_data[2:]

    print(f"[*] PSID: load=${load_addr:04X} init=${init_addr:04X} play=${play_addr:04X} size={len(sid_data)} bytes")

    if play_addr == 0:
        print(f"[*] play_addr=0 — SID installs play via IRQ vector during init")
        print(f"[*] Python will read $0314/$0315 after init to get real play address")

    if load_addr <= 0x07E7 and (load_addr + len(sid_data)) >= 0x0400:
        print(f"[!] PSID driver overlaps screen RAM ($0400-$07E7) — rows 0-7 may show driver artifacts")

    return {"load_addr": load_addr, "init_addr": init_addr,
            "play_addr": play_addr, "data": sid_data}

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
        # Give PRG time to call init, which installs real play addr into $0314/$0315
        time.sleep(0.3)
        # Read back the real play address from $0314/$0315
        vec = u64_get("machine:readmem?address=314&length=2")
        if vec and len(vec) == 2:
            real_play = vec[0] | (vec[1] << 8)
            print(f"[*] SID installed play address: ${real_play:04X} — patching $C610...")
            write_mem(0xC610, [0x4C, real_play & 0xFF, (real_play >> 8) & 0xFF])
        else:
            print(f"[!] Could not read $0314 — play may not work")

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
                    state["color_mode"]    = (state["color_mode"] + 1) % 3
                    state["color_pending"] = True
                    label = ["rainbow", "white", "fire"][state["color_mode"]]
                    sys.stdout.write(f"\r\n[*] Color -> {label}\r\n")
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

def pixel_to_char(val):
    return CHARS[val * (len(CHARS) - 1) // 255]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global U64, FPS

    args = parse_args()

    if args.version:
        print(f"sidviz_u64  v{VERSION}  build {BUILD}")
        sys.exit(0)

    filepath = os.path.expanduser(args.file) if args.file else \
               os.path.expanduser(input("Audio/SID file path: ").strip())

    if not is_url(filepath) and not os.path.isfile(filepath):
        print(f"[!] File not found: {filepath}"); sys.exit(1)

    U64 = f"http://{args.ip}"
    FPS = args.fps

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
            print("[!] Failed to fetch stream metadata — is yt-dlp installed and the URL valid?")
            sys.exit(1)
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

    # Force PAL 50Hz CIA1 timer A — only needed in C64 audio mode
    # where the SID play routine expects PAL timing.
    # For local/MP3 modes the KERNAL timer is already correct.
    if c64_audio:
        print("[*] Setting CIA1 timer for PAL 50Hz...")
        write_mem(0xDC04, [0xF8, 0x4C])   # timer A latch lo=$F8, hi=$4C
        write_byte(0xDC0E, 0x11)           # start timer A continuous, force reload

    # Send initial color mode and ticker
    _cflag_map = {0: 2, 1: 1, 2: 3}
    write_byte(COLOR_FLAG, _cflag_map[color_mode_init])
    send_ticker(ticker_str)

    # Start audio/waveform processes
    if mode == "sid":
        make_fifo(FIFO_PATH)
        ffmpeg_proc   = start_ffmpeg_waveform_fifo()
        time.sleep(0.3)
        sid_fifo_proc = start_sidplayfp_fifo(filepath, sid_duration_secs)

        if c64_audio:
            # Upload SID code + write trampolines + write $C002=$02 to release PRG
            upload_sid_to_c64(psid)
            procs = [sid_fifo_proc, ffmpeg_proc]
        else:
            # local audio — $C002 stays $00, PRG already in main loop
            sid_audio_proc = start_sidplayfp_audio(filepath, sid_duration_secs)
            procs = [sid_fifo_proc, sid_audio_proc, ffmpeg_proc]
    elif mode == "stream":
        yt_viz_proc,   ffmpeg_proc = start_ffmpeg_waveform_stream(stream_url)
        yt_audio_proc, ffplay_proc = start_ffplay_stream(stream_url)
        procs = [yt_viz_proc, ffmpeg_proc, yt_audio_proc, ffplay_proc]
        if args.save:
            print(f"[*] Saving stream to: {args.save}")
            save_proc = subprocess.Popen(
                ["yt-dlp", "-q", "-x", "--audio-format", "mp3", "-o", args.save, stream_url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(save_proc)
    else:
        # MP3/audio mode — $C002 stays $00, PRG already in main loop
        ffmpeg_proc = start_ffmpeg_waveform_file(filepath)
        ffplay_proc = start_ffplay_audio(filepath)
        procs = [ffplay_proc, ffmpeg_proc]

    frame_size   = WIDTH * HEIGHT
    sid_end_time = (time.time() + sid_duration_secs) if mode == "sid" and sid_duration_secs else None
    state        = {"color_mode": color_mode_init, "color_pending": False, "quit": False}
    kthread      = make_keypress_listener(state)
    kthread.start()

    frame_num = 0
    print(f"[*] Streaming to C64 at {FPS}fps -- [c] color, [q] quit\n")

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
                write_byte(COLOR_FLAG, _cflag_map[state["color_mode"]])

            # Check if data available before blocking read (allows q to work)
            ready, _, _ = _select.select([ffmpeg_proc.stdout], [], [], 0.5)
            if not ready:
                continue

            # Blocking read — ffmpeg throttles to FPS naturally via FIFO or file rate
            raw = ffmpeg_proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                print("\r\n[*] Stream ended."); break

            screen = bytes(pixel_to_char(p) for p in raw)
            write_mem(FRAME_BUF, screen)
            write_byte(FRAME_FLAG, 1)

            frame_num += 1
            ind = ["R","W","F"][state["color_mode"]]
            print(f"\r[*] Frame {frame_num:05d} [{ind}]", end="", flush=True)

    except KeyboardInterrupt:
        print("\r\n[*] Interrupted.")
    finally:
        state["quit"] = True
        # Silence SID chip first — clears gate bits, waveforms, and volume on
        # all three voices so the last note doesn't ring on after we stop
        if c64_audio:
            write_mem(0xD400, [0] * 25)
        for p in procs:
            try: p.terminate()
            except: pass
        # Clean up display
        write_byte(FRAME_FLAG, 0)
        write_mem(FRAME_BUF, [0x20] * (WIDTH * HEIGHT))
        # Restore original IRQ vector (saved at $F9/$FA by PRG) to stop ticker
        orig = u64_get("machine:readmem?address=F9&length=2")
        if orig and len(orig) == 2:
            u64_put("machine:writemem", {"address": "314",
                                          "data": f"{orig[0]:02X}{orig[1]:02X}"})
        write_mem(TICKER_ROW, [0x20] * 40)
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
