#!/usr/bin/env python3
"""Seed files for tools/fuzz.sh: every colour type and depth, interlacing, APNG, and the metadata
pngfit parses (ICC profile names, EXIF, sBIT and bKGD, which format reduction rewrites).

  tools/fuzz_seeds.py DIR
"""

import os
import struct
import sys
import zlib

import numpy as np
from testimages import apng, chunk, photo, png


def icc_profile(description):
    """An iCCP chunk whose profile holds one 'desc' tag."""
    profile = bytearray(144) + description
    profile[128:132] = struct.pack(">I", 1)
    profile[132:144] = b"desc" + struct.pack(">II", 144, len(description))
    return chunk(b"iCCP", b"sRGB\0\0" + zlib.compress(bytes(profile)))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)

    for samples in (1, 2, 3, 4):
        for depth in (8, 16):
            png(f"{out}/s{samples}_{depth}.png", photo(5, 7, samples, depth), depth)
    png(f"{out}/adam7.png", photo(9, 9, 3), interlace=True)

    # reducible three ways: 16-bit samples holding 8-bit values, grey colour, alpha opaque everywhere
    grey = np.repeat(photo(4, 6, 1), 3, axis=2)
    reducible = np.concatenate([grey, np.full((4, 6, 1), 255)], 2) * 257
    bits = chunk(b"sBIT", bytes([8, 8, 8, 8])) + chunk(b"bKGD", struct.pack(">HHH", 0, 257, 514))
    png(f"{out}/reduce.png", reducible, 16, extra=bits)

    # an APNG whose default image is frame 0, then a smaller frame at an offset
    frames = [(photo(6, 8, 4), 0, 0, 1, 10, 0, 0), (photo(3, 4, 4), 2, 1, 1, 10, 1, 1)]
    apng(f"{out}/apng.png", frames, (8, 6))

    # metadata: ICC profile names as v2 'desc' text and as v4 'mluc', EXIF, EXIF inside a text chunk
    text_name = b"desc" + bytes(4) + struct.pack(">I", 5) + b"sRGB\0"
    unicode_name = b"mluc" + bytes(16) + struct.pack(">II", 4, 28) + b"\0s\0R"
    meta = chunk(b"eXIf", b"MM\0*\0\0\0\x08\0\0")
    meta += chunk(b"tEXt", b"Raw profile type exif\0\nexif\n4\n4d4d")
    meta += chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1))
    meta += chunk(b"tIME", struct.pack(">HBBBBB", 2026, 1, 2, 3, 4, 5))
    png(f"{out}/icc2.png", photo(4, 4, 3), extra=icc_profile(text_name) + meta)
    png(f"{out}/icc4.png", photo(4, 4, 3), extra=icc_profile(unicode_name))


if __name__ == "__main__":
    main()
