"""Builders for hostile-but-decodable test videos (R3-12, R3-14).

Adapted from the round-3 audit PoCs: marker NAL units of arbitrary types in
an Annex B stream, and an MP4 whose video track has a second sample
description carrying its own decoder configuration."""

from __future__ import annotations

import struct

MARK = b"GPS:50.0755N,14.4378E SERIAL:CAM123456"


def inject_nal_units(annexb: bytes, types: list[int], *, hevc: bool) -> bytes:
    """Insert one marker NAL unit of each type in ``types`` before every IDR
    (H.264 type 5) / IRAP (HEVC 19, 20) slice."""
    parts = annexb.split(b"\x00\x00\x01")
    out = []
    for p in parts:
        if p:
            t = (p[0] >> 1) & 0x3F if hevc else p[0] & 0x1F
            if (not hevc and t == 5) or (hevc and t in (19, 20)):
                for ty in types:
                    header = bytes([ty << 1, 1]) if hevc else bytes([ty])
                    out.append(header + MARK + b"\x80\x00")
        out.append(p)
    return b"\x00\x00\x01".join(out)


def add_second_sample_description(mp4: bytes) -> bytes:
    """Split the (only) video track's samples over two chunks and give the
    second chunk its own avc1 sample description, whose avcC carries an SEI
    with the marker. Needs moov after mdat (no faststart), one trak."""
    d = bytearray(mp4)

    def box(t):
        a = d.find(t) - 4
        return a, struct.unpack(">I", d[a : a + 4])[0]

    assert box(b"moov")[0] > d.find(b"mdat")
    a, _ = box(b"stsz")
    n = struct.unpack(">I", d[a + 16 : a + 20])[0]
    sizes = struct.unpack(f">{n}I", d[a + 20 : a + 20 + 4 * n])
    a, _ = box(b"stco")
    off0 = struct.unpack(">I", d[a + 16 : a + 20])[0]
    half = n // 2
    off1 = off0 + sum(sizes[:half])
    new_stco = struct.pack(">I4sII", 24, b"stco", 0, 2) + struct.pack(">II", off0, off1)
    new_stsc = (
        struct.pack(">I4sII", 40, b"stsc", 0, 2) + struct.pack(">III", 1, half, 1) + struct.pack(">III", 2, n - half, 2)
    )
    sa, ss = box(b"stsd")
    ea = d.find(b"avc1", sa) - 4
    es = struct.unpack(">I", d[ea : ea + 4])[0]
    entry = bytearray(d[ea : ea + es])
    ca = entry.find(b"avcC") - 4
    cs = struct.unpack(">I", entry[ca : ca + 4])[0]
    j = ca + 8 + 5
    nsps = entry[j] & 0x1F
    j += 1
    for _ in range(nsps):
        length = struct.unpack(">H", entry[j : j + 2])[0]
        j += 2 + length
    entry[j] += 1
    j += 1
    nal = bytes([0x06, 0x05, len(MARK) + 16]) + b"\x11" * 16 + MARK + b"\x80"
    payload = struct.pack(">H", len(nal)) + nal
    entry[j:j] = payload
    entry[ca : ca + 4] = struct.pack(">I", cs + len(payload))
    entry[0:4] = struct.pack(">I", es + len(payload))
    new_stsd = bytearray(d[sa : sa + ss])
    new_stsd[12:16] = struct.pack(">I", 2)
    new_stsd += entry
    new_stsd[0:4] = struct.pack(">I", len(new_stsd))

    def replace(t, new):
        nonlocal d
        a, s = box(t)
        delta = len(new) - s
        for anc in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
            x, y = box(anc)
            if x < a < x + y:
                d[x : x + 4] = struct.pack(">I", y + delta)
        d[a : a + s] = new

    replace(b"stco", new_stco)
    replace(b"stsc", new_stsc)
    replace(b"stsd", bytes(new_stsd))
    return bytes(d)
