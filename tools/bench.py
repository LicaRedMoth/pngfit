#!/usr/bin/env python3
"""Benchmark the pngfit binary on a dataset sample: exact-hit rate, quality, time.

  PNGFIT_DATASET=/path/to/data tools/bench.py results.csv [scratch_dir]

Resumable: rows already in results.csv are skipped. Every output is checked with
tools/check.py's independent decoder before it is deleted.
"""
import csv, glob, json, os, random, subprocess, sys, tempfile
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from check import worst_error  # noqa: E402

BIN = os.path.join(HERE, os.pardir, "pngfit")
D = os.environ.get("PNGFIT_DATASET") or sys.exit("set PNGFIT_DATASET to the dataset root")
SETS = {"kodak": ("Kodak-Lossless-True-Color-Image-Suite", 24),
        "clic": ("CLIC-2020-mobile-train", 8),
        "usc-sipi": ("USC-SIPI-Image-Database", 12),
        "screenshots": ("Screenshots", 16),
        "synthetic": ("Synthetic-Screenshots", 8),
        "pngsuite": ("The-official-test-suite-for-PNG", 10 ** 9),   # every file: robustness
        "apng": ("Animated_PNG_example_bouncing_beach_ball.apng", 1)}
TARGETS = {"pngsuite": ["90%"]}
out_csv = sys.argv[1]
tmpdir = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp()
COLS = ["set", "file", "target", "status", "lossless", "size", "target_bytes", "psnr", "max_error",
        "changed", "depth", "time", "check"]

done = set()
if os.path.exists(out_csv):
    with open(out_csv) as fh:
        done = {(r["set"], r["file"], r["target"]) for r in csv.DictReader(fh)}
rng = random.Random(1)   # the same sample the Python prototype was measured on
with open(out_csv, "a", newline="") as fh:
    w = csv.writer(fh)
    if not done:
        w.writerow(COLS)
    for name, (sub, n) in SETS.items():
        files = [f"{D}/{sub}"] if sub.endswith(".apng") else sorted(glob.glob(f"{D}/{sub}/**/*.png", recursive=True))
        if name not in ("pngsuite", "apng"):
            def ok(f):
                try:
                    return Image.open(f).mode in ("L", "LA", "RGB", "RGBA")
                except Exception:
                    return False
            files = [f for f in files if ok(f)]
        for f in (files if len(files) <= n else rng.sample(files, n)):
            for t in TARGETS.get(name, ["90%", "75%"]):
                rel = os.path.relpath(f, D)
                if (name, rel, t) in done:
                    continue
                dst = os.path.join(tmpdir, "b.png")
                p = subprocess.run([BIN, "-q", "--json", f, dst, "-s", t], capture_output=True, text=True)
                if p.returncode not in (0, 1) or not p.stdout.strip():
                    r = {"status": f"CRASH rc={p.returncode}", "time": 0}
                else:
                    r = json.loads(p.stdout)
                chk = ""
                if r["status"] in ("exact", "short", "lossless", "minimum"):
                    try:   # independent decode: same frames, error as reported, exact size
                        err = worst_error(f, dst)
                        chk = "ok" if err is not None and err == r["max_error"] and \
                            os.path.getsize(dst) == r["size"] else f"MISMATCH err={err}"
                    except Exception as e:
                        chk = f"MISMATCH {type(e).__name__}: {e}"
                if os.path.exists(dst):
                    os.remove(dst)
                row = [name, rel, t, r["status"], r.get("lossless", ""), r.get("size", ""), r.get("target", ""),
                       r.get("psnr", ""), r.get("max_error", ""), r.get("changed", ""), r.get("depth", ""),
                       r.get("time", ""), chk or r.get("error", "")]
                w.writerow(row)
                fh.flush()
                print(*row[:12], chk, flush=True)
