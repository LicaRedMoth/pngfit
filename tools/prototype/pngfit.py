#!/usr/bin/env python3
"""pngfit — make a truecolor PNG exactly N bytes with the least visible loss.

PNG predicts every byte from its neighbours (filter) and DEFLATEs the residual.
We round residuals to multiples of a step q, which shrinks the symbol alphabet
DEFLATE has to code. Error per channel is at most floor(q/2) and never drifts,
because prediction always uses the already-quantized neighbours (what a decoder sees).

Exactness comes from structure, not from modelling DEFLATE:
  * the image is cut into strips; each strip is compressed by libdeflate on its
    own and stitched into one zlib stream (see fitcore.c), so file size is a plain
    sum of strip sizes;
  * the last row of every strip is kept lossless, so the next strip predicts from
    original pixels and strips do not depend on each other's settings;
  * inside a strip pixels are ranked by local texture (visual masking) and one
    integer P says how far the ranking is pushed up the ladder of steps; P is the
    knob for rate-distortion optimisation across strips and, at the end, the knob
    that lands the file on the exact byte.
"""
import argparse, ctypes, os, re, struct, sys, time, zlib
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from numba import njit
from PIL import Image
from scipy.ndimage import uniform_filter

HERE = os.path.dirname(os.path.abspath(__file__))
_lib = ctypes.CDLL(os.path.join(HERE, "libfitcore.so"))
_lib.fit_strip.restype = ctypes.c_size_t
_lib.fit_strip.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                           ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
_lib.fit_bound.restype = ctypes.c_size_t
_lib.fit_bound.argtypes = [ctypes.c_size_t]

# --keep categories (cwebp -metadata / jpegtran -copy style); raw chunk names work too
COLOR_CHUNKS = {b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"cICP", b"mDCV", b"cLLI", b"sBIT"}
PHYS_CHUNKS = {b"pHYs", b"oFFs", b"sCAL", b"pCAL"}
TEXT_CHUNKS = {b"tEXt", b"zTXt", b"iTXt"}
CATEGORIES = ("color", "exif", "xmp", "iptc", "text", "phys", "time", "other")
# never copied: animation and palette-bound chunks make no sense for the single image we write
NEVER = {b"acTL", b"fcTL", b"fdAT", b"hIST", b"sPLT"}


def chunk_category(t, d):
    """Category of an ancillary chunk, or None if it must not be copied at all."""
    if t in COLOR_CHUNKS:
        return "color"
    if t == b"eXIf":
        return "exif"
    if t in TEXT_CHUNKS:   # ImageMagick/GIMP/exiv2 hide EXIF/XMP/IPTC in text chunks
        key = d.split(b"\0", 1)[0].lower()
        if key in (b"xml:com.adobe.xmp", b"raw profile type xmp"):
            return "xmp"
        if key in (b"raw profile type exif", b"raw profile type app1"):
            return "exif"
        if key in (b"raw profile type iptc", b"raw profile type 8bim"):
            return "iptc"
        return "text"
    if t in PHYS_CHUNKS:
        return "phys"
    if t == b"tIME":
        return "time"
    if t in NEVER or t[0:1].isupper():
        return None
    if t in (b"bKGD", b"sTER") or t[3:4].islower():   # known, or unknown but safe-to-copy
        return "other"
    return None


def parse_keep(s):
    want = set()
    for tok in filter(None, (x.strip() for x in s.split(","))):
        if tok in ("all", "none") or tok in CATEGORIES:
            want |= set(CATEGORIES) if tok == "all" else set() if tok == "none" else {tok}
        elif len(tok) == 4 and tok.isascii() and tok.isalpha():
            want.add(tok.encode())
        else:
            raise argparse.ArgumentTypeError(
                f"unknown --keep item {tok!r}: use {', '.join(CATEGORIES)}, all, none or a chunk name like tEXt")
    return want


