"""Koalavideo REUs for sidviz: any video (or a link) as C64 Koala pictures with sampled sound,
for daddlerTL's Koalavideo v06 player (koalavideo.prg). Koalavideo is daddlerTL's (2022-2026,
freeware and open source); as he asks, his name stays with it.

The REU is laid out exactly as his "Koalavideo v06 REUmaker" writes it (the same as SIDlab's
video maker): 16-byte lines from $000000 ("KOALAVIDEO06REU", INDEX, INFOSCR, CHAPTERS, VIDEO,
AUDIO; addresses high byte first), the picture index (4 bytes a picture: address, background
colour), the info screen (the song's details, or empty) and an empty chapter list, the pictures, then the sound from the next
256-byte page: 11.7 kHz, a byte a sample, through daddlerTL's Mahoney table for the SID
(from his BREADAMP REUmaker) or as signed linear samples for Ultimate Audio / FPGASID.

ffmpeg scales, colours (to the 16 C64 colours) and dithers each frame; Python then gives each
4x8 cell its Koala colours (the background plus the three most used) and packs the REU."""

import os, subprocess, tempfile

REU  = 16 * 1024 * 1024
RATE = 11700
# the picture shapes: name, bytes a picture, area of the screen (cell column, cell row, columns, rows)
SHAPES = {
    1: ("16:10 (320x200)", 9500, (0, 0, 40, 25)),
    2: ("16:9 (320x184)",  8740, (0, 1, 40, 23)),
    3: ("4:3 (256x192)",   7296, (0, 0, 32, 24)),
    4: ("2.35:1 (320x136)", 6460, (0, 4, 40, 17)),
}
# C64 frames a picture (PAL, 50 a second); 2 (25 fps) only for 2.35:1
SPEEDS = {2: "25 fps", 3: "16.7 fps", 4: "12.5 fps", 5: "10 fps", 6: "8.33 fps", 7: "7.14 fps", 8: "6.25 fps"}

# daddlerTL's Mahoney tables (BREADAMP_v05-v07_REUmaker.bas, freeware, "please keep my name
# daddlerTL"): an 8-bit sample (128 = silence) -> the $D418 value that makes the SID output it
MAHONEY = {
    "6581": bytes([
        159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 159, 158, 158, 158, 158, 158,
        157, 157, 157, 157, 157, 157, 156, 156, 156, 156, 156, 156, 155, 155, 155, 155, 155, 220, 220, 154, 154, 154, 154, 219,
        153, 153, 153, 153, 153, 153, 218, 152, 152, 152, 152, 152, 217, 186, 186, 151, 151, 151, 151, 151, 151, 151, 151, 215,
        215, 150, 150, 150, 150, 214, 214, 214, 149, 149, 149, 149, 149, 213, 213, 213, 148, 148, 148, 148, 181, 212, 212, 212,
        180, 180, 147, 147, 244, 211, 211, 179, 179, 179, 146, 146, 146, 210, 210, 178, 178, 242, 242, 145, 145, 145, 209, 177,
        241, 241, 18, 81, 81, 113, 144, 48, 0, 161, 162, 163, 164, 164, 165, 65, 97, 33, 33, 1, 1, 66, 66, 66,
        98, 98, 34, 34, 34, 34, 67, 67, 99, 99, 99, 35, 35, 35, 35, 68, 100, 100, 100, 69, 69, 69, 36, 36,
        36, 36, 101, 70, 70, 70, 70, 37, 37, 37, 71, 71, 71, 103, 103, 38, 38, 38, 38, 72, 72, 72, 104, 104,
        39, 39, 39, 73, 73, 105, 105, 74, 74, 74, 74, 40, 40, 40, 40, 75, 75, 107, 41, 41, 41, 41, 41, 108,
        108, 42, 42, 42, 42, 42, 42, 78, 78, 43, 43, 43, 43, 43, 43, 44, 44, 44, 44, 44, 44, 44, 45, 45,
        45, 45, 45, 45, 45, 46, 46, 46, 46, 46, 46, 14, 47, 47, 47, 47]),
    "8580": bytes([
        159, 159, 223, 223, 223, 223, 223, 158, 158, 158, 158, 158, 222, 222, 222, 222, 222, 157, 157, 157, 157, 157, 157, 221,
        221, 221, 221, 221, 156, 156, 156, 156, 156, 220, 220, 220, 220, 220, 155, 155, 155, 155, 155, 155, 219, 219, 219, 219,
        219, 154, 154, 154, 154, 154, 218, 218, 218, 218, 218, 153, 153, 153, 153, 153, 153, 217, 217, 217, 217, 217, 152, 152,
        152, 152, 152, 216, 216, 216, 216, 216, 216, 63, 63, 63, 63, 151, 151, 151, 215, 215, 62, 62, 126, 126, 126, 61,
        61, 125, 125, 125, 60, 60, 124, 124, 124, 59, 59, 123, 123, 123, 58, 58, 122, 122, 122, 57, 57, 57, 121, 121,
        24, 24, 88, 88, 88, 120, 23, 23, 55, 87, 119, 22, 22, 22, 86, 86, 86, 21, 21, 85, 85, 85, 20, 20,
        20, 84, 84, 19, 19, 19, 83, 83, 83, 18, 18, 18, 82, 82, 17, 17, 17, 81, 81, 240, 144, 128, 128, 128,
        128, 1, 1, 1, 65, 65, 65, 2, 2, 66, 66, 66, 3, 3, 3, 3, 67, 67, 4, 4, 4, 4, 68, 68,
        68, 5, 5, 5, 69, 69, 6, 6, 6, 6, 70, 70, 7, 7, 7, 7, 71, 71, 71, 40, 8, 8, 8, 72,
        72, 41, 9, 9, 9, 73, 73, 10, 10, 10, 10, 74, 74, 11, 11, 11, 75, 75, 75, 44, 12, 12, 12, 76,
        76, 45, 13, 13, 77, 77, 77, 46, 14, 14, 78, 78, 78, 47, 15, 15]),
}

