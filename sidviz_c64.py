#!/usr/bin/env python3
"""
sidviz_c64.py -- SID/audio waveform visualizer -> C64 via U64 API
Uses sidviz.prg (from sidviz.asm) running on C64 as display driver.

version 1.0.0 (2026-04-16-1)

Memory protocol:
  $C000 = frame flag  (Python writes 1, ASM clears to 0)
  $C001 = color flag  (Python writes 1=white, 2=rainbow, ASM applies+clears)
  $C100 = frame buffer, 1000 bytes PETSCII screen codes

Usage:
  1. Assemble: 64tass -a -B -o sidviz.prg sidviz.asm
  2. Run: python3 sidviz_c64.py [file]
"""

VERSION = "1.0.0"
BUILD   = "2026-04-16-1"

import os, sys, time, subprocess, urllib.request, urllib.parse
import argparse, threading, termios, tty, re

FIFO_PATH   = "/tmp/sidpipe.wav"
WIDTH       = 40
HEIGHT      = 25
FRAME_BUF   = 0xC100
FRAME_FLAG  = 0xC000
COLOR_FLAG  = 0xC001
PRG_LOCAL   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidviz.prg")
PRG_REMOTE  = "sidviz.prg"
TIMEOUT     = 5.0
CHARS       = [32, 46, 58, 42, 35, 64]
SID_EXTS    = {".sid"}
U64         = ""
FPS         = 10

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
    p.add_argument("--fps",      type=int, default=10,   help="Frame rate (default 10)")
    p.add_argument("--version",  action="store_true",    help="Show version and exit")
    return p.parse_args()

# ---------------------------------------------------------------------------
# SID info
# ---------------------------------------------------------------------------

