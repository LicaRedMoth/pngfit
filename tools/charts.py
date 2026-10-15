#!/usr/bin/env python3
"""Charts for the README from docs/data: rd_cover.csv and rd_synthetic.csv (tools/rd_data.py) and
bench_summary.csv, which a tools/bench.py results file can refresh. Writes docs/img/*.png.

  tools/charts.py [bench_results.csv]

Only the summary is kept in the repository, so per-file names from private datasets never are.
Photographs and synthetic images get separate charts: they behave too differently to average.
"""

import collections
import csv
import math
import os
import statistics
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, os.pardir, "docs", "data")
IMAGES = os.path.join(HERE, os.pardir, "docs", "img")

INK = "#1b1b1b"
GRID = "#d8d8d8"
SERIES = ["#2f6fb5", "#c4622d", "#3d8b5f", "#8a5fa8", "#b03a51", "#1f9099", "#8a7a20", "#5f5f5f", "#c23f8a"]
BLUE = SERIES[0]
CODECS = {"JPEG 4:4:4": SERIES[1], "WebP": SERIES[2], "AVIF 4:4:4": SERIES[3]}
PNGFIT = "pngfit (exact size)"
DEMO = "uniform ladder (demo video)"
MARKS = {10_485_760: "10 MiB (Bandcamp)"}
CLASSES = {
    "photographic": {"kodak": "Kodak, 768x512", "clic": "CLIC 2020 mobile, ~2000x1500", "usc-sipi": "USC-SIPI"},
    "synthetic": {"screenshots": "desktop screenshots", "synthetic": "Wikimedia screenshots"},
}

# line and label styles, so the plotting calls below say only what differs
CODEC_LINE = dict(marker="o", markersize=3.8, linewidth=1.4)
PNGFIT_LINE = dict(marker="o", markersize=5.5, linewidth=2.2, color=BLUE, zorder=4)
DEMO_LINE = dict(linestyle="--", linewidth=1.2, alpha=0.6, color=BLUE)
MARK_LABEL = dict(textcoords="offset points", fontsize=7, color=BLUE, ha="left", weight="bold")
RANGE_BAR = dict(linewidth=2.4, alpha=0.35, solid_capstyle="round")
MEAN_DOT = dict(s=48, zorder=3, edgecolor="white", linewidth=0.8)
SUBTITLE = dict(xy=(0, 1), xycoords="axes fraction", xytext=(0, 9), textcoords="offset points", va="bottom")

Point = collections.namedtuple("Point", "pct psnr max_error bytes")

plt.rcParams.update(
    {
        "figure.dpi": 130,
        "savefig.dpi": 130,
        "font.size": 9,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK,
        "text.color": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    }
)


def finish(fig, axes, title, subtitle, name):
    """Title, a subtitle of at most two lines (the title's pad leaves room for that), and save."""
    axes[0].set_title(title, fontsize=11, weight="bold", loc="left", pad=30)
    axes[0].annotate(subtitle, fontsize=7.5, color="#666666", **SUBTITLE)
    for ax in axes:
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    fig.tight_layout()
    path = os.path.join(IMAGES, name)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print("wrote", path)


def size_axis(ax, lo, hi):
    """Log scale with 100 % on the left: further right means squeezed harder."""
    ax.set_xscale("log")
    ticks = [t for t in (0.3, 0.5, 1, 2, 5, 10, 20, 30, 50, 100, 200, 400) if lo / 1.15 <= t <= hi * 1.15]
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}%"))
    ax.set_xlim(hi * 1.15, lo / 1.15)


def load_rd(name):
    """Points per method, sorted by size; rows without a finite PSNR (lossless) are left out."""
    by_method = collections.defaultdict(list)
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return by_method

    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            psnr = float(r["psnr"])
            if math.isfinite(psnr):
                point = Point(float(r["pct_of_source"]), psnr, int(r["max_error"]), int(r["bytes"]))
                by_method[r["method"]].append(point)

    for points in by_method.values():
        points.sort()
    return by_method


def plot_rd(by_method, title, subtitle, name, marks=None):
    if not by_method:
        return
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(7.4, 7.0), sharex=True, gridspec_kw={"height_ratios": [1.25, 1]})

    demo = by_method.get(DEMO)
    if demo:
        label = "pngfit, demo video (uniform ladder)"
        top.plot([p.pct for p in demo], [p.psnr for p in demo], label=label, **DEMO_LINE)

    for method, colour in CODECS.items():
        points = by_method.get(method)
        if points:
            sizes = [p.pct for p in points]
            top.plot(sizes, [p.psnr for p in points], color=colour, label=method, **CODEC_LINE)
            bottom.plot(sizes, [p.max_error for p in points], color=colour, label=method, **CODEC_LINE)

    exact = by_method.get(PNGFIT, [])
    if exact:
        sizes = [p.pct for p in exact]
        top.plot(sizes, [p.psnr for p in exact], label="pngfit, exact byte targets", **PNGFIT_LINE)
        bottom.plot(sizes, [p.max_error for p in exact], label="pngfit, exact byte targets", **PNGFIT_LINE)

    for p in exact:
        if marks and p.bytes in marks:  # under the blue line, where no other codec runs
            offset = (8, 6) if p.pct > 60 else (6, -16)
            top.annotate(f"{marks[p.bytes]}, +-{p.max_error}", (p.pct, p.psnr), xytext=offset, **MARK_LABEL)

    shown = [p.pct for method, points in by_method.items() for p in points if not (method == DEMO and p.pct > 100)]
    size_axis(bottom, min(shown), max(shown))
    top.set_ylabel("PSNR over RGB, dB (higher is better)")
    top.legend(frameon=False, fontsize=8, loc="lower left")

    bottom.set_yscale("log")
    bottom.yaxis.set_major_locator(FixedLocator([1, 2, 5, 10, 20, 50, 100, 255]))
    bottom.yaxis.set_minor_locator(NullLocator())
    bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"+-{v:g}"))
    bottom.set_ylabel("worst pixel, levels of 255 (log, lower is better)")
    bottom.set_xlabel("size, % of the source PNG (log scale; further right = squeezed harder)")

    finish(fig, (top, bottom), title, subtitle, name)


