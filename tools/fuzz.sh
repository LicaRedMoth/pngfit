#!/bin/sh
# Fuzz the PNG reader with libFuzzer, ASan and UBSan (clang only); a crashing input lands in fuzz/.
# Usage: tools/fuzz.sh [seconds], 60 by default. Replay one input: fuzz/fuzz fuzz/crash-...
set -eu
cd "$(dirname "$0")/.."
secs=${1:-60}
mkdir -p fuzz/corpus

clang -g -O1 -fsanitize=fuzzer,address,undefined -fno-sanitize-recover=undefined -std=gnu11 \
    -Ivendor/libdeflate -Ivendor/zlib -o fuzz/fuzz tools/fuzz.c \
    vendor/libdeflate/lib/*.c vendor/libdeflate/lib/x86/cpu_features.c vendor/libdeflate/lib/arm/cpu_features.c \
    vendor/zlib/adler32.c vendor/zlib/crc32.c vendor/zlib/inflate.c vendor/zlib/inffast.c \
    vendor/zlib/inftrees.c vendor/zlib/uncompr.c vendor/zlib/zutil.c -lpthread -lm

# seeds: one small file per colour type and depth, interlaced, animated, and with the metadata pngfit parses
python3 - fuzz/corpus <<'EOF'
import random, struct, sys, zlib

out = sys.argv[1]
rnd = random.Random(1)


def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))


def rows(w, h, bpp, f=lambda x, y, i: rnd.randrange(256)):
    return [bytes(f(x, y, i) for x in range(w) for i in range(bpp)) for y in range(h)]


def idat(rs, filt=1):
    return zlib.compress(b"".join(bytes([filt]) + r for r in rs))


def adam7(w, h, bpp, px):
    data = b""
    for x0, y0, dx, dy in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)):
        for y in range(y0, h, dy):
            line = b"".join(px[y][x * bpp:(x + 1) * bpp] for x in range(x0, w, dx))
            if line:
                data += b"\0" + line
    return zlib.compress(data)


def png(name, w, h, depth, ctype, body, extra=b"", interlace=0):
    ihdr = struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, interlace)
    with open(f"{out}/{name}.png", "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + extra + body + chunk(b"IEND", b""))


chans = {0: 1, 2: 3, 4: 2, 6: 4}
for ctype, c in chans.items():
    for depth in (8, 16):
        bpp = c * depth // 8
        png(f"t{ctype}_{depth}", 7, 5, depth, ctype, chunk(b"IDAT", idat(rows(7, 5, bpp))))
# reducible: 16-bit holding 8-bit values, opaque alpha, grey colour
png("reduce", 6, 4, 16, 6, chunk(b"IDAT", idat(rows(6, 4, 8, lambda x, y, i: 255 if i >= 6 else 17 * (x + y)), 4)),
    chunk(b"sBIT", bytes([8, 8, 8, 8])) + chunk(b"bKGD", struct.pack(">HHH", 0, 257, 514)))
px = rows(9, 9, 3)
png("adam7", 9, 9, 8, 2, chunk(b"IDAT", adam7(9, 9, 3, px)), interlace=1)
# APNG: the default image is frame 0, then a smaller frame at an offset
actl = chunk(b"acTL", struct.pack(">II", 2, 0))
f0 = chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 8, 6, 0, 0, 1, 10, 0, 0))
f1 = chunk(b"fcTL", struct.pack(">IIIIIHHBB", 2, 4, 3, 2, 1, 1, 10, 1, 1))
fd = chunk(b"fdAT", struct.pack(">I", 3) + idat(rows(4, 3, 4)))
png("apng", 8, 6, 8, 6, f0 + chunk(b"IDAT", idat(rows(8, 6, 4))) + f1 + fd, actl)
# metadata: ICC profiles with a v2 'desc' and a v4 'mluc' name, EXIF, EXIF hidden in a text chunk
def icc(tag):
    prof = bytearray(144) + tag
    prof[128:132] = struct.pack(">I", 1)
    prof[132:144] = b"desc" + struct.pack(">II", 144, len(tag))
    return chunk(b"iCCP", b"sRGB\0\0" + zlib.compress(bytes(prof)))

v2 = b"desc" + bytes(4) + struct.pack(">I", 5) + b"sRGB\0"
v4 = b"mluc" + bytes(16) + struct.pack(">II", 4, 28) + b"\0s\0R"
meta = (chunk(b"eXIf", b"MM\0*\0\0\0\x08\0\0") + chunk(b"tEXt", b"Raw profile type exif\0\nexif\n4\n4d4d") +
        chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1)) + chunk(b"tIME", struct.pack(">HBBBBB", 2026, 1, 2, 3, 4, 5)))
png("icc2", 4, 4, 8, 2, chunk(b"IDAT", idat(rows(4, 4, 3))), icc(v2) + meta)
png("icc4", 4, 4, 8, 2, chunk(b"IDAT", idat(rows(4, 4, 3))), icc(v4))
EOF

ASAN_OPTIONS=allocator_may_return_null=1 \
    fuzz/fuzz -max_total_time="$secs" -max_len=65536 -rss_limit_mb=4096 -artifact_prefix=fuzz/ fuzz/corpus
