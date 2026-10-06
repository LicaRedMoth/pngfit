#!/usr/bin/env python3
"""Charts for the README, in the style of PXL's bench/plots.py.

  tools/charts.py [bench_results.csv]     # writes docs/img/*.png

Inputs: docs/data/rd_cover.csv and docs/data/rd_synthetic.csv (tools/rd_data.py) and,
optionally, a results CSV from tools/bench.py. That one is summarised into
docs/data/bench_summary.csv first, so the repository never carries per-file names
from private datasets.

Photographs and synthetic images get separate charts and never a pooled one: the
two behave too differently for an average to describe either.
"""
import collections, csv, math, os, statistics, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, FixedLocator, NullLocator

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, os.pardir, "docs")
DATA, OUT = os.path.join(DOCS, "data"), os.path.join(DOCS, "img")

INK = "#1b1b1b"
GRID = "#d8d8d8"
SERIES = ["#2f6fb5", "#c4622d", "#3d8b5f", "#8a5fa8", "#b03a51", "#1f9099", "#8a7a20", "#5f5f5f", "#c23f8a"]
CODECS = {"JPEG 4:4:4": SERIES[1], "WebP": SERIES[2], "AVIF 4:4:4": SERIES[3]}
PNGFIT, DEMO = "pngfit (exact size)", "uniform ladder (demo video)"

plt.rcParams.update({
    "figure.dpi": 130, "savefig.dpi": 130, "font.size": 9,
    "axes.edgecolor": GRID, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK, "ytick.color": INK, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6,
    "figure.facecolor": "white", "axes.facecolor": "white",
})


def finish(fig, axes, title, subtitle, name):
    # two subtitle lines at most: the title's pad leaves room for exactly that
    top = axes[0]
    top.set_title(title, fontsize=11, weight="bold", loc="left", pad=30)
    top.annotate(subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 9), textcoords="offset points",
                 fontsize=7.5, color="#666666", va="bottom")
    for ax in axes:
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    fig.tight_layout()
    path = os.path.join(OUT, name)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print("wrote", path)


def size_axis(ax, lo, hi):
    """log scale, 100 % on the left: going right means squeezing harder"""
    ax.set_xscale("log")
    ticks = [t for t in (0.3, 0.5, 1, 2, 5, 10, 20, 30, 50, 100, 200, 400) if lo / 1.15 <= t <= hi * 1.15]
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}%"))
    ax.set_xlim(hi * 1.15, lo / 1.15)


def load_rd(name):
    by = collections.defaultdict(list)
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return by
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            p = float(r["psnr"])
            if math.isfinite(p):
                by[r["method"]].append((float(r["pct_of_source"]), p, int(r["max_error"]), int(r["bytes"])))
    for v in by.values():
        v.sort()
    return by


MARKS = {10_000_000: "10 MB (Bandcamp)"}


def plot_rd(by, title, subtitle, name, marks=None):
    if not by:
        return
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.4, 7.0), sharex=True, gridspec_kw={"height_ratios": [1.25, 1]})
    if by.get(DEMO):
        u = by[DEMO]
        a1.plot([p for p, *_ in u], [q for _, q, *_ in u], "--", color=SERIES[0], linewidth=1.2, alpha=0.6,
                label="pngfit, demo video (uniform ladder)")
    for m, c in CODECS.items():
        v = by.get(m)
        if v:
            a1.plot([p for p, *_ in v], [q for _, q, *_ in v], "-o", color=c, markersize=3.8, linewidth=1.4, label=m)
            a2.plot([p for p, *_ in v], [e for _, _, e, _ in v], "-o", color=c, markersize=3.8, linewidth=1.4,
                    label=m)
    pf = by.get(PNGFIT, [])
    if pf:
        for ax, k in ((a1, 1), (a2, 2)):
            ax.plot([r[0] for r in pf], [r[k] for r in pf], "-o", color=SERIES[0], markersize=5.5, linewidth=2.2,
                    label="pngfit, exact byte targets", zorder=4)
        for p, q, e, b in pf:
            if marks and b in marks:   # under the blue line, where no other codec runs
                top = p > 60
                a1.annotate(f"{marks[b]}, ±{e}", (p, q), textcoords="offset points",
                            xytext=(8, 6) if top else (6, -16), fontsize=7, color=SERIES[0], ha="left",
                            weight="bold")
    pts = [p for k, v in by.items() for p, *_ in v if not (k == DEMO and p > 100)]
    size_axis(a2, min(pts), max(pts))
    a1.set_ylabel("PSNR over RGB, dB (higher is better)")
    a2.set_yscale("log")
    a2.yaxis.set_major_locator(FixedLocator([1, 2, 5, 10, 20, 50, 100, 255]))
    a2.yaxis.set_minor_locator(NullLocator())
    a2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"±{v:g}"))
    a2.set_ylabel("worst pixel, levels of 255 (log, lower is better)")
    a2.set_xlabel("size, % of the source PNG (log scale; further right = squeezed harder)")
    a1.legend(frameon=False, fontsize=8, loc="lower left")
    finish(fig, (a1, a2), title, subtitle, name)