class Progress:
    """One-line progress bar on stderr; silent when stderr is not a terminal."""
    def __init__(self, on=True):
        self.on = on and sys.stderr.isatty()
        self.t0 = time.time()
        self.label, self.total, self.done, self.note, self.ts = "", None, 0, "", self.t0

    @staticmethod
    def _fmt(t):
        return f"{int(t) // 60}:{int(t) % 60:02d}"

    def start(self, label, total=None, note=""):
        self.label, self.total, self.done, self.note, self.ts = label, total, 0, note, time.time()
        self.draw()

    def tick(self, n=1, note=None):
        self.done += n
        if note is not None:
            self.note = note
        self.draw()

    def draw(self):
        if not self.on or not self.label:
            return
        if self.total:
            f = min(self.done / self.total, 1)
            bar = "█" * int(f * 24) + "░" * (24 - int(f * 24))
            s = f"{self.label:<18} {bar} {self.done}/{self.total}"
            if self.done:
                s += f"  ETA {self._fmt((time.time() - self.ts) / self.done * (self.total - self.done))}"
        else:
            s = f"{self.label:<18}" + (f" {self.done} probes" if self.done else "")
        if self.note:
            s += f"  {self.note}"
        s += f"  [{self._fmt(time.time() - self.t0)}]"
        sys.stderr.write("\r\x1b[K" + s)
        sys.stderr.flush()

    def log(self, *msg):
        if self.on:
            sys.stderr.write("\r\x1b[K"); sys.stderr.flush()
        print(*msg, flush=True)
        self.draw()

    def end(self):
        if self.on:
            sys.stderr.write("\r\x1b[K"); sys.stderr.flush()
        self.on = False


def deflate_fragment(buf, level=12):
    n = buf.nbytes
    cap = _lib.fit_bound(n)
    out = np.empty(cap, np.uint8)
    scratch = np.empty(n + 1, np.uint8)
    m = _lib.fit_strip(buf.ctypes.data, n, level, out.ctypes.data, cap, scratch.ctypes.data)
    if not m:
        raise RuntimeError("fit_strip failed")
    return out[:m].tobytes()


@njit(nogil=True, cache=True)
def _paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a); pb = abs(p - b); pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


# bits-ish cost of a residual byte: log2(1+|r|) for r as signed int8 (row filter heuristic)
COST_TAB = np.log2(1 + np.abs(np.arange(256) - 256 * (np.arange(256) > 127))).astype(np.float64)


@njit(nogil=True, cache=True)
def _quant_row(src, up, rung_row, ladder, lossless, f, chmask, rec, res, tab, clamp):
    """Quantize one row with PNG filter f, predicting from `up` (row above) and the
    row's own already-quantized pixels. Fills rec/res, returns heuristic cost.
    If the rounded value leaves 0..255: clamp=False keeps the pixel exact (the error
    bound floor(q/2) holds), clamp=True takes the nearest in-range step (bigger error,
    but no lossless islands at huge q; used for the extreme end of the video)."""
    W, C = src.shape
    cost = 0.0
    for x in range(W):
        q = 1 if lossless else ladder[rung_row[x]]
        for c in range(C):
            a = np.int32(rec[x - 1, c]) if x > 0 else 0
            b = np.int32(up[x, c])
            cc = np.int32(up[x - 1, c]) if x > 0 else 0
            if f == 0:
                p = 0
            elif f == 1:
                p = a
            elif f == 2:
                p = b
            elif f == 3:
                p = (a + b) >> 1
            else:
                p = _paeth(a, b, cc)
            v = np.int32(src[x, c])
            val = v
            if q > 1 and chmask[c]:
                rq = int(np.floor((v - p) / q + 0.5)) * q
                if 0 <= p + rq <= 255:
                    val = p + rq
                elif clamp:
                    alt = rq - q if p + rq > 255 else rq + q
                    val = p + alt if 0 <= p + alt <= 255 else p
            rec[x, c] = val
            r = (val - p) & 255
            res[x * C + c] = r
            cost += tab[r]
    return cost