# ---------------------------------------------------------------------------
# REU layout and packing (daddlerTL's format, as SIDlab's sidlab-koalavideo.js writes it)
# ---------------------------------------------------------------------------

def layout(n, shape, sound):
    """Where each of n pictures goes, where they end, and where the sound starts."""
    size, at = SHAPES[shape][1], []
    p = 16 * (6 if sound else 5) + 4 * n + 1000 + 64
    for _ in range(n):
        at.append(p)
        p += size
        if p // 524288 != (p + size) // 524288:      # daddlerTL's alignment at the 512 KB steps
            p = (p // 512 + 1) * 512
    return at, p, (p + 255) // 256 * 256

def fits(n, shape, fps, sound):
    at, end, snd = layout(n, shape, sound)
    return end <= REU and (not sound or snd + int(n / fps * RATE + 0.999) <= REU)

def max_pictures(shape, fps, sound):
    lo, hi = 0, REU // SHAPES[shape][1]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid, shape, fps, sound): lo = mid
        else: hi = mid - 1
    return lo

def _picture(kla, shape):
    """A picture's bytes from a Koala file: bitmap and screen for the shape, colour RAM two to a byte."""
    bm, scr, col = kla[2:8002], kla[8002:9002], kla[9002:10002]
    pack = lambda a, b, n: bytes((col[a + i] & 15) | (col[b + i] & 15) << 4 for i in range(n))
    if shape == 1: return [bm, scr, pack(0, 500, 500)]
    if shape == 2: return [bm[320:7680], scr[40:960], pack(40, 500, 460)]
    if shape == 4: return [bm[1280:6720], scr[160:840], pack(160, 500, 340)]
    out = [bm[320 * r:320 * r + 256] for r in range(24)] + [scr[40 * r:40 * r + 32] for r in range(24)]
    out.append(bytes((col[40 * r + j] & 15) | (col[480 + 40 * r + j] & 15) << 4
                     for r in range(12) for j in range(32)))
    return out

def build(klas, shape, speed, samples=None, model="6581", border=0, info=None):
    """The 16 MB REU. samples: 8-bit unsigned at RATE (128 = silence) or None;
    model: "6581" / "8580" (Mahoney) or "ua" (Ultimate Audio / FPGASID, linear);
    info: lines for the player's info screen (shown for 10 seconds before the video), or None."""
    reu, n, has = bytearray(REU), len(klas), bool(samples)
    at, end, snd = layout(n, shape, has)
    if end > REU:
        raise ValueError("the pictures don't fit in 16 MB")
    lines = 6 if has else 5
    idx = 16 * lines
    info_at, chap = idx + 4 * n, idx + 4 * n + 1000
    a3 = lambda v: bytes([v >> 16 & 255, v >> 8 & 255, v & 255])
    def put(o, b): reu[o:o + len(b)] = b
    for i, k in enumerate(klas):
        o = at[i]
        for part in _picture(k, shape):
            put(o, part); o += len(part)
        put(idx + 4 * i, a3(at[i]) + bytes([k[10002] & 15]))
    d = f"{min(9999, n):04d}"
    bcd = bytes([int(d[0]) * 16 + int(d[1]), int(d[2]) * 16 + int(d[3])])
    put(0,  b"KOALAVIDEO06REU" + bytes([lines - 1]))
    put(16, b"INDEX" + bytes([0, 0, n >> 8, n & 255, 0]) + a3(idx) + a3(4 * n))
    if info:                                       # $80: there is one (border, background, text colours)
        put(info_at, info_screen(info))
        put(32, b"INFOSCR" + bytes([0x80 | 14, 6, 1]) + a3(info_at) + bytes([0, 3, 232]))
    else:
        put(32, b"INFOSCR" + bytes([14, 6, 14]) + a3(info_at) + bytes([0, 3, 232]))   # no info screen
    put(48, b"CHAPTERS" + bytes([0, 0]) + a3(chap) + bytes([0, 0, 64]))
    video0 = 16 * lines + 4 * n + 1000 + 64
    put(64, b"VIDEO" + bcd + bytes([(shape - 1) * 8 + (2 if has else 0) * 2, (border & 15) * 16, speed])
        + a3(video0) + a3(end - video0))
    used = end
    if has:
        s = bytes(samples[:REU - snd])
        sid = {"6581": 1, "8580": 2}.get(model, 3)
        tab = MAHONEY[model] if sid != 3 else bytes(v ^ 0x80 for v in range(256))
        put(snd, s.translate(tab))
        put(80, b"AUDIO" + bytes([0, 0, 32 + sid * 4 + (3 if sid == 3 else 1), 64 + 8, 0xB7])
            + a3(snd) + a3((len(s) + 255) // 256 * 256))
        used = snd + len(s)
    return reu, used

# ---------------------------------------------------------------------------
# Song info: a scroller drawn into the pictures, and the player's info screen
# ---------------------------------------------------------------------------

# a 3x7 font (each character 4 Koala pixels wide with its gap, 8 high), capitals only
_FONT_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,!?-:;\'"()/&+*#=_@[]<>%$'
_FONT_BITS = [0xadf6d,0x1add6e,0xe4923,0x1adb6e,0x1e4d27,0x1e4d24,0xe4b6b,0x16df6d,0x1d2497,0x4926a,0x16e9ad,0x124927,0x17fb6d,0x1adb6d,0xadb6a,0x1add24,0xadb51,0x1add6d,0xe444e,0x1d2492,0x16db6f,0x16db52,0x16dbfd,0x16a4ad,0x16d492,0x1c9527,0x1edb6f,0xb2497,0x189527,0x18944e,0x16de49,0x1e4c4e,0xe4d6a,0x1c9492,0xad56a,0xad64e,0x00000,0x00002,0x00014,0x92482,0x189482,0x00e00,0x12090,0x12094,0x94000,0x168000,0x52491,0x112494,0x4a4a4,0xad56b,0x02e80,0x2aea8,0x17db7d,0x071c0,0x00007,0xaff23,0x1a4926,0xc924b,0x0a888,0x222a0,0x149525,0xbce7a]
FONT = dict(zip(_FONT_CHARS, _FONT_BITS))
RAINBOW = [2, 8, 7, 13, 3, 14, 4, 10]           # red, orange, yellow, light green, cyan, light blue, purple, pink
SCROLL_SPEEDS = {"slow": 16, "medium": 32, "fast": 64}   # Koala pixels a second (normal size)

def _plain(text):
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join(t.split())

class Scroller:
    """Song info drawn into each picture: 1 or 2 rows, over the video or on a black strip of its own
    (the video then shrinks to make room), at the top or bottom, normal or double size, in one colour,
    rainbow bands (fixed or moving) or cycling colours, with or without a shadow. A row that fits
    stays still in the middle; a longer one scrolls in from the right."""
    def __init__(self, lines, place="overlay", at="bottom", size=1, color=1, shadow=11,
                 speed="medium", fps=12.5):
        self.lines = [_plain(l).upper() for l in lines if l and _plain(l)][:2]
        self.strip, self.top = place == "strip", at == "top"
        self.size = 2 if int(size) == 2 else 1
        self.color, self.shadow = color, shadow
        self.lh = 8 * self.size
        self.height = self.lh * len(self.lines)
        self.step = max(1, round(SCROLL_SPEEDS.get(speed, 32) * self.size / fps))
        self.cycle = max(1, round(fps / 4))                 # pictures a colour, for "cycle"
        self.bitmaps = [self._bitmap(l) for l in self.lines]

    def _bitmap(self, text):
        """Rows of 0/1 for a line of text, at the scroller's size."""
        rows = [bytearray() for _ in range(8)]      # a blank line above, then the 7 of the font
        for ch in text:
            bits = FONT.get(ch, FONT["?"])
            rows[0].extend(b"\0\0\0\0")
            for r in range(7):
                for c in range(3):
                    rows[r + 1].append(bits >> (20 - (r * 3 + c)) & 1)
                rows[r + 1].append(0)
        if self.size == 2:
            rows = [bytearray(b for b in row for _ in (0, 1)) for row in rows for _ in (0, 1)]
        return rows

    def _colour(self, x, i, k):
        c = self.color
        if c == "rainbow": return RAINBOW[(x // 4 + k) % len(RAINBOW)]
        if c == "bands":   return RAINBOW[(x // 4 + k + i) % len(RAINBOW)]
        if c == "cycle":   return RAINBOW[(i // self.cycle + k) % len(RAINBOW)]
        return int(c) & 15

    def draw(self, q, w, h, i):
        """Draw picture i's scroller into q (w x h C64 colours). Returns, for each 4x8 cell it
        touches, the colours that cell has to keep (the text's first)."""
        must = {}
        y0 = 0 if self.top else h - self.height
        if self.strip:
            for y in range(y0, y0 + self.height):
                q[y * w:(y + 1) * w] = bytes(w)
        for k, bm in enumerate(self.bitmaps):
            tw, top = len(bm[0]), y0 + k * self.lh
            if tw <= w:
                cols = [(x, x - (w - tw) // 2) for x in range(w)]
            else:
                period = tw + w // 2
                cols = []
                for x in range(w):
                    c = x + i * self.step - w
                    cols.append((x, c % period if c >= 0 else -1))
            text, shade = {}, {}
            for x, c in cols:
                if not 0 <= c < tw:
                    continue
                for r in range(self.lh):
                    if bm[r][c]:
                        text[(x, top + r)] = self._colour(x, i, k)
                        if self.shadow is not None:
                            shade[(x + 1, top + r + 1)] = self.shadow
            for layer in (shade, text):
                for (x, y), col in layer.items():
                    if x < w and y < min(h, y0 + self.height if self.strip else h) and \
                            (layer is text or (x, y) not in text):
                        q[y * w + x] = col
                        keep = must.setdefault((y // 8) * (w // 4) + x // 4, [])
                        if col not in keep:
                            keep.append(col)
        if self.strip:                                     # the strip's black stays in every cell
            for y in range(y0, y0 + self.height, 8):
                for cx in range(w // 4):
                    keep = must.setdefault((y // 8) * (w // 4) + cx, [])
                    if 0 not in keep:
                        keep.append(0)
        for keep in must.values():                         # text before shadow before black
            keep.sort(key=lambda c: (c == 0, self.shadow is not None and c == self.shadow))
        return must

def song_lines(info, rows=1):
    """Scroller rows from song info (title, artist / author, album, released / year, extra)."""
    f = lambda k: _plain(info.get(k) or "")
    who = f("artist") or f("author")
    first = "  -  ".join(v for v in (f("title"), who) if v)
    second = "  -  ".join(v for v in (f("album"), f("released") or f("year"), f("extra")) if v)
    if rows == 2 and second:
        return [first or second, second if first else ""]
    return ["  -  ".join(v for v in (first, second) if v)]

def info_screen(lines):
    """The player's info screen (1000 screen codes, lower/upper case set): the lines in the middle."""
    def code(ch):
        o = ord(ch)
        if 97 <= o <= 122: return o - 96
        if 65 <= o <= 90:  return o
        if 32 <= o <= 63:  return o
        return 63 if o != 64 else 0
    rows = []
    for l in (_plain(x) for x in lines):
        while len(l) > 40:
            cut = l.rfind(" ", 0, 41)
            cut = cut if cut > 0 else 40
            rows.append(l[:cut]); l = l[cut:].strip()
        rows.append(l)
    rows = rows[:23]
    scr = bytearray(b" " * 1000)
    top = (25 - (2 * len(rows) - 1)) // 2 if 2 * len(rows) - 1 <= 25 else 0
    gap = 2 if 2 * len(rows) - 1 <= 25 else 1
    for n, l in enumerate(rows):
        r = top + n * gap
        x = (40 - len(l)) // 2
        for j, ch in enumerate(l):
            scr[r * 40 + x + j] = code(ch)
    return bytes(scr)

# ---------------------------------------------------------------------------
# Frames: ffmpeg -> 16 C64 colours -> Koala cells
# ---------------------------------------------------------------------------

def _png(path, w, h, rgb):
    import struct, zlib
    ck = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + ck(b"IDAT", zlib.compress(raw)) + ck(b"IEND", b""))

def _viz_filter(viz, w, h, fps):
    f = {"showwaves":    f"showwaves=s={w}x{h}:mode=cline:rate={fps}:colors=white",
         "showfreqs":    f"showfreqs=s={w}x{h}:mode=bar:fscale=log:colors=white",
         "avectorscope": f"avectorscope=s={w}x{h}:zoom=1.5:draw=line:rate={fps}",
         "showspectrum": f"showspectrum=s={w}x{h}:slide=scroll:color=intensity",
         "ahistogram":   f"ahistogram=s={w}x{h}:slide=scroll"}[viz]
    return f + f",fps={fps},format=rgb24"

def frames(video, shape, fps, seconds, palette, dither="bayer", viz=None, viz_audio=None, loop=False,
           scroller=None):
    """Koala files (10003 bytes each) for the video, one per picture. palette: 16 (r, g, b);
    scroller: a Scroller drawn into each picture (on a strip of its own, the video makes room)."""
    c0, r0, cols, rows = SHAPES[shape][2]
    w, h = cols * 4, rows * 8                       # Koala pixels (two hires pixels wide)
    tmp = tempfile.mkdtemp(prefix="sidviz_kv_")
    pal_png = os.path.join(tmp, "pal.png")
    _png(pal_png, 16, 16, b"".join(bytes(palette[i % 16]) for i in range(256)))
    dith = {"bayer": "bayer:bayer_scale=2", "none": "none", "diffusion": "floyd_steinberg"}[dither]
    cmd = ["ffmpeg", "-v", "error"] + (["-stream_loop", "-1"] if loop else []) + ["-i", video]
    band = scroller.height if scroller and scroller.strip else 0
    vh = h - band                                   # the video's own height (less a strip)
    y = f"{band}+({vh}-ih)/2" if band and scroller.top else f"({vh}-ih)/2"
    graph = (f"[0:v]fps={fps},scale={cols * 8}:{vh}:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"pad={cols * 8}:{h}:(ow-iw)/2:{y},scale={w}:{h}:flags=area,format=rgb24[v]")
    src, pin = "[v]", 1
    if viz:
        cmd += ["-i", viz_audio or video]
        graph += f";[1:a]{_viz_filter(viz, w, h, fps)}[z];[v][z]blend=all_mode=lighten[b]"
        src, pin = "[b]", 2
    cmd += ["-i", pal_png, "-filter_complex", graph + f";{src}[{pin}:v]paletteuse=dither={dith}[o]",
            "-map", "[o]", "-t", f"{seconds:.3f}", "-f", "rawvideo", "-pix_fmt", "pal8", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    # the C64 colour of each palette entry ffmpeg uses, and how far apart the 16 colours are
    lookup = {tuple(c): i for i, c in enumerate(palette)}
    dist = [[sum((a - b) ** 2 for a, b in zip(palette[i], palette[j])) for j in range(16)] for i in range(16)]
    fsize = w * h + 1024
    i = 0
    try:
        while True:
            buf = p.stdout.read(fsize)
            if len(buf) < fsize:
                break
            pal = buf[w * h:]
            remap = bytes(lookup.get((pal[4 * i + 2], pal[4 * i + 1], pal[4 * i]), 0) for i in range(256))
            q, must = buf[:w * h].translate(remap), None
            if scroller and scroller.lines:
                q = bytearray(q)
                must = scroller.draw(q, w, h, i)
            i += 1
            yield _koala(bytes(q), w, c0, r0, cols, rows, dist, must)
    finally:
        p.stdout.close(); p.wait()
        try:
            os.remove(pal_png); os.rmdir(tmp)
        except OSError:
            pass

def _koala(q, w, c0, r0, cols, rows, dist, must=None):
    """One frame of C64 colour indexes (w x rows*8) -> a Koala file, placed at the shape's cells.
    must: for some cells, colours they keep before the most used (a scroller's)."""
    counts = [q.count(c) for c in range(16)]
    bg = max(range(16), key=counts.__getitem__)
    kla = bytearray(10003)
    kla[0], kla[1] = 0x00, 0x60
    kla[10002] = bg
    for cy in range(rows):
        base = cy * 8 * w
        for cx in range(cols):
            rws = [q[base + r * w + cx * 4: base + r * w + cx * 4 + 4] for r in range(8)]
            cell = b"".join(rws)
            cnt = sorted(((cell.count(c), c) for c in set(cell) if c != bg), reverse=True)
            keep = must.get(cy * cols + cx) if must else None
            if keep:
                keep = [c for c in keep if c != bg][:3]
                cnt = [(0, c) for c in keep] + [t for t in cnt if t[1] not in keep]
            cnt = cnt[:3]
            choice = [(bg, 0)] + [(c, b) for (_, c), b in zip(cnt, (1, 2, 3))]
            m = [min(choice, key=lambda cb: dist[c][cb[0]])[1] for c in range(16)]
            o = 2 + ((r0 + cy) * 40 + c0 + cx) * 8
            for r in range(8):
                p = rws[r]
                kla[o + r] = m[p[0]] << 6 | m[p[1]] << 4 | m[p[2]] << 2 | m[p[3]]
            cs = [c for (_, c) in cnt] + [0, 0, 0]
            si = (r0 + cy) * 40 + c0 + cx
            kla[8002 + si] = cs[0] << 4 | cs[1]
            kla[9002 + si] = cs[2]
    return bytes(kla)

def reu_sound(data):
    """A Koalavideo REU's sound back as 8-bit unsigned samples at RATE (128 = silence), whether it
    has an info screen (the player shows that first), and how it plays (1 / 2 = the SID at $D400,
    6581 / 8580; 3 = Ultimate Audio; 0 = silent). For the C64U's lights; b"" when silent."""
    lines = data[15] + 1 if len(data) > 16 else 0
    snd, info, kind = b"", False, 0
    for n in range(1, lines):
        ln = data[16 * n:16 * n + 16]
        a3 = lambda o: ln[o] << 16 | ln[o + 1] << 8 | ln[o + 2]
        if ln.startswith(b"INFOSCR"):
            info = bool(ln[7] & 0x80)
        elif ln.startswith(b"AUDIO"):
            raw = data[a3(10):a3(10) + a3(13)]
            kind = (ln[7] - 32) // 4
            if kind == 3:
                snd = bytes(v ^ 0x80 for v in range(256))
                snd = raw.translate(snd)
            else:
                tab = MAHONEY["6581" if kind == 1 else "8580"]
                back = {}
                for i, v in enumerate(tab):
                    back.setdefault(v, []).append(i)
                inv = bytes(sum(back[v]) // len(back[v]) if v in back else 128 for v in range(256))
                snd = raw.translate(inv)
    return snd, info, kind

def sound(path, seconds):
    """8-bit unsigned mono samples at RATE (128 = silence), levelled so quiet parts still carry."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-t", f"{seconds:.3f}", "-vn",
                        "-af", "dynaudnorm=f=200:g=15", "-ac", "1", "-ar", str(RATE), "-f", "u8", "pipe:1"],
                       stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    return r.stdout

def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None

def has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool(r.stdout.strip())
