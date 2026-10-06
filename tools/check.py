#!/usr/bin/env python3
"""Independent check of a pngfit output: decode both PNGs with a small reference
decoder written here (numpy + zlib, 8/16-bit, any colour type, Adam7), then report
size, max error and PSNR. Pillow is not used because it truncates 16-bit RGB.

  tools/check.py SOURCE.png OUTPUT.png [EXPECTED_SIZE]   # exit 1 on mismatch
"""
import struct, sys, zlib
import numpy as np

A7 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]


try:                       # numba only makes it fast; the logic is plain Python either way
    from numba import njit
except ImportError:
    def njit(f):
        return f


@njit
def _unfilter(buf, h, rowbytes, bpp, out):
    pos = 0
    for y in range(h):
        ft = buf[pos]
        pos += 1
        for i in range(rowbytes):
            a = out[y, i - bpp] if i >= bpp else 0
            b = out[y - 1, i] if y > 0 else 0
            c = out[y - 1, i - bpp] if (y > 0 and i >= bpp) else 0
            if ft == 0:
                p = 0
            elif ft == 1:
                p = a
            elif ft == 2:
                p = b
            elif ft == 3:
                p = (a + b) >> 1
            elif ft == 4:
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                p = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            else:
                return -1
            out[y, i] = (buf[pos + i] + p) & 255
        pos += rowbytes
    return pos


def unfilter(raw, h, rowbytes, bpp):
    out = np.zeros((h, rowbytes), np.int32)
    pos = _unfilter(np.frombuffer(raw, np.uint8).astype(np.int32), h, rowbytes, bpp, out)
    assert pos >= 0, "bad filter type"
    return out.astype(np.uint8), pos


def decode_frames(path):
    """APNG-aware: [(fcTL fields without the sequence number, or None for a default
    image outside the animation, pixels)], plus num_plays (None for a still)."""
    d = open(path, "rb").read()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i, chunks = 8, []
    while i < len(d):
        n, = struct.unpack(">I", d[i:i + 4]); t = d[i + 4:i + 8]; body = d[i + 8:i + 8 + n]
        assert zlib.crc32(t + body) == struct.unpack(">I", d[i + 8 + n:i + 12 + n])[0], f"CRC {t}"
        chunks.append((t, body)); i += 12 + n
    W, H, depth, ct, _, _, il = struct.unpack(">IIBBBBB", chunks[0][1])
    C = {0: 1, 2: 3, 4: 2, 6: 4}[ct]; bps = depth // 8; bpp = C * bps
    anim = any(t == b"acTL" for t, _ in chunks)
    plays = struct.unpack(">I", next(b for t, b in chunks if t == b"acTL")[4:8])[0] if anim else None
    frames, cur, seq = [], None, []
    for t, b in chunks:
        if anim and t == b"fcTL":
            seq.append(struct.unpack(">I", b[:4])[0])
            cur = [b[4:], b""]; frames.append(cur)
        elif t == b"IDAT":
            if cur is None or (frames and frames[-1] is not cur):
                cur = [None, b""]; frames.append(cur)
            cur[1] += b
        elif anim and t == b"fdAT":
            seq.append(struct.unpack(">I", b[:4])[0]); cur[1] += b[4:]
    assert seq == list(range(len(seq))), f"APNG sequence numbers out of order: {seq[:8]}"
    out = []
    for fc, z in frames:
        w, h = (W, H) if fc is None else struct.unpack(">II", fc[:8])
        raw = zlib.decompress(z)
        if not il:
            px, _ = unfilter(raw, h, w * bpp, bpp); px = px.reshape(h, w, bpp)
        else:
            px = np.zeros((h, w, bpp), np.uint8); pos = 0
            for x0, y0, dx, dy in A7:
                if w <= x0 or h <= y0:
                    continue
                pw, ph = (w - x0 + dx - 1) // dx, (h - y0 + dy - 1) // dy
                sub, used = unfilter(raw[pos:], ph, pw * bpp, bpp); pos += used
                px[y0::dy, x0::dx] = sub.reshape(ph, pw, bpp)
        if bps == 2:
            px = (px[..., 0::2].astype(np.int64) << 8) | px[..., 1::2]
        out.append((fc, px.astype(np.int64)))
    return out, plays, depth


