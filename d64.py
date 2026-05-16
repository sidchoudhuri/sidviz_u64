"""
d64.py — minimal Commodore 1541 D64 image writer.

Creates a standard 35-track, single-sided D64 image and writes PRG files to
it so a real C64 can load them with  LOAD"*",8,1  from a 1541/1571 drive.

D64 geometry:
  Tracks 1-17  : 21 sectors each
  Tracks 18-24 : 19 sectors each
  Tracks 25-30 : 18 sectors each
  Tracks 31-35 : 17 sectors each
  Each sector  : 256 bytes
  Track 18     : directory track (sector 0 = BAM, sector 1+ = dir entries)
  Sector link  : first 2 bytes of every sector = (next_track, next_sector)
                 0, 0xFF = end of chain
  Usable bytes : 254 per sector (bytes 2-255)
"""

import struct

# Sectors per track (index 0 unused; index 1 = track 1)
_SECTORS = [0] + [21]*17 + [19]*7 + [18]*6 + [17]*5   # tracks 1-35

DIR_TRACK  = 18
BAM_SECTOR = 0
DIR_SECTOR = 1

def _track_offset(track, sector):
    """Byte offset into the flat D64 image for the given track/sector."""
    off = 0
    for t in range(1, track):
        off += _SECTORS[t] * 256
    return off + sector * 256


