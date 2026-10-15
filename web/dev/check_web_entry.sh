#!/bin/sh
# Compiles pngfit.c's web entry point natively against a stand-in emscripten.h and runs
# it twice in a row on a test image: catches breakage without an Emscripten toolchain.
set -e
cd "$(dirname "$0")/../.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for variant in threads single; do
    flag=
    if [ "$variant" = single ]; then flag=-DSTUB_NO_THREADS; fi
    cc -O2 -std=c11 -Wall -Wextra -D__EMSCRIPTEN__ ${flag:+"$flag"} -Iweb/dev -Dmain=pngfit_native_main -c pngfit.c -o "$tmp/pngfit.o"
    cc -O2 -std=c11 -Wall -Wextra -c web/dev/harness.c -o "$tmp/harness.o"
    cc -o "$tmp/web_entry_$variant" "$tmp/pngfit.o" "$tmp/harness.o" -ldeflate -lz -lpthread -lm
done
python3 tools/testimages.py "$tmp/in.png"
for variant in threads single; do
    echo "== $variant"
    "$tmp/web_entry_$variant" "$tmp/in.png" "$tmp/out.png" 70%
done