def show_sid_info(filepath):
    """Run sidplayfp -v, parse and display SID metadata."""
    try:
        result = subprocess.run(
            ["sidplayfp", "-v", filepath],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout + result.stderr
    except Exception as e:
        print(f"[!] Could not get SID info: {e}")
        return

    # Fields to extract and their display labels
    fields = [
        ("Title",       "Title"),
        ("Author",      "Author"),
        ("Released",    "Released"),
        ("File format", "Format"),
        ("Playlist",    "Playlist"),
        ("Song Speed",  "Speed"),
        ("Song Length", "Length"),
        ("Addresses",   "Addresses"),
        ("Condition",   "Condition"),
    ]

    width = 54
    print("+" + "-" * width + "+")
    print(f"|  sidviz_c64  v{VERSION}  build {BUILD}".ljust(width + 1) + "|")
    print("+" + "-" * width + "+")

    for key, label in fields:
        # Match "| Key   : Value  |" or "| Key   : Value"
        pattern = rf"\|\s*{re.escape(key)}\s*:\s*(.+?)(?:\s*\|)?\s*$"
        for line in output.splitlines():
            m = re.search(pattern, line)
            if m:
                value = m.group(1).strip()
                row = f"  {label:<12} {value}"
                print(f"|{row:<{width}}|")
                break

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

def start_ffmpeg_waveform_fifo():
    cmd = ["ffmpeg", "-loglevel", "quiet", "-f", "wav", "-i", FIFO_PATH,
           "-filter_complex",
           f"[0:a]showwaves=s={WIDTH}x{HEIGHT}:mode=cline:rate={FPS}:colors=#ffffff,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    print("[*] ffmpeg waveform (FIFO) started."); return p

def start_ffmpeg_waveform_file(filepath):
    cmd = ["ffmpeg", "-loglevel", "quiet", "-i", filepath,
           "-filter_complex",
           f"[0:a]showwaves=s={WIDTH}x{HEIGHT}:mode=cline:rate={FPS}:colors=#ffffff,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS), "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    print("[*] ffmpeg waveform (file) started."); return p

def start_ffplay_audio(filepath):
    p = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", filepath],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] ffplay audio started."); return p

def start_sidplayfp_fifo(filepath):
    p = subprocess.Popen(["sidplayfp", f"-w{FIFO_PATH}", filepath],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] sidplayfp -> FIFO started."); return p

def start_sidplayfp_audio(filepath):
    p = subprocess.Popen(["sidplayfp", filepath],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[*] sidplayfp -> audio started."); return p

# ---------------------------------------------------------------------------
# Keypress toggle
# ---------------------------------------------------------------------------

def make_keypress_listener(state):
    def _listen():
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while not state["quit"]:
                ch = sys.stdin.read(1)
                if not ch: break
                if ch in ("c", "C"):
                    state["rainbow"]       = not state["rainbow"]
                    state["color_pending"] = True
                    label = "rainbow" if state["rainbow"] else "white"
                    sys.stdout.write(f"\n[*] Color -> {label}\n")
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
        print(f"sidviz_c64  v{VERSION}  build {BUILD}")
        sys.exit(0)

    filepath = os.path.expanduser(args.file) if args.file else \
               os.path.expanduser(input("Audio/SID file path: ").strip())

    if not os.path.isfile(filepath):
        print(f"[!] File not found: {filepath}"); sys.exit(1)

    U64 = f"http://{args.ip}"
    FPS = args.fps

    if args.color:      rainbow = True
    elif args.no_color: rainbow = False
    else:
        ans     = input("Rainbow color? [Y/n]: ").strip().lower()
        rainbow = ans not in ("n", "no")

    mode = detect_mode(filepath, force_sid=args.sid, force_audio=args.audio)

    # Show SID metadata before doing anything else
    if mode == "sid":
        show_sid_info(filepath)

    if not os.path.isfile(PRG_LOCAL):
        print(f"[!] sidviz.prg not found at {PRG_LOCAL}")
        print(f"    Build it: 64tass -a -B -o sidviz.prg sidviz.asm")
        sys.exit(1)
    print(f"[*] sidviz.prg: {os.path.getsize(PRG_LOCAL)} bytes")

    if not smoke_test(): sys.exit(1)

    print("[*] Rebooting C64...")
    u64_put("machine:reboot")
    time.sleep(4.0)

    print("[*] Uploading sidviz.prg...")
    if not ftp_upload(PRG_LOCAL, PRG_REMOTE): sys.exit(1)
    print("[*] Running sidviz.prg...")
    if not run_prg_from_temp(PRG_REMOTE): sys.exit(1)
    time.sleep(1.0)

    if not rainbow:
        write_byte(COLOR_FLAG, 1)

    sid_audio_proc = None
    procs = []

    if mode == "sid":
        make_fifo(FIFO_PATH)
        ffmpeg_proc    = start_ffmpeg_waveform_fifo()
        time.sleep(0.3)
        sid_fifo_proc  = start_sidplayfp_fifo(filepath)
        sid_audio_proc = start_sidplayfp_audio(filepath)
        procs = [sid_fifo_proc, sid_audio_proc, ffmpeg_proc]
    else:
        ffmpeg_proc = start_ffmpeg_waveform_file(filepath)
        procs = [start_ffplay_audio(filepath), ffmpeg_proc]

    state   = {"rainbow": rainbow, "color_pending": False, "quit": False}
    kthread = make_keypress_listener(state)
    kthread.start()

    frame_size = WIDTH * HEIGHT
    frame_num  = 0
    print(f"[*] Streaming to C64 at {FPS}fps -- [c] color, [q] quit\n")

    try:
        while not state["quit"]:
            # Stop as soon as sidplayfp audio finishes
            if sid_audio_proc is not None and sid_audio_proc.poll() is not None:
                print("\n[*] Song ended.")
                break

            if state["color_pending"]:
                state["color_pending"] = False
                write_byte(COLOR_FLAG, 2 if state["rainbow"] else 1)

            raw = ffmpeg_proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                print("\n[*] Stream ended."); break

            screen = bytes(pixel_to_char(p) for p in raw)
            write_mem(FRAME_BUF, screen)
            write_byte(FRAME_FLAG, 1)

            frame_num += 1
            ind = "R" if state["rainbow"] else "W"
            print(f"\r[*] Frame {frame_num:05d} [{ind}]", end="", flush=True)

    except KeyboardInterrupt:
        print("\n[*] Interrupted.")
    finally:
        state["quit"] = True
        for p in procs:
            try: p.terminate()
            except: pass
        try: os.remove(FIFO_PATH)
        except: pass
        print("[*] Done.")

if __name__ == "__main__":
    main()
