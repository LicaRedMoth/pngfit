#!/bin/sh
# Compiles pngfit.c's web entry point natively against a stand-in emscripten.h and runs
# it twice in a row on a test image: catches breakage without an Emscripten toolchain.
set -e
cd "$(dirname "$0")/../.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for variant in threads single; do
    flag=$([ $variant = single ] && echo -DSTUB_NO_THREADS || true)
    cc -O2 -std=c11 -Wall -Wextra -D__EMSCRIPTEN__ $flag -Iweb/dev -Dmain=pngfit_native_main -c pngfit.c -o "$tmp/pngfit.o"
    cc -O2 -std=c11 -Wall -Wextra -c web/dev/harness.c -o "$tmp/harness.o"
    cc -o "$tmp/web_entry_$variant" "$tmp/pngfit.o" "$tmp/harness.o" -ldeflate -lz -lpthread -lm
done
python3 - "$tmp/in.png" <<'PY'
import sys, zlib, struct, random
random.seed(1); w, h = 96, 64
rows = b"".join(b"\0" + bytes((x * 3 + y * 2 + random.randint(0, 9)) & 255 for x in range(w * 3)) for y in range(h))
def ch(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
open(sys.argv[1], "wb").write(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(rows, 9)) + ch(b"IEND", b""))
PY
for variant in threads single; do
    echo "== $variant"
    "$tmp/web_entry_$variant" "$tmp/in.png" "$tmp/out.png" 70%
done
