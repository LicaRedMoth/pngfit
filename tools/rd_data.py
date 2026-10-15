#!/usr/bin/env python3
"""Rate-distortion data for one image, for tools/charts.py. Appends rows of method, setting, bytes,
size in % of the source PNG file, PSNR and worst error to the CSV.

  tools/rd_data.py IMAGE.png OUT.csv codecs [NAMES]        JPEG 4:4:4, WebP and AVIF 4:4:4 quality sweeps
  tools/rd_data.py IMAGE.png OUT.csv uniform [CURVE.npy]   the demo video's uniform ladder (prototype)
  tools/rd_data.py IMAGE.png OUT.csv pngfit [FRACTIONS]    real pngfit runs at exact byte targets
"""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
CODECS = {  # name: (quality settings, Pillow save options)
    "JPEG 4:4:4": ((50, 70, 80, 85, 90, 93, 95, 97, 98, 99, 100), dict(format="JPEG", subsampling=0, optimize=True)),
    "WebP": ((50, 70, 80, 85, 90, 95, 98, 100), dict(format="WEBP", method=4)),
    "AVIF 4:4:4": ((50, 70, 80, 85, 90, 95, 98, 99), dict(format="AVIF", speed=8, subsampling="4:4:4")),
}
UNIFORM = "uniform ladder (demo video)"
PNGFIT = "pngfit (exact size)"
COVER_FRACTIONS = (0.95, 0.9, 0.7, 0.5, 0.35, 0.2, 0.1, 0.05)
BANDCAMP_LIMIT = 10_485_760


def quality(image, decoded):
    """PSNR over RGB and the worst sample error."""
    diff = decoded.astype(np.int32) - image.astype(np.int32)
    mse = float((diff.astype(np.float64) ** 2).mean())
    psnr = 10 * np.log10(255**2 / mse) if mse else float("inf")
    return psnr, int(np.abs(diff).max())


def append(path, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["method", "setting", "bytes", "pct_of_source", "psnr", "max_error"])
        for row in rows:
            writer.writerow(row)
            print(*row, flush=True)


def codecs(image, source_bytes, names):
    rows = []
    for name, (settings, options) in CODECS.items():
        if names and name not in names:
            continue

        for q in settings:
            encoded = io.BytesIO()
            try:
                Image.fromarray(image).save(encoded, quality=q, **options)
            except Exception as e:  # an encoder missing from this Pillow build
                print(name, "skipped:", e)
                break

            size = encoded.tell()
            decoded = np.asarray(Image.open(io.BytesIO(encoded.getvalue())).convert("RGB"))
            psnr, worst = quality(image, decoded)
            rows.append([name, f"q{q}", size, 100 * size / source_bytes, f"{psnr:.3f}", worst])
    return rows


def uniform(image, source_bytes, curve):
    """Every pixel's step from its texture rank and one threshold t, as in the demo video."""
    sys.path.insert(0, os.path.join(HERE, "prototype"))
    import pngfit as prototype  # needs tools/prototype/libfitcore.so
    from scipy.ndimage import uniform_filter

    height, width, channels = image.shape
    thresholds, sizes = np.load(curve)

    luma = image.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    mean = uniform_filter(luma, 5)
    texture = np.sqrt(np.maximum(uniform_filter(luma * luma, 5) - mean * mean, 0))
    rank = np.empty(height * width, np.int64)
    rank[np.argsort(texture, axis=None, kind="stable")] = np.arange(height * width)
    rank = rank.reshape(height, width)
    ladder = np.arange(1, 256, 2).astype(np.int32)

    rows = []
    for t, size in zip(thresholds, sizes):
        rung = np.minimum((int(t * height * width) + rank) // (height * width), len(ladder) - 1)
        filtered = np.empty((height, 1 + width * channels), np.uint8)
        decoded = np.empty_like(image)
        prototype.quant_strip(
            img=image,
            prev=np.zeros((width, channels), np.uint8),
            rung=rung,
            ladder=ladder,
            filt=3,
            adaptive=0.1,
            anchor=False,
            chmask=np.ones(channels, np.bool_),
            wgt=np.ones((height, width)),
            out_f=filtered,
            out_rec=decoded,
            tab=prototype.COST_TAB,
            clamp=True,
        )
        psnr, worst = quality(image, decoded)
        rows.append([UNIFORM, f"t{t:g}", int(size), 100 * size / source_bytes, f"{psnr:.3f}", worst])
    return rows


def pngfit_runs(path, image, source_bytes, fractions, csv_path):
    """One row per target, written as soon as it is done: the large ones take minutes."""
    binary = os.path.join(HERE, os.pardir, "pngfit")
    targets = [round(f * source_bytes) for f in fractions]
    if fractions == COVER_FRACTIONS:
        targets.append(BANDCAMP_LIMIT)
    output = os.path.join(tempfile.mkdtemp(), "o.png")

    for target in sorted(targets, reverse=True):
        started = time.time()
        command = [binary, "-q", "--json", "-k", "none", path, output, "-s", str(target)]
        r = json.loads(subprocess.run(command, capture_output=True, text=True).stdout)
        if r["status"] not in ("exact", "short", "minimum"):
            print("skip", target, r.get("status"), r.get("error", ""))
            continue

        decoded = np.asarray(Image.open(output).convert("RGB"))
        os.remove(output)
        psnr, worst = quality(image, decoded)
        size = r["size"]
        append(csv_path, [[PNGFIT, r["status"], size, 100 * size / source_bytes, f"{psnr:.3f}", worst]])
        print(f"  {time.time() - started:.0f}s", flush=True)


def main():
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    path, csv_path, what = sys.argv[1:4]
    extra = sys.argv[4] if len(sys.argv) == 5 else None

    image = np.asarray(Image.open(path).convert("RGB"))
    source_bytes = os.path.getsize(path)

    if what == "codecs":
        append(csv_path, codecs(image, source_bytes, extra.split(",") if extra else None))
    elif what == "uniform":
        curve = extra or os.path.join(HERE, os.pardir, "video", "curve_clamp.npy")
        append(csv_path, uniform(image, source_bytes, curve))
    elif what == "pngfit":
        fractions = tuple(float(f) for f in extra.split(",")) if extra else COVER_FRACTIONS
        pngfit_runs(path, image, source_bytes, fractions, csv_path)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
