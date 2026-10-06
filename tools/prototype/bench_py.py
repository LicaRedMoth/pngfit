#!/usr/bin/env python3
"""Benchmark pngfit on a sample of the PXL dataset: exact-hit rate, quality, time."""
import csv, glob, os, random, sys, tempfile, time
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pngfit

D = os.environ.get("PNGFIT_DATASET") or sys.exit("set PNGFIT_DATASET to the dataset root")
SETS = {"kodak": ("Kodak-Lossless-True-Color-Image-Suite", 24),
        "clic": ("CLIC-2020-mobile-train", 8),
        "usc-sipi": ("USC-SIPI-Image-Database", 12),
        "screenshots": ("Screenshots", 16),
        "synthetic": ("Synthetic-Screenshots", 8)}
TARGETS = ["90%", "75%"]
out_csv, tmpdir = sys.argv[1], sys.argv[2]
rng = random.Random(1)
done = set()
if os.path.exists(out_csv):   # resume: skip what is already measured
    with open(out_csv) as fh:
        done = {(r["set"], r["file"], r["target"]) for r in csv.DictReader(fh)}
with open(out_csv, "a", newline="") as fh:
    w = csv.writer(fh)
    if not done:
        w.writerow(["set", "file", "target", "lossless", "size", "exact", "psnr", "maxerr", "changed", "time"])
    for name, (sub, n) in SETS.items():
        files = sorted(glob.glob(f"{D}/{sub}/**/*.png", recursive=True))
        def ok(f):
            try:
                return Image.open(f).mode in ("L", "LA", "RGB", "RGBA")
            except Exception:   # empty/broken files exist in the dataset
                return False
        files = [f for f in files if ok(f)]
        for f in (files if len(files) <= n else rng.sample(files, n)):
            for t in TARGETS:
                if (name, os.path.relpath(f, D), t) in done:
                    continue
                dst = os.path.join(tmpdir, "b.png")
                a = pngfit.build_parser().parse_args([f, dst, "--size", t, "-q"])
                try:
                    r = pngfit.fit(a, lambda *x: None)
                except pngfit.FitError as e:
                    print(name, os.path.basename(f), t, "ERR", e, flush=True); continue
                finally:
                    if os.path.exists(dst): os.remove(dst)
                row = [name, os.path.relpath(f, D), t, r["lossless"], r["size"], r["size"] == r["target"],
                       f"{r['psnr']:.2f}", r["maxerr"], f"{r['changed']:.3f}", f"{r['time']:.1f}"]
                w.writerow(row); fh.flush()
                print(*row, flush=True)
