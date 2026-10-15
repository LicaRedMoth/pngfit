#!/usr/bin/env python3
"""Independent check of a pngfit output, with a small PNG and APNG decoder of its own (numpy and zlib).
Pillow is not used because it truncates 16-bit RGB.

  tools/check.py SOURCE.png OUTPUT.png [EXPECTED_SIZE]    exit code 1 on a mismatch
"""

import collections
import os
import struct
import sys
import zlib

import numpy as np

SIGNATURE = b"\x89PNG\r\n\x1a\n"
SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}  # PNG colour type -> samples per pixel
ADAM7 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]

# frames: (frame control without its sequence number, samples); the control is None for a still image
Decoded = collections.namedtuple("Decoded", "frames plays depth chunk_types")
Comparison = collections.namedtuple("Comparison", "max_error psnr")

try:  # numba only makes it fast; the logic is plain Python either way
    from numba import njit
except ImportError:

    def njit(f):
        return f


@njit
def _unfilter(data, height, rowbytes, bpp, rows):
    pos = 0
    for y in range(height):
        filter_type = data[pos]
        pos += 1

        for i in range(rowbytes):
            left = rows[y, i - bpp] if i >= bpp else 0
            up = rows[y - 1, i] if y > 0 else 0
            up_left = rows[y - 1, i - bpp] if y > 0 and i >= bpp else 0

            if filter_type == 0:
                predicted = 0
            elif filter_type == 1:
                predicted = left
            elif filter_type == 2:
                predicted = up
            elif filter_type == 3:
                predicted = (left + up) >> 1
            elif filter_type == 4:
                guess = left + up - up_left
                to_left, to_up, to_up_left = abs(guess - left), abs(guess - up), abs(guess - up_left)
                if to_left <= to_up and to_left <= to_up_left:
                    predicted = left
                elif to_up <= to_up_left:
                    predicted = up
                else:
                    predicted = up_left
            else:
                return -1

            rows[y, i] = (data[pos + i] + predicted) & 255
        pos += rowbytes
    return pos


def unfilter(data, height, rowbytes, bpp):
    """Undo the row filters; returns the rows and how many bytes of data they took."""
    rows = np.zeros((height, rowbytes), np.int32)
    used = _unfilter(np.frombuffer(data, np.uint8).astype(np.int32), height, rowbytes, bpp, rows)
    assert used >= 0, "bad filter type"
    return rows.astype(np.uint8), used


def read_chunks(path):
    """The chunks of a PNG as (type, data) pairs, with the signature and every CRC checked."""
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == SIGNATURE, f"{path}: not a PNG"

    chunks = []
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
        assert zlib.crc32(kind + body) == crc, f"{path}: bad CRC in {kind.decode()}"
        chunks.append((kind, body))
        pos += 12 + length
    return chunks


def decode_pixels(data, width, height, bpp, interlaced):
    """The bytes of one image, shape (height, width, bpp)."""
    if not interlaced:
        rows, _ = unfilter(data, height, width * bpp, bpp)
        return rows.reshape(height, width, bpp)

    pixels = np.zeros((height, width, bpp), np.uint8)
    pos = 0
    for x0, y0, dx, dy in ADAM7:
        if width <= x0 or height <= y0:
            continue
        pass_width = (width - x0 + dx - 1) // dx
        pass_height = (height - y0 + dy - 1) // dy
        rows, used = unfilter(data[pos:], pass_height, pass_width * bpp, bpp)
        pixels[y0::dy, x0::dx] = rows.reshape(pass_height, pass_width, bpp)
        pos += used
    return pixels


def decode(path):
    chunks = read_chunks(path)
    width, height, depth, colour_type, _, _, interlaced = struct.unpack(">IIBBBBB", chunks[0][1])
    bpp = SAMPLES[colour_type] * depth // 8
    actl = [body for kind, body in chunks if kind == b"acTL"]
    plays = struct.unpack(">I", actl[0][4:8])[0] if actl else None

    # each frame's compressed data; image data before any fcTL is a still or a default image
    frames = []
    sequence = []
    for kind, body in chunks:
        if actl and kind == b"fcTL":
            sequence.append(struct.unpack(">I", body[:4])[0])
            frames.append([body[4:], b""])
        elif kind == b"IDAT":
            if not frames:
                frames.append([None, b""])
            frames[-1][1] += body
        elif actl and kind == b"fdAT":
            sequence.append(struct.unpack(">I", body[:4])[0])
            frames[-1][1] += body[4:]
    assert sequence == list(range(len(sequence))), f"{path}: APNG sequence numbers out of order"

    decoded = []
    for control, data in frames:
        frame_width, frame_height = (width, height) if control is None else struct.unpack(">II", control[:8])
        pixels = decode_pixels(zlib.decompress(data), frame_width, frame_height, bpp, interlaced)
        samples = pixels.astype(np.int64)
        if depth == 16:
            samples = (samples[..., 0::2] << 8) | samples[..., 1::2]
        decoded.append((control, samples))

    return Decoded(decoded, plays, depth, [kind for kind, _ in chunks])


def as_source_format(samples, source_samples, depth, source_depth):
    """Undo pngfit's lossless reductions (grey from colour, opaque alpha dropped, 16 to 8 bits)."""
    if samples.shape[2] <= 2 and source_samples >= 3:
        grey = [samples[..., :1]] * 3
        alpha = [samples[..., 1:2]] if samples.shape[2] == 2 else []
        samples = np.concatenate(grey + alpha, 2)

    if samples.shape[2] < source_samples:
        opaque = np.full(samples.shape[:2] + (1,), 255 if depth == 8 else 65535)
        samples = np.concatenate([samples, opaque], 2)

    if depth == 8 and source_depth == 16:
        samples = samples * 257
    return samples


def compare(source, output):
    """Worst sample error (in the output's units, as pngfit reports it) and PSNR of output against
    source; both None when the frames or their controls differ."""
    a, b = decode(source), decode(output)
    if len(a.frames) != len(b.frames) or a.plays != b.plays:
        return Comparison(None, None)

    reduced_depth = a.depth == 16 and b.depth == 8
    worst = 0
    squares = 0.0
    count = 0
    for (control_a, samples_a), (control_b, samples_b) in zip(a.frames, b.frames):
        if control_a != control_b or samples_a.shape[:2] != samples_b.shape[:2]:
            return Comparison(None, None)

        samples_b = as_source_format(samples_b, samples_a.shape[2], b.depth, a.depth)
        diff = samples_a - samples_b
        error = int(np.abs(diff).max())
        worst = max(worst, error // 257 if reduced_depth else error)
        squares += float((diff.astype(np.float64) ** 2).sum())
        count += diff.size

    peak = 255 if a.depth == 8 else 65535
    psnr = 10 * np.log10(peak**2 * count / squares) if squares else float("inf")
    return Comparison(worst, psnr)


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    source, output = sys.argv[1], sys.argv[2]

    size = os.path.getsize(output)
    result = compare(source, output)
    ok = result.max_error is not None
    if len(sys.argv) == 4:
        ok = ok and size == int(sys.argv[3])

    frames = len(decode(output).frames)
    error = "frames differ" if result.max_error is None else f"max error {result.max_error}, PSNR {result.psnr:.2f} dB"
    print(f"{output}: {size} B, {frames} frame(s), {error} -> {'OK' if ok else 'MISMATCH'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