def worst_error(src, out):
    """Worst per-sample error between a source and a pngfit output, PNG or APNG, with the
    output expanded back to the source's format (pngfit may reduce it losslessly: opaque
    alpha dropped, grey from colour, 8-bit from 16). In the output's units, as pngfit
    reports it. None if frames or frame controls differ."""
    fa, pa, da = decode_frames(src)
    fb, pb, db = decode_frames(out)
    if len(fa) != len(fb) or pa != pb:
        return None
    worst = 0
    for (ca, a), (cb, b) in zip(fa, fb):
        if ca != cb or a.shape[:2] != b.shape[:2]:
            return None
        Ca, Cb = a.shape[2], b.shape[2]
        if Cb < Ca:
            if Cb in (1, 2) and Ca in (3, 4):
                b = np.concatenate([b[..., :1]] * 3 + ([b[..., 1:2]] if Cb == 2 else []), 2)
            if b.shape[2] < Ca:
                b = np.concatenate([b, np.full(b.shape[:2] + (1,), 255 if db == 8 else 65535)], 2)
        if db == 8 and da == 16:
            b = b * 257
        e = int(abs(a - b).max())
        worst = max(worst, e // 257 if (db == 8 and da == 16) else e)
    return worst


def decode(path):
    d = open(path, "rb").read()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i, idat, chunks = 8, b"", []
    while i < len(d):
        n, = struct.unpack(">I", d[i:i + 4]); t = d[i + 4:i + 8]; body = d[i + 8:i + 8 + n]
        assert zlib.crc32(t + body) == struct.unpack(">I", d[i + 8 + n:i + 12 + n])[0], f"CRC {t}"
        chunks.append(t)
        if t == b"IHDR":
            W, H, depth, ct, _, _, il = struct.unpack(">IIBBBBB", body)
        if t == b"IDAT":
            idat += body
        i += 12 + n
    C = {0: 1, 2: 3, 4: 2, 6: 4}[ct]; bps = depth // 8; bpp = C * bps
    raw = zlib.decompress(idat)
    if not il:
        px, _ = unfilter(raw, H, W * bpp, bpp)
        px = px.reshape(H, W, bpp)
    else:
        px = np.zeros((H, W, bpp), np.uint8); pos = 0
        for x0, y0, dx, dy in A7:
            if W <= x0 or H <= y0:
                continue
            pw, ph = (W - x0 + dx - 1) // dx, (H - y0 + dy - 1) // dy
            sub, used = unfilter(raw[pos:], ph, pw * bpp, bpp); pos += used
            px[y0::dy, x0::dx] = sub.reshape(ph, pw, bpp)
    if bps == 2:
        px = (px[..., 0::2].astype(np.int64) << 8) | px[..., 1::2]
    return px.astype(np.int64), depth, chunks


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--apng":
    src, out = sys.argv[2], sys.argv[3]
    fa, pa, da = decode_frames(src); fb, pb, db = decode_frames(out)
    ok = len(fa) == len(fb) and pa == pb and da == db
    err, n = 0, 0
    for (ca, xa), (cb, xb) in zip(fa, fb):
        ok &= ca == cb and xa.shape == xb.shape   # size, offset, delay, dispose, blend all equal
        if xa.shape == xb.shape:
            err = max(err, int(abs(xa - xb).max())); n += 1
    print(f"{out}: {len(open(out, 'rb').read())} B, {len(fb)} frames (source {len(fa)}), plays {pb}, "
          f"frame controls {'identical' if ok else 'DIFFER'}, max error {err} -> {'OK' if ok else 'MISMATCH'}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    a, da, _ = decode(src); b, db, ch = decode(out)
    ok = a.shape == b.shape and da == db
    size = len(open(out, "rb").read())
    if len(sys.argv) > 3:
        ok &= size == int(sys.argv[3])
    d = (a - b) if a.shape == b.shape else np.array([0])
    peak = 255 if da == 8 else 65535
    mse = float((d.astype(np.float64) ** 2).mean())
    psnr = 10 * np.log10(peak ** 2 / mse) if mse else float("inf")
    print(f"{out}: {size} B, {b.shape[1]}x{b.shape[0]} {db}-bit, max error {int(abs(d).max())}, "
          f"PSNR {psnr:.2f} dB, chunks {b' '.join(ch).decode()} -> {'OK' if ok else 'MISMATCH'}")
    sys.exit(0 if ok else 1)
