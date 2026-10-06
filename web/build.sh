#!/bin/sh
# Builds the web version of pngfit into web/:
#   pngfit.js + pngfit.wasm        threaded (needs a cross-origin isolated page: COOP/COEP)
#   pngfit-st.js + pngfit-st.wasm  single-threaded fallback for pages served without them
#
# Needs Emscripten (emcc) and libdeflate's sources, linked as a git submodule rather than
# copied, so an upstream update is one command away:
#   git submodule add https://github.com/ebiggers/libdeflate vendor/libdeflate   # once
#   git submodule update --init                                                  # after a clone
#   git submodule update --remote vendor/libdeflate                              # to update
# zlib comes from Emscripten's own port (-sUSE_ZLIB=1).
set -e
cd "$(dirname "$0")/.."
LD=vendor/libdeflate
[ -f "$LD/libdeflate.h" ] || { echo "missing $LD: run git submodule update --init" >&2; exit 1; }
command -v emcc >/dev/null || { echo "emcc not found: install Emscripten (pacman -S emscripten)" >&2; exit 1; }

SRC="pngfit.c $(ls $LD/lib/*.c)"
COMMON="-O3 -flto -std=gnu11 -I$LD -sUSE_ZLIB=1
  -sALLOW_MEMORY_GROWTH=1 -sINITIAL_MEMORY=64MB -sMAXIMUM_MEMORY=4GB -sSTACK_SIZE=2MB
  -sMODULARIZE=1 -sEXPORT_NAME=createPngfit -sENVIRONMENT=worker
  -sINVOKE_RUN=0 -sEXIT_RUNTIME=0
  -sEXPORTED_FUNCTIONS=_pngfit_start,_malloc,_free
  -sEXPORTED_RUNTIME_METHODS=FS,stringToNewUTF8,setValue"

# shellcheck disable=SC2086
emcc $SRC $COMMON -pthread -sPTHREAD_POOL_SIZE=navigator.hardwareConcurrency+1 \
    -sPTHREAD_POOL_SIZE_STRICT=0 -sDEFAULT_PTHREAD_STACK_SIZE=1MB -o web/pngfit.js
# shellcheck disable=SC2086
emcc $SRC $COMMON -o web/pngfit-st.js
ls -l web/pngfit*.js web/pngfit*.wasm
