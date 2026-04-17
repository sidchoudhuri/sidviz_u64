#!/usr/bin/env python3
"""
sidviz_c64.py -- SID/audio waveform visualizer -> C64 via U64 API
Uses sidviz.prg (from sidviz.asm) running on C64 as display driver.

version 1.4.0-exp (2026-04-17-exp1)

Memory protocol:
  $C000     = frame flag  (Python writes 1, ASM clears to 0)
  $C001     = color flag  (Python writes 1=white, 2=rainbow, ASM applies+clears)
  $C100     = frame buffer, 920 bytes PETSCII (rows 2-24, $C100-$C497)
  $C500     = ticker buffer, up to 253 PETSCII chars
  $C5FE     = ticker length (Python writes)

Usage:
  1. Assemble: 64tass -a -B -o sidviz.prg sidviz.asm
  2. Run: python3 sidviz_c64.py [file]
"""

VERSION = "1.4.0-exp"
BUILD   = "2026-04-17-exp1"

import os, sys, time, subprocess, urllib.request, urllib.parse
import argparse, threading, termios, tty, re, json, select as _select

FIFO_PATH    = "/tmp/sidpipe.wav"

# EXP: waveform starts at row 8 to protect SID driver at $0400-$04FF
# 17 rows * 40 = 680 bytes
HEIGHT       = 17

# ---------------------------------------------------------------------------
# PSID header parser
# ---------------------------------------------------------------------------

def parse_psid(filepath):
    """
    Parse PSID/RSID header. Returns dict with:
      load_addr, init_addr, play_addr, data (raw 6502 code bytes)
    Returns None if not a valid PSID file.
    """
    import struct
    with open(filepath, "rb") as f:
        raw = f.read()

    magic = raw[0:4]
    if magic not in (b"PSID", b"RSID"):
        print(f"[!] Not a PSID/RSID file (magic: {magic})")
        return None

    version    = struct.unpack_from(">H", raw, 4)[0]
    data_offset= struct.unpack_from(">H", raw, 6)[0]
    load_addr  = struct.unpack_from(">H", raw, 8)[0]
    init_addr  = struct.unpack_from(">H", raw, 10)[0]
    play_addr  = struct.unpack_from(">H", raw, 12)[0]

    sid_data = raw[data_offset:]

    # If load_addr is 0, first 2 bytes of data are the load address (LE)
    if load_addr == 0:
        load_addr = struct.unpack_from("<H", sid_data, 0)[0]
        sid_data  = sid_data[2:]

    print(f"[*] PSID: load=${load_addr:04X} init=${init_addr:04X} play=${play_addr:04X} size={len(sid_data)} bytes")

    # Warn about screen RAM conflict
    if load_addr <= 0x07E7 and (load_addr + len(sid_data)) >= 0x0400:
        print(f"[!] PSID driver overlaps screen RAM ($0400-$07E7) — display may corrupt")

    return {
        "load_addr":  load_addr,
        "init_addr":  init_addr,
        "play_addr":  play_addr,
        "data":       sid_data,
    }

def upload_sid_to_c64(psid):
    """
    Upload PSID code to C64 RAM via writemem.
    Patch play address into ZP $F7/$F8.
    Write JMP initAddress trampoline at $C600.
    """
    load_addr = psid["load_addr"]
    init_addr = psid["init_addr"]
    play_addr = psid["play_addr"]
    data      = psid["data"]

    print(f"[*] Uploading SID code ({len(data)} bytes) to ${load_addr:04X}...")
    write_mem(load_addr, data)

    # Write JMP initAddress trampoline at $C600 ($4C = JMP absolute)
    print(f"[*] Writing init trampoline at $C600 -> ${init_addr:04X}...")
    write_mem(0xC600, [0x4C, init_addr & 0xFF, (init_addr >> 8) & 0xFF])

    # Write JMP playAddress trampoline at $C610 — no ZP, SID can't clobber it
    print(f"[*] Writing play trampoline at $C610 -> ${play_addr:04X}...")
    write_mem(0xC610, [0x4C, play_addr & 0xFF, (play_addr >> 8) & 0xFF])

    # Signal PRG that SID is ready — PRG is spinning on $C002
    write_byte(SID_READY, 1)
    print(f"[*] SID uploaded and patched — signalling PRG.")
