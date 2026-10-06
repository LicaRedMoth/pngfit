#!/usr/bin/env python3
"""Video of pngfit quality falling from lossless to DEFLATE's floor, on a log scale.

Exact landing per frame would take hours, so: build the curve size(t) once with the
real compressor (libdeflate 12, strips, same as pngfit), then for every frame pick t
by interpolation and quantize (0.5 s). Images are real; sizes in captions are ≈.
"""
import argparse, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pngfit

ap = argparse.ArgumentParser()
ap.add_argument("src"); ap.add_argument("dst")
ap.add_argument("--fps", type=int, default=30)
ap.add_argument("--zoom", default="720,1330", help="x,y of the 280×360 magnified region")
ap.add_argument("--preview", type=float, help="render one frame at this %% to PNG instead")
ap.add_argument("--hold", type=float, default=2.5, help="seconds on the original and on the last frame")
ap.add_argument("--fall", type=float, default=15, help="seconds of the log-scale descent")
a = ap.parse_args()

img = np.ascontiguousarray(np.asarray(Image.open(a.src).convert("RGB")))
H, W, C = img.shape
LADDER = np.arange(1, 256, 2).astype(np.int32)          # q = 1, 3, …, 255
lum = img.astype(np.float32) @ np.array([.299, .587, .114], np.float32)
from scipy.ndimage import uniform_filter
m = uniform_filter(lum, 5)
act = np.sqrt(np.maximum(uniform_filter(lum * lum, 5) - m * m, 0))
rank = np.empty(H * W, np.int64)
rank[np.argsort(act, axis=None, kind="stable")] = np.arange(H * W)
rank = rank.reshape(H, W)
N = H * W
pool = ThreadPoolExecutor(os.cpu_count())
ones = np.ones((H, W))


