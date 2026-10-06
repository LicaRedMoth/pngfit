#!/usr/bin/env python3
"""Self-test for the pngfit binary (`make check`). Needs Python 3 with numpy and Pillow.

Builds its own test images, runs pngfit on them and checks every output with the
independent decoder in tools/check.py: exact size, error as reported, alpha untouched,
APNG frame controls, lossless format reductions, refusals, batch mode.

  tools/selftest.py [path/to/pngfit]
"""
import json, os, shutil, struct, subprocess, sys, tempfile, zlib
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from check import decode_frames  # noqa: E402

BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, os.pardir, "pngfit"))
TMP = tempfile.mkdtemp(prefix="pngfit-selftest-")
rng = np.random.default_rng(2026)
fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{(': ' + detail) if detail and not cond else ''}", flush=True)
    if not cond:
        fails.append(name)


def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))


CT = {1: 0, 3: 2, 2: 4, 4: 6}


def rows(arr, depth):
    return [(r.astype(">u2") if depth == 16 else r.astype(np.uint8)).tobytes() for r in arr]


def adam7(arr, depth):
    out = b""
    for x0, y0, dx, dy in [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]:
        sub = arr[y0::dy, x0::dx]
        if sub.size:
            out += b"".join(b"\0" + r for r in rows(sub, depth))
    return out


def png(path, arr, depth=8, extra=b"", interlace=False):
    h, w, c = arr.shape
    raw = adam7(arr, depth) if interlace else b"".join(b"\0" + r for r in rows(arr, depth))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, CT[c], 0, 0, int(interlace)))
                + extra + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return path


def apng(path, frames, canvas, plays=0):
    """frames: (array, x, y, delay_num, delay_den, dispose, blend); RGBA 8-bit"""
    W, H = canvas
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
    out += chunk(b"acTL", struct.pack(">II", len(frames), plays))
    seq = 0
    for i, (a, x, y, dn, dd, dis, bl) in enumerate(frames):
        h, w, _ = a.shape
        out += chunk(b"fcTL", struct.pack(">IIIIIHHBB", seq, w, h, x, y, dn, dd, dis, bl)); seq += 1
        z = zlib.compress(b"".join(b"\0" + r for r in rows(a, 8)), 9)
        if i == 0:
            out += chunk(b"IDAT", z)
        else:
            out += chunk(b"fdAT", struct.pack(">I", seq) + z); seq += 1
    open(path, "wb").write(out + chunk(b"IEND", b""))
    return path


def photo(h, w, c=3, depth=8):
    """photo-like: smooth shapes, an edge, grain"""
    y, x = np.mgrid[:h, :w].astype(float)
    base = [128 + 60 * np.sin(x / 17 + k) * np.cos(y / 23 - k) + 40 * (x + y > w) for k in range(3)]
    img = np.stack(base[:max(1, min(c, 3))], 2) + rng.normal(0, 6, (h, w, max(1, min(c, 3))))
    if c in (2, 4):
        alpha = np.clip((np.hypot(y - h / 2, x - w / 2) - h / 4) * -20 + 128, 0, 255)[..., None]
        img = np.concatenate([img, alpha], 2)
    img = np.clip(img, 0, 255)
    if depth == 16:
        return np.clip(img * 257 + rng.normal(0, 40, img.shape), 0, 65535).astype(np.int64)
    return img.astype(np.int64)


def run(args, expect_rc=(0,)):
    p = subprocess.run([BIN, "-q", "--json"] + args, capture_output=True, text=True, errors="strict")
    rs = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    if p.returncode not in expect_rc:
        fails.append(f"exit code {p.returncode} for {' '.join(args)}: {p.stderr.strip()[:200]}")
    return rs[0] if len(rs) == 1 else rs


def frames_of(path):
    fr, plays, depth = decode_frames(path)
    return fr, plays, depth


