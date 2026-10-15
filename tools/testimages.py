#!/usr/bin/env python3
"""Small PNG and APNG files written from numpy arrays, for the self-test and the fuzzer's seeds.
Run as a script, it writes one test image (check_web_entry.sh uses it).

  tools/testimages.py OUT.png    a photo-like RGB image, 96x64
"""

import struct
import sys
import zlib

import numpy as np

SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOUR_TYPE = {1: 0, 2: 4, 3: 2, 4: 6}  # samples per pixel -> PNG colour type
ADAM7 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]

rng = np.random.default_rng(2026)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def ihdr(width, height, depth, colour_type, interlace=0):
    return chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, colour_type, 0, 0, interlace))


def scanlines(pixels, depth):
    """Rows with filter type 0, ready to compress."""
    sample_type = ">u2" if depth == 16 else np.uint8
    return b"".join(b"\0" + row.astype(sample_type).tobytes() for row in pixels)


def image_data(pixels, depth, interlace=False):
    if not interlace:
        return zlib.compress(scanlines(pixels, depth), 9)

    passes = b""
    for x0, y0, dx, dy in ADAM7:
        sub = pixels[y0::dy, x0::dx]
        if sub.size:
            passes += scanlines(sub, depth)
    return zlib.compress(passes, 9)


def png(path, pixels, depth=8, extra=b"", interlace=False):
    """Write pixels of shape (height, width, samples); extra chunks go before the image data."""
    height, width, samples = pixels.shape
    header = ihdr(width, height, depth, COLOUR_TYPE[samples], int(interlace))
    data = chunk(b"IDAT", image_data(pixels, depth, interlace))

    with open(path, "wb") as f:
        f.write(SIGNATURE + header + extra + data + chunk(b"IEND", b""))
    return path


def apng(path, frames, canvas, plays=0, extra=b""):
    """frames: (RGBA 8-bit pixels, x, y, delay numerator, delay denominator, dispose, blend)."""
    out = SIGNATURE + ihdr(canvas[0], canvas[1], 8, 6) + extra
    out += chunk(b"acTL", struct.pack(">II", len(frames), plays))

    sequence = 0
    for i, (pixels, x, y, delay_num, delay_den, dispose, blend) in enumerate(frames):
        height, width, _ = pixels.shape
        control = struct.pack(">IIIIIHHBB", sequence, width, height, x, y, delay_num, delay_den, dispose, blend)
        out += chunk(b"fcTL", control)
        sequence += 1

        data = image_data(pixels, 8)
        if i == 0:
            out += chunk(b"IDAT", data)
        else:
            out += chunk(b"fdAT", struct.pack(">I", sequence) + data)
            sequence += 1

    with open(path, "wb") as f:
        f.write(out + chunk(b"IEND", b""))
    return path


def photo(height, width, samples=3, depth=8):
    """Photo-like content: smooth shapes, an edge and grain; alpha is a soft disc."""
    y, x = np.mgrid[:height, :width].astype(float)
    colours = min(samples, 3)
    planes = [128 + 60 * np.sin(x / 17 + k) * np.cos(y / 23 - k) + 40 * (x + y > width) for k in range(3)]
    image = np.stack(planes[:colours], 2) + rng.normal(0, 6, (height, width, colours))

    if samples in (2, 4):
        alpha = np.clip((np.hypot(y - height / 2, x - width / 2) - height / 4) * -20 + 128, 0, 255)
        image = np.concatenate([image, alpha[..., None]], 2)

    image = np.clip(image, 0, 255)
    if depth == 16:
        return np.clip(image * 257 + rng.normal(0, 40, image.shape), 0, 65535).astype(np.int64)
    return image.astype(np.int64)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    png(sys.argv[1], photo(64, 96))