def quant(t):
    rung = np.minimum((int(t * N) + rank) // N, len(LADDER) - 1)
    out_f = np.empty((H, 1 + W * C), np.uint8)
    rec = np.empty_like(img)
    pngfit.quant_strip(img, np.zeros((W, C), np.uint8), rung, LADDER, 3, 0.1, False,
                       np.ones(C, np.bool_), ones, out_f, rec, pngfit.COST_TAB, True)
    return out_f, rec


def size(out_f):
    parts = [out_f[y:y + 128] for y in range(0, H, 128)]
    return sum(len(f) for f in pool.map(lambda p: pngfit.deflate_fragment(np.ascontiguousarray(p), 12), parts)) + 69


cache = os.path.join(os.path.dirname(os.path.abspath(a.dst)), "curve_clamp.npy")
if os.path.exists(cache):
    T, S = np.load(cache)
else:
    T = np.array([0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 3.5, 4, 5, 6,
                  7, 8, 10, 12, 14, 16, 20, 24, 28, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 127])
    S = []
    for i, t in enumerate(T):
        S.append(size(quant(t)[0]))
        print(f"curve {i + 1}/{len(T)}: t={t:g} → {S[-1]} B ({S[-1] / S[0] * 100:.1f}%)", flush=True)
    S = np.array(S, float)
    np.save(cache, np.stack([T, S]))
S0 = S[0]
keep = S <= np.minimum.accumulate(S)        # drop the tail where bigger q stops paying off
T, S = T[keep], S[keep]


END = float(np.ceil(S.min() / S0 * 1e4) / 1e2)   # lowest reachable %, rounded up to 0.01
RAW = H * (1 + W * C)                              # filtered bytes DEFLATE has to code
print(f"floor: {S.min():.0f} B = {S.min() / S0 * 100:.3f}% (DEFLATE limit ~{RAW / 1032:.0f} B)", flush=True)


def t_for(pct):   # S decreases with t; np.interp wants increasing x
    order = np.argsort(S)
    return float(np.interp(pct / 100 * S0, S[order], T[order]))


F_BIG = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Bold.ttf", 64)
F_MED = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Medium.ttf", 30)
F_SM = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Regular.ttf", 22)
F_TINY = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Regular.ttf", 17)
zx, zy = map(int, a.zoom.split(","))


def frame(rec, pct, psnr, maxe):
    canvas = Image.new("RGB", (1920, 1080), (14, 14, 16))
    full = Image.fromarray(rec).resize((1080, 1080), Image.LANCZOS)
    canvas.paste(full, (0, 0))
    zoom = Image.fromarray(rec[zy:zy + 360, zx:zx + 280]).resize((840, 1080), Image.NEAREST)
    canvas.paste(zoom, (1080, 0))
    d = ImageDraw.Draw(canvas, "RGBA")
    s = 1080 / W   # mark the magnified region on the overview
    d.rectangle([zx * s, zy * s, (zx + 280) * s, (zy + 360) * s], outline=(255, 90, 40, 255), width=3)
    d.line([(1080, 0), (1080, 1080)], fill=(14, 14, 16, 255), width=4)
    # caption
    ox = 1080 - 24 - 536    # caption on the window glass, clear of the album title
    d.rounded_rectangle([ox, 24, ox + 536, 230], 18, fill=(0, 0, 0, 170))
    if pct >= 100:
        d.text((ox + 20, 34), "original", font=F_BIG, fill=(255, 255, 255))
        d.text((ox + 22, 120), f"lossless · {S0 / 1e6:.2f} MB", font=F_MED, fill=(220, 220, 220))
        d.text((ox + 22, 170), "pngfit · Anomie cover", font=F_SM, fill=(170, 170, 170))
    else:
        txt = f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%" if pct >= 1 else f"{pct:.2f}%"
        d.text((ox + 20, 34), txt, font=F_BIG, fill=(255, 255, 255))
        b = pct / 100 * S0
        sz = f"~{b / 1e6:.2f} MB" if b >= 1e6 else f"~{b / 1e3:.0f} KB"
        d.text((ox + 22, 120), f"{sz} of {S0 / 1e6:.2f} MB", font=F_MED, fill=(220, 220, 220))
        d.text((ox + 22, 170), f"PSNR {psnr:.1f} dB · max error ±{maxe}", font=F_SM, fill=(170, 170, 170))
    # log-scale position bar along the bottom of the overview
    x0, x1, y = 60, 1020, 1032
    pos = lambda p: x0 + (x1 - x0) * np.log(100 / p) / np.log(100 / END)
    d.rounded_rectangle([x0 - 24, y - 34, x1 + 24, y + 30], 14, fill=(0, 0, 0, 150))
    d.line([(x0, y), (x1, y)], fill=(120, 120, 120, 255), width=3)
    d.line([(x0, y), (pos(max(pct, END)), y)], fill=(255, 90, 40, 255), width=5)
    for p_, lab in ((100, "100%"), (10, "10%"), (1, "1%"), (END, f"{END:g}%")):
        if p_ >= END:
            d.line([(pos(p_), y - 7), (pos(p_), y + 7)], fill=(220, 220, 220, 255), width=2)
            w_ = d.textlength(lab, font=F_TINY)
            d.text((min(max(pos(p_) - w_ / 2, x0 - 16), x1 + 16 - w_), y + 9), lab, font=F_TINY, fill=(200, 200, 200))
    d.text((x0 - 16, y - 31), "size, log scale", font=F_TINY, fill=(160, 160, 160))
    d.rounded_rectangle([1104, 1020, 1330, 1060], 12, fill=(0, 0, 0, 150))
    d.text((1120, 1024), "zoom ×3", font=F_SM, fill=(220, 220, 220))
    return canvas


def render(pct):
    if pct >= 100:
        return frame(img, 100, 0, 0)
    _, rec = quant(t_for(pct))
    d = rec.astype(np.int32) - img
    mse = (d * d).mean()
    return frame(rec, pct, 10 * np.log10(255 ** 2 / mse) if mse else 99, int(np.abs(d).max()))


if a.preview is not None:
    render(a.preview).save(a.dst)
    sys.exit()

fps = a.fps
hold, fall = round(a.hold * fps), round(a.fall * fps)
pcts = [100.0] * hold + list(100 * (END / 100) ** np.linspace(0, 1, fall)) + [END] * hold
ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                       "-s", "1920x1080", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "slow",
                       "-crf", "14", "-pix_fmt", "yuv420p", "-movflags", "+faststart", a.dst], stdin=subprocess.PIPE)
last, last_pct, t0 = None, None, time.time()
for i, pct in enumerate(pcts):
    if pct != last_pct:
        last, last_pct = render(pct).tobytes(), pct
    ff.stdin.write(last)
    if i % 15 == 0:
        print(f"frame {i + 1}/{len(pcts)} ({pct:.1f}%) {time.time() - t0:.0f}s", flush=True)
ff.stdin.close()
ff.wait()
print("done", a.dst)
