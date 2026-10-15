#!/usr/bin/env python3
"""Self-test for the pngfit binary (make check): builds test images, runs pngfit on them and checks
every output with the independent decoder in tools/check.py. Needs Python 3 with numpy and Pillow.

  tools/selftest.py [path/to/pngfit]
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

import numpy as np
from check import compare, decode, read_chunks
from testimages import apng, chunk, photo, png

HERE = os.path.dirname(os.path.abspath(__file__))

BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, os.pardir, "pngfit"))
TMP = tempfile.mkdtemp(prefix="pngfit-selftest-")
failures = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(name)


def results(args, codes=(0,)):
    """Run pngfit with --json and return its results, one per file."""
    p = subprocess.run([BIN, "-q", "--json"] + args, capture_output=True, text=True)
    if p.returncode not in codes:
        failures.append(f"exit code {p.returncode} for {' '.join(args)}: {p.stderr.strip()[:200]}")
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def run(args, codes=(0,)):
    """Run pngfit on one file; the result is empty when it printed none, after a crash for one."""
    found = results(args, codes)
    return found[0] if found else {}


def max_error(source, output):
    """None when the output is missing or its frames do not match the source."""
    return compare(source, output).max_error if os.path.exists(output) else None


def test_formats():
    print("formats, two targets each")
    cases = [
        ("grey8", photo(96, 128, 1)),
        ("greyalpha8", photo(96, 128, 2)),
        ("rgb8", photo(96, 128, 3)),
        ("rgba8", photo(96, 128, 4)),
        ("grey16", photo(80, 96, 1, 16)),
        ("rgb16", photo(80, 96, 3, 16)),
    ]
    for name, pixels in cases:
        depth = 16 if name.endswith("16") else 8
        source = png(f"{TMP}/{name}.png", pixels, depth)

        for target in ("90%", "65%"):
            output = f"{TMP}/{name}_{target[:-1]}.png"
            r = run([source, output, "-s", target])
            exact = r.get("status") == "exact" and os.path.getsize(output) == r["target"]
            check(f"{name} at {target}: exact size", exact, str(r))

            error = max_error(source, output)
            check(
                f"{name} at {target}: error as reported",
                error == r.get("max_error"),
                f"{error} vs {r.get('max_error')}",
            )

            if pixels.shape[2] in (2, 4):
                alpha_in = decode(source).frames[0][1][..., -1]
                alpha_out = decode(output).frames[0][1][..., -1]
                check(f"{name} at {target}: alpha untouched", (alpha_in == alpha_out).all())


def test_odd_inputs():
    source = png(f"{TMP}/interlaced.png", photo(77, 101, 3), interlace=True)
    output = f"{TMP}/interlaced_out.png"
    r = run([source, output, "-s", "80%"])
    check("interlaced input: exact", r.get("status") == "exact")
    check("interlaced input: error as reported", max_error(source, output) == r.get("max_error"))

    for name, (height, width) in (("tiny", (1, 1)), ("three rows", (3, 160)), ("one column", (90, 1))):
        source = png(f"{TMP}/small.png", photo(height, width, 3))
        r = run([source, f"{TMP}/small_out.png", "-s", "90%"], (0, 1))
        check(f"{name}: handled", r.get("status") in ("exact", "skipped", "lossless", "minimum", "short"), str(r))


def test_reduction():
    print("lossless format reduction")
    rgb = photo(64, 80, 3)
    opaque = np.full(rgb.shape[:2] + (1,), 255)
    cases = (
        ("opaque RGBA", np.concatenate([rgb, opaque], 2), 8, 3, 8),
        ("grey RGB", np.concatenate([rgb[..., :1]] * 3, 2), 8, 1, 8),
        ("8-bit values in 16 bits", rgb * 257, 16, 3, 8),
    )
    for name, pixels, depth, want_samples, want_depth in cases:
        source = png(f"{TMP}/red.png", pixels, depth)
        output = f"{TMP}/red_out.png"

        for target in ("100%", "80%"):
            r = run([source, output, "-s", target])
            stored = decode(output)
            samples = stored.frames[0][1].shape[2]
            stored_as = samples == want_samples and stored.depth == want_depth
            check(f"{name} at {target}: stored as {want_samples} channel(s), {want_depth}-bit", stored_as)

            if target == "100%":
                error = max_error(source, output)
                check(f"{name}: lossless at full size", error == 0, str(error))
            else:
                check(f"{name} at 80%: exact", r.get("status") == "exact", str(r))

        run([source, output, "-s", "100%", "--no-reduce"])
        stored = decode(output)
        kept = stored.frames[0][1].shape[2] == pixels.shape[2] and stored.depth == depth
        check(f"{name}: --no-reduce keeps the format", kept)


def test_apng():
    print("animated PNG")
    frames = [
        (photo(48, 64, 4), 0, 0, 1, 10, 0, 0),
        (photo(20, 30, 4), 10, 12, 2, 25, 1, 1),
        (photo(48, 64, 4), 0, 0, 1, 10, 2, 1),
        (photo(16, 16, 4), 40, 30, 7, 100, 0, 0),
    ]
    source = apng(f"{TMP}/anim.png", frames, (64, 48), plays=3)
    output = f"{TMP}/anim_out.png"

    for target in ("85%", "60%"):
        r = run([source, output, "-s", target])
        check(f"APNG at {target}: exact", r.get("status") == "exact", str(r))

        a, b = decode(source), decode(output)
        same_controls = [control for control, _ in a.frames] == [control for control, _ in b.frames]
        check(
            f"APNG at {target}: frames, delays, offsets, dispose, blend, loop count identical",
            same_controls and a.plays == b.plays,
        )
        check(f"APNG at {target}: error as reported", max_error(source, output) == r.get("max_error"))

    r = run([source, f"{TMP}/anim_min.png", "-s", "0"])
    all_frames = len(decode(f"{TMP}/anim_min.png").frames) == 4
    check("APNG at 0: the minimum, still all frames", r.get("status") == "minimum" and all_frames, str(r))


def visible_psnr(source, output):
    """PSNR of the colour under pixels that are not fully transparent."""
    a = decode(source).frames[0][1]
    b = decode(output).frames[0][1]
    visible = a[..., 3] > 0
    diff = (a[visible][:, :3] - b[visible][:, :3]).astype(float)
    return round(10 * np.log10(255**2 / max((diff**2).mean(), 1e-9)), 2)


def test_free_alpha():
    print("--alpha: colour under transparent pixels")
    sticker = photo(96, 128, 4)
    sticker[..., 3] = np.where(sticker[..., 3] < 60, 0, sticker[..., 3])
    source = png(f"{TMP}/sticker.png", sticker)

    # with --alpha even "100%" is smaller, so the second run gets the byte count of the first
    target = "70%"
    psnr = {}
    for option in ([], ["--alpha"]):
        output = f"{TMP}/sticker_out{len(option)}.png"
        r = run([source, output, "-s", target] + option)
        target = str(r.get("size"))
        psnr[len(option)] = visible_psnr(source, output)

        # with --alpha the visibly lossless file may already fit those bytes: that is the best outcome
        allowed = ("exact", "lossless") if option else ("exact",)
        alpha_kept = (decode(source).frames[0][1][..., 3] == decode(output).frames[0][1][..., 3]).all()
        check(
            f"sticker {' '.join(option) or 'default'}: {r.get('status')} at {r.get('size')} B, alpha untouched",
            r.get("status") in allowed and alpha_kept,
        )

    better = psnr[1] > psnr[0]
    check(f"--alpha: visible pixels better within the same bytes, PSNR {psnr[0]} -> {psnr[1]} dB", better, str(psnr))


def test_max_error():
    print("--max-error: a hard cap on the error")
    source = png(f"{TMP}/cap.png", photo(96, 128, 3))
    output = f"{TMP}/cap_out.png"

    for cap, target in (("1", "80%"), ("1", "40%"), ("3", "50%"), ("0", "90%")):
        r = run([source, output, "-s", target, "-e", cap])
        error = max_error(source, output)
        within = error is not None and error <= int(cap)
        check(
            f"-e {cap} at {target}: {r.get('status')}, worst error {error} <= {cap}",
            within and r.get("status") in ("exact", "minimum", "short", "lossless"),
            str(r),
        )

    plain = run([source, f"{TMP}/cap_a.png", "-s", "40%"])
    capped = run([source, f"{TMP}/cap_b.png", "-s", "40%", "-e", "1"])
    same = plain.get("target") == capped.get("target")
    check(
        "-e with a percent target: the same target as without", same, f"{plain.get('target')} vs {capped.get('target')}"
    )


def test_targets():
    print("targets that cannot be met")
    source = png(f"{TMP}/edge.png", photo(64, 96, 3))
    output = f"{TMP}/edge_out.png"

    r = run([source, output, "-s", str(os.path.getsize(source) + 10)])
    untouched = r.get("status") == "skipped" and not os.path.exists(output)
    check("source already fits: untouched, nothing written", untouched)

    r = run([source, output, "-s", "1e30"])
    check("-s 1e30: more than any file, untouched", r.get("status") == "skipped", str(r))

    for target in ("0", "0%", "1"):
        r = run([source, output, "-s", target])
        valid = max_error(source, output) is not None
        check(f"-s {target}: smallest file, valid", r.get("status") == "minimum" and valid, str(r))

    p = subprocess.run([BIN, source, f"{TMP}/x.png", "-s", "-5"], capture_output=True)
    check("-s -5: refused as a typo", p.returncode == 2)


def test_metadata():
    print("metadata")
    meta = chunk(b"gAMA", struct.pack(">I", 45455)) + chunk(b"tEXt", b"Comment\0kept by default")
    source = png(f"{TMP}/meta.png", photo(64, 96, 3), extra=meta)
    output = f"{TMP}/meta_out.png"

    for option, want in (([], {b"gAMA", b"tEXt"}), (["--strip", "meta"], {b"gAMA"}), (["-k", "none"], set())):
        r = run([source, output, "-s", "85%"] + option)
        kept = {kind for kind, _ in read_chunks(output)} & {b"gAMA", b"tEXt"}
        names = sorted(kind.decode() for kind in want) or "nothing"
        check(
            f"metadata {' '.join(option) or 'default'}: exact, kept {names}",
            r.get("status") == "exact" and kept == want,
            str(sorted(kept)),
        )


def broken_files():
    """Files pngfit must refuse with a reason: unsupported, damaged, or with impossible sizes."""
    from PIL import Image

    Image.fromarray(photo(32, 32, 3).astype(np.uint8)).quantize(16).save(f"{TMP}/palette.png")
    png(f"{TMP}/trns.png", photo(32, 32, 3), extra=chunk(b"tRNS", struct.pack(">HHH", 0, 0, 0)))

    good = open(png(f"{TMP}/good.png", photo(32, 32, 3)), "rb").read()
    open(f"{TMP}/truncated.png", "wb").write(good[: len(good) // 2])
    open(f"{TMP}/notpng.png", "wb").write(b"definitely not a PNG")

    bad_crc = bytearray(good)
    bad_crc[40] ^= 0xFF
    open(f"{TMP}/badcrc.png", "wb").write(bad_crc)

    # sizes that fit only an unsigned 32-bit field: a width of 2^31, a frame at x = 2^32 - 1
    wide = bytearray(good)
    wide[16:20] = struct.pack(">I", 1 << 31)
    wide[29:33] = struct.pack(">I", zlib.crc32(bytes(wide[12:29])))
    open(f"{TMP}/wide.png", "wb").write(wide)

    control = chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 32, 32, 0xFFFFFFFF, 0, 1, 10, 0, 0))
    png(f"{TMP}/offcanvas.png", photo(32, 32, 3), extra=chunk(b"acTL", struct.pack(">II", 1, 0)) + control)

    return ["palette", "trns", "truncated", "notpng", "badcrc", "wide", "offcanvas"]


def broken_icc_profile():
    """An ICC profile whose name offset wraps around 32 bits; pngfit reads it when dropping it."""
    profile = bytearray(172)
    profile[128:132] = struct.pack(">I", 1)
    profile[132:144] = b"desc" + struct.pack(">II", 144, 28)
    profile[144:148] = b"mluc"
    profile[164:172] = struct.pack(">II", 4, 0xFFFFFFFF)
    iccp = chunk(b"iCCP", b"x\0\0" + zlib.compress(bytes(profile)))
    return png(f"{TMP}/icc.png", photo(32, 32, 3), extra=iccp)


def test_refusals():
    print("refusals")
    for name in broken_files():
        r = run([f"{TMP}/{name}.png", f"{TMP}/r.png", "-s", "50%"], (1,))
        check(f"{name}: refused with a reason", r.get("status") == "error" and r.get("error"), str(r))

    r = run([broken_icc_profile(), f"{TMP}/r.png", "-s", "80%", "-k", "none"])
    check("broken ICC profile dropped without a crash", r.get("status") == "exact", str(r))

    same = f"{TMP}/same.png"
    shutil.copy(f"{TMP}/good.png", same)
    r = run([same, same, "-s", "80%"], (1,))
    intact = open(same, "rb").read() == open(f"{TMP}/good.png", "rb").read()
    check("output = input: refused, source intact", r.get("status") == "error" and intact)


def test_batch():
    print("batch mode")
    sources = [png(f"{TMP}/b{i}.png", photo(48 + 8 * i, 64, 3)) for i in range(4)]
    inputs = sources + [f"{TMP}/notpng.png"]

    for parallel in ("1", "2"):
        os.makedirs(f"{TMP}/out{parallel}", exist_ok=True)
        rs = results(["-s", "80%", "-o", f"{TMP}/out{parallel}", "-P", parallel] + inputs, (1,))
        exact = sum(r["status"] == "exact" for r in rs)
        check(f"batch -P {parallel}: one JSON line per file, the bad one failed alone", len(rs) == 5 and exact == 4)

    same = all(open(f"{TMP}/out1/b{i}.png", "rb").read() == open(f"{TMP}/out2/b{i}.png", "rb").read() for i in range(4))
    check("batch: parallel output identical to one by one", same)

    # more threads than this machine has are fine: the file must not depend on the count
    same = True
    for source in sources:
        outputs = set()
        for jobs in ("1", "4", "7"):
            run([source, f"{TMP}/j.png", "-s", "80%", "-j", jobs])
            outputs.add(open(f"{TMP}/j.png", "rb").read())
        same = same and len(outputs) == 1
    check("threads: the same bytes with -j 1, 4 and 7", same)


def main():
    print(f"pngfit self-test: {BIN}")
    if not os.path.exists(BIN):
        sys.exit("binary not found: build it with make first")

    test_formats()
    test_odd_inputs()
    test_reduction()
    test_apng()
    test_free_alpha()
    test_max_error()
    test_targets()
    test_metadata()
    test_refusals()
    test_batch()

    shutil.rmtree(TMP, ignore_errors=True)
    print("\nall passed" if not failures else f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