@njit(nogil=True, cache=True)
def quant_strip(img, prev, rung, ladder, filt, adaptive, anchor, chmask, wgt, out_f, out_rec, tab, clamp=False):
    """img h×W×C, prev W×C (row above, already lossless), rung h×W int.
    Every row uses the base filter unless another filter's heuristic cost is lower
    by more than `adaptive` (fraction); 0 disables switching. Keeping one filter
    most of the time matters: DEFLATE's matches and Huffman tables like uniform rows.
    Writes PNG-filtered rows to out_f, reconstruction to out_rec.
    Returns (weighted SSE, SSE, max abs error)."""
    h, W, C = img.shape
    tmp_rec = np.empty((W, C), np.uint8)
    tmp_res = np.empty(W * C, np.uint8)
    wsse = 0.0; sse = 0.0; maxe = 0
    for y in range(h):
        up = out_rec[y - 1] if y > 0 else prev
        lossless = anchor and y == h - 1
        base = _quant_row(img[y], up, rung[y], ladder, lossless, filt, chmask, out_rec[y], out_f[y, 1:], tab, clamp)
        best_f = filt; best = base * (1.0 - adaptive)
        if adaptive > 0:
            for f in range(5):
                if f != filt:
                    cst = _quant_row(img[y], up, rung[y], ladder, lossless, f, chmask, tmp_rec, tmp_res, tab, clamp)
                    if cst < best:
                        best = cst; best_f = f
            if best_f != filt:
                _quant_row(img[y], up, rung[y], ladder, lossless, best_f, chmask, out_rec[y], out_f[y, 1:], tab, clamp)
        out_f[y, 0] = best_f
        for x in range(W):
            pe = 0
            for c in range(C):
                d = np.int32(out_rec[y, x, c]) - np.int32(img[y, x, c])
                pe += d * d
                if abs(d) > maxe:
                    maxe = abs(d)
            sse += pe
            wsse += pe * wgt[y, x]
    return wsse, sse, maxe


class Fitter:
    def __init__(self, img, strip_h, filt, ladder, mask_scale, level, alpha_lossless, adaptive=0.0):
        self.img = img
        self.adaptive = adaptive
        self.H, self.W, self.C = img.shape
        self.filt, self.level = filt, level
        self.ladder = np.asarray(ladder, np.int32)
        self.chmask = np.ones(self.C, np.bool_)
        if alpha_lossless and self.C in (2, 4):
            self.chmask[-1] = False
        self.bounds = [(y, min(y + strip_h, self.H)) for y in range(0, self.H, strip_h)]
        # visual masking: local texture of luma, 5×5 std
        f = img.astype(np.float32)
        lum = f[..., :3] @ np.array([.299, .587, .114], np.float32) if self.C >= 3 else f[..., 0]
        m = uniform_filter(lum, 5)
        act = np.sqrt(np.maximum(uniform_filter(lum * lum, 5) - m * m, 0))
        self.wgt = (1.0 / (1.0 + act / mask_scale) ** 2).astype(np.float64)
        # per-strip rank by activity, 0 = flattest; busiest pixels climb the ladder first
        self.act = act
        self.seed_ranks = {}
        self.rank = np.empty((self.H, self.W), np.int64)
        for y0, y1 in self.bounds:
            a = act[y0:y1].ravel()
            r = np.empty(a.size, np.int64)
            r[np.argsort(a, kind="stable")] = np.arange(a.size)
            self.rank[y0:y1] = r.reshape(y1 - y0, self.W)
        self.npx = [(y1 - y0) * self.W for y0, y1 in self.bounds]
        self.maxP = [n * (len(ladder) - 1) for n in self.npx]
        self.cache = {}

    def strip_rank(self, k, seed):
        """Ranking of strip k. Seed 0 is the plain activity order; other seeds shuffle
        each pixel among its neighbours in that order (±0.5 % of the strip), so pixels
        of near-equal texture swap places. Quality is practically the same, the byte
        size is not: fresh, independent sizes for the exact-landing search."""
        y0, y1 = self.bounds[k]
        if seed == 0:
            return self.rank[y0:y1]
        if (k, seed) not in self.seed_ranks:
            n = self.npx[k]
            a = self.rank[y0:y1].ravel() + np.random.default_rng(seed * 1000003 + k).uniform(0, max(64, n // 200), n)
            r = np.empty(a.size, np.int64)
            r[np.argsort(a, kind="stable")] = np.arange(a.size)
            self.seed_ranks[(k, seed)] = r.reshape(y1 - y0, self.W)
        return self.seed_ranks[(k, seed)]

    def run(self, k, P, keep=False):
        """P is an int (seed 0) or a (P, seed) tuple."""
        if not keep and (k, P) in self.cache:
            return self.cache[(k, P)]
        key = P
        P, seed = P if isinstance(P, tuple) else (P, 0)
        y0, y1 = self.bounds[k]
        n = self.npx[k]
        rung = np.minimum((P + self.strip_rank(k, seed)) // n, len(self.ladder) - 1)
        prev = self.img[y0 - 1] if y0 > 0 else np.zeros((self.W, self.C), np.uint8)
        anchor = k < len(self.bounds) - 1
        h = y1 - y0
        out_f = np.empty((h, 1 + self.W * self.C), np.uint8)
        out_rec = np.empty((h, self.W, self.C), np.uint8)
        wsse, sse, maxe = quant_strip(self.img[y0:y1], prev, rung, self.ladder, self.filt, self.adaptive,
                                      anchor, self.chmask, self.wgt[y0:y1], out_f, out_rec, COST_TAB, False)
        frag = deflate_fragment(out_f, self.level)
        res = (len(frag), wsse, sse, maxe)
        self.cache[(k, key)] = res
        return (res, frag, out_f, out_rec) if keep else res


def parse_size(s):
    if s.strip().endswith("%"):
        try:
            v = float(s.strip()[:-1]) / 100      # relative to our own lossless size
        except ValueError:
            raise argparse.ArgumentTypeError(f"bad size {s!r}")
        if v <= 0:
            raise argparse.ArgumentTypeError("size must be above 0%")
        return ("pct", v)
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)(i?)[bB]?\s*", s)
    if not m:
        raise argparse.ArgumentTypeError(f"bad size {s!r}")
    base = 1024 if m.group(3) else 1000
    v = int(float(m.group(1)) * base ** " kmg".index(m.group(2).lower() or " "))
    if v <= 0:
        raise argparse.ArgumentTypeError("size must be at least 1 byte")
    return v


def png_chunk(typ, data):
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data))


