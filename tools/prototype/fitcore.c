/* fitcore — compress one strip of PNG filtered rows into a raw-DEFLATE fragment
 * that can be concatenated with other fragments into one valid stream.
 *
 * libdeflate always ends its output with a BFINAL block. We locate that block's
 * header with zlib's inflate(Z_BLOCK) (same trick as zlib's examples/gzjoin.c),
 * clear the BFINAL bit, then byte-align with an empty stored block (00 00 FF FF),
 * exactly what a zlib Z_SYNC_FLUSH emits. Fragments are therefore independent:
 * stream size = 2 (zlib hdr) + sum(fragments) + 2 (empty final block) + 4 (adler). */
#include <libdeflate.h>
#include <zlib.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static __thread struct libdeflate_compressor *comp[13];

/* returns fragment size, 0 on error. out must hold libdeflate bound + 8 bytes */
size_t fit_strip(const uint8_t *in, size_t n, int level, uint8_t *out, size_t cap, uint8_t *scratch)
{
    if (level < 1 || level > 12) return 0;
    if (!comp[level]) comp[level] = libdeflate_alloc_compressor(level);
    size_t m = libdeflate_deflate_compress(comp[level], in, n, out, cap - 8);
    if (!m) return 0;

    z_stream s; memset(&s, 0, sizeof s);
    if (inflateInit2(&s, -15) != Z_OK) return 0;
    s.next_in = out; s.avail_in = (uInt)m;
    s.next_out = scratch; s.avail_out = (uInt)n + 1;
    uint64_t last_hdr = 0, end_bit = 0;
    int ret;
    /* bit position of the very first block header is 0 */
    /* Z_BLOCK stops after every end-of-block code with the exact bit position in
     * data_type (bits 0-2 = unused bits of the last consumed byte, 64 = that block was
     * final, 128 = at a block boundary). Z_STREAM_END itself is useless for this:
     * by then inflate has already discarded the padding up to a byte boundary. */
    for (;;) {
        ret = inflate(&s, Z_BLOCK);
        if (ret == Z_STREAM_END) break;
        if (ret != Z_OK) { inflateEnd(&s); return 0; }
        if (s.data_type & 128) {
            uint64_t pos = (uint64_t)(s.next_in - out) * 8 - (s.data_type & 7);
            if (s.data_type & 64) end_bit = pos;   /* end of the final block */
            else last_hdr = pos;                   /* header of the next block */
        }
    }
    if (!end_bit) { inflateEnd(&s); return 0; }
    inflateEnd(&s);
    if (s.total_out != n) return 0;

    out[last_hdr >> 3] &= (uint8_t)~(1u << (last_hdr & 7));   /* clear BFINAL */
    size_t bytes = (size_t)((end_bit + 7) >> 3);
    unsigned used = end_bit & 7;                                /* bits used in last byte */
    if (used) out[bytes - 1] &= (uint8_t)((1u << used) - 1);    /* zero the padding */
    /* empty stored block: 3 header bits (all zero), align, LEN=0, NLEN=0xFFFF */
    if (used == 0 || used > 5) out[bytes++] = 0;                /* header needs its own byte */
    out[bytes++] = 0; out[bytes++] = 0; out[bytes++] = 0xFF; out[bytes++] = 0xFF;
    return bytes;
}

size_t fit_bound(size_t n)
{
    static struct libdeflate_compressor *c;
    if (!c) c = libdeflate_alloc_compressor(12);
    return libdeflate_deflate_compress_bound(c, n) + 8;
}