WIDTH        = 40
HEIGHT       = 23              # rows 2-24
FRAME_BUF    = 0xC100
FRAME_FLAG   = 0xC000
COLOR_FLAG   = 0xC001
SID_READY    = 0xC002    # exp: Python sets 1 after uploading SID
TICKER_BUF   = 0xC500
TICKER_LEN   = 0xC5FE
TICKER_ROW   = 0x0428          # screen RAM row 1
PRG_LOCAL    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidviz_exp.prg")
PRG_REMOTE   = "sidviz_exp.prg"
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
        prog="sidviz_c64",
        description=f"SID/audio waveform visualizer for C64 via U64 API  v{VERSION} build {BUILD}"
    )
    p.add_argument("file",       nargs="?",              help="Audio/SID file")
    p.add_argument("--ip",       default="192.168.2.64", help="U64 IP address")
    p.add_argument("--color",    action="store_true",    help="Start with rainbow color")
    p.add_argument("--no-color", action="store_true",    help="Start with flat white")
    p.add_argument("--sid",      action="store_true",    help="Force sidplayfp mode")
    p.add_argument("--audio",    action="store_true",    help="Force ffmpeg audio mode")
    p.add_argument("--c64audio", action="store_true",    help="Play SID audio on C64 hardware")
    p.add_argument("--macaudio", action="store_true",    help="Play SID audio on Mac (default)")
    p.add_argument("--fps",      type=int, default=10,   help="Frame rate (default 10)")
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
    # Cap at 253 chars (ticker_buf is $C500-$C5FC, 253 bytes safe)
    if len(ticker) > 253:
        ticker = ticker[:253]
    return ticker

def show_info_header(info, mode, filepath):
    width = 54
    print("+" + "-" * width + "+")
    print(f"|  sidviz_c64  v{VERSION}  build {BUILD}".ljust(width + 1) + "|")
    print("+" + "-" * width + "+")
    print(f"|  File: {os.path.basename(filepath)}".ljust(width + 1) + "|")
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
            print(f"|  {label:<12} {info[key]}".ljust(width + 1) + "|")
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

def detect_mode(filepath, force_sid=False, force_audio=False):
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

# sidplay_on_c64 replaced by upload_sid_to_c64 in exp version

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