def summarise_bench(path):
    rows = list(csv.DictReader(open(path, newline="")))
    g = collections.defaultdict(list)
    for r in rows:
        g[(r["set"], r["target"])].append(r)
    out = []
    for (s, t), rs in g.items():
        done = [r for r in rs if r["status"] in ("exact", "short", "lossless", "minimum")]
        ps = [float(r["psnr"]) for r in done if r["psnr"] not in ("", "null") and math.isfinite(float(r["psnr"]))
              and r["depth"] == "8"]
        out.append(dict(set=s, target=t, files=len(rs), encoded=len(done),
                        exact=sum(r["status"] == "exact" for r in rs),
                        short=sum(r["status"] == "short" for r in rs),
                        lossless=sum(r["status"] == "lossless" for r in rs),
                        skipped=sum(r["status"] == "skipped" for r in rs),
                        errors=sum(r["status"] == "error" for r in rs),
                        crashes=sum(r["status"].startswith("CRASH") for r in rs),
                        checked=sum(r["check"] == "ok" for r in done),
                        psnr_mean=f"{statistics.mean(ps):.2f}" if ps else "",
                        psnr_min=f"{min(ps):.2f}" if ps else "", psnr_max=f"{max(ps):.2f}" if ps else "",
                        max_error=max((int(r["max_error"]) for r in done if r["depth"] == "8"), default=""),
                        time_median=f"{statistics.median(float(r['time']) for r in done):.1f}" if done else ""))
    with open(os.path.join(DATA, "bench_summary.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print("wrote", os.path.join(DATA, "bench_summary.csv"))


CLASSES = {
    "photographic": {"kodak": "Kodak, 768×512", "clic": "CLIC 2020 mobile, ~2000×1500", "usc-sipi": "USC-SIPI"},
    "synthetic": {"screenshots": "desktop screenshots", "synthetic": "Wikimedia screenshots"},
}


def plot_bench(cls):
    path = os.path.join(DATA, "bench_summary.csv")
    if not os.path.exists(path):
        return
    labels = CLASSES[cls]
    rows = [r for r in csv.DictReader(open(path, newline="")) if r["set"] in labels and r["psnr_mean"]]
    sets = [s for s in labels if any(r["set"] == s for r in rows)]
    if not sets:
        return
    fig, ax = plt.subplots(figsize=(7.4, 1.25 + 0.75 * len(sets)))
    for j, (t, c) in enumerate((("90%", SERIES[0]), ("75%", SERIES[1]))):
        for i, s in enumerate(sets):
            r = next((r for r in rows if r["set"] == s and r["target"] == t), None)
            if not r:
                continue
            y = len(sets) - 1 - i + (0.15 if j == 0 else -0.15)
            ax.plot([float(r["psnr_min"]), float(r["psnr_max"])], [y, y], color=c, linewidth=2.4, alpha=0.35,
                    solid_capstyle="round")
            ax.scatter(float(r["psnr_mean"]), y, s=48, color=c, zorder=3, edgecolor="white", linewidth=0.8,
                       label=f"target {t} of lossless" if i == 0 else None)
            ll = f" + {r['lossless']} lossless" if int(r["lossless"]) else ""
            ax.annotate(f"{r['exact']}/{r['encoded']} exact{ll} · worst ±{r['max_error']} · {r['time_median']} s",
                        (float(r["psnr_max"]), y), textcoords="offset points", xytext=(7, -3), fontsize=7, color=c)
    ax.set_yticks(range(len(sets)))
    ax.set_yticklabels([labels[s] for s in reversed(sets)])
    ax.set_ylim(-0.6, len(sets) - 0.4)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("PSNR, dB (dot: mean, bar: min to max over the files)")
    ax.legend(frameon=False, fontsize=8, loc="lower left", ncol=2, bbox_to_anchor=(0, -0.02))
    lo, hi = ax.get_xlim()
    ax.set_xlim(lo, hi + (hi - lo) * 0.45)
    tot = sum(int(r["encoded"]) for r in rows)
    ex = sum(int(r["exact"]) for r in rows)
    ll = sum(int(r["lossless"]) for r in rows)
    finish(fig, (ax,), f"Benchmark, {cls}: exact size on {ex} of {tot} encodes" +
           (f", the other {ll} fit losslessly" if ll and ex + ll == tot else ""),
           "Random samples, two targets each, on a 2011 dual-core Pentium; label: exact hits, the worst pixel in "
           "the set, median time.\nEvery output re-decoded by an independent decoder and checked for size and "
           "error.", f"bench_{cls}.png")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if len(sys.argv) > 1:
        summarise_bench(sys.argv[1])
    plot_rd(load_rd("rd_cover.csv"), "What each byte buys: a photograph",
            "Anomie album cover, 3000×3000, 12.12 MB lossless PNG. Solid blue: real pngfit runs on exact byte "
            "counts; dashed: the demo video.\nJPEG and AVIF at full colour (4:4:4); WebP lossy is always 4:2:0.",
            "rd_photographic.png", MARKS)
    plot_rd(load_rd("rd_synthetic.csv"), "What each byte buys: a screenshot",
            "Desktop screenshot from Wikimedia Commons, 1600×900, 161 KB lossless PNG. On text and flat colour "
            "the lossy codecs\nneed more bytes than lossless PNG for decent quality; pngfit starts from the "
            "lossless file and only goes down.", "rd_synthetic.png")
    for cls in CLASSES:
        plot_bench(cls)