def read_chunks(path):
    with open(path, "rb") as f:
        d = f.read()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    i, out = 8, []
    while i < len(d):
        n, = struct.unpack(">I", d[i:i + 4])
        out.append((d[i + 4:i + 8], d[i + 8:i + 8 + n]))
        i += 12 + n
    return out


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("-s", "--size", type=parse_size, required=True,
                    help="exact output size: 10000000, 10MB (=10^7), 10MiB (=10·2^20), "
                         "or 80%% of the lossless size")
    ap.add_argument("--strip", type=int, default=128, help="rows per independent strip")
    ap.add_argument("--filter", type=int, default=-1, choices=[-1, 0, 1, 2, 3, 4],
                    help="base PNG filter, -1 = probe (default)")
    ap.add_argument("--row-switch", type=float, default=-1, metavar="FRAC",
                    help="a row leaves the base filter only if another is cheaper by this fraction "
                         "(0 = never; -1 = probe 0/0.05/0.1/0.2, default)")
    ap.add_argument("--ladder", default="odd", help="'odd' (1,3,5,..), 'all' (1,2,3,..) or list like 1,2,3,5")
    ap.add_argument("--mask", type=float, default=4.0, help="masking strength (smaller = trust texture more)")
    ap.add_argument("--level", type=int, default=12, help="libdeflate level 1..12")
    ap.add_argument("-k", "--keep", type=parse_keep, default=parse_keep("color"), metavar="LIST",
                    help="metadata to keep, comma-separated: " + ", ".join(CATEGORIES) +
                         ", all, none, or chunk names (tEXt,pHYs). Default: color. "
                         "Kept chunks are counted in the budget; the file still lands on --size exactly")
    ap.add_argument("-p", "--preserve", dest="keep", action="store_const", const=set(CATEGORIES),
                    help="keep all metadata (same as --keep all)")
    ap.add_argument("--lossy-alpha", action="store_true")
    ap.add_argument("--rounds", type=int, default=4, help="RD refinement rounds")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    ap.add_argument("-q", "--quiet", action="store_true")
    return ap


class FitError(Exception):
    pass


def main(argv=None):
    a = build_parser().parse_args(argv)
    prog = Progress(not a.quiet)
    try:
        fit(a, (lambda *x: None) if a.quiet else prog.log, prog)
    except FitError as e:
        prog.end()
        sys.exit(str(e))
    except KeyboardInterrupt:
        prog.end()
        sys.exit("interrupted")
    prog.end()


def icc_name(data):
    try:
        from PIL import ImageCms
        import io
        prof = zlib.decompress(data.split(b"\0", 1)[1][1:])
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(prof))).strip()
    except Exception:
        return "unknown profile"


class NotLanded(Exception):
    pass


