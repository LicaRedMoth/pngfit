#!/bin/sh
# Builds a single-threaded Node.js variant of the web build and checks that WebAssembly
# pngfit writes byte-for-byte the same files as the native binary (same libdeflate).
#   web/dev/node_check.sh image.png [image.png ...]
set -e
cd "$(dirname "$0")/../.."
LD=vendor/libdeflate
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
emcc pngfit.c $LD/lib/*.c -O3 -std=gnu11 -I$LD -sUSE_ZLIB=1 -sALLOW_MEMORY_GROWTH=1 -sMAXIMUM_MEMORY=4GB \
    -sSTACK_SIZE=2MB -sMODULARIZE=1 -sEXPORT_NAME=createPngfit -sENVIRONMENT=node -sINVOKE_RUN=0 \
    -sEXIT_RUNTIME=0 -sEXPORTED_FUNCTIONS=_pngfit_start,_malloc,_free \
    -sEXPORTED_RUNTIME_METHODS=FS,stringToNewUTF8,setValue -o "$tmp/pngfit-node.cjs"
# the native side gets the same libdeflate as the wasm side, whatever the system has
cc -O2 -std=gnu11 -I$LD -o "$tmp/native" pngfit.c $LD/lib/*.c $LD/lib/arm/cpu_features.c \
    $LD/lib/x86/cpu_features.c -lz -lpthread -lm
fail=0
for img in "$@"; do
    for target in 90% 70%; do
        "$tmp/native" -q -j 1 "$img" "$tmp/native.png" -s "$target" || true
        node web/dev/node_run.cjs "$tmp/pngfit-node.cjs" "$img" "$tmp/wasm.png" -s "$target" -j 1 > "$tmp/json"
        if [ -f "$tmp/native.png" ] && cmp -s "$tmp/native.png" "$tmp/wasm.png"; then
            echo "same bytes  $target  $(basename "$img")  $(stat -c %s "$tmp/wasm.png") B"
        else
            echo "DIFFERENT   $target  $(basename "$img")"; cat "$tmp/json"; fail=1
        fi
        rm -f "$tmp/native.png" "$tmp/wasm.png"
    done
done
exit $fail