def summarise(rows):
    """One summary row for the runs of one set at one target."""
    encoded = [r for r in rows if r["status"] in ("exact", "short", "lossless", "minimum")]
    eight_bit = [r for r in encoded if r["depth"] == "8"]
    psnr = [float(r["psnr"]) for r in eight_bit if r["psnr"] not in ("", "null") and math.isfinite(float(r["psnr"]))]

    def count(status):
        return sum(r["status"] == status for r in rows)

    return {
        "files": len(rows),
        "encoded": len(encoded),
        "exact": count("exact"),
        "short": count("short"),
        "lossless": count("lossless"),
        "skipped": count("skipped"),
        "errors": count("error"),
        "crashes": sum(r["status"].startswith("CRASH") for r in rows),
        "checked": sum(r["check"] == "ok" for r in encoded),
        "psnr_mean": f"{statistics.mean(psnr):.2f}" if psnr else "",
        "psnr_min": f"{min(psnr):.2f}" if psnr else "",
        "psnr_max": f"{max(psnr):.2f}" if psnr else "",
        "max_error": max((int(r["max_error"]) for r in eight_bit), default=""),
        "time_median": f"{statistics.median(float(r['time']) for r in encoded):.1f}" if encoded else "",
    }


def summarise_bench(path):
    groups = collections.defaultdict(list)
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            groups[(r["set"], r["target"])].append(r)

    summary = [{"set": s, "target": t, **summarise(rows)} for (s, t), rows in groups.items()]
    out = os.path.join(DATA, "bench_summary.csv")
    with open(out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    print("wrote", out)


def plot_bench(cls):
    path = os.path.join(DATA, "bench_summary.csv")
    if not os.path.exists(path):
        return
    labels = CLASSES[cls]
    with open(path, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["set"] in labels and r["psnr_mean"]]
    sets = [s for s in labels if any(r["set"] == s for r in rows)]
    if not sets:
        return

    fig, ax = plt.subplots(figsize=(7.4, 1.25 + 0.75 * len(sets)))
    for target, colour, shift in (("90%", SERIES[0], 0.15), ("75%", SERIES[1], -0.15)):
        for i, s in enumerate(sets):
            r = next((r for r in rows if r["set"] == s and r["target"] == target), None)
            if not r:
                continue

            y = len(sets) - 1 - i + shift
            low, mean, high = float(r["psnr_min"]), float(r["psnr_mean"]), float(r["psnr_max"])
            ax.plot([low, high], [y, y], color=colour, **RANGE_BAR)
            ax.scatter(mean, y, color=colour, label=f"target {target} of lossless" if i == 0 else None, **MEAN_DOT)

            lossless = f" + {r['lossless']} lossless" if int(r["lossless"]) else ""
            label = f"{r['exact']}/{r['encoded']} exact{lossless}, worst +-{r['max_error']}, {r['time_median']} s"
            ax.annotate(label, (high, y), textcoords="offset points", xytext=(7, -3), fontsize=7, color=colour)

    ax.set_yticks(range(len(sets)))
    ax.set_yticklabels([labels[s] for s in reversed(sets)])
    ax.set_ylim(-0.6, len(sets) - 0.4)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("PSNR, dB (dot: mean, bar: min to max over the files)")
    ax.legend(frameon=False, fontsize=8, loc="lower left", ncol=2, bbox_to_anchor=(0, -0.02))
    lo, hi = ax.get_xlim()
    ax.set_xlim(lo, hi + (hi - lo) * 0.45)

    encoded = sum(int(r["encoded"]) for r in rows)
    exact = sum(int(r["exact"]) for r in rows)
    lossless = sum(int(r["lossless"]) for r in rows)
    title = f"Benchmark, {cls}: exact size on {exact} of {encoded} encodes"
    if lossless and exact + lossless == encoded:
        title += f", the other {lossless} fit losslessly"
    subtitle = (
        "Random samples, two targets each, on a 2011 dual-core Pentium; label: exact hits, the worst pixel in "
        "the set, median time.\nEvery output re-decoded by an independent decoder and checked for size and error."
    )
    finish(fig, (ax,), title, subtitle, f"bench_{cls}.png")


def main():
    os.makedirs(IMAGES, exist_ok=True)
    if len(sys.argv) > 1:
        summarise_bench(sys.argv[1])

    plot_rd(
        load_rd("rd_cover.csv"),
        "What each byte buys: a photograph",
        "Anomie album cover, 3000x3000, 12.12 MB lossless PNG. Solid blue: real pngfit runs on exact byte "
        "counts; dashed: the demo video.\nJPEG and AVIF at full colour (4:4:4); WebP lossy is always 4:2:0.",
        "rd_photographic.png",
        MARKS,
    )
    plot_rd(
        load_rd("rd_synthetic.csv"),
        "What each byte buys: a screenshot",
        "Desktop screenshot from Wikimedia Commons, 1600x900, 161 KB lossless PNG. On text and flat colour "
        "the lossy codecs\nneed more bytes than lossless PNG for decent quality; pngfit starts from the "
        "lossless file and only goes down.",
        "rd_synthetic.png",
    )
    for cls in CLASSES:
        plot_bench(cls)


if __name__ == "__main__":
    main()
