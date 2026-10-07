#!/usr/bin/env python3
"""Rate-distortion data for one image, for tools/charts.py.

  tools/rd_data.py IMAGE.png OUT.csv codecs [NAMES]  # JPEG 4:4:4, WebP, AVIF 4:4:4 sweeps
  tools/rd_data.py IMAGE.png OUT.csv uniform    # the demo video's uniform ladder (prototype)
  tools/rd_data.py IMAGE.png OUT.csv pngfit [FRACTIONS]  # real pngfit runs at exact byte targets

Rows are appended: method, setting, bytes, pct_of_source, psnr, max_error.
Sizes are percentages of the source PNG file.
"""
import csv, io, os, subprocess, sys, tempfile, time
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
src, out, what = sys.argv[1:4]
SRC_BYTES = os.path.getsize(src)
img = np.asarray(Image.open(src).convert("RGB"))
ref = img.astype(np.int32)


def stats(rec):
    d = rec.astype(np.int32) - ref
    mse = float((d.astype(np.float64) ** 2).mean())
    return (10 * np.log10(255 ** 2 / mse) if mse else float("inf")), int(np.abs(d).max())


def emit(rows):
    new = not os.path.exists(out)
    with open(out, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["method", "setting", "bytes", "pct_of_source", "psnr", "max_error"])
        for r in rows:
            w.writerow(r)
            print(*r, flush=True)


if what == "codecs":
    pil = Image.fromarray(img)
    for name, qs, kw in (("JPEG 4:4:4", (50, 70, 80, 85, 90, 93, 95, 97, 98, 99, 100),
                          dict(format="JPEG", subsampling=0, optimize=True)),
                         ("WebP", (50, 70, 80, 85, 90, 95, 98, 100), dict(format="WEBP", method=4)),
                         ("AVIF 4:4:4", (50, 70, 80, 85, 90, 95, 98, 99),
                          dict(format="AVIF", speed=8, subsampling="4:4:4"))):
        if len(sys.argv) > 4 and name not in sys.argv[4].split(","):
            continue
        rows = []
        for q in qs:
            b = io.BytesIO()
            try:
                pil.save(b, quality=q, **kw)
            except Exception as e:   # an encoder missing from this Pillow build
                print(name, "skipped:", e)
                break
            rec = np.asarray(Image.open(io.BytesIO(b.getvalue())).convert("RGB"))
            p, m = stats(rec)
            rows.append([name, f"q{q}", b.tell(), 100 * b.tell() / SRC_BYTES, f"{p:.3f}", m])
        emit(rows)

elif what == "uniform":
    sys.path.insert(0, os.path.join(HERE, "prototype"))
    import pngfit as proto   # needs tools/prototype/libfitcore.so
    from scipy.ndimage import uniform_filter
    H, W, C = img.shape
    T, S = np.load(sys.argv[4] if len(sys.argv) > 4 else os.path.join(HERE, os.pardir, "video", "curve_clamp.npy"))
    lum = img.astype(np.float32) @ np.array([.299, .587, .114], np.float32)
    m = uniform_filter(lum, 5)
    act = np.sqrt(np.maximum(uniform_filter(lum * lum, 5) - m * m, 0))
    rank = np.empty(H * W, np.int64)
    rank[np.argsort(act, axis=None, kind="stable")] = np.arange(H * W)
    rank = rank.reshape(H, W)
    ladder = np.arange(1, 256, 2).astype(np.int32)
    rows = []
    for t, s in zip(T, S):
        rung = np.minimum((int(t * H * W) + rank) // (H * W), len(ladder) - 1)
        out_f = np.empty((H, 1 + W * C), np.uint8); rec = np.empty_like(img)
        proto.quant_strip(img, np.zeros((W, C), np.uint8), rung, ladder, 3, 0.1, False, np.ones(C, np.bool_),
                          np.ones((H, W)), out_f, rec, proto.COST_TAB, True)
        p, mx = stats(rec)
        rows.append(["uniform ladder (demo video)", f"t{t:g}", int(s), 100 * s / SRC_BYTES, f"{p:.3f}", mx])
    emit(rows)

elif what == "pngfit":
    binary = os.path.join(HERE, os.pardir, "pngfit")
    if len(sys.argv) > 4:   # fractions of the source, e.g. 0.95,0.9,0.8
        targets = [round(float(f) * SRC_BYTES) for f in sys.argv[4].split(",")]
    else:                   # the Anomie cover: plus Bandcamp's limit, 10 MiB
        targets = [round(f * SRC_BYTES) for f in (0.95, 0.9, 0.7, 0.5, 0.35, 0.2, 0.1, 0.05)] + [10_485_760]
    tmp = tempfile.mkdtemp()
    for t in sorted(targets, reverse=True):
        dst = os.path.join(tmp, "o.png")
        t0 = time.time()
        p = subprocess.run([binary, "-q", "--json", "-k", "none", src, dst, "-s", str(t)], capture_output=True, text=True)
        import json
        r = json.loads(p.stdout)
        if r["status"] not in ("exact", "short", "minimum"):
            print("skip", t, r.get("status"), r.get("error", ""))
            continue
        rec = np.asarray(Image.open(dst).convert("RGB"))
        ps, mx = stats(rec)
        os.remove(dst)
        emit([["pngfit (exact size)", r["status"], r["size"], 100 * r["size"] / SRC_BYTES, f"{ps:.3f}", mx]])
        print(f"  {time.time() - t0:.0f}s", flush=True)