class D64:
    """Build a 35-track D64 image in memory and write it to a file."""

    def __init__(self, disk_name="SIDVIZ", disk_id="SV"):
        # Allocate full image (all sectors, all tracks 1-35)
        total_sectors = sum(_SECTORS[1:36])
        self._data = bytearray(total_sectors * 256)

        self._dir_entries = []   # list of 32-byte dir entry bytearrays
        self._used = set()       # (track, sector) pairs that are allocated

        # Track 18 is reserved for directory
        for s in range(_SECTORS[DIR_TRACK]):
            self._used.add((DIR_TRACK, s))

        self._init_bam(disk_name, disk_id)

    # ── low-level sector I/O ────────────────────────────────────────────────

    def _sector(self, track, sector):
        """Return a 256-byte memoryview of the given sector."""
        off = _track_offset(track, sector)
        return self._data[off:off + 256]

    def _write_sector(self, track, sector, data):
        off = _track_offset(track, sector)
        self._data[off:off + len(data)] = data

    # ── BAM ────────────────────────────────────────────────────────────────

    def _init_bam(self, disk_name, disk_id):
        bam = bytearray(256)
        bam[0x00] = DIR_TRACK   # first dir sector track
        bam[0x01] = DIR_SECTOR  # first dir sector sector
        bam[0x02] = 0x41        # 'A' DOS version
        bam[0x03] = 0x00

        # BAM entries: 4 bytes per track, tracks 1-35 at offsets $04-$8F
        for t in range(1, 36):
            nsec = _SECTORS[t]
            # All sectors free initially
            free = nsec
            # Bitmap: bit i of bytes 1-3 → sector i is free (1=free, 0=used)
            bits = (1 << nsec) - 1   # nsec lowest bits set
            b0 = bits & 0xFF
            b1 = (bits >> 8) & 0xFF
            b2 = (bits >> 16) & 0xFF
            if t == DIR_TRACK:
                # Mark all dir-track sectors as used in BAM
                free = 0
                b0 = b1 = b2 = 0x00
            bam[0x04 + (t - 1) * 4 + 0] = free
            bam[0x04 + (t - 1) * 4 + 1] = b0
            bam[0x04 + (t - 1) * 4 + 2] = b1
            bam[0x04 + (t - 1) * 4 + 3] = b2

        # Disk name (16 bytes, PETSCII, $A0-padded)
        name_b = disk_name.upper().encode('ascii', errors='replace')[:16]
        name_b = name_b + b'\xa0' * (16 - len(name_b))
        bam[0x90:0xa0] = name_b

        bam[0xa0] = 0xa0
        bam[0xa1] = 0xa0

        # Disk ID (2 bytes)
        id_b = disk_id.upper().encode('ascii', errors='replace')[:2]
        id_b = id_b + b'\xa0' * (2 - len(id_b))
        bam[0xa2:0xa4] = id_b

        bam[0xa4] = 0xa0
        bam[0xa5] = 0x32  # '2'
        bam[0xa6] = 0x41  # 'A'
        bam[0xa7] = 0xa0
        bam[0xa8] = 0xa0
        bam[0xa9] = 0xa0
        bam[0xaa] = 0xa0

        self._write_sector(DIR_TRACK, BAM_SECTOR, bam)

    def _bam_alloc(self, track, sector):
        """Mark a sector as used in the BAM."""
        bam = bytearray(self._sector(DIR_TRACK, BAM_SECTOR))
        base = 0x04 + (track - 1) * 4
        if bam[base] > 0:
            bam[base] -= 1
        bit = 1 << sector
        w = bam[base+1] | (bam[base+2] << 8) | (bam[base+3] << 16)
        w &= ~bit
        bam[base+1] = w & 0xFF
        bam[base+2] = (w >> 8) & 0xFF
        bam[base+3] = (w >> 16) & 0xFF
        self._write_sector(DIR_TRACK, BAM_SECTOR, bam)

    # ── sector allocation ──────────────────────────────────────────────────

    def _alloc_sector(self):
        """Find and allocate the next free sector (track-first, skip track 18)."""
        for t in range(1, 36):
            if t == DIR_TRACK:
                continue
            for s in range(_SECTORS[t]):
                if (t, s) not in self._used:
                    self._used.add((t, s))
                    self._bam_alloc(t, s)
                    return t, s
        raise RuntimeError("D64 disk full — not enough space for all data")

    # ── file writing ───────────────────────────────────────────────────────

    def add_prg(self, filename, data):
        """
        Add a PRG file to the disk.

        filename : up to 16 characters (ASCII/PETSCII)
        data     : raw bytes to store (already includes the 2-byte load-address
                   header that PRG files carry)
        Returns  : number of 254-byte blocks used
        """
        if not data:
            raise ValueError("file data must not be empty")

        # Chain sectors
        chain = []
        remaining = bytearray(data)
        while remaining:
            t, s = self._alloc_sector()
            chain.append((t, s, remaining[:254]))
            remaining = remaining[254:]

        # Write sector chain with link bytes
        for i, (t, s, chunk) in enumerate(chain):
            sector = bytearray(256)
            if i + 1 < len(chain):
                nt, ns, _ = chain[i + 1]
                sector[0] = nt
                sector[1] = ns
            else:
                sector[0] = 0x00    # end of chain
                sector[1] = 0xFF
            sector[2:2 + len(chunk)] = chunk
            self._write_sector(t, s, sector)

        # Create directory entry
        first_t, first_s, _ = chain[0]
        nblocks = len(chain)

        fname_b = filename.upper().encode('ascii', errors='replace')[:16]
        fname_b = fname_b + b'\xa0' * (16 - len(fname_b))

        entry = bytearray(32)
        entry[0x00] = 0x82          # PRG, closed
        entry[0x01] = first_t
        entry[0x02] = first_s
        entry[0x03:0x13] = fname_b
        entry[0x1e] = nblocks & 0xFF
        entry[0x1f] = (nblocks >> 8) & 0xFF
        self._dir_entries.append(entry)

        return nblocks

    def _write_directory(self):
        """Write all directory entries to track 18, sector 1+."""
        entries = self._dir_entries
        # Pad to multiple of 8 (8 entries per sector)
        while len(entries) % 8 != 0:
            entries.append(bytearray(32))

        dir_sectors = []
        for i in range(0, len(entries), 8):
            dir_sectors.append(entries[i:i+8])

        for i, block in enumerate(dir_sectors):
            sec_num = DIR_SECTOR + i
            sector = bytearray(256)
            if i + 1 < len(dir_sectors):
                sector[0] = DIR_TRACK
                sector[1] = DIR_SECTOR + i + 1
            else:
                sector[0] = 0x00
                sector[1] = 0xFF
            for j, entry in enumerate(block):
                # First entry in first sector: byte $00 is the link, not file-type,
                # so the file-type for entry 0 of each sector is at offset $02+j*32
                # Wait: the standard dir layout is:
                #   sector byte 0-1: link to next dir sector
                #   bytes 2-33: entry 0
                #   bytes 34-65: entry 1
                #   ... 8 entries per sector
                off = 2 + j * 32
                sector[off:off + 32] = entry
            self._write_sector(DIR_TRACK, DIR_SECTOR + i, sector)

    def save(self, path):
        """Write the D64 image to a file."""
        self._write_directory()
        with open(path, 'wb') as f:
            f.write(self._data)


# ── convenience wrapper ──────────────────────────────────────────────────────

def build_d64(output_path, files, disk_name="SIDVIZ", disk_id="SV"):
    """
    Create a D64 image.

    files : list of (filename_str, bytes_data) tuples.
            The first file becomes the autostart target of LOAD"*",8,1.
    """
    d = D64(disk_name=disk_name, disk_id=disk_id)
    for name, data in files:
        blocks = d.add_prg(name, data)
        print(f"[d64] added '{name}': {len(data)} bytes ({blocks} blocks)")
    d.save(output_path)
    print(f"[d64] saved: {output_path}  ({sum(_SECTORS[1:36]) * 256 // 1024} KB capacity)")