def same_pixels_after(src, out):
    """worst error between source and output frames, output expanded back to the source format"""
    fa, _, da = frames_of(src)
    fb, _, db = frames_of(out)
    if len(fa) != len(fb):
        return None
    worst = 0
    for (ca, a), (cb, b) in zip(fa, fb):
        if a.shape[:2] != b.shape[:2] or ca != cb:
            return None
        if b.shape[2] < a.shape[2]:
            Ca, Cb = a.shape[2], b.shape[2]
            if Cb in (1, 2) and Ca in (3, 4):          # grey reduction
                b = np.concatenate([b[..., :1]] * 3 + ([b[..., 1:2]] if Cb == 2 else []), 2)
            if b.shape[2] < Ca:                          # opaque alpha dropped
                b = np.concatenate([b, np.full(b.shape[:2] + (1,), 255 if db == 8 else 65535)], 2)
        if db == 8 and da == 16:
            b = b * 257
        worst = max(worst, int(abs(a - b).max()))
    return worst


def main():
    print(f"pngfit self-test: {BIN}")
    if not os.path.exists(BIN):
        sys.exit("binary not found: build it with make first")

    print("formats, two targets each")
    cases = [("grey8", photo(96, 128, 1)), ("greyalpha8", photo(96, 128, 2)), ("rgb8", photo(96, 128, 3)),
             ("rgba8", photo(96, 128, 4)), ("grey16", photo(80, 96, 1, 16)), ("rgb16", photo(80, 96, 3, 16))]
    for name, arr in cases:
        depth = 16 if name.endswith("16") else 8
        src = png(f"{TMP}/{name}.png", arr, depth)
        for t in ("90%", "65%"):
            out = f"{TMP}/{name}_{t[:-1]}.png"
            r = run([src, out, "-s", t])
            err = same_pixels_after(src, out) if os.path.exists(out) else None
            check(f"{name} at {t}: exact size", r.get("status") == "exact" and os.path.getsize(out) == r["target"],
                  str(r))
            check(f"{name} at {t}: error as reported", err == r.get("max_error"), f"{err} vs {r.get('max_error')}")
            if arr.shape[2] in (2, 4):
                fa, _, _ = frames_of(src)
                fb, _, _ = frames_of(out)
                check(f"{name} at {t}: alpha untouched", (fa[0][1][..., -1] == fb[0][1][..., -1]).all())
    src = png(f"{TMP}/interlaced.png", photo(77, 101, 3), interlace=True)
    r = run([src, f"{TMP}/interlaced_out.png", "-s", "80%"])
    check("interlaced input: exact", r.get("status") == "exact")
    check("interlaced input: error as reported",
          same_pixels_after(src, f"{TMP}/interlaced_out.png") == r.get("max_error"))
    for name, (h, w) in (("tiny", (1, 1)), ("three rows", (3, 160)), ("one column", (90, 1))):
        src = png(f"{TMP}/small.png", photo(h, w, 3))
        r = run([src, f"{TMP}/small_out.png", "-s", "90%"], (0, 1))
        check(f"{name}: handled", r.get("status") in ("exact", "skipped", "lossless", "minimum", "short"), str(r))

    print("lossless format reduction")
    a = photo(64, 80, 3)
    grey = a[..., :1]
    for name, arr, depth, want_c, want_d in (("opaque RGBA", np.concatenate([a, np.full(a.shape[:2] + (1,), 255)], 2), 8, 3, 8),
                                            ("grey RGB", np.concatenate([grey] * 3, 2), 8, 1, 8),
                                            ("8-bit values in 16 bits", a * 257, 16, 3, 8)):
        src = png(f"{TMP}/red.png", arr, depth)
        for t in ("100%", "80%"):
            out = f"{TMP}/red_out.png"
            r = run([src, out, "-s", t])
            fb, _, db = frames_of(out)
            check(f"{name} at {t}: stored as {want_c} channel(s), {want_d}-bit",
                  fb[0][1].shape[2] == want_c and db == want_d, f"{fb[0][1].shape[2]} ch, {db}-bit")
            err = same_pixels_after(src, out)
            if t == "100%":
                check(f"{name}: lossless at full size", err == 0, str(err))
            else:
                check(f"{name} at 80%: exact", r.get("status") == "exact", str(r))
        r = run([src, f"{TMP}/red_out.png", "-s", "100%", "--no-reduce"])
        fb, _, db = frames_of(f"{TMP}/red_out.png")
        check(f"{name}: --no-reduce keeps the format", fb[0][1].shape[2] == arr.shape[2] and db == depth)

    print("animated PNG")
    f0 = photo(48, 64, 4)
    f1 = photo(20, 30, 4)
    f2 = photo(48, 64, 4)
    f3 = photo(16, 16, 4)
    src = apng(f"{TMP}/anim.png", [(f0, 0, 0, 1, 10, 0, 0), (f1, 10, 12, 2, 25, 1, 1), (f2, 0, 0, 1, 10, 2, 1),
                                   (f3, 40, 30, 7, 100, 0, 0)], (64, 48), plays=3)
    for t in ("85%", "60%"):
        out = f"{TMP}/anim_out.png"
        r = run([src, out, "-s", t])
        check(f"APNG at {t}: exact", r.get("status") == "exact", str(r))
        fa, pa, _ = frames_of(src)
        fb, pb, _ = frames_of(out)
        check(f"APNG at {t}: frames, delays, offsets, dispose, blend, loop count identical",
              len(fa) == len(fb) and pa == pb and all(x[0] == y[0] for x, y in zip(fa, fb)))
        check(f"APNG at {t}: error as reported", same_pixels_after(src, out) == r.get("max_error"))
    r = run([src, f"{TMP}/anim_min.png", "-s", "0"])
    check("APNG at 0: the minimum, still all frames", r.get("status") == "minimum" and
          len(frames_of(f"{TMP}/anim_min.png")[0]) == 4, str(r))

    print("--alpha: colour under transparent pixels")
    st = photo(96, 128, 4)
    st[..., 3] = np.where(st[..., 3] < 60, 0, st[..., 3])
    src = png(f"{TMP}/sticker.png", st)
    sizes, target = {}, "70%"
    for opt in ([], ["--alpha"]):   # same bytes: with --alpha even "100%" is smaller, so pass the byte count
        out = f"{TMP}/sticker_out{len(opt)}.png"
        r = run([src, out, "-s", target] + opt)
        target = str(r.get("size"))
        fa, _, _ = frames_of(src)
        fb, _, _ = frames_of(out)
        a, b = fa[0][1], fb[0][1]
        vis = a[..., 3] > 0
        d = (a[vis][:, :3] - b[vis][:, :3]).astype(float)
        sizes[len(opt)] = round(10 * np.log10(255 ** 2 / max((d ** 2).mean(), 1e-9)), 2)
        # with --alpha the visible-lossless file may already fit those bytes: "lossless" is the best outcome
        check(f"sticker {' '.join(opt) or 'default'}: {r.get('status')} at {r.get('size')} B, alpha untouched",
              r.get("status") in (("exact", "lossless") if opt else ("exact",)) and (a[..., 3] == b[..., 3]).all())
    check(f"--alpha: visible pixels better within the same bytes, PSNR {sizes[0]} -> {sizes[1]} dB",
          sizes[1] > sizes[0], str(sizes))

    print("--max-error: a hard cap on the error")
    src = png(f"{TMP}/cap.png", photo(96, 128, 3))
    for e, t in (("1", "80%"), ("1", "40%"), ("3", "50%"), ("0", "90%")):
        out = f"{TMP}/cap_out.png"
        r = run([src, out, "-s", t, "-e", e])
        err = same_pixels_after(src, out)
        check(f"-e {e} at {t}: {r.get('status')}, worst error {err} <= {e}",
              err is not None and err <= int(e) and r.get("status") in ("exact", "minimum", "short", "lossless"), str(r))
    r0 = run([src, f"{TMP}/cap_a.png", "-s", "40%"])
    r1 = run([src, f"{TMP}/cap_b.png", "-s", "40%", "-e", "1"])
    check("-e with a percent target: the same target as without", r0.get("target") == r1.get("target"),
          f"{r0.get('target')} vs {r1.get('target')}")

    print("targets that cannot be met")
    src = png(f"{TMP}/edge.png", photo(64, 96, 3))
    r = run([src, f"{TMP}/edge_out.png", "-s", str(os.path.getsize(src) + 10)])
    check("source already fits: untouched, nothing written",
          r.get("status") == "skipped" and not os.path.exists(f"{TMP}/edge_out.png"))
    for t in ("0", "0%", "1"):
        r = run([src, f"{TMP}/edge_out.png", "-s", t])
        check(f"-s {t}: smallest file, valid", r.get("status") == "minimum" and
              same_pixels_after(src, f"{TMP}/edge_out.png") is not None, str(r))
    p = subprocess.run([BIN, src, f"{TMP}/x.png", "-s", "-5"], capture_output=True)
    check("-s -5: refused as a typo", p.returncode == 2)

    print("metadata")
    meta = chunk(b"gAMA", struct.pack(">I", 45455)) + chunk(b"tEXt", b"Comment\0kept by default")
    src = png(f"{TMP}/meta.png", photo(64, 96, 3), extra=meta)
    for opt, want in (([], {b"gAMA", b"tEXt"}), (["--strip", "meta"], {b"gAMA"}), (["-k", "none"], set())):
        out = f"{TMP}/meta_out.png"
        r = run([src, out, "-s", "85%"] + opt)
        d = open(out, "rb").read()
        have = set()
        i = 8
        while i < len(d):
            n, = struct.unpack(">I", d[i:i + 4]); have.add(d[i + 4:i + 8]); i += 12 + n
        check(f"metadata {' '.join(opt) or 'default'}: exact, kept {sorted(x.decode() for x in want) or 'nothing'}",
              r.get("status") == "exact" and have & {b"gAMA", b"tEXt"} == want, str(sorted(have)))

    print("refusals")
    from PIL import Image
    Image.fromarray(photo(32, 32, 3).astype(np.uint8)).quantize(16).save(f"{TMP}/palette.png")
    png(f"{TMP}/trns.png", photo(32, 32, 3), extra=chunk(b"tRNS", struct.pack(">HHH", 0, 0, 0)))
    good = open(png(f"{TMP}/good.png", photo(32, 32, 3)), "rb").read()
    open(f"{TMP}/truncated.png", "wb").write(good[:len(good) // 2])
    open(f"{TMP}/notpng.png", "wb").write(b"definitely not a PNG")
    bad = bytearray(good); bad[40] ^= 0xFF
    open(f"{TMP}/badcrc.png", "wb").write(bytes(bad))
    for name in ("palette", "trns", "truncated", "notpng", "badcrc"):
        r = run([f"{TMP}/{name}.png", f"{TMP}/r.png", "-s", "50%"], (1,))
        check(f"{name}: refused with a reason", r.get("status") == "error" and r.get("error"), str(r))
    shutil.copy(src, f"{TMP}/same.png")
    r = run([f"{TMP}/same.png", f"{TMP}/same.png", "-s", "80%"], (1,))
    check("output = input: refused, source intact",
          r.get("status") == "error" and open(f"{TMP}/same.png", "rb").read() == open(src, "rb").read())

    print("batch mode")
    ins = [png(f"{TMP}/b{i}.png", photo(48 + 8 * i, 64, 3)) for i in range(4)] + [f"{TMP}/notpng.png"]
    for par in ("1", "2"):
        os.makedirs(f"{TMP}/out{par}", exist_ok=True)
        rs = run(["-s", "80%", "-o", f"{TMP}/out{par}", "-P", par] + ins, (1,))
        check(f"batch -P {par}: one JSON line per file, the bad one failed alone",
              len(rs) == 5 and sum(r["status"] == "exact" for r in rs) == 4)
    check("batch: parallel output identical to one by one",
          all(open(f"{TMP}/out1/b{i}.png", "rb").read() == open(f"{TMP}/out2/b{i}.png", "rb").read() for i in range(4)))

    # more threads than this machine has are fine: the file must not depend on the count
    same = True
    for src in ins[:4]:
        outs = set()
        for jobs in ("1", "4", "7"):
            run([src, f"{TMP}/j.png", "-s", "80%", "-j", jobs])
            outs.add(open(f"{TMP}/j.png", "rb").read())
        same &= len(outs) == 1
    check("threads: the same bytes with -j 1, 4 and 7", same)

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'all passed' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