def fit(a, log=print, prog=None):
    """Run the whole pipeline for parsed args `a`; returns a stats dict.
    If the exact byte cannot be hit (a single strip that sits on a libdeflate
    block-split cliff), retry with thinner strips: two strips make pair search
    possible, and sums of two noisy size sets have no gaps."""
    import copy
    a = copy.copy(a)
    t0 = a.t_start = time.time()
    if not isinstance(a.size, tuple) and os.path.getsize(a.src) <= a.size:
        return _untouched(a, a.size, log)
    while True:
        try:
            r = _fit(a, log, prog, last_try=a.strip <= 8)
            r["time"] = time.time() - t0
            return r
        except NotLanded:
            a.strip = max(8, a.strip // 2)
            log(f"retrying with {a.strip}-row strips")


def _untouched(a, size, log):
    n = os.path.getsize(a.src)
    log(f"{a.src} is already {n} B <= {size} B: left untouched, nothing written")
    return dict(size=n, target=size, skipped=True, lossless=n, psnr=float("inf"), maxerr=0,
                changed=0.0, wsse=0.0, filt=None, switch=None, strips=0, time=time.time() - a.t_start)


def _fit(a, log, prog, last_try):
    prog = prog or Progress(False)
    t0 = a.t_start
    im = Image.open(a.src)
    if im.mode not in ("L", "LA", "RGB", "RGBA"):
        raise FitError(f"mode {im.mode} not supported (need 8-bit L/LA/RGB/RGBA)")
    img = np.ascontiguousarray(np.asarray(im))
    if img.ndim == 2:
        img = img[..., None]
    H, W, C = img.shape
    color_type = {1: 0, 2: 4, 3: 2, 4: 6}[C]

    ladder = {"odd": list(range(1, 64, 2)), "all": list(range(1, 33))}.get(
        a.ladder) or [int(v) for v in a.ladder.split(",")]

    # fixed overhead of the PNG container
    chunks = read_chunks(a.src)
    ihdr = next(d for t, d in chunks if t == b"IHDR")
    if ihdr[8] != 8:   # Pillow would silently truncate 16-bit RGB to 8 bits
        raise FitError(f"bit depth {ihdr[8]} is not supported (need 8-bit)")
    if any(t == b"tRNS" for t, _ in chunks):
        raise FitError("tRNS colour key is not supported: quantization could punch or fill transparent holes")
    meta, dropped = [], []
    for t, d in chunks:
        if t in (b"IHDR", b"IDAT", b"IEND", b"PLTE"):
            continue
        cat = chunk_category(t, d)
        keep = cat is not None and (cat in a.keep or t in a.keep)
        (meta if keep else dropped).append((t, d))
    desc = lambda lst: ", ".join(f"{t.decode()}[{chunk_category(t, d) or '-'}] {len(d) + 12} B"
                                 for t, d in lst) or "nothing"
    log(f"metadata kept: {desc(meta)}; dropped: {desc(dropped)}")
    for t, d in dropped:
        if t == b"iCCP" and not icc_name(d).lower().startswith("srgb"):   # "Display P3 … sRGB Transfer" is not sRGB
            log(f"WARNING: dropping colour profile '{icc_name(d)}' — colours will look different")
        elif t in (b"gAMA", b"cHRM", b"cICP"):
            log(f"WARNING: dropping {t.decode()} — colours may look different")
    head = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, color_type, 0, 0, 0))
    head += b"".join(png_chunk(t, d) for t, d in meta)
    fixed = len(head) + 12 + 12 + 2 + 2 + 4   # IDAT hdr+crc, IEND, zlib hdr, final block, adler
    # hard floor: the container itself, plus DEFLATE's best ratio (~1032:1) on the
    # filtered rows; nothing below this can exist for an image of these dimensions
    raw = H * (1 + W * C)
    floor = fixed + -(-raw // 1032)
    if not isinstance(a.size, tuple) and a.size < floor:
        raise FitError(
            f"{a.size} B is impossible for a {W}×{H} PNG: the container needs {fixed} B "
            f"(signature, IHDR, kept chunks, IDAT/IEND, zlib framing) and DEFLATE cannot code "
            f"{raw} B of rows in under ~{raw // 1032} B (max ratio ≈1032:1). Absolute floor ≈{floor} B; "
            f"pngfit's own floor is higher, see the top of its ladder")
    pool = ThreadPoolExecutor(a.jobs)

    # probe base filter and row-switch threshold on a few strips at a typical step
    filt, switch = a.filter, a.row_switch
    if filt < 0 or switch < 0:
        probe = Fitter(img, a.strip, 3, ladder, a.mask, a.level, not a.lossy_alpha)
        ks = sorted(set(np.linspace(0, len(probe.bounds) - 1, 4).astype(int)))
        fs = (1, 2, 3, 4) if filt < 0 else (filt,)
        sws = (0.0, 0.05, 0.1, 0.2) if switch < 0 else (switch,)
        prog.start("filter probe", len(ks) * (len(fs) + len(sws) - 1))

        def trial(f, sw):
            probe.filt, probe.adaptive = f, sw
            probe.cache.clear()
            tot = 0
            for r in pool.map(lambda k: probe.run(k, probe.npx[k] // 2), ks):
                tot += r[0]; prog.tick()
            return tot

        sw0 = 0.1 if switch < 0 else switch
        res = {(f, sw0): trial(f, sw0) for f in fs}
        filt = min(fs, key=lambda f: res[(f, sw0)])
        res.update({(filt, sw): trial(filt, sw) for sw in sws if sw != sw0})
        switch = min(sws, key=lambda sw: res[(filt, sw)])
    log(f"filter: {'None Sub Up Average Paeth'.split()[filt]}, row switch {switch:g}")

    F = Fitter(img, a.strip, filt, ladder, a.mask, a.level, not a.lossy_alpha, switch)
    K = len(F.bounds)
    pts = [set() for _ in range(K)]

    def evaluate(reqs, label):
        reqs = [(k, P) for k, P in reqs if (k, P) not in F.cache]
        prog.start(label, len(reqs))
        for _ in pool.map(lambda kp: F.run(*kp), reqs):
            prog.tick()
        for k, P in reqs:
            pts[k].add(P)

    # 1. lossless and coarse ladder until the sum fits
    rung = 0
    evaluate([(k, 0) for k in range(K)], "lossless")
    total0 = sum(F.cache[(k, 0)][0] for k in range(K))
    size = round(a.size[1] * (total0 + fixed)) if isinstance(a.size, tuple) else a.size
    T = size - fixed
    if os.path.getsize(a.src) <= size:      # percent targets are only known here
        pool.shutdown()
        return _untouched(a, size, log)
    log(f"lossless (strips, level {a.level}): {total0 + fixed} B, budget {size} B")
    if total0 <= T:
        log("our lossless re-encode fits (the source does not): writing it, smaller than the "
            "target; reaching the exact size would only take padding")
        Pchoice = [0] * K
    else:
        # one pass at the top of the ladder tells right away whether the target is reachable
        top = len(ladder) - 1
        evaluate([(k, F.maxP[k]) for k in range(K)], f"floor check q={ladder[top]}")
        tot_top = sum(F.cache[(k, F.maxP[k])][0] for k in range(K))
        if tot_top > T:
            pool.shutdown()
            raise FitError(
                f"target {size} B is out of reach: the smallest this image gets is {tot_top + fixed} B "
                f"(step q={ladder[top]} everywhere, error up to ±{ladder[top] // 2}); "
                f"the PNG/DEFLATE floor for {W}×{H} is ≈{floor} B")
        while True:
            rung += 1
            if rung >= len(ladder):
                raise FitError("cannot reach that size even at the top of the ladder")
            for frac in (0.5, 1.0):
                evaluate([(k, min(int((rung - 1 + frac) * F.npx[k]), F.maxP[k])) for k in range(K)],
                         f"ladder q={ladder[rung]}")
            tot = sum(F.cache[(k, rung * F.npx[k])][0] for k in range(K))
            log(f"  rung {rung} (q={ladder[rung]}) everywhere: {tot + fixed} B")
            if tot <= T:
                break

        def solve(lam):
            ch = []
            for k in range(K):
                ch.append(min(pts[k], key=lambda P: F.cache[(k, P)][1] + lam * F.cache[(k, P)][0]))
            return ch, sum(F.cache[(k, P)][0] for k, P in enumerate(ch))

        def best_under():
            lo, hi = 0.0, 1.0
            while solve(hi)[1] > T:
                hi *= 4
            for _ in range(60):
                mid = (lo + hi) / 2
                if solve(mid)[1] > T:
                    lo = mid
                else:
                    hi = mid
            ch, tot = solve(hi)
            # Lagrange only reaches the convex hull; spend what it leaves on the best
            # distortion-per-byte upgrades that still fit
            while True:
                best = None
                for k in range(K):
                    R0, D0 = F.cache[(k, ch[k])][:2]
                    for P in pts[k]:
                        R, D = F.cache[(k, P)][:2]
                        if R0 < R <= R0 + T - tot and D < D0:
                            g = (D0 - D) / (R - R0)
                            if best is None or g > best[0]:
                                best = (g, k, P, R - R0)
                if best is None:
                    return ch, tot
                _, k, P, dR = best
                ch[k] = P; tot += dR

        # 2. RD refinement: densify each strip's curve around its chosen point
        for rnd in range(a.rounds):
            ch, tot = best_under()
            new = []
            for k in range(K):
                s = sorted(pts[k]); i = s.index(ch[k])
                if i > 0: new.append((k, (s[i - 1] + s[i]) // 2))
                if i + 1 < len(s): new.append((k, (s[i] + s[i + 1]) // 2))
            evaluate(new, f"RD round {rnd + 1}/{a.rounds}")
            wd = sum(F.cache[(k, P)][1] for k, P in enumerate(ch))
            log(f"  RD round {rnd + 1}: {tot + fixed} B, weighted SSE {wd:.4g}")
        Pchoice, tot = best_under()

        # 3. exact landing. Strip sizes move by ~0.4 B per pixel but libdeflate's block
        #    decisions add jumps of tens of bytes, so a single strip can skip the target.
        #    First try each strip alone; then pairs: strip i takes slack-x, strip j takes x.
        #    The sum of two noisy size sets leaves almost no gaps.
        slack = T - tot
        log(f"slack after RD: {slack} B — landing on the exact byte")
        base = {k: F.cache[(k, Pchoice[k])][0] for k in range(K)}
        order = sorted(range(K), key=lambda k: -Pchoice[k])
        probes = [0]
        prog.start("exact landing", None, f"slack {slack} B")

        def probe(k, P):
            if 0 <= (P[0] if isinstance(P, tuple) else P) <= F.maxP[k]:
                if (k, P) not in F.cache:
                    probes[0] += 1
                    prog.tick()
                return F.run(k, P)[0]
            return None

        brk = {}   # strip -> (P near the target, bytes per unit of P), left by single()

        def single(k, need, seed=0):
            P1 = Pchoice[k]
            P0 = max([P for P in pts[k] if P <= P1 and F.cache[(k, P)][0] >= need], default=None)
            if P0 is None:
                return None
            g = max((F.cache[(k, P0)][0] - F.cache[(k, P1)][0]) / max(P1 - P0, 1), 1e-6)
            lo, hi = P0, P1                     # lower P = fewer lossy pixels = bigger
            while hi - lo > 1:
                mid = (lo + hi) // 2
                r = probe(k, mid)
                if r == need:
                    return mid
                lo, hi = (mid, hi) if r > need else (lo, mid)
            brk[k] = (lo, g)
            # sizes drift ~g bytes per step away from the bracket, so only nearby probes can hit
            for d in range(24):
                for P in (lo - d, hi + d):
                    if probe(k, P) == need:
                        return P
            return None

        def seed_walk(k, need, seed):
            """Newton-like walk from the seed-0 bracket under a reshuffled tie order:
            a handful of probes per seed, each landing near the target."""
            P, g = brk[k]
            tried = set()
            for _ in range(8):
                P = int(min(max(P, 0), F.maxP[k]))
                if P in tried:
                    break
                tried.add(P)
                r = probe(k, (P, seed))
                if r == need:
                    return (P, seed)
                step = (r - need) / g           # too big -> push more pixels up the ladder
                P += int(np.sign(step) * max(1, round(abs(step))))
            return None

        def sizes(k):  # cached alternatives of strip k: size -> best (lowest wsse) P
            m = {}
            for (kk, P), (R, w_, *_r) in F.cache.items():
                if kk == k and (R not in m or w_ < F.cache[(k, m[R])][1]):
                    m[R] = P
            return m

        landed = slack == 0

        def try_single(ks):
            for k in ks:
                P = single(k, base[k] + slack)
                if P is not None:
                    Pchoice[k] = P
                    log(f"  landed in strip {k} alone ({probes[0]} probes)")
                    return True
            return False

        def try_pairs():
            for j in order:
                # alternatives of j next to its RD choice, probed in parallel
                todo = [P for d in range(1, 25) for P in (Pchoice[j] - d, Pchoice[j] + d)
                        if 0 <= P <= F.maxP[j] and (j, P) not in F.cache]
                for _ in pool.map(lambda P: F.run(j, P), todo):
                    probes[0] += 1; prog.tick()
                sj = sizes(j)
                best = None
                for i in range(K):
                    if i == j:
                        continue
                    si = sizes(i)
                    for Rj, Pj in sj.items():
                        Pi = si.get(base[i] + base[j] + slack - Rj)
                        if Pi is not None:
                            cost = F.cache[(i, Pi)][1] + F.cache[(j, Pj)][1]
                            if best is None or cost < best[0]:
                                best = (cost, i, Pi, Pj)
                if best:
                    _, i, Pi, Pj = best
                    Pchoice[i], Pchoice[j] = Pi, Pj
                    log(f"  landed with strips {i}+{j} ({probes[0]} probes)")
                    return True
            return False

        # a few single tries leave dense size neighbourhoods behind, which is exactly
        # what the pair search feeds on; only then fall back to the remaining strips
        landed = landed or try_single(order[:3]) or try_pairs() or try_single(order[3:])
        # last resort (one-strip images, flat screenshots): reshuffle ties, seeds in parallel
        for k in [k for k in order if k in brk][:3]:
            for s0 in range(1, 33, a.jobs):
                if landed:
                    break
                seeds = range(s0, min(s0 + a.jobs, 33))
                hits = [h for h in pool.map(lambda sd: seed_walk(k, base[k] + slack, sd), seeds) if h]
                if hits:
                    Pchoice[k] = min(hits, key=lambda h: F.cache[(k, h)][1])
                    landed = True
                    log(f"  landed in strip {k} alone (seed {Pchoice[k][1]}) ({probes[0]} probes)")
        # and if even that fails: the old wide scan around the seed-0 bracket
        for k in [k for k in order if k in brk][:3]:
            if landed:
                break
            P, _ = brk[k]
            for d in range(24, int(np.clip(3e7 / (F.npx[k] * C), 24, 2000))):
                hit = next((Q for Q in (P - d, P + 1 + d) if probe(k, Q) == base[k] + slack), None)
                if hit is not None:
                    Pchoice[k] = hit; landed = True
                    log(f"  landed in strip {k} alone, wide scan ({probes[0]} probes)")
                    break
        if not landed:
            tot = sum(F.cache[(k, P)][0] for k, P in enumerate(Pchoice))
            if not last_try and K < H:
                log(f"  no exact hit with {K} strip(s), {T - tot} B short")
                pool.shutdown()
                raise NotLanded
            log(f"WARNING: could not land exactly, {T - tot} B short")

    # 4. assemble
    frags, adler, rec = [], 1, np.empty_like(img)
    prog.start("writing", K)
    outs = []
    for o in pool.map(lambda k: F.run(k, Pchoice[k], keep=True), range(K)):
        outs.append(o); prog.tick()
    wsse = sse = 0.0; maxe = 0
    for k, ((R, w_, s_, m_), frag, out_f, out_rec) in enumerate(outs):
        y0, y1 = F.bounds[k]
        frags.append(frag); adler = zlib.adler32(out_f.tobytes(), adler); rec[y0:y1] = out_rec
        wsse += w_; sse += s_; maxe = max(maxe, m_)
    idat = b"\x78\xda" + b"".join(frags) + b"\x03\x00" + struct.pack(">I", adler)
    data = head + png_chunk(b"IDAT", idat) + png_chunk(b"IEND", b"")
    with open(a.dst, "wb") as f:
        f.write(data)

    # 5. verify by decoding what we wrote
    prog.start("verifying", None)
    back = np.asarray(Image.open(a.dst))
    back = back[..., None] if back.ndim == 2 else back
    assert np.array_equal(back, rec), "decoded image differs from the model!"
    mse = sse / img.size
    changed = np.any(rec != img, axis=2).mean()
    psnr = 10 * np.log10(255 ** 2 / mse) if mse else float("inf")
    log(f"wrote {a.dst}: {len(data)} B (target {size}, {'EXACT' if len(data) == size else 'off by %d' % (size - len(data))})")
    log(f"PSNR {psnr:.2f} dB, max error {maxe}, pixels changed {changed * 100:.1f}%, time {time.time() - t0:.0f}s")
    pool.shutdown()
    return dict(size=len(data), target=size, lossless=total0 + fixed, psnr=psnr, maxerr=maxe,
                changed=changed, wsse=wsse, filt=filt, switch=switch, strips=K, time=time.time() - t0)


if __name__ == "__main__":
    main()
