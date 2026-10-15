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

python3 tools/fuzz_seeds.py fuzz/corpus

ASAN_OPTIONS=allocator_may_return_null=1 \
    fuzz/fuzz -max_total_time="$secs" -max_len=65536 -rss_limit_mb=4096 -artifact_prefix=fuzz/ fuzz/corpus