# (simple blocking read used — see main loop)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global U64, FPS

    args = parse_args()

    if args.version:
        print(f"sidviz_c64  v{VERSION}  build {BUILD}")
        sys.exit(0)

    filepath = os.path.expanduser(args.file) if args.file else \
               os.path.expanduser(input("Audio/SID file path: ").strip())

    if not os.path.isfile(filepath):
        print(f"[!] File not found: {filepath}"); sys.exit(1)

    U64 = f"http://{args.ip}"
    FPS = args.fps

    if args.color:      color_mode_init = 0
    elif args.no_color: color_mode_init = 1
    else:
        ans = input("Color mode? [0=rainbow, 1=white, 2=fire] (default 0): ").strip()
        color_mode_init = int(ans) if ans in ("0","1","2") else 0

    mode = detect_mode(filepath, force_sid=args.sid, force_audio=args.audio)

    # Determine audio destination for SID files
    c64_audio = False
    if mode == "sid":
        if args.c64audio:
            c64_audio = True
        elif args.macaudio:
            c64_audio = False
        else:
            ans = input("Audio output? [m=Mac (default), c=C64]: ").strip().lower()
            c64_audio = ans in ("c", "c64")
        print(f"[*] SID audio: {'C64 hardware' if c64_audio else 'Mac (sidplayfp)'}")

    # Get and display metadata
    info = get_sid_info(filepath) if mode == "sid" else get_audio_info(filepath)
    show_info_header(info, mode, filepath)
    ticker_str = build_ticker_string(info, mode)

    if not os.path.isfile(PRG_LOCAL):
        print(f"[!] sidviz.prg not found at {PRG_LOCAL}")
        print(f"    Build: 64tass -a -B -o sidviz_exp.prg sidviz_exp.asm")
        sys.exit(1)
    print(f"[*] sidviz.prg: {os.path.getsize(PRG_LOCAL)} bytes")

    if not smoke_test(): sys.exit(1)

    print("[*] Rebooting C64...")
    u64_put("machine:reboot")
    time.sleep(4.0)

    print("[*] Uploading sidviz_exp.prg...")
    if not ftp_upload(PRG_LOCAL, PRG_REMOTE): sys.exit(1)

    # Start processes and parse PSID before running PRG
    sid_audio_proc    = None
    ffplay_proc       = None
    sid_duration_secs = None
    psid              = None
    procs = []

    if mode == "sid":
        # Parse song length for wall-clock stop timer
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
                print("[!] PSID parse failed, falling back to Mac audio")
                c64_audio = False

    # Run PRG first — it will spin on sid_ready ($C002) waiting for us
    print("[*] Running sidviz_exp.prg...")
    if not run_prg_from_temp(PRG_REMOTE): sys.exit(1)
    time.sleep(0.5)  # give PRG time to start and reach wait_sid loop

    # Force PAL 50Hz CIA1 timer A via writemem
    # PAL: 985248 Hz / 50 = 19704 = $4CF8
    # PRG is spinning on wait_sid with CLI active — safe to patch CIA1 now
    print("[*] Setting CIA1 timer for PAL 50Hz...")
    write_mem(0xDC04, [0xF8, 0x4C])  # timer lo, timer hi
    write_byte(0xDC0E, 0x11)          # start timer A, continuous, force reload

    # Send initial color mode: 2=rainbow, 1=white density, 3=fire density
    _cflag_map = {0: 2, 1: 1, 2: 3}
    write_byte(COLOR_FLAG, _cflag_map[color_mode_init])

    # Send ticker to C64
    send_ticker(ticker_str)

    if mode == "sid":
        make_fifo(FIFO_PATH)
        ffmpeg_proc   = start_ffmpeg_waveform_fifo()
        time.sleep(0.3)
        sid_fifo_proc = start_sidplayfp_fifo(filepath, sid_duration_secs)

        if c64_audio:
            # PRG is spinning on $C002 — upload SID, patch addresses, then signal
            upload_sid_to_c64(psid)
            procs = [sid_fifo_proc, ffmpeg_proc]
        else:
            sid_audio_proc = start_sidplayfp_audio(filepath, sid_duration_secs)
            procs = [sid_fifo_proc, sid_audio_proc, ffmpeg_proc]
    else:
        ffmpeg_proc = start_ffmpeg_waveform_file(filepath)
        ffplay_proc = start_ffplay_audio(filepath)
        procs = [ffplay_proc, ffmpeg_proc]

    frame_size = WIDTH * HEIGHT

    sid_end_time = (time.time() + sid_duration_secs) if mode == "sid" and sid_duration_secs else None
    state   = {"color_mode": color_mode_init, "color_pending": False, "quit": False}
    kthread = make_keypress_listener(state)
    kthread.start()

    frame_num  = 0
    print(f"[*] Streaming to C64 at {FPS}fps -- [c] color, [q] quit\n")

    try:
        while not state["quit"]:
            # Stop when audio process finishes (song ended)
            if sid_audio_proc is not None and sid_audio_proc.poll() is not None:
                print("\r\n[*] Song ended.")
                break
            if ffplay_proc is not None and ffplay_proc.poll() is not None:
                print("\r\n[*] Song ended.")
                break
            # Stop at parsed song duration (overrides SONGLENGTHS early cutoff)
            if sid_end_time is not None and time.time() >= sid_end_time:
                print("\r\n[*] Song ended.")
                break

            if state["color_pending"]:
                state["color_pending"] = False
                _cflag_map = {0: 2, 1: 1, 2: 3}
                write_byte(COLOR_FLAG, _cflag_map[state["color_mode"]])

            # Simple blocking read — ffmpeg throttles to FPS naturally
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
        for p in procs:
            try: p.terminate()
            except: pass
        # Clean up display
        write_byte(FRAME_FLAG, 0)
        write_mem(FRAME_BUF, [0x20] * (WIDTH * HEIGHT))
        # Restore original IRQ vector to stop ticker
        orig = u64_get("machine:readmem?address=F9&length=2")
        if orig and len(orig) == 2:
            u64_put("machine:writemem", {"address": "314",
                                          "data": f"{orig[0]:02X}{orig[1]:02X}"})
        write_mem(TICKER_ROW, [0x20] * 40)
        try: os.remove(FIFO_PATH)
        except: pass
        # Restore terminal regardless of how we exited
        if "_term_fd" in state and "_term_old" in state:
            try:
                termios.tcsetattr(state["_term_fd"], termios.TCSADRAIN, state["_term_old"])
            except Exception:
                pass
        print("[*] Done.")

if __name__ == "__main__":
    main()
