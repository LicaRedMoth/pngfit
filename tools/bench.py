#!/usr/bin/env python3
"""Benchmark the pngfit binary on a dataset sample: exact hits, quality, time. Resumable: rows already
in the CSV are skipped. Every output is checked with tools/check.py before it is deleted.

  PNGFIT_DATASET=/path/to/data tools/bench.py results.csv [scratch_dir]
"""

import csv
import glob
import json
import os
import random
import subprocess
import sys
import tempfile

from check import compare
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

BIN = os.path.join(HERE, os.pardir, "pngfit")
SETS = {  # name: (folder or file under the dataset root, how many files to sample)
    "kodak": ("Kodak-Lossless-True-Color-Image-Suite", 24),
    "clic": ("CLIC-2020-mobile-train", 8),
    "usc-sipi": ("USC-SIPI-Image-Database", 12),
    "screenshots": ("Screenshots", 16),
    "synthetic": ("Synthetic-Screenshots", 8),
    "pngsuite": ("The-official-test-suite-for-PNG", 10**9),  # every file: robustness
    "apng": ("Animated_PNG_example_bouncing_beach_ball.apng", 1),
}
TARGETS = {"pngsuite": ["90%"]}
COLUMNS = "set file target status lossless size target_bytes psnr max_error changed depth time check".split()


def truecolour(path):
    try:
        return Image.open(path).mode in ("L", "LA", "RGB", "RGBA")
    except Exception:
        return False


def sample(root, name, folder, count, rng):
    if folder.endswith(".apng"):
        return [f"{root}/{folder}"]

    files = sorted(glob.glob(f"{root}/{folder}/**/*.png", recursive=True))
    if name not in ("pngsuite", "apng"):
        files = [f for f in files if truecolour(f)]
    return files if len(files) <= count else rng.sample(files, count)


def encode(source, output, target):
    p = subprocess.run([BIN, "-q", "--json", source, output, "-s", target], capture_output=True, text=True)
    if p.returncode not in (0, 1) or not p.stdout.strip():
        return {"status": f"CRASH rc={p.returncode}", "time": 0}
    return json.loads(p.stdout)


def verify(source, output, result):
    """Independent decode: the same frames, the error as reported, the exact size."""
    if result["status"] not in ("exact", "short", "lossless", "minimum"):
        return result.get("error", "")
    try:
        error = compare(source, output).max_error
    except Exception as e:
        return f"MISMATCH {type(e).__name__}: {e}"

    if error is None or error != result["max_error"] or os.path.getsize(output) != result["size"]:
        return f"MISMATCH err={error}"
    return "ok"


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    root = os.environ.get("PNGFIT_DATASET") or sys.exit("set PNGFIT_DATASET to the dataset root")
    results = sys.argv[1]
    scratch = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp()
    output = os.path.join(scratch, "b.png")

    done = set()
    if os.path.exists(results):
        with open(results) as fh:
            done = {(r["set"], r["file"], r["target"]) for r in csv.DictReader(fh)}

    rng = random.Random(1)  # the same sample the Python prototype was measured on
    with open(results, "a", newline="") as fh:
        writer = csv.DictWriter(fh, COLUMNS)
        if not done:
            writer.writeheader()

        for name, (folder, count) in SETS.items():
            for source in sample(root, name, folder, count, rng):
                for target in TARGETS.get(name, ["90%", "75%"]):
                    file = os.path.relpath(source, root)
                    if (name, file, target) in done:
                        continue

                    r = encode(source, output, target)
                    checked = verify(source, output, r)
                    if os.path.exists(output):
                        os.remove(output)

                    row = {"set": name, "file": file, "target": target, "target_bytes": r.get("target", "")}
                    for column in ("status", "lossless", "size", "psnr", "max_error", "changed", "depth", "time"):
                        row[column] = r.get(column, "")
                    row["check"] = checked
                    writer.writerow(row)
                    fh.flush()
                    print(*(row[column] for column in COLUMNS), flush=True)


if __name__ == "__main__":
    main()
