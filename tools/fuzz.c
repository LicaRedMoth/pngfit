/* libFuzzer target for everything pngfit reads from a file: chunks, pixels, format reduction and
 * the metadata parsers. Build and run it with tools/fuzz.sh. */
#define main pngfit_main
#include "../pngfit.c"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t n);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t n)
{
    uint8_t *d = malloc(n ? n : 1);
    memcpy(d, data, n);
    for (size_t i = 8; i + 12 <= n;) { /* fresh checksums, so mutations reach past them */
        uint32_t len = be32(d + i);
        if (len > n - i - 12)
            break;
        put32(d + i + 8 + len, (uint32_t)crc32(0, d + i + 4, 4 + len));
        i += 12 + (size_t)len;
    }
    Arena A = {NULL};
    arena = &A;
    if (!setjmp(fail_jmp)) {
        Png g;
        png_parse(&g, d, n);
        if ((uint64_t)g.W * g.H * g.bpp <= 64 << 20) { /* a huge canvas only tests malloc */
            png_decode(&g);
            int oldC = g.C, red = reduce_format(&g);
            for (int i = 0; i < g.nch; i++) {
                const Chunk *c = &g.ch[i];
                uint8_t nb[8];
                char nm[128];
                chunk_category(c);
                if (red && (!strcmp(c->type, "sBIT") || !strcmp(c->type, "bKGD")))
                    rewrite_for_reduction(c, red, oldC, g.C, g.depth, nb);
                if (!strcmp(c->type, "iCCP"))
                    icc_name(c, nm, sizeof nm);
            }
        }
    }
    arena_free();
    arena = NULL;
    free(d);
    return 0;
}
