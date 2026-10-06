#!/usr/bin/env python3
"""Re-run current pngfit at the exact byte sizes an older results CSV produced (fair A/B)."""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pngfit
D = os.environ.get("PNGFIT_DATASET") or sys.exit("set PNGFIT_DATASET to the dataset root")
old_csv, out_csv, tmpdir = sys.argv[1:4]
done = set()
if os.path.exists(out_csv):
    done = {(r["file"], r["target"]) for r in csv.DictReader(open(out_csv))}
with open(out_csv, "a", newline="") as fh:
    w = csv.writer(fh)
    if not done:
        w.writerow(["file", "target", "old_psnr", "new_psnr", "exact", "old_maxerr", "new_maxerr"])
    for r in csv.DictReader(open(old_csv)):
        if r["set"] != "kodak" or (r["file"], r["size"]) in done:
            continue
        dst = os.path.join(tmpdir, "s.png")
        a = pngfit.build_parser().parse_args([os.path.join(D, r["file"]), dst, "-s", r["size"], "-q"])
        res = pngfit.fit(a, lambda *x: None)
        os.remove(dst)
        row = [r["file"], r["size"], r["psnr"], f"{res['psnr']:.2f}", res["size"] == res["target"], r["maxerr"], res["maxerr"]]
        w.writerow(row); fh.flush(); print(*row, flush=True)
