/* pngfit — make a truecolor PNG exactly N bytes with the least visible loss.
 *
 * PNG predicts every byte from its neighbours (the filter) and DEFLATEs the
 * residual. pngfit rounds residuals to multiples of a step q. Prediction always
 * uses the already-rounded neighbours, which is what a decoder sees, so the error
 * never drifts: at most floor(q/2) per sample.
 *
 * Exactness comes from structure, not from modelling DEFLATE:
 *   - the image is cut into strips; each strip is compressed by libdeflate on its
 *     own and the fragments are stitched into one zlib stream (final-block bit
 *     cleared via zlib's inflate(Z_BLOCK), byte-aligned with an empty stored
 *     block), so the file size is a plain sum of strip sizes;
 *   - the last row of every strip stays lossless, so the next strip predicts from
 *     original pixels and strips never depend on each other's settings;
 *   - inside a strip pixels are ranked by local texture (visual masking); one
 *     integer P per strip says how far that ranking climbs a ladder of steps.
 *     P is the knob for rate-distortion allocation across strips and, at the
 *     end, the knob that lands the file on the exact byte.
 *
 * 16-bit images: PNG predicts bytes, not samples, so the ladder first rounds the
 * low bytes (error below half an 8-bit level) and only then the high bytes.
 *
 * Build:   cc -O3 -flto -o pngfit pngfit.c -ldeflate -lz -lpthread -lm
 * License: MIT, see LICENSE.
 */
#define _GNU_SOURCE
#ifdef _WIN32 /* MinGW-w64 / MSYS2 */
#define __USE_MINGW_ANSI_STDIO 1 /* C99 printf: %zu, %lld */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <io.h>
#include <shellapi.h>
#endif
#include <ctype.h>
#include <errno.h>
#include <getopt.h>
#include <libdeflate.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <zlib.h>

#define VERSION "1.0.0"

/* ------------------------------------------------------------------ basics */

static double now(void)
{
#ifdef _WIN32
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / f.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
#endif
}

/* ------------------------------------------------------- platform layer */
/* Paths are UTF-8 everywhere; on Windows they go through the wide-character API so
 * that names outside the ANSI code page work. */
#ifdef _WIN32
static wchar_t *wpath(const char *s)
{
    int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    wchar_t *w = malloc((n > 0 ? n : 1) * sizeof(wchar_t));
    if (!w)
        exit(2);
    if (n <= 0 || !MultiByteToWideChar(CP_UTF8, 0, s, -1, w, n))
        w[0] = 0;
    return w;
}

static FILE *xfopen(const char *path, const char *mode)
{
    wchar_t *wp = wpath(path), *wm = wpath(mode);
    FILE *f = _wfopen(wp, wm);
    free(wp);
    free(wm);
    return f;
}

static int xrename(const char *from, const char *to) /* rename() on Windows refuses to replace */
{
    wchar_t *a = wpath(from), *b = wpath(to);
    int ok = MoveFileExW(a, b, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    free(a);
    free(b);
    if (!ok)
        errno = EACCES;
    return ok ? 0 : -1;
}

static int xremove(const char *path)
{
    wchar_t *w = wpath(path);
    int r = _wremove(w);
    free(w);
    return r;
}

static int file_id(const char *path, BY_HANDLE_FILE_INFORMATION *fi)
{
    wchar_t *w = wpath(path);
    HANDLE h = CreateFileW(w, 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                           FILE_FLAG_BACKUP_SEMANTICS, NULL);
    free(w);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    int ok = GetFileInformationByHandle(h, fi);
    CloseHandle(h);
    return ok;
}

static int same_file(const char *a, const char *b) /* st_ino is always 0 on Windows */
{
    BY_HANDLE_FILE_INFORMATION x, y;
    return file_id(a, &x) && file_id(b, &y) && x.dwVolumeSerialNumber == y.dwVolumeSerialNumber &&
           x.nFileIndexHigh == y.nFileIndexHigh && x.nFileIndexLow == y.nFileIndexLow;
}

static int64_t file_size(FILE *f)
{
    if (_fseeki64(f, 0, SEEK_END))
        return -1;
    int64_t n = _ftelli64(f);
    _fseeki64(f, 0, SEEK_SET);
    return n;
}

static int stderr_tty(void)
{
    return _isatty(_fileno(stderr));
}

static int ncpu(void)
{
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    return si.dwNumberOfProcessors > 0 ? (int)si.dwNumberOfProcessors : 1;
}

static void platform_init(int *argc, char ***argv)
{
    /* UTF-8 arguments instead of the ANSI code page, UTF-8 output, and the ANSI
     * escapes the progress bar uses */
    int n;
    wchar_t **wa = CommandLineToArgvW(GetCommandLineW(), &n);
    if (wa) {
        char **a = malloc((n + 1) * sizeof(char *));
        for (int i = 0; a && i < n; i++) {
            int len = WideCharToMultiByte(CP_UTF8, 0, wa[i], -1, NULL, 0, NULL, NULL);
            a[i] = malloc(len > 0 ? len : 1);
            if (!a[i] || len <= 0 || !WideCharToMultiByte(CP_UTF8, 0, wa[i], -1, a[i], len, NULL, NULL))
                a[i] = "";
        }
        if (a) {
            a[n] = NULL;
            *argc = n;
            *argv = a;
        }
        LocalFree(wa);
    }
    SetConsoleOutputCP(CP_UTF8);
    HANDLE h = GetStdHandle(STD_ERROR_HANDLE);
    DWORD mode;
    if (h != INVALID_HANDLE_VALUE && GetConsoleMode(h, &mode))
        SetConsoleMode(h, mode | 0x0004 /* ENABLE_VIRTUAL_TERMINAL_PROCESSING */);
}
#else
#define xfopen fopen
#define xrename rename
#define xremove remove

static int same_file(const char *a, const char *b)
{
    struct stat sa, sb;
    return !stat(a, &sa) && !stat(b, &sb) && sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino;
}

static int64_t file_size(FILE *f)
{
    struct stat st;
    if (fstat(fileno(f), &st) || !S_ISREG(st.st_mode))
        return -1;
    return (int64_t)st.st_size;
}

static int stderr_tty(void)
{
    return isatty(2);
}

static int ncpu(void)
{
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? (int)n : 1;
}

static void platform_init(int *argc, char ***argv)
{
    (void)argc;
    (void)argv;
}
#endif

static void *xmalloc(size_t n)
{
    void *p = malloc(n ? n : 1);
    if (!p) {
        fputs("pngfit: out of memory\n", stderr);
        exit(2);
    }
    return p;
}

/* Per-file failure: message + longjmp back to the file loop. Only the thread that
 * owns the file may fail(); pool workers never do. Thread-local, because batch mode
 * runs several files at once. */
static _Thread_local jmp_buf fail_jmp;
static _Thread_local char fail_msg[1024];

static _Noreturn void fail(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(fail_msg, sizeof fail_msg, fmt, ap);
    va_end(ap);
    longjmp(fail_jmp, 1);
}

/* Per-file arena: everything a file needs is freed in one go, also when it fails.
 * Each thread allocates from its current arena; pool workers borrow the arena of
 * the file they work for (and only allocate under that file's cache lock). */
typedef struct Blk {
    struct Blk *next;
    max_align_t data[];
} Blk;
typedef struct {
    Blk *head;
} Arena;
static _Thread_local Arena *arena;

static void *amalloc(size_t n)
{
    Blk *b = malloc(sizeof(Blk) + (n ? n : 1));
    if (!b) {
        if (!arena)
            exit(2);
        fail("out of memory");
    }
    b->next = arena->head;
    arena->head = b;
    return b->data;
}

static void *acalloc(size_t n)
{
    void *p = amalloc(n);
    memset(p, 0, n);
    return p;
}

static void arena_free(void)
{
    while (arena->head) {
        Blk *n = arena->head->next;
        free(arena->head);
        arena->head = n;
    }
}

typedef struct {
    uint8_t *p;
    size_t n, cap;
} Buf;

static void buf_put(Buf *b, const void *d, size_t n)
{
    if (b->n + n > b->cap) {
        size_t cap = (b->n + n) * 2 + 256;
        uint8_t *p = amalloc(cap);
        if (b->n)
            memcpy(p, b->p, b->n);
        b->p = p;
        b->cap = cap;
    }
    memcpy(b->p + b->n, d, n);
    b->n += n;
}

static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24;
    p[1] = v >> 16;
    p[2] = v >> 8;
    p[3] = v;
}

static void buf_chunk(Buf *b, const char *type, const uint8_t *data, uint32_t len)
{
    uint8_t h[8];
    put32(h, len);
    memcpy(h + 4, type, 4);
    buf_put(b, h, 8);
    if (len)
        buf_put(b, data, len);
    uLong crc = crc32(0, (const Bytef *)type, 4);
    if (len)
        crc = crc32(crc, data, len);
    put32(h, (uint32_t)crc);
    buf_put(b, h, 4);
}

/* ------------------------------------------------------------- progress */

typedef struct {
    int on, quiet;
    double t0, ts;
    char label[48], note[64], prefix[96];
    int64_t total, done;
    pthread_mutex_t mu;
} Prog;

static Prog prog = {.mu = PTHREAD_MUTEX_INITIALIZER};

static void fmt_time(char *s, double t)
{
    sprintf(s, "%d:%02d", (int)t / 60, (int)t % 60);
}

static void prog_draw_locked(void)
{
    if (!prog.on)
        return;
    char line[256], el[16];
    int n = snprintf(line, sizeof line, "%s%-18s", prog.prefix, prog.label);
    if (prog.total > 0) {
        double f = (double)prog.done / prog.total;
        if (f > 1)
            f = 1;
        int fill = (int)(f * 24);
        char bar[24 * 3 + 1];
        int p = 0;
        for (int i = 0; i < 24; i++) {
            const char *g = i < fill ? "\xe2\x96\x88" : "\xe2\x96\x91";
            memcpy(bar + p, g, 3);
            p += 3;
        }
        bar[p] = 0;
        n += snprintf(line + n, sizeof line - n, " %s %lld/%lld", bar, (long long)prog.done,
                      (long long)prog.total);
        if (prog.done) {
            fmt_time(el, (now() - prog.ts) / prog.done * (prog.total - prog.done));
            n += snprintf(line + n, sizeof line - n, "  ETA %s", el);
        }
    } else if (prog.done) {
        n += snprintf(line + n, sizeof line - n, " %lld probes", (long long)prog.done);
    }
    if (prog.note[0])
        n += snprintf(line + n, sizeof line - n, "  %s", prog.note);
    fmt_time(el, now() - prog.t0);
    snprintf(line + n, sizeof line - n, "  [%s]", el);
    fprintf(stderr, "\r\x1b[K%s", line);
    fflush(stderr);
}

static void prog_start(const char *label, int64_t total, const char *note)
{
    pthread_mutex_lock(&prog.mu);
    snprintf(prog.label, sizeof prog.label, "%s", label);
    snprintf(prog.note, sizeof prog.note, "%s", note ? note : "");
    prog.total = total;
    prog.done = 0;
    prog.ts = now();
    prog_draw_locked();
    pthread_mutex_unlock(&prog.mu);
}

static void prog_tick(void)
{
    pthread_mutex_lock(&prog.mu);
    prog.done++;
    prog_draw_locked();
    pthread_mutex_unlock(&prog.mu);
}

static void prog_clear(void)
{
    if (prog.on) {
        fputs("\r\x1b[K", stderr);
        fflush(stderr);
    }
}

static void logmsg(const char *fmt, ...)
{
    if (prog.quiet)
        return;
    pthread_mutex_lock(&prog.mu);
    prog_clear();
    va_list ap;
    va_start(ap, fmt);
    fputs(prog.prefix, stderr);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    prog_draw_locked();
    pthread_mutex_unlock(&prog.mu);
}

/* ------------------------------------------------------------ PNG reading */

typedef struct {
    char type[5];
    const uint8_t *data;
    uint32_t len;
} Chunk;

/* One encoded image: the still itself, or one APNG frame (plus the default image
 * when it is not part of the animation). Each has its own zlib stream. */
typedef struct {
    int W, H, x, y;   /* stored size; APNG: offset on the canvas */
    size_t row;       /* W * bpp */
    uint8_t *px;      /* H * row, PNG byte order (16-bit samples big endian) */
    Buf z;            /* its zlib data */
    int fctl;         /* an animation frame, with an fcTL */
    int in_idat;      /* stored in IDAT (else fdAT) */
    uint8_t fcb[26];  /* fcTL body; the sequence number is rewritten on output */
    float *act, *wgt; /* local texture and error weight per pixel */
} Plane;

typedef struct {
    int W, H, C, depth, bps, bpp, ctype, interlace;
    size_t row; /* canvas: W * bpp */
    Plane *pl;
    int npl, anim;
    uint8_t actl[8];
    Chunk *ch;
    int nch;
    const uint8_t *file;
    size_t size;
} Png;

static const uint8_t SIG[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};

static uint8_t *read_file(const char *path, size_t *n)
{
    FILE *f = xfopen(path, "rb");
    if (!f)
        fail("cannot open %s: %s", path, strerror(errno));
    int64_t sz = file_size(f);
    if (sz < 0) {
        fclose(f);
        fail("%s is not a regular file", path);
    }
    uint8_t *d = amalloc((size_t)sz);
    if (fread(d, 1, (size_t)sz, f) != (size_t)sz) {
        fclose(f);
        fail("cannot read %s", path);
    }
    fclose(f);
    *n = (size_t)sz;
    return d;
}

static void png_parse(Png *g, const uint8_t *d, size_t n)
{
    memset(g, 0, sizeof *g);
    g->file = d;
    g->size = n;
    if (n < 8 || memcmp(d, SIG, 8))
        fail("not a PNG file");
    int cap = 16;
    g->ch = amalloc(cap * sizeof(Chunk));
    size_t i = 8;
    int seen_end = 0;
    while (i + 12 <= n) {
        uint32_t len = be32(d + i);
        if (len > n - i - 12)
            fail("truncated chunk");
        Chunk c;
        memcpy(c.type, d + i + 4, 4);
        c.type[4] = 0;
        c.data = d + i + 8;
        c.len = len;
        uLong crc = crc32(crc32(0, d + i + 4, 4), c.data, len);
        int bad = crc != be32(d + i + 8 + len);
        i += 12 + (size_t)len;
        if (bad) {
            if (isupper((unsigned char)c.type[0]))
            {
                char t[5];
                for (int j = 0; j < 4; j++) /* a damaged type may not even be text */
                    t[j] = isalpha((unsigned char)c.type[j]) ? c.type[j] : '?';
                t[4] = 0;
                fail("CRC error in critical chunk %s", t);
            }
            continue; /* damaged ancillary chunk: drop it, as libpng does */
        }
        if (g->nch == cap) {
            Chunk *nc = amalloc(2 * cap * sizeof(Chunk));
            memcpy(nc, g->ch, cap * sizeof(Chunk));
            g->ch = nc;
            cap *= 2;
        }
        g->ch[g->nch++] = c;
        if (!strcmp(c.type, "IEND")) {
            seen_end = 1;
            break;
        }
    }
    if (!g->nch || strcmp(g->ch[0].type, "IHDR") || g->ch[0].len != 13)
        fail("missing IHDR");
    if (!seen_end)
        fail("missing IEND (truncated file?)");
    const uint8_t *h = g->ch[0].data;
    g->W = be32(h);
    g->H = be32(h + 4);
    g->depth = h[8];
    g->ctype = h[9];
    g->interlace = h[12];
    if (!g->W || !g->H || g->W > (1 << 24) || g->H > (1 << 24))
        fail("bad image dimensions");
    if (h[10] || h[11] || g->interlace > 1)
        fail("unknown compression, filter or interlace method");
    switch (g->ctype) {
    case 0: g->C = 1; break;
    case 2: g->C = 3; break;
    case 4: g->C = 2; break;
    case 6: g->C = 4; break;
    case 3: fail("palette images are not supported: pngfit keeps truecolor (a palette is pngquant's trade-off)");
    default: fail("bad colour type %d", g->ctype);
    }
    if (g->depth != 8 && g->depth != 16)
        fail("bit depth %d is not supported (8 or 16)", g->depth);
    g->bps = g->depth / 8;
    g->bpp = g->C * g->bps;
    g->row = (size_t)g->W * g->bpp;

    /* image data: one plane for a still; for APNG one per frame, plus the default
     * image if it is not the first frame. fcTL/fdAT only count after an acTL. */
    int cap_pl = 4, nfctl = 0, seen_fdat = 0;
    g->pl = acalloc(cap_pl * sizeof(Plane));
    Plane *cur = NULL, *idat = NULL;
    for (int i = 0; i < g->nch; i++) {
        const Chunk *c = &g->ch[i];
        int is_idat = !strcmp(c->type, "IDAT");
        if (!strcmp(c->type, "acTL") && !idat) {
            if (c->len != 8)
                fail("broken APNG: bad acTL");
            g->anim = 1;
            memcpy(g->actl, c->data, 8);
            continue;
        }
        if (!is_idat && !(g->anim && (!strcmp(c->type, "fcTL") || !strcmp(c->type, "fdAT"))))
            continue;
        if (!strcmp(c->type, "fdAT")) {
            if (!cur || !cur->fctl || cur->in_idat || c->len < 4)
                fail("broken APNG: fdAT without its fcTL");
            seen_fdat = 1;
            buf_put(&cur->z, c->data + 4, c->len - 4);
            continue;
        }
        if (is_idat && idat) {
            if (cur != idat)
                fail("broken PNG: IDAT chunks are not consecutive");
            buf_put(&idat->z, c->data, c->len);
            continue;
        }
        if (g->npl == cap_pl) {
            Plane *np = acalloc(2 * cap_pl * sizeof(Plane));
            memcpy(np, g->pl, cap_pl * sizeof(Plane));
            g->pl = np;
            cap_pl *= 2;
            idat = NULL; /* pointers into the old array are stale */
            for (int j = 0; j < g->npl; j++)
                if (g->pl[j].in_idat)
                    idat = &g->pl[j];
            cur = &g->pl[g->npl - 1];
        }
        if (is_idat) {
            if (seen_fdat)
                fail("broken APNG: IDAT after fdAT");
            if (cur && cur->fctl && !cur->z.n && g->npl == 1) { /* the default image is frame 0 */
                cur->in_idat = 1;
                idat = cur;
            } else {
                cur = idat = &g->pl[g->npl++];
                cur->W = g->W;
                cur->H = g->H;
                cur->in_idat = 1;
            }
            buf_put(&idat->z, c->data, c->len);
            continue;
        }
        /* fcTL: a new frame */
        if (c->len != 26)
            fail("broken APNG: bad fcTL");
        cur = &g->pl[g->npl++];
        cur->fctl = 1;
        memcpy(cur->fcb, c->data, 26);
        cur->W = be32(c->data + 4);
        cur->H = be32(c->data + 8);
        cur->x = be32(c->data + 12);
        cur->y = be32(c->data + 16);
        if (!cur->W || !cur->H || (uint64_t)cur->x + cur->W > (uint64_t)g->W ||
            (uint64_t)cur->y + cur->H > (uint64_t)g->H)
            fail("broken APNG: frame %d lies outside the canvas", nfctl);
        nfctl++;
    }
    if (!g->npl)
        fail("no image data");
    if (g->anim && (uint32_t)nfctl != be32(g->actl))
        fail("broken APNG: acTL announces %u frames, the file has %d", be32(g->actl), nfctl);
    for (int i = 0; i < g->npl; i++) {
        if (!g->pl[i].z.n)
            fail("broken APNG: frame %d has no image data", i);
        g->pl[i].row = (size_t)g->pl[i].W * g->bpp;
    }
}

static size_t raw_bytes(const Png *g) /* filtered bytes DEFLATE has to code, all planes */
{
    size_t r = 0;
    for (int i = 0; i < g->npl; i++)
        r += (size_t)g->pl[i].H * (1 + g->pl[i].row);
    return r;
}

static void unfilter(uint8_t *cur, const uint8_t *prev, size_t n, int bpp, int ft)
{
    switch (ft) {
    case 0:
        break;
    case 1:
        for (size_t i = bpp; i < n; i++)
            cur[i] += cur[i - bpp];
        break;
    case 2:
        if (prev)
            for (size_t i = 0; i < n; i++)
                cur[i] += prev[i];
        break;
    case 3:
        for (size_t i = 0; i < n; i++) {
            int a = i >= (size_t)bpp ? cur[i - bpp] : 0, b = prev ? prev[i] : 0;
            cur[i] += (a + b) >> 1;
        }
        break;
    case 4:
        for (size_t i = 0; i < n; i++) {
            int a = i >= (size_t)bpp ? cur[i - bpp] : 0, b = prev ? prev[i] : 0;
            int c = (prev && i >= (size_t)bpp) ? prev[i - bpp] : 0;
            int p = a + b - c, pa = abs(p - a), pb = abs(p - b), pc = abs(p - c);
            cur[i] += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c);
        }
        break;
    default:
        fail("bad row filter %d", ft);
    }
}

static const int A7[7][4] = {{0, 0, 8, 8}, {4, 0, 8, 8}, {0, 4, 4, 8}, {2, 0, 4, 4},
                             {0, 2, 2, 4}, {1, 0, 2, 2}, {0, 1, 1, 2}};

static void decode_plane(const Png *g, Plane *pl)
{
    const int W = pl->W, H = pl->H, bpp = g->bpp;
    const size_t row = pl->row;
    size_t raw = 0;
    if (!g->interlace)
        raw = (size_t)H * (1 + row);
    else
        for (int p = 0; p < 7; p++) {
            size_t pw = (W - A7[p][0] + A7[p][2] - 1) / A7[p][2];
            size_t ph = (H - A7[p][1] + A7[p][3] - 1) / A7[p][3];
            if (W > A7[p][0] && H > A7[p][1])
                raw += ph * (1 + pw * bpp);
        }
    uint8_t *buf = amalloc(raw);
    z_stream s;
    memset(&s, 0, sizeof s);
    if (inflateInit(&s) != Z_OK)
        fail("zlib init failed");
    s.next_in = pl->z.p;
    s.next_out = buf;
    size_t in_left = pl->z.n, out_left = raw;
    int ret;
    do { /* chunked, so files above 4 GB of zlib data would still work */
        s.avail_in = in_left > UINT_MAX ? UINT_MAX : (uInt)in_left;
        s.avail_out = out_left > UINT_MAX ? UINT_MAX : (uInt)out_left;
        uInt ai = s.avail_in, ao = s.avail_out;
        ret = inflate(&s, Z_NO_FLUSH);
        in_left -= ai - s.avail_in;
        out_left -= ao - s.avail_out;
    } while (ret == Z_OK && out_left && in_left);
    inflateEnd(&s);
    if (out_left)
        fail("corrupt or truncated image data");
    pl->px = amalloc((size_t)H * row);
    if (!g->interlace) {
        for (int y = 0; y < H; y++) {
            uint8_t *cur = buf + (size_t)y * (1 + row);
            unfilter(cur + 1, y ? cur - row : NULL, row, bpp, cur[0]);
            memcpy(pl->px + (size_t)y * row, cur + 1, row);
        }
        return;
    }
    uint8_t *p = buf;
    for (int ps = 0; ps < 7; ps++) {
        if (W <= A7[ps][0] || H <= A7[ps][1])
            continue;
        size_t pw = (W - A7[ps][0] + A7[ps][2] - 1) / A7[ps][2];
        size_t ph = (H - A7[ps][1] + A7[ps][3] - 1) / A7[ps][3];
        size_t pr = pw * bpp;
        for (size_t y = 0; y < ph; y++) {
            uint8_t *cur = p + y * (1 + pr);
            unfilter(cur + 1, y ? cur - pr : NULL, pr, bpp, cur[0]);
            size_t oy = A7[ps][1] + y * A7[ps][3];
            for (size_t x = 0; x < pw; x++)
                memcpy(pl->px + oy * row + (A7[ps][0] + x * A7[ps][2]) * bpp, cur + 1 + x * bpp, bpp);
        }
        p += ph * (1 + pr);
    }
}

static void png_decode(Png *g)
{
    for (int i = 0; i < g->npl; i++)
        decode_plane(g, &g->pl[i]);
}

/* ----------------------------------------------- lossless format reduction */

enum { R_16TO8 = 1, R_ALPHA = 2, R_GREY = 4 };

static int sample(const Png *g, const uint8_t *px, int c)
{
    return g->bps == 1 ? px[c] : (px[2 * c] << 8) | px[2 * c + 1];
}

/* What oxipng does first: a format that holds the very same pixels in fewer bytes.
 * 16-bit samples that are all 8-bit values (v = x * 257), an alpha channel that is
 * opaque everywhere, colour that is grey everywhere. For APNG an opaque alpha is only
 * dropped when every frame covers the whole canvas: elsewhere the canvas shows
 * through, and that stays transparent whatever the frames hold. */
static int reduce_format(Png *g)
{
    const int C = g->C, hasA = C == 2 || C == 4, colour = C >= 3, top = g->bps == 1 ? 255 : 65535;
    int can8 = g->bps == 2, opaque = hasA, grey = colour;
    for (int p = 0; p < g->npl && opaque; p++)
        if (g->pl[p].fctl && (g->pl[p].x || g->pl[p].y || g->pl[p].W != g->W || g->pl[p].H != g->H))
            opaque = 0;
    for (int p = 0; p < g->npl && (can8 || opaque || grey); p++) {
        const Plane *pl = &g->pl[p];
        size_t n = (size_t)pl->W * pl->H;
        for (size_t i = 0; i < n && (can8 || opaque || grey); i++) {
            const uint8_t *px = pl->px + i * g->bpp;
            if (can8)
                for (int c = 0; c < C; c++)
                    can8 &= px[2 * c] == px[2 * c + 1];
            if (opaque)
                opaque = sample(g, px, C - 1) == top;
            if (grey)
                grey = sample(g, px, 0) == sample(g, px, 1) && sample(g, px, 1) == sample(g, px, 2);
        }
    }
    if (!can8 && !opaque && !grey)
        return 0;
    const int nC = (grey ? 1 : colour ? 3 : 1) + (hasA && !opaque), nbps = can8 ? 1 : g->bps;
    const int nbpp = nC * nbps;
    for (int p = 0; p < g->npl; p++) {
        Plane *pl = &g->pl[p];
        size_t n = (size_t)pl->W * pl->H;
        uint8_t *out = amalloc(n * nbpp);
        for (size_t i = 0; i < n; i++) {
            const uint8_t *src = pl->px + i * g->bpp;
            uint8_t *dst = out + i * nbpp;
            int chans[4], m = 0;
            if (grey)
                chans[m++] = 0;
            else if (colour) {
                chans[m++] = 0;
                chans[m++] = 1;
                chans[m++] = 2;
            } else
                chans[m++] = 0;
            if (hasA && !opaque)
                chans[m++] = C - 1;
            for (int j = 0; j < m; j++) {
                const uint8_t *sb = src + chans[j] * g->bps;
                if (nbps == 1)
                    dst[j] = sb[0]; /* 8-bit source, or the high byte that equals the low one */
                else {
                    dst[2 * j] = sb[0];
                    dst[2 * j + 1] = sb[1];
                }
            }
        }
        pl->px = out;
        pl->row = (size_t)pl->W * nbpp;
    }
    g->C = nC;
    g->bps = nbps;
    g->depth = 8 * nbps;
    g->bpp = nbpp;
    g->ctype = nC == 1 ? 0 : nC == 2 ? 4 : nC == 3 ? 2 : 6;
    g->row = (size_t)g->W * nbpp;
    return (can8 ? R_16TO8 : 0) | (hasA && opaque ? R_ALPHA : 0) | (grey ? R_GREY : 0);
}

/* sBIT and bKGD describe channels, so a reduced format needs them rewritten; returns
 * the new body length, 0 = drop the chunk (it no longer means anything valid) */
static uint32_t rewrite_for_reduction(const Chunk *c, int red, int oldC, int newC, int newdepth, uint8_t *out)
{
    if (!strcmp(c->type, "sBIT")) {
        if ((int)c->len != oldC)
            return 0;
        int hasA = oldC == 2 || oldC == 4, colour = oldC >= 3, m = 0;
        if (newC == 1 || newC == 2) /* grey: the grey entry, or the largest colour one */
            out[m++] = colour ? (uint8_t)(c->data[0] > c->data[1] ? (c->data[0] > c->data[2] ? c->data[0] : c->data[2])
                                                              : (c->data[1] > c->data[2] ? c->data[1] : c->data[2]))
                              : c->data[0];
        else
            for (int i = 0; i < 3; i++)
                out[m++] = c->data[i];
        if (hasA && (newC == 2 || newC == 4))
            out[m++] = c->data[oldC - 1];
        for (int i = 0; i < m; i++)
            if (out[i] > newdepth)
                out[i] = (uint8_t)newdepth;
        return m;
    }
    if (!strcmp(c->type, "bKGD")) {
        int colour = oldC >= 3, n = colour ? 3 : 1;
        if ((int)c->len != 2 * n)
            return 0;
        int v[3];
        for (int i = 0; i < n; i++)
            v[i] = (c->data[2 * i] << 8) | c->data[2 * i + 1];
        if ((red & R_GREY) && !(v[0] == v[1] && v[1] == v[2]))
            return 0;
        int m = (red & R_GREY) || !colour ? 1 : 3;
        for (int i = 0; i < m; i++) {
            int x = v[i];
            if (red & R_16TO8) {
                if (x % 257)
                    return 0;
                x /= 257;
            }
            out[2 * i] = (uint8_t)(x >> 8);
            out[2 * i + 1] = (uint8_t)x;
        }
        return 2 * m;
    }
    return c->len;
}

/* ------------------------------------------------------------- metadata */

enum { K_COLOR = 1, K_EXIF = 2, K_XMP = 4, K_IPTC = 8, K_TEXT = 16, K_PHYS = 32, K_TIME = 64, K_OTHER = 128 };
static const char *CAT_NAMES[] = {"color", "exif", "xmp", "iptc", "text", "phys", "time", "other"};

static int in_list(const char *t, const char *const *list)
{
    for (; *list; list++)
        if (!strcmp(t, *list))
            return 1;
    return 0;
}

/* category bit of an ancillary chunk, 0 = never copied */
static int chunk_category(const Chunk *c)
{
    static const char *color[] = {"iCCP", "sRGB", "gAMA", "cHRM", "cICP", "mDCV", "cLLI", "sBIT", 0};
    static const char *phys[] = {"pHYs", "oFFs", "sCAL", "pCAL", 0};
    static const char *text[] = {"tEXt", "zTXt", "iTXt", 0};
    static const char *never[] = {"acTL", "fcTL", "fdAT", "hIST", "sPLT", "tRNS", 0};
    const char *t = c->type;
    if (in_list(t, color))
        return K_COLOR;
    if (!strcmp(t, "eXIf"))
        return K_EXIF;
    if (in_list(t, text)) { /* ImageMagick/GIMP/exiv2 hide EXIF/XMP/IPTC in text chunks */
        char key[80];
        size_t i = 0;
        for (; i < c->len && i < sizeof key - 1 && c->data[i]; i++)
            key[i] = tolower(c->data[i]);
        key[i] = 0;
        if (!strcmp(key, "xml:com.adobe.xmp") || !strcmp(key, "raw profile type xmp"))
            return K_XMP;
        if (!strcmp(key, "raw profile type exif") || !strcmp(key, "raw profile type app1"))
            return K_EXIF;
        if (!strcmp(key, "raw profile type iptc") || !strcmp(key, "raw profile type 8bim"))
            return K_IPTC;
        return K_TEXT;
    }
    if (in_list(t, phys))
        return K_PHYS;
    if (!strcmp(t, "tIME"))
        return K_TIME;
    if (in_list(t, never) || isupper((unsigned char)t[0]))
        return 0;
    if (!strcmp(t, "bKGD") || !strcmp(t, "sTER") || islower((unsigned char)t[3]))
        return K_OTHER; /* known, or unknown but marked safe-to-copy */
    return 0;
}

static const char *cat_name(int cat)
{
    for (int i = 0; i < 8; i++)
        if (cat == 1 << i)
            return CAT_NAMES[i];
    return "-";
}

/* description of an embedded ICC profile ('desc' tag, v2 text or v4 mluc) */
static void icc_name(const Chunk *c, char *out, size_t outn)
{
    snprintf(out, outn, "unknown profile");
    const uint8_t *nul = memchr(c->data, 0, c->len);
    if (!nul || (size_t)(nul - c->data) + 2 > c->len)
        return;
    const uint8_t *zs = nul + 2;
    size_t zl = c->len - (zs - c->data);
    uLongf cap = 1 << 16;
    uint8_t *p = NULL;
    for (int tries = 0; tries < 8; tries++, cap *= 4) {
        p = amalloc(cap);
        uLongf got = cap;
        int r = uncompress(p, &got, zs, zl);
        if (r == Z_OK) {
            cap = got;
            break;
        }
        if (r != Z_BUF_ERROR)
            return;
        p = NULL;
    }
    if (!p || cap < 132)
        return;
    uint32_t nt = be32(p + 128);
    for (uint32_t i = 0; i < nt && 132 + 12 * (i + 1) <= cap; i++) {
        const uint8_t *e = p + 132 + 12 * i;
        if (memcmp(e, "desc", 4))
            continue;
        uint32_t off = be32(e + 4), sz = be32(e + 8);
        if (off > cap || sz > cap - off || sz < 12)
            return;
        const uint8_t *t = p + off;
        size_t k = 0;
        if (!memcmp(t, "desc", 4)) {
            uint32_t n = be32(t + 8);
            for (uint32_t j = 0; j < n && 12 + j < sz && t[12 + j] && k < outn - 1; j++)
                out[k++] = t[12 + j];
        } else if (!memcmp(t, "mluc", 4) && sz >= 28) {
            uint32_t len = be32(t + 20), o = be32(t + 24);
            for (uint32_t j = 0; j + 1 < len && o + j + 1 < sz && k < outn - 1; j += 2)
                out[k++] = t[o + j] ? '?' : t[o + j + 1];
        } else
            return;
        while (k && out[k - 1] == ' ')
            k--;
        out[k] = 0;
        return;
    }
}

/* ------------------------------------------------------- DEFLATE fragments */

/* Compress one strip into a raw-DEFLATE fragment that concatenates with others:
 * libdeflate always ends with a BFINAL block; inflate(Z_BLOCK) finds that block's
 * header and the exact end bit (zlib's gzjoin.c trick), we clear BFINAL and append
 * an empty stored block (00 00 FF FF), exactly what Z_SYNC_FLUSH emits. */
static size_t fit_strip(struct libdeflate_compressor *comp, const uint8_t *in, size_t n, uint8_t *out,
                        size_t cap, uint8_t *scratch)
{
    size_t m = libdeflate_deflate_compress(comp, in, n, out, cap - 8);
    if (!m)
        return 0;
    z_stream s;
    memset(&s, 0, sizeof s);
    if (inflateInit2(&s, -15) != Z_OK)
        return 0;
    s.next_in = out;
    s.avail_in = (uInt)m;
    s.next_out = scratch;
    s.avail_out = (uInt)n + 1;
    uint64_t last_hdr = 0, end_bit = 0;
    for (;;) {
        int ret = inflate(&s, Z_BLOCK);
        if (ret == Z_STREAM_END)
            break;
        if (ret != Z_OK) {
            inflateEnd(&s);
            return 0;
        }
        if (s.data_type & 128) { /* at a block boundary; Z_STREAM_END comes after byte alignment */
            uint64_t pos = (uint64_t)(s.next_in - out) * 8 - (s.data_type & 7);
            if (s.data_type & 64)
                end_bit = pos;
            else
                last_hdr = pos;
        }
    }
    inflateEnd(&s);
    if (!end_bit || s.total_out != n)
        return 0;
    out[last_hdr >> 3] &= (uint8_t) ~(1u << (last_hdr & 7));
    size_t bytes = (size_t)((end_bit + 7) >> 3);
    unsigned used = end_bit & 7;
    if (used)
        out[bytes - 1] &= (uint8_t)((1u << used) - 1);
    if (used == 0 || used > 5)
        out[bytes++] = 0;
    out[bytes++] = 0;
    out[bytes++] = 0;
    out[bytes++] = 0xFF;
    out[bytes++] = 0xFF;
    return bytes;
}

/* ---------------------------------------------------------------- fitter */

typedef struct {
    int64_t R;   /* fragment bytes */
    double wsse; /* masking-weighted squared error, 8-bit units */
    double sse;  /* squared error, native units */
    int maxe;    /* max abs error, native units */
} Res;

typedef struct Entry {
    struct Entry *next;
    int k, seed, lv;
    int64_t P;
    Res r;
} Entry;

#define NB 8192

typedef struct {
    struct libdeflate_compressor *comp[2]; /* exploration level, final level */
    uint8_t *out_f, *rec, *tmp_rec, *tmp_res, *rung, *frag, *scratch;
    size_t frag_cap;
    int32_t *srank;
    struct SK { double key; int32_t idx; } *skeys;
    int sk, sseed; /* strip/seed the srank buffer holds */
} Work;

typedef struct {
    const Png *g;
    int C, bpp, bps;
    int K, maxh;           /* strips over all planes; tallest strip */
    size_t maxrow, maxfb;  /* widest plane row; most filtered bytes in one strip */
    int64_t maxn;          /* most pixels in one strip */
    int *sp, *y0, *y1;     /* strip k: plane and rows */
    int64_t *npx, *maxP;
    int32_t **rank;        /* per plane, H x W: rank inside its strip, 0 = flattest */
    int filt, L, nocache;
    int lv[2], cur; /* libdeflate levels to explore with and to finish with; the one in use */
    int clamp_above; /* extended ladder: steps above this may clamp at 0/255 instead of staying exact */
    int free_alpha;  /* --alpha: colour under alpha 0 costs nothing and counts for nothing */
    double sw;
    uint8_t *qtab; /* L x bpp: step per rung per byte of a pixel */
    uint8_t *zeros;
    Entry *bk[NB];
    pthread_mutex_t mu;
    int jobs;
    Work mainw;
    Arena *arena;
} Fit;

static double cost_tab[256]; /* log2(1 + |signed residual|): row-filter heuristic */

static inline int floordiv(int a, int b) /* floor(a / b), b > 0 */
{
    int q = a / b;
    return (a % b && a < 0) ? q - 1 : q;
}

static inline int predict(int f, int a, int b, int c)
{
    switch (f) {
    case 0: return 0;
    case 1: return a;
    case 2: return b;
    case 3: return (a + b) >> 1;
    default: {
        int p = a + b - c, pa = abs(p - a), pb = abs(p - b), pc = abs(p - c);
        return (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c);
    }
    }
}

/* Quantize one row with filter f, predicting from `up` and the row's own
 * already-rounded bytes. Out-of-range rounding keeps the byte exact, so the error
 * bound floor(q/2) always holds. Returns the heuristic cost log2(1+|residual|). */
static double quant_row(const Fit *F, int W, const uint8_t *src, const uint8_t *up, const uint8_t *rung,
                        int lossless, int f, uint8_t *rec, uint8_t *res)
{
    const int bpp = F->bpp, ncol = (F->C - 1) * F->bps; /* with alpha, colour bytes come first */
    double cost = 0;
    for (int x = 0; x < W; x++) {
        const uint8_t *qt = F->qtab + (lossless ? 0 : rung[x]) * bpp;
        int inv = 0; /* fully transparent, and allowed to forget its colour */
        if (F->free_alpha && !lossless) {
            const uint8_t *al = src + (size_t)x * bpp + ncol;
            inv = F->bps == 1 ? !al[0] : !(al[0] | al[1]);
        }
        for (int b = 0; b < bpp; b++) {
            size_t j = (size_t)x * bpp + b;
            int a = x ? rec[j - bpp] : 0, bb = up[j], c = x ? up[j - bpp] : 0;
            int p = predict(f, a, bb, c), v = src[j], val = v, q = qt[b];
            if (inv) {
                if (b < ncol)
                    val = p; /* colour nobody sees: the bare prediction, a zero residual */
            } else if (q > 1) {
                int rq = q * floordiv(2 * (v - p) + q, 2 * q);
                if (p + rq >= 0 && p + rq <= 255)
                    val = p + rq;
                else if (F->clamp_above && q > F->clamp_above) {
                    /* huge steps: take the in-range neighbour step (or the bare prediction)
                     * rather than an exact byte, which would leave lossless islands */
                    int alt = p + rq > 255 ? rq - q : rq + q;
                    val = p + alt >= 0 && p + alt <= 255 ? p + alt : p;
                }
            }
            rec[j] = (uint8_t)val;
            uint8_t e = (uint8_t)(val - p);
            res[j] = e;
            cost += cost_tab[e];
        }
    }
    return cost;
}

static Res quant_strip(const Fit *F, int k, Work *w)
{
    const Plane *pl = &F->g->pl[F->sp[k]];
    const int W = pl->W, bpp = F->bpp, C = F->C, h = F->y1[k] - F->y0[k], y0 = F->y0[k];
    const size_t row = pl->row;
    const uint8_t *prev = y0 ? pl->px + (size_t)(y0 - 1) * row : F->zeros;
    const int anchor = k + 1 < F->K && F->sp[k + 1] == F->sp[k]; /* not the last strip of its plane */
    Res r = {0, 0, 0, 0};
    for (int y = 0; y < h; y++) {
        const uint8_t *src = pl->px + (size_t)(y0 + y) * row;
        const uint8_t *up = y ? w->rec + (size_t)(y - 1) * row : prev;
        uint8_t *rec = w->rec + (size_t)y * row, *res = w->out_f + (size_t)y * (row + 1) + 1;
        const uint8_t *rg = w->rung + (size_t)y * W;
        const int lossless = anchor && y == h - 1;
        double base = quant_row(F, W, src, up, rg, lossless, F->filt, rec, res);
        int bf = F->filt;
        double best = base * (1.0 - F->sw);
        if (F->sw > 0) {
            for (int f = 0; f < 5; f++) {
                if (f == F->filt)
                    continue;
                double cst = quant_row(F, W, src, up, rg, lossless, f, w->tmp_rec, w->tmp_res);
                if (cst < best) {
                    best = cst;
                    bf = f;
                }
            }
            if (bf != F->filt)
                quant_row(F, W, src, up, rg, lossless, bf, rec, res);
        }
        w->out_f[(size_t)y * (row + 1)] = (uint8_t)bf;
        const float *wg = pl->wgt + (size_t)(y0 + y) * W;
        for (int x = 0; x < W; x++) {
            double pe8 = 0, pen = 0;
            int c0 = 0; /* invisible pixels: only their (exact) alpha counts */
            if (F->free_alpha) {
                const uint8_t *al = src + (size_t)x * bpp + (C - 1) * F->bps;
                if (F->bps == 1 ? !al[0] : !(al[0] | al[1]))
                    c0 = C - 1;
            }
            for (int c = c0; c < C; c++) {
                int d;
                if (F->bps == 1) {
                    d = rec[x * bpp + c] - src[x * bpp + c];
                    pe8 += (double)d * d;
                } else {
                    const uint8_t *a = rec + x * bpp + 2 * c, *s = src + x * bpp + 2 * c;
                    d = ((a[0] << 8) | a[1]) - ((s[0] << 8) | s[1]);
                    pe8 += (d / 257.0) * (d / 257.0);
                }
                pen += (double)d * d;
                if (abs(d) > r.maxe)
                    r.maxe = abs(d);
            }
            r.sse += pen;
            r.wsse += pe8 * wg[x];
        }
    }
    return r;
}

static void work_init(Work *w, const Fit *F)
{
    memset(w, 0, sizeof *w);
    size_t fb = F->maxfb;
    w->comp[1] = libdeflate_alloc_compressor(F->lv[1]);
    w->comp[0] = F->lv[0] == F->lv[1] ? w->comp[1] : libdeflate_alloc_compressor(F->lv[0]);
    if (!w->comp[0] || !w->comp[1]) {
        fputs("pngfit: cannot allocate compressor\n", stderr);
        exit(2);
    }
    w->out_f = xmalloc(fb);
    w->rec = xmalloc(fb);
    w->tmp_rec = xmalloc(F->maxrow);
    w->tmp_res = xmalloc(F->maxrow);
    w->rung = xmalloc(F->maxn);
    w->frag_cap = libdeflate_deflate_compress_bound(w->comp[1], fb) + 16;
    w->frag = xmalloc(w->frag_cap);
    w->scratch = xmalloc(fb + 1);
    w->sk = -1;
}

static void work_free(Work *w)
{
    if (w->comp[0] && w->comp[0] != w->comp[1])
        libdeflate_free_compressor(w->comp[0]);
    if (w->comp[1])
        libdeflate_free_compressor(w->comp[1]);
    free(w->out_f);
    free(w->rec);
    free(w->tmp_rec);
    free(w->tmp_res);
    free(w->rung);
    free(w->frag);
    free(w->scratch);
    free(w->srank);
    free(w->skeys);
    memset(w, 0, sizeof *w);
}

static uint64_t splitmix(uint64_t *s)
{
    uint64_t z = (*s += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
}

static int cmp_sk(const void *a, const void *b)
{
    const struct SK *x = a, *y = b;
    if (x->key != y->key)
        return x->key < y->key ? -1 : 1;
    return (x->idx > y->idx) - (x->idx < y->idx);
}

/* Seed > 0: shuffle each pixel among its neighbours in the texture order
 * (±0.5 % of the strip). Quality is practically the same, byte sizes are new. */
static const int32_t *seed_rank(const Fit *F, int k, int seed, Work *w)
{
    int64_t n = F->npx[k];
    if (w->sk == k && w->sseed == seed)
        return w->srank;
    if (!w->srank) {
        w->srank = xmalloc(F->maxn * sizeof(int32_t));
        w->skeys = xmalloc(F->maxn * sizeof(struct SK));
    }
    const int32_t *rk = F->rank[F->sp[k]] + (size_t)F->y0[k] * F->g->pl[F->sp[k]].W;
    double width = n / 200 > 64 ? n / 200 : 64;
    uint64_t st = (uint64_t)seed * 1000003u + k;
    for (int64_t i = 0; i < n; i++) {
        w->skeys[i].key = rk[i] + (splitmix(&st) >> 11) * (1.0 / 9007199254740992.0) * width;
        w->skeys[i].idx = (int32_t)i;
    }
    qsort(w->skeys, n, sizeof *w->skeys, cmp_sk);
    for (int64_t i = 0; i < n; i++)
        w->srank[w->skeys[i].idx] = (int32_t)i;
    w->sk = k;
    w->sseed = seed;
    return w->srank;
}

typedef struct {
    uint8_t *frag;
    size_t len;
    uLong adler;
    size_t raw;
} Keep;

static Res compute(Fit *F, int k, int64_t P, int seed, Work *w, Keep *keep, uint8_t *rec_dst)
{
    int64_t n = F->npx[k];
    const Plane *pl = &F->g->pl[F->sp[k]];
    const int32_t *rk = seed ? seed_rank(F, k, seed, w) : F->rank[F->sp[k]] + (size_t)F->y0[k] * pl->W;
    const int64_t top = F->L - 1;
    for (int64_t i = 0; i < n; i++) {
        int64_t r = (P + rk[i]) / n;
        w->rung[i] = (uint8_t)(r > top ? top : r);
    }
    Res r = quant_strip(F, k, w);
    size_t fl = (size_t)(F->y1[k] - F->y0[k]) * (pl->row + 1);
    size_t m = fit_strip(w->comp[F->cur], w->out_f, fl, w->frag, w->frag_cap, w->scratch);
    if (!m) {
        fputs("pngfit: internal error: DEFLATE fragment could not be stitched\n", stderr);
        exit(2);
    }
    r.R = (int64_t)m;
    if (keep) {
        keep->frag = xmalloc(m);
        memcpy(keep->frag, w->frag, m);
        keep->len = m;
        keep->adler = adler32(1, w->out_f, (uInt)fl);
        keep->raw = fl;
        memcpy(rec_dst, w->rec, (size_t)(F->y1[k] - F->y0[k]) * pl->row);
    }
    return r;
}

static unsigned hsh(int k, int64_t P, int seed, int lv)
{
    uint64_t h = (uint64_t)P * 0x9E3779B97F4A7C15ull ^ (uint64_t)k * 0xC2B2AE3D27D4EB4Full ^ (uint64_t)seed * 31 ^
                 (uint64_t)lv * 0x165667B19E3779F9ull;
    return (unsigned)((h ^ (h >> 29)) & (NB - 1));
}

static Entry *cache_find_lv(Fit *F, int k, int64_t P, int seed, int lv)
{
    for (Entry *e = F->bk[hsh(k, P, seed, lv)]; e; e = e->next)
        if (e->k == k && e->P == P && e->seed == seed && e->lv == lv)
            return e;
    return NULL;
}

static Entry *cache_find(Fit *F, int k, int64_t P, int seed) /* at the level in use */
{
    return cache_find_lv(F, k, P, seed, F->cur);
}

/* thread-safe evaluation with cache; *fresh = 1 if it was really computed */
static Res eval(Fit *F, int k, int64_t P, int seed, Work *w, int *fresh)
{
    pthread_mutex_lock(&F->mu);
    Entry *e = cache_find(F, k, P, seed);
    pthread_mutex_unlock(&F->mu);
    if (fresh)
        *fresh = !e;
    if (e)
        return e->r;
    Res r = compute(F, k, P, seed, w, NULL, NULL);
    if (F->nocache)
        return r;
    pthread_mutex_lock(&F->mu);
    if (!cache_find(F, k, P, seed)) {
        e = amalloc(sizeof *e); /* workers only run while the main thread waits */
        e->k = k;
        e->P = P;
        e->seed = seed;
        e->lv = F->cur;
        e->r = r;
        unsigned h = hsh(k, P, seed, F->cur);
        e->next = F->bk[h];
        F->bk[h] = e;
    }
    pthread_mutex_unlock(&F->mu);
    return r;
}

/* ---------------------------------------------------------- parallel for */

typedef void (*TaskFn)(Fit *F, void *ctx, int i, Work *w);
typedef struct {
    Fit *F;
    void *ctx;
    TaskFn fn;
    int n, next;
    pthread_mutex_t mu;
} Pool;

static void *pool_worker(void *arg)
{
    Pool *p = arg;
    arena = p->F->arena;
    Work w;
    work_init(&w, p->F);
    for (;;) {
        pthread_mutex_lock(&p->mu);
        int i = p->next++;
        pthread_mutex_unlock(&p->mu);
        if (i >= p->n)
            break;
        p->fn(p->F, p->ctx, i, &w);
    }
    work_free(&w);
    return NULL;
}

static void parallel_for(Fit *F, int n, TaskFn fn, void *ctx)
{
    if (n <= 0)
        return;
    int t = F->jobs < n ? F->jobs : n;
    if (t <= 1) {
        for (int i = 0; i < n; i++)
            fn(F, ctx, i, &F->mainw);
        return;
    }
    Pool p = {.F = F, .ctx = ctx, .fn = fn, .n = n, .next = 0, .mu = PTHREAD_MUTEX_INITIALIZER};
    pthread_t th[64];
    if (t > 64)
        t = 64;
    for (int i = 0; i < t; i++)
        if (pthread_create(&th[i], NULL, pool_worker, &p)) {
            fputs("pngfit: cannot start threads\n", stderr);
            exit(2);
        }
    for (int i = 0; i < t; i++)
        pthread_join(th[i], NULL);
}

typedef struct {
    int k;
    int64_t P;
    int seed;
} Req;

static void task_eval(Fit *F, void *ctx, int i, Work *w)
{
    Req *r = (Req *)ctx + i;
    int fresh;
    eval(F, r->k, r->P, r->seed, w, &fresh);
    prog_tick();
}

/* evaluate the uncached requests in parallel */
static void evaluate(Fit *F, Req *rq, int n, const char *label)
{
    int m = 0;
    for (int i = 0; i < n; i++) {
        int dup = 0;
        for (int j = 0; j < m && !dup; j++)
            dup = rq[j].k == rq[i].k && rq[j].P == rq[i].P && rq[j].seed == rq[i].seed;
        if (!dup && !cache_find(F, rq[i].k, rq[i].P, rq[i].seed))
            rq[m++] = rq[i];
    }
    prog_start(label, m, NULL);
    parallel_for(F, m, task_eval, rq);
}

static Res cached(Fit *F, int k, int64_t P, int seed)
{
    Entry *e = cache_find(F, k, P, seed);
    if (!e) {
        fputs("pngfit: internal error: missing cache entry\n", stderr);
        exit(2);
    }
    return e->r;
}

/* ---------------------------------------------------------- RD allocation */

typedef struct {
    int64_t P, R;
    double D;
} Pt;

static int cmp_pt(const void *a, const void *b)
{
    const Pt *x = a, *y = b;
    return (x->P > y->P) - (x->P < y->P);
}

/* seed-0 points of strip k, sorted by P (arena) */
static int get_pts(Fit *F, int k, Pt **out)
{
    int n = 0, cap = 64;
    Pt *p = amalloc(cap * sizeof(Pt));
    for (int b = 0; b < NB; b++)
        for (Entry *e = F->bk[b]; e; e = e->next)
            if (e->k == k && e->seed == 0 && e->lv == F->cur) {
                if (n == cap) {
                    Pt *q = amalloc(2 * cap * sizeof(Pt));
                    memcpy(q, p, cap * sizeof(Pt));
                    p = q;
                    cap *= 2;
                }
                p[n++] = (Pt){e->P, e->r.R, e->r.wsse};
            }
    qsort(p, n, sizeof(Pt), cmp_pt);
    *out = p;
    return n;
}

static int64_t solve(int K, Pt **pts, const int *cnt, double lam, int *idx)
{
    int64_t tot = 0;
    for (int k = 0; k < K; k++) {
        int bi = 0;
        double bv = INFINITY;
        for (int i = 0; i < cnt[k]; i++) {
            double v = pts[k][i].D + lam * pts[k][i].R;
            if (v < bv) {
                bv = v;
                bi = i;
            }
        }
        idx[k] = bi;
        tot += pts[k][bi].R;
    }
    return tot;
}

/* best seed-0 allocation within T: Lagrange on the hull, then greedy upgrades
 * that still fit (Lagrange only reaches convex-hull points) */
static int64_t best_under(Fit *F, int64_t T, int64_t *P)
{
    int K = F->K;
    Pt **pts = amalloc(K * sizeof(Pt *));
    int *cnt = amalloc(K * sizeof(int)), *idx = amalloc(K * sizeof(int));
    for (int k = 0; k < K; k++)
        cnt[k] = get_pts(F, k, &pts[k]);
    double lo = 0, hi = 1;
    for (int i = 0; i < 400 && solve(K, pts, cnt, hi, idx) > T; i++)
        hi *= 4;
    for (int i = 0; i < 60; i++) {
        double mid = (lo + hi) / 2;
        if (solve(K, pts, cnt, mid, idx) > T)
            lo = mid;
        else
            hi = mid;
    }
    int64_t tot = solve(K, pts, cnt, hi, idx);
    for (;;) {
        double bg = -1;
        int bk = -1, bi = -1;
        for (int k = 0; k < K; k++) {
            Pt c = pts[k][idx[k]];
            for (int i = 0; i < cnt[k]; i++) {
                Pt q = pts[k][i];
                if (q.R > c.R && q.R <= c.R + T - tot && q.D < c.D) {
                    double gn = (c.D - q.D) / (q.R - c.R);
                    if (gn > bg) {
                        bg = gn;
                        bk = k;
                        bi = i;
                    }
                }
            }
        }
        if (bk < 0)
            break;
        tot += pts[bk][bi].R - pts[bk][idx[bk]].R;
        idx[bk] = bi;
    }
    for (int k = 0; k < K; k++)
        P[k] = pts[k][idx[k]].P;
    return tot;
}

/* ------------------------------------------------------------- one fit */

typedef struct {
    int64_t size;  /* bytes, or -1 for percent */
    double pct;    /* if size < 0 */
    int keep_mask;
    char keep_names[32][5], strip_names[32][5];
    int nkeep_names, nstrip_names;
    int lossy_alpha, filter, level, strip, rounds, jobs, quiet, json, parallel, no_reduce, free_alpha;
    int max_error; /* -1: none; else no sample may move further, in its own units */
    int explore_level; /* libdeflate level for the rough search; the final level decides */
    int strip_set, explore_set; /* given on the command line: no automatic choice */
    int no_thin;                /* no thin landing strips (diagnostics) */
    double row_switch, mask;
    int ladder_kind; /* 0 odd, 1 all, 2 custom */
    int ladder[256], nladder;
} Opts;

static int64_t probe_(Fit *F, int k, int64_t P, int seed, int64_t *probes);

typedef struct {
    int status; /* 0 exact, 1 skipped, 2 lossless-smaller, 3 short, -1 error */
    int64_t size, target, lossless;
    double psnr, changed, time;
    int maxe, depth, filt, strips;
    double sw;
    char msg[1024];
} Result;

enum { NOT_LANDED = 1, NEED_EXTEND = 2, NEED_MIN = 3, NEED_ONESTRIP = 4 };

static void build_ladder(const Opts *o, const Png *g, Fit *F, int extended)
{
    int st[512], n = 0;
    if (o->ladder_kind == 2) {
        for (int i = 0; i < o->nladder; i++)
            st[n++] = o->ladder[i];
        if (extended) /* a custom list still gets the way down to the floor */
            for (int q = st[n - 1] + 2 - (st[n - 1] % 2 == 0); q <= 255; q += 2)
                st[n++] = q;
    } else
        for (int q = 1; q <= 255; q += o->ladder_kind == 0 ? 2 : 1)
            st[n++] = q;
    /* 8-bit: steps up to 63 (error ±31 at the top); the extended ladder, used only
     * when a target is below that, goes on to 255. 16-bit: the low bytes take the
     * whole list, then the high bytes climb a second time, to 63 or 255. */
    int top = extended ? 255 : 63, E = o->max_error;
    /* --max-error E: a step q moves a byte by at most floor(q/2); never past E */
    int lo_top = g->bps == 1 ? (o->ladder_kind == 2 && !extended ? 255 : top) : 255;
    if (E >= 0 && 2 * E + 1 < lo_top)
        lo_top = 2 * E + 1;
    int lo_n = 0, hi_n = 1;
    while (lo_n < n && st[lo_n] <= lo_top)
        lo_n++;
    if (g->bps == 2) /* high bytes: worst error = (q_hi / 2) * 256 + (last low step) / 2 */
        while (hi_n < n && st[hi_n] <= top && (E < 0 || st[hi_n] / 2 * 256 + st[lo_n - 1] / 2 <= E))
            hi_n++;
    F->clamp_above = extended ? 63 : 0;
    F->L = g->bps == 1 ? lo_n : lo_n + hi_n - 1;
    if (F->L > 255)
        F->L = 255;
    F->qtab = amalloc((size_t)F->L * g->bpp);
    for (int r = 0; r < F->L; r++)
        for (int c = 0; c < g->C; c++) {
            int lossy = o->lossy_alpha || !(g->C == 2 || g->C == 4) || c != g->C - 1;
            if (g->bps == 1)
                F->qtab[r * g->bpp + c] = lossy ? st[r] : 1;
            else {
                int ql = r < lo_n ? st[r] : st[lo_n - 1], qh = r < lo_n ? 1 : st[r - lo_n + 1];
                F->qtab[r * g->bpp + 2 * c] = lossy ? qh : 1;
                F->qtab[r * g->bpp + 2 * c + 1] = lossy ? ql : 1;
            }
        }
}

static void describe_rung(const Fit *F, int r, char *s, size_t n)
{
    if (F->bps == 1)
        snprintf(s, n, "q=%d", F->qtab[r * F->bpp]);
    else
        snprintf(s, n, "q=%d/%d", F->qtab[r * F->bpp], F->qtab[r * F->bpp + 1]);
}

/* max error in native units for rung r (worst colour channel) */
static int rung_err(const Fit *F, int r)
{
    int m = 0;
    for (int c = 0; c < F->C; c++) {
        int e = F->bps == 1 ? F->qtab[r * F->bpp + c] / 2
                            : F->qtab[r * F->bpp + 2 * c] / 2 * 256 + F->qtab[r * F->bpp + 2 * c + 1] / 2;
        if (e > m)
            m = e;
    }
    return m;
}

static int cmp_float_idx(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

static void task_rank(Fit *F, void *ctx, int k, Work *w)
{
    (void)w;
    (void)ctx;
    const Plane *pl = &F->g->pl[F->sp[k]];
    int64_t n = F->npx[k];
    size_t off = (size_t)F->y0[k] * pl->W;
    uint64_t *key = xmalloc(n * sizeof(uint64_t));
    for (int64_t i = 0; i < n; i++) { /* act >= 0, so its IEEE bits sort like the value */
        uint32_t bits;
        memcpy(&bits, &pl->act[off + i], 4);
        key[i] = (uint64_t)bits << 32 | (uint32_t)i;
    }
    qsort(key, n, sizeof *key, cmp_float_idx);
    for (int64_t i = 0; i < n; i++)
        F->rank[F->sp[k]][off + (uint32_t)key[i]] = (int32_t)i;
    free(key);
    prog_tick();
}

typedef struct {
    int k, seed;
    int64_t need, P0;
    double g;
    int64_t hitP; /* out */
    int hit;      /* out: the seed that landed, 0 = none */
} SeedCtx;

/* Newton-like walk from the seed-0 bracket under one reshuffled tie order */
static void task_seed(Fit *F, void *ctx, int i, Work *w)
{
    SeedCtx *s = (SeedCtx *)ctx + i;
    int seed = s->seed;
    int64_t P = s->P0, tried[8];
    int nt = 0;
    for (int it = 0; it < 8; it++) {
        if (P < 0)
            P = 0;
        if (P > F->maxP[s->k])
            P = F->maxP[s->k];
        int dup = 0;
        for (int j = 0; j < nt; j++)
            dup |= tried[j] == P;
        if (dup)
            break;
        tried[nt++] = P;
        int fresh;
        Res r = eval(F, s->k, P, seed, w, &fresh);
        if (fresh)
            prog_tick();
        if (r.R == s->need) {
            s->hit = seed;
            s->hitP = P;
            return;
        }
        double step = (double)(r.R - s->need) / s->g;
        int64_t st = (int64_t)llround(fabs(step));
        P += (step > 0 ? 1 : -1) * (st > 1 ? st : 1);
    }
}

typedef struct {
    int64_t P;
    int seed;
} Choice;

typedef struct {
    Entry **e;
    int n;
} Sizes;

static int cmp_entry_R(const void *a, const void *b)
{
    const Entry *x = *(Entry *const *)a, *y = *(Entry *const *)b;
    if (x->r.R != y->r.R)
        return (x->r.R > y->r.R) - (x->r.R < y->r.R);
    return (x->r.wsse > y->r.wsse) - (x->r.wsse < y->r.wsse);
}

static Sizes sizes_of(Fit *F, int k)
{
    Sizes s = {0, 0};
    int cap = 64;
    s.e = amalloc(cap * sizeof(Entry *));
    for (int b = 0; b < NB; b++)
        for (Entry *e = F->bk[b]; e; e = e->next)
            if (e->k == k && e->lv == F->cur) {
                if (s.n == cap) {
                    Entry **q = amalloc(2 * cap * sizeof(Entry *));
                    memcpy(q, s.e, cap * sizeof(Entry *));
                    s.e = q;
                    cap *= 2;
                }
                s.e[s.n++] = e;
            }
    qsort(s.e, s.n, sizeof(Entry *), cmp_entry_R);
    return s;
}

static Entry *sizes_find(const Sizes *s, int64_t R) /* lowest-distortion entry of size R */
{
    int lo = 0, hi = s->n;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (s->e[mid]->r.R < R)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo < s->n && s->e[lo]->r.R == R ? s->e[lo] : NULL;
}

typedef struct {
    Fit *F;
    const Choice *ch;
    Keep *keep;
    uint8_t **rec; /* per plane */
} AsmCtx;

static void task_assemble(Fit *F, void *ctx, int k, Work *w)
{
    AsmCtx *a = ctx;
    compute(F, k, a->ch[k].P, a->ch[k].seed, w, &a->keep[k],
            a->rec[F->sp[k]] + (size_t)F->y0[k] * F->g->pl[F->sp[k]].row);
    prog_tick();
}

typedef struct {
    Fit *F;
    int *ks;
    int64_t *R;
} ProbeCtx;

static void task_probe_filter(Fit *F, void *ctx, int i, Work *w)
{
    ProbeCtx *p = ctx;
    int k = p->ks[i];
    p->R[i] = compute(F, k, F->L > 40 ? F->maxP[k] / 2 : F->npx[k] / 2, 0, w, NULL, NULL).R;
    prog_tick();
}

/* compress the chosen configuration, write it atomically, re-decode and compare */
static void write_result(Fit *F, const Png *g, const Choice *ch, const uint8_t *head, size_t headn, int64_t size,
                         const char *dst, Result *res)
{
    int K = F->K;
    Keep *keep = acalloc(K * sizeof(Keep));
    uint8_t **rec = amalloc(g->npl * sizeof(uint8_t *));
    for (int p = 0; p < g->npl; p++)
        rec[p] = amalloc((size_t)g->pl[p].H * g->pl[p].row);
    AsmCtx ac = {F, ch, keep, rec};
    prog_start("writing", K, NULL);
    parallel_for(F, K, task_assemble, &ac);

    /* one zlib stream per plane: IDAT for the default image, fcTL + fdAT for frames;
     * fcTL and fdAT share one sequence counter */
    Buf b = {0};
    buf_put(&b, head, headn);
    uint32_t seq = 0;
    for (int p = 0, k = 0; p < g->npl; p++) {
        const Plane *pl = &g->pl[p];
        if (pl->fctl) {
            uint8_t fc[26];
            memcpy(fc, pl->fcb, 26);
            put32(fc, seq++);
            buf_chunk(&b, "fcTL", fc, 26);
        }
        size_t zlen = 4 + 2 + 2 + 4;
        for (int j = k; j < K && F->sp[j] == p; j++)
            zlen += keep[j].len;
        if (zlen > 0x7fffffff)
            fail("output too large for one PNG chunk");
        uint8_t *z = amalloc(zlen);
        size_t zp = 0;
        if (!pl->in_idat) {
            put32(z, seq++);
            zp = 4;
        }
        z[zp++] = 0x78;
        z[zp++] = 0xDA;
        uLong ad = 1;
        for (; k < K && F->sp[k] == p; k++) {
            memcpy(z + zp, keep[k].frag, keep[k].len);
            zp += keep[k].len;
            ad = adler32_combine(ad, keep[k].adler, (z_off_t)keep[k].raw);
            free(keep[k].frag);
        }
        z[zp++] = 0x03; /* empty final fixed-Huffman block */
        z[zp++] = 0x00;
        put32(z + zp, (uint32_t)ad);
        zp += 4;
        buf_chunk(&b, pl->in_idat ? "IDAT" : "fdAT", z, (uint32_t)zp);
    }
    buf_chunk(&b, "IEND", NULL, 0);
    if ((int64_t)b.n > size && res->status != 4)
        fail("internal error: output is %zu B, above the %lld B target", b.n, (long long)size);

    char tmp[4200];
    snprintf(tmp, sizeof tmp, "%s.pngfit-tmp", dst);
    FILE *f = xfopen(tmp, "wb");
    if (!f)
        fail("cannot write %s: %s", tmp, strerror(errno));
    if (fwrite(b.p, 1, b.n, f) != b.n || fclose(f)) {
        xremove(tmp);
        fail("cannot write %s", tmp);
    }
    /* verify by decoding what we wrote: every plane, every byte */
    prog_start("verifying", 0, NULL);
    Png back;
    png_parse(&back, b.p, b.n);
    png_decode(&back);
    int same = back.npl == g->npl;
    for (int p = 0; same && p < g->npl; p++)
        same = back.pl[p].W == g->pl[p].W && back.pl[p].H == g->pl[p].H &&
               !memcmp(back.pl[p].px, rec[p], (size_t)g->pl[p].H * g->pl[p].row);
    if (!same) {
        xremove(tmp);
        fail("internal error: decoded image differs from the model");
    }
    if (xrename(tmp, dst)) {
        xremove(tmp);
        fail("cannot rename %s to %s: %s", tmp, dst, strerror(errno));
    }
    double sse = 0, npx = 0;
    int maxe = 0;
    int64_t changed = 0;
    for (int p = 0; p < g->npl; p++) {
        const Plane *pl = &g->pl[p];
        npx += (double)pl->W * pl->H;
        for (int y = 0; y < pl->H; y++) {
            const uint8_t *a = rec[p] + (size_t)y * pl->row, *s = pl->px + (size_t)y * pl->row;
            for (int x = 0; x < pl->W; x++) {
                int ch_ = 0, c0 = 0;
                if (F->free_alpha) { /* colour under alpha 0 is not an error: nobody can see it */
                    const uint8_t *al = s + x * g->bpp + (g->C - 1) * g->bps;
                    if (g->bps == 1 ? !al[0] : !(al[0] | al[1]))
                        c0 = g->C - 1;
                }
                for (int c = c0; c < g->C; c++) {
                    int d = g->bps == 1 ? a[x * g->bpp + c] - s[x * g->bpp + c]
                                        : ((a[x * g->bpp + 2 * c] << 8) | a[x * g->bpp + 2 * c + 1]) -
                                              ((s[x * g->bpp + 2 * c] << 8) | s[x * g->bpp + 2 * c + 1]);
                    sse += (double)d * d;
                    if (abs(d) > maxe)
                        maxe = abs(d);
                    ch_ |= d != 0;
                }
                changed += ch_;
            }
        }
    }
    double peak = g->bps == 1 ? 255.0 : 65535.0;
    double mse = sse / (npx * g->C);
    res->size = (int64_t)b.n;
    res->psnr = mse > 0 ? 10 * log10(peak * peak / mse) : INFINITY;
    res->maxe = maxe;
    res->changed = (double)changed / npx;
    if (res->status == 0 && res->size != size)
        res->status = 3;
    if (res->status == 4 && res->size == size)
        res->status = 0;
    logmsg("wrote %s: %lld B (target %lld, %s)", dst, (long long)b.n, (long long)size,
           b.n == (size_t)size ? "EXACT" : res->status == 4 ? "the minimum" : "not exact");
    if (g->bps == 1)
        logmsg("PSNR %.2f dB, max error %d, pixels changed %.1f%%", res->psnr, maxe, res->changed * 100);
    else
        logmsg("PSNR %.2f dB (16-bit), max error %d of 65535 (%.2f of an 8-bit level), pixels changed %.1f%%",
               res->psnr, maxe, maxe / 257.0, res->changed * 100);
}

/* In strip k find P whose size lies in [lo, hi] (at the level in use), either side of
 * the points known so far: start from the tightest known points around the window,
 * then bisect (size falls as P rises, roughly). Records the last bracket. */
static int64_t window_(Fit *F, int k, int64_t lo, int64_t hi, int64_t *probes, int64_t *brk_lo, double *brk_g,
                       char *has_brk)
{
    Pt *pts;
    int c = get_pts(F, k, &pts); /* sorted by P */
    int64_t a = -1, b = -1, Ra = 0, Rb = 0;
    for (int i = 0; i < c; i++) {
        if (pts[i].R >= lo && pts[i].R <= hi)
            return pts[i].P;
        if (pts[i].R > hi && (a < 0 || pts[i].P > a)) { /* too big: the lossier the better */
            a = pts[i].P;
            Ra = pts[i].R;
        }
    }
    for (int i = c - 1; i >= 0; i--)
        if (pts[i].R < lo && pts[i].P > a && (b < 0 || pts[i].P < b)) { /* too small, above a */
            b = pts[i].P;
            Rb = pts[i].R;
        }
    if (a < 0 || b < 0)
        return -1; /* the window lies outside what this strip can do */
    while (b - a > 1) {
        int64_t mid = a + (b - a) / 2, r = probe_(F, k, mid, 0, probes);
        if (r >= lo && r <= hi)
            return mid;
        if (r > hi) {
            a = mid;
            Ra = r;
        } else {
            b = mid;
            Rb = r;
        }
    }
    brk_lo[k] = a;
    brk_g[k] = (double)(Ra - Rb) > 1e-6 ? (double)(Ra - Rb) : 1e-6;
    has_brk[k] = 1;
    return -1;
}

static int64_t single_(Fit *F, int k, int64_t need, int64_t *probes, int64_t *brk_lo, double *brk_g, char *has_brk)
{
    int64_t P = window_(F, k, need, need, probes, brk_lo, brk_g, has_brk);
    if (P >= 0 || !has_brk[k])
        return P;
    int64_t lo = brk_lo[k], cand[48];
    int nc = 0;
    for (int d = 0; d < 24; d++) {
        if (lo - d >= 0)
            cand[nc++] = lo - d;
        if (lo + 1 + d <= F->maxP[k])
            cand[nc++] = lo + 1 + d;
    }
    int step = 2 * F->jobs;
    Req *cr = amalloc(step * sizeof(Req));
    for (int b0 = 0; b0 < nc; b0 += step) {
        int m = 0;
        for (int i = b0; i < nc && i < b0 + step; i++)
            if (!cache_find(F, k, cand[i], 0))
                cr[m++] = (Req){k, cand[i], 0};
        *probes += m;
        parallel_for(F, m, task_eval, cr);
        for (int i = b0; i < nc && i < b0 + step; i++)
            if (cached(F, k, cand[i], 0).R == need)
                return cand[i];
    }
    return -1;
}

static int fit_once(const Opts *o, const Png *g, const uint8_t *head, size_t headn, int64_t fixed, int strip_h,
                    int last_try, int extended, int minimum, const char *dst, Result *res)
{
    if (minimum) /* one strip per plane: no lossless anchor rows, nothing left to land */
        strip_h = g->H;
    Fit *F = acalloc(sizeof(Fit));
    F->g = g;
    F->C = g->C;
    F->bpp = g->bpp;
    F->bps = g->bps;
    F->lv[1] = o->level;
    F->lv[0] = o->explore_level > 0 && o->explore_level < o->level ? o->explore_level : o->level;
    F->cur = 1;
    F->jobs = o->jobs;
    F->free_alpha = o->free_alpha && (g->C == 2 || g->C == 4);
    F->arena = arena;
    pthread_mutex_init(&F->mu, NULL);
    build_ladder(o, g, F, extended);
    /* strips run through every plane in order; strips of one plane are contiguous. The
     * last plane ends in two thin strips: landing probes on them cost a fraction of a
     * full strip and move the size in finer steps */
    const int LAND = 16;
    int lp = 0; /* the largest plane ends in the two thin landing strips */
    for (int p = 1; p < g->npl; p++)
        if ((int64_t)g->pl[p].W * g->pl[p].H > (int64_t)g->pl[lp].W * g->pl[lp].H)
            lp = p;
    int land = !o->no_thin && strip_h > 2 * LAND && g->pl[lp].H >= strip_h + 2 * LAND, ws = 0;
    /* at the bottom: the last strip of a plane needs no anchor row, so the two thin
     * strips add just one; placed in busy rows instead, their lossless anchors cost
     * 0.2 dB on a 3000x3000 photo. If the bottom is flat and cannot take the slack, a
     * full strip takes the bulk first (the coarse step of the landing) */
    if (land)
        ws = g->pl[lp].H - 2 * LAND;
    int K = 0;
    for (int p = 0; p < g->npl; p++)
        K += land && p == lp ? (ws + strip_h - 1) / strip_h + 2 + (g->pl[p].H - ws - 2 * LAND + strip_h - 1) / strip_h
                             : (g->pl[p].H + strip_h - 1) / strip_h;
    F->K = K;
    F->sp = amalloc(K * sizeof(int));
    F->y0 = amalloc(K * sizeof(int));
    F->y1 = amalloc(K * sizeof(int));
    F->npx = amalloc(K * sizeof(int64_t));
    F->maxP = amalloc(K * sizeof(int64_t));
    F->rank = amalloc(g->npl * sizeof(int32_t *));
    for (int p = 0, k = 0; p < g->npl; p++) {
        const Plane *pl = &g->pl[p];
        F->rank[p] = amalloc((size_t)pl->W * pl->H * sizeof(int32_t));
        if (pl->row > F->maxrow)
            F->maxrow = pl->row;
        for (int y = 0; y < pl->H; k++) {
            int hh;
            if (land && p == lp && y >= ws && y < ws + 2 * LAND)
                hh = LAND;
            else {
                int lim = land && p == lp && y < ws ? ws : pl->H;
                hh = y + strip_h < lim ? strip_h : lim - y;
            }
            F->sp[k] = p;
            F->y0[k] = y;
            F->y1[k] = y + hh;
            y += hh;
            int h = F->y1[k] - F->y0[k];
            F->npx[k] = (int64_t)h * pl->W;
            F->maxP[k] = F->npx[k] * (F->L - 1);
            if (h > F->maxh)
                F->maxh = h;
            if ((size_t)h * (pl->row + 1) > F->maxfb)
                F->maxfb = (size_t)h * (pl->row + 1);
            if (F->npx[k] > F->maxn)
                F->maxn = F->npx[k];
        }
    }
    F->zeros = acalloc(F->maxrow);
    work_init(&F->mainw, F);
    prog_start("ranking", K, NULL);
    parallel_for(F, K, task_rank, NULL);

    int rv = 0;
    if (minimum) {
        /* the smallest file: every pixel at the top step; which filter gets there
         * smallest varies a lot (Sub can be 15x bigger than Average), so try all */
        int64_t best = -1;
        int bf = 1;
        prog_start("minimum", 5 * K, NULL);
        for (int f = 0; f < 5; f++) {
            F->filt = f;
            F->sw = 0;
            int64_t R = 0;
            for (int k = 0; k < K; k++) {
                R += compute(F, k, F->maxP[k], 0, &F->mainw, NULL, NULL).R;
                prog_tick();
            }
            if (best < 0 || R < best) {
                best = R;
                bf = f;
            }
        }
        F->filt = bf;
        int64_t size = o->size >= 0 ? o->size : llround(o->pct * (res->lossless ? res->lossless : best + fixed));
        res->target = size;
        if (best + fixed <= size) { /* one strip gets there: land on it exactly with this filter */
            res->filt = bf;
            rv = NEED_ONESTRIP;
            goto out;
        }
        char rd[32];
        describe_rung(F, F->L - 1, rd, sizeof rd);
        logmsg("WARNING: %lld B cannot be reached: the smallest pngfit can make of this image is %lld B (%s "
               "everywhere, one strip, %s filter); writing that",
               (long long)size, (long long)(best + fixed), rd, (const char *[]){"None", "Sub", "Up", "Average", "Paeth"}[bf]);
        Choice *ch = acalloc(K * sizeof(Choice));
        for (int k = 0; k < K; k++)
            ch[k] = (Choice){F->maxP[k], 0};
        res->status = 4;
        write_result(F, g, ch, head, headn, size, dst, res);
        goto out;
    }
    /* probe base filter and row-switch threshold on a few strips at a typical step */
    int filt = o->filter;
    double sw = o->row_switch;
    if (filt < 0 || sw < 0) {
        int ks[4], nk = 0;
        for (int i = 0; i < 4; i++) {
            int k = (int)((double)i * (K - 1) / 3 + 1e-9);
            if (!nk || ks[nk - 1] != k)
                ks[nk++] = k;
        }
        const int fs_all[4] = {1, 2, 3, 4};
        const double sws_all[4] = {0, 0.05, 0.1, 0.2};
        int nf = filt < 0 ? 4 : 1, ns = sw < 0 ? 4 : 1;
        double sw0 = sw < 0 ? 0.1 : sw;
        int64_t Rs[4];
        ProbeCtx pc = {F, ks, Rs};
        prog_start("filter probe", (int64_t)nk * (nf + ns - 1), NULL);
        int64_t best = -1;
        int bf = filt;
        for (int i = 0; i < nf; i++) {
            F->filt = filt < 0 ? fs_all[i] : filt;
            F->sw = sw0;
            parallel_for(F, nk, task_probe_filter, &pc);
            int64_t s = 0;
            for (int j = 0; j < nk; j++)
                s += Rs[j];
            if (best < 0 || s < best) {
                best = s;
                bf = F->filt;
            }
        }
        filt = bf;
        double bsw = sw0;
        for (int i = 0; i < ns; i++) {
            double s_ = sw < 0 ? sws_all[i] : sw;
            if (s_ == sw0)
                continue;
            F->filt = filt;
            F->sw = s_;
            parallel_for(F, nk, task_probe_filter, &pc);
            int64_t s = 0;
            for (int j = 0; j < nk; j++)
                s += Rs[j];
            if (s < best) {
                best = s;
                bsw = s_;
            }
        }
        sw = bsw;
    }
    F->filt = filt;
    F->sw = sw;
    static const char *FN[] = {"None", "Sub", "Up", "Average", "Paeth"};
    logmsg("filter: %s, row switch %g", FN[filt], sw);

    Req *rq = amalloc((size_t)K * 8 * sizeof(Req));
    int n = 0;
    for (int k = 0; k < K; k++)
        rq[n++] = (Req){k, 0, 0};
    evaluate(F, rq, n, "lossless");
    int64_t total0 = 0;
    for (int k = 0; k < K; k++)
        total0 += cached(F, k, 0, 0).R;
    /* hybrid search: the rough work (ladder, rate-distortion rounds) runs at a faster
     * libdeflate level, priced in its bytes; the final level decides the result */
    const int hybrid = F->lv[0] != F->lv[1];
    int64_t total0x = total0;
    if (hybrid) {
        F->cur = 0;
        n = 0;
        for (int k = 0; k < K; k++)
            rq[n++] = (Req){k, 0, 0};
        evaluate(F, rq, n, "lossless, fast level");
        total0x = 0;
        for (int k = 0; k < K; k++)
            total0x += cached(F, k, 0, 0).R;
        F->cur = 1;
    }
    int64_t size = o->size >= 0 ? o->size : llround(o->pct * (total0 + fixed));
    int64_t T = size - fixed;
    res->target = size;
    res->lossless = total0 + fixed;
    logmsg("lossless (strips, level %d): %lld B, budget %lld B", o->level, (long long)(total0 + fixed),
           (long long)size);
    if ((int64_t)g->size <= size) {
        logmsg("source is already %lld B <= %lld B: left untouched, nothing written", (long long)g->size,
               (long long)size);
        res->status = 1;
        res->size = g->size;
        goto out;
    }
    int64_t phys = fixed + (int64_t)((raw_bytes(g) + 1031) / 1032);
    if (size < phys && !extended) { /* percent targets are only known here */
        logmsg("WARNING: %lld B is below what any %dx%d PNG can be (about %lld B: headers plus DEFLATE's "
               "1032:1 limit); writing the smallest file pngfit can make",
               (long long)size, g->W, g->H, (long long)phys);
        rv = NEED_EXTEND;
        goto out;
    }

    Choice *ch = acalloc(K * sizeof(Choice));
    int64_t *Pc = amalloc(K * sizeof(int64_t));
    if (total0 <= T) {
        logmsg("our lossless re-encode fits (the source does not): writing it, smaller than the target; "
               "reaching the exact size would only take padding");
        res->status = 2;
    } else {
        /* one pass at the top of the ladder tells right away whether the target is reachable */
        char rd[32];
        describe_rung(F, F->L - 1, rd, sizeof rd);
        char lab[48];
        snprintf(lab, sizeof lab, "floor check %s", rd);
        n = 0;
        for (int k = 0; k < K; k++)
            rq[n++] = (Req){k, F->maxP[k], 0};
        evaluate(F, rq, n, lab);
        int64_t tot_top = 0;
        for (int k = 0; k < K; k++)
            tot_top += cached(F, k, F->maxP[k], 0).R;
        if (tot_top > T && o->max_error >= 0) {
            logmsg("WARNING: %lld B is below what --max-error %d allows (%lld B); writing the smallest file "
                   "within that error",
                   (long long)size, o->max_error, (long long)(tot_top + fixed));
            rv = NEED_MIN;
            goto out;
        }
        if (tot_top > T && !extended) {
            logmsg("WARNING: %lld B is below what the standard ladder reaches (%lld B, %s everywhere, error "
                   "±%d); going on with steps up to 255, where the error is no longer bounded",
                   (long long)size, (long long)(tot_top + fixed), rd, rung_err(F, F->L - 1));
            rv = NEED_EXTEND;
            goto out;
        }
        if (tot_top > T) { /* strips and their lossless anchor rows cost too much down here */
            rv = NEED_MIN;
            goto out;
        }
        /* exploration budget, in bytes of the exploration level */
        int64_t Tx = hybrid ? (int64_t)((double)T * total0x / total0) : T;
        F->cur = hybrid ? 0 : 1;
        /* coarse grid: whole rungs (and half rungs) until everything at rung r fits */
        int r_fit;
        if (F->L <= 40) {
            for (r_fit = 1;; r_fit++) {
                n = 0;
                for (int k = 0; k < K; k++) {
                    int64_t a = (int64_t)((r_fit - 0.5) * F->npx[k]), b = r_fit * F->npx[k];
                    rq[n++] = (Req){k, a < F->maxP[k] ? a : F->maxP[k], 0};
                    rq[n++] = (Req){k, b < F->maxP[k] ? b : F->maxP[k], 0};
                }
                describe_rung(F, r_fit, rd, sizeof rd);
                snprintf(lab, sizeof lab, "ladder %s", rd);
                evaluate(F, rq, n, lab);
                int64_t tot = 0;
                for (int k = 0; k < K; k++) {
                    int64_t b = r_fit * F->npx[k];
                    tot += cached(F, k, b < F->maxP[k] ? b : F->maxP[k], 0).R;
                }
                logmsg("  rung %d (%s) everywhere: %lld B", r_fit, rd, (long long)(tot + fixed));
                if (tot <= Tx || r_fit >= F->L - 1)
                    break;
            }
        } else { /* long 16-bit ladder: bisect the rung, then lay a log-spaced grid below it */
            int lo = 0, hi = F->L - 1;
            while (hi - lo > 1) {
                int mid = (lo + hi) / 2;
                n = 0;
                for (int k = 0; k < K; k++)
                    rq[n++] = (Req){k, mid * F->npx[k], 0};
                describe_rung(F, mid, rd, sizeof rd);
                snprintf(lab, sizeof lab, "ladder %s", rd);
                evaluate(F, rq, n, lab);
                int64_t tot = 0;
                for (int k = 0; k < K; k++)
                    tot += cached(F, k, mid * F->npx[k], 0).R;
                logmsg("  rung %d (%s) everywhere: %lld B", mid, rd, (long long)(tot + fixed));
                if (tot <= Tx)
                    hi = mid;
                else
                    lo = mid;
            }
            r_fit = hi;
            n = 0;
            const double fr[] = {0.0625, 0.125, 0.25, 0.5};
            for (int k = 0; k < K; k++) {
                for (int i = 0; i < 4; i++)
                    rq[n++] = (Req){k, (int64_t)(fr[i] * r_fit * F->npx[k]), 0};
                rq[n++] = (Req){k, (int64_t)((r_fit - 0.5) * F->npx[k]), 0};
                rq[n++] = (Req){k, (int64_t)(r_fit - 1) * F->npx[k], 0};
                rq[n++] = (Req){k, (int64_t)r_fit * F->npx[k], 0};
            }
            evaluate(F, rq, n, "ladder grid");
        }

        /* RD refinement: densify each strip's curve around its chosen point */
        for (int rnd = 0; rnd < o->rounds; rnd++) {
            int64_t tot = best_under(F, Tx, Pc);
            n = 0;
            double wd = 0;
            for (int k = 0; k < K; k++) {
                Pt *pts;
                int c = get_pts(F, k, &pts), i = 0;
                while (i < c && pts[i].P != Pc[k])
                    i++;
                wd += pts[i].D;
                if (i > 0 && pts[i].P - pts[i - 1].P > 1)
                    rq[n++] = (Req){k, (pts[i - 1].P + pts[i].P) / 2, 0};
                if (i + 1 < c && pts[i + 1].P - pts[i].P > 1)
                    rq[n++] = (Req){k, (pts[i].P + pts[i + 1].P) / 2, 0};
            }
            snprintf(lab, sizeof lab, "RD round %d/%d", rnd + 1, o->rounds);
            evaluate(F, rq, n, lab);
            logmsg("  RD round %d: %lld B, weighted SSE %.4g", rnd + 1, (long long)(tot + fixed), wd);
        }
        int64_t tot = best_under(F, Tx, Pc);
        if (hybrid) {
            /* price the chosen allocation at the final level; the levels differ by a few
             * per cent and not evenly across strips, so re-aim the exploration budget by
             * what was measured until the final total sits just under the target */
            int64_t best_ok = -1, *Pbest = amalloc(K * sizeof(int64_t));
            for (int it = 0; it < 8; it++) {
                F->cur = 1;
                n = 0;
                for (int k = 0; k < K; k++)
                    rq[n++] = (Req){k, Pc[k], 0};
                evaluate(F, rq, n, "final level");
                int64_t t1 = 0, tx = 0;
                for (int k = 0; k < K; k++) {
                    t1 += cached(F, k, Pc[k], 0).R;
                    tx += cache_find_lv(F, k, Pc[k], 0, 0)->r.R;
                }
                logmsg("  at level %d: %lld B (exploration budget %lld B)", F->lv[1], (long long)(t1 + fixed),
                       (long long)(Tx + fixed));
                if (t1 <= T && t1 > best_ok) {
                    best_ok = t1;
                    memcpy(Pbest, Pc, K * sizeof(int64_t));
                }
                if (t1 <= T && T - t1 <= (T / 500 > 4096 ? T / 500 : 4096))
                    break;
                Tx += (int64_t)((double)(T - t1) * tx / t1);
                F->cur = 0;
                best_under(F, Tx, Pc);
            }
            F->cur = 1;
            if (best_ok < 0) { /* never under: fall back to the most lossy point every strip has priced */
                for (int k = 0; k < K; k++)
                    Pbest[k] = F->maxP[k];
                best_ok = 0;
                for (int k = 0; k < K; k++)
                    best_ok += cached(F, k, F->maxP[k], 0).R;
            }
            memcpy(Pc, Pbest, K * sizeof(int64_t));
            tot = best_ok;
            /* brackets for the landing: the next exploration point below each choice,
             * for the strips the landing tries first */
            n = 0;
            for (int k = 0; k < K; k++) {
                int before = 0;
                for (int j = 0; j < K; j++)
                    before += F->npx[j] < F->npx[k] || (F->npx[j] == F->npx[k] && Pc[j] > Pc[k]);
                if (before >= 4)
                    continue;
                F->cur = 0;
                Pt *pts;
                int c = get_pts(F, k, &pts), i = 0;
                while (i < c && pts[i].P < Pc[k])
                    i++;
                F->cur = 1;
                if (i > 0)
                    rq[n++] = (Req){k, pts[i - 1].P, 0};
            }
            evaluate(F, rq, n, "brackets");
        }
        F->cur = 1;
        for (int k = 0; k < K; k++)
            ch[k] = (Choice){Pc[k], 0};

        /* exact landing */
        int64_t slack = T - tot;
        logmsg("slack after RD: %lld B, landing on the exact byte", (long long)slack);
        int64_t *base = amalloc(K * sizeof(int64_t));
        for (int k = 0; k < K; k++)
            base[k] = cached(F, k, ch[k].P, 0).R;
        int *order = amalloc(K * sizeof(int));
        for (int k = 0; k < K; k++)
            order[k] = k;
        /* cheapest strips first (fewest pixels per probe), then the most lossy (stable) */
#define LAND_BEFORE(a, b) (F->npx[a] < F->npx[b] || (F->npx[a] == F->npx[b] && ch[a].P > ch[b].P))
        for (int i = 1; i < K; i++)
            for (int j = i; j > 0 && LAND_BEFORE(order[j], order[j - 1]); j--) {
                int t = order[j];
                order[j] = order[j - 1];
                order[j - 1] = t;
            }
        int64_t probes = 0;
        int landed = slack == 0;
        int64_t *brk_lo = amalloc(K * sizeof(int64_t));
        double *brk_g = amalloc(K * sizeof(double));
        char *has_brk = acalloc(K);
        char note[64];
        snprintf(note, sizeof note, "slack %lld B", (long long)slack);
        prog_start("exact landing", 0, note);
#define PROBE(k, P, seed) probe_(F, k, P, seed, &probes)
        const int nth = K < 2 ? K : 2; /* the thin strips (or the two cheapest) */

        /* In strip k find P whose size lies in [lo, hi], either side of the current
         * point: start from the tightest known points around the window, then bisect
         * (size falls as P rises, roughly). Leaves the final bracket in brk_*. */
#define WINDOW(k, lo_, hi_) window_(F, k, lo_, hi_, &probes, brk_lo, brk_g, has_brk)
        /* exact hit in one strip: the window is a single byte; then the bracket's
         * neighbours, nearest first, a batch per core count at once */
#define SINGLE(k, need_) single_(F, k, need_, &probes, brk_lo, brk_g, has_brk)

        /* coarse step: if the thin strips cannot take the slack either way, one full strip
         * takes the bulk, so that what is left sits well inside their range */
        if (!landed && K > nth) {
            /* the thin strips should only make a small correction near their own choice:
             * up to 2 % of their size, never deep into big steps */
            int64_t up = 0, down = 0, thin = 0;
            for (int ii = 0; ii < nth; ii++) {
                int k = order[ii];
                Entry *e0 = cache_find(F, k, 0, 0), *et = cache_find(F, k, F->maxP[k], 0);
                up += e0 ? e0->r.R - base[k] : 0;
                down += et ? base[k] - et->r.R : 0;
                thin += base[k];
            }
            int64_t D = thin / 50 > 256 ? thin / 50 : 256;
            up = up * 4 / 5 < D ? up * 4 / 5 : D;
            down = down * 4 / 5 < D ? down * 4 / 5 : D;
            if (slack > up || slack < -down) {
                int64_t dlo = slack - up, dhi = slack + down; /* the full strip's change */
                if (dlo > dhi)
                    dlo = dhi = slack;
                for (int ii = nth; ii < K && ii < nth + 4; ii++) {
                    int k = order[ii];
                    int64_t P = WINDOW(k, base[k] + dlo, base[k] + dhi);
                    if (P < 0)
                        continue;
                    int64_t r = cached(F, k, P, 0).R;
                    slack -= r - base[k];
                    base[k] = r;
                    ch[k] = (Choice){P, 0};
                    landed = slack == 0;
                    logmsg("  coarse step in strip %d: %lld B left for the thin strips (%lld probes)", k,
                           (long long)slack, (long long)probes);
                    break;
                }
            }
        }
        /* 1. the thin strips alone */
        for (int ii = 0; ii < nth && !landed; ii++) {
            int k = order[ii];
            int64_t P = SINGLE(k, base[k] + slack);
            if (P >= 0) {
                ch[k] = (Choice){P, 0};
                landed = 1;
                logmsg("  landed in strip %d alone (%lld probes)", k, (long long)probes);
            }
        }
        /* 2. pairs: strip i takes slack - x, strip j takes x; sums of two noisy size sets
         *    leave almost no gaps. j runs over the cheap strips only */
        for (int jj = 0; jj < K && jj < nth + 4 && !landed; jj++) {
            int j = order[jj];
            n = 0;
            Req *pr = amalloc(48 * sizeof(Req));
            for (int d = 1; d <= 24; d++)
                for (int sgn = -1; sgn <= 1; sgn += 2) {
                    int64_t P = ch[j].P + sgn * d;
                    if (P >= 0 && P <= F->maxP[j] && !cache_find(F, j, P, 0))
                        pr[n++] = (Req){j, P, 0};
                }
            probes += n;
            parallel_for(F, n, task_eval, pr);
            Sizes sj = sizes_of(F, j);
            double bc = INFINITY;
            Entry *bi_e = NULL, *bj_e = NULL;
            int bi = -1;
            for (int i = 0; i < K; i++) {
                if (i == j)
                    continue;
                Sizes si = sizes_of(F, i);
                double was = cached(F, i, ch[i].P, ch[i].seed).wsse + cached(F, j, ch[j].P, ch[j].seed).wsse;
                for (int t = 0; t < sj.n; t++) {
                    if (t && sj.e[t]->r.R == sj.e[t - 1]->r.R)
                        continue;
                    Entry *ei = sizes_find(&si, base[i] + base[j] + slack - sj.e[t]->r.R);
                    /* only pairs about as good as the allocation they replace: an exact
                     * sum from a lossless strip plus one at the top step is not a landing */
                    if (ei && ei->r.wsse + sj.e[t]->r.wsse < bc && ei->r.wsse + sj.e[t]->r.wsse <= 1.25 * was + 1.0) {
                        bc = ei->r.wsse + sj.e[t]->r.wsse;
                        bi = i;
                        bi_e = ei;
                        bj_e = sj.e[t];
                    }
                }
            }
            if (bi >= 0) {
                ch[bi] = (Choice){bi_e->P, bi_e->seed};
                ch[j] = (Choice){bj_e->P, bj_e->seed};
                landed = 1;
                logmsg("  landed with strips %d+%d (%lld probes)", bi, j, (long long)probes);
            }
        }
        /* 3. reshuffled tie orders on the bracketed strips, cheapest first, walked in
         *    parallel; then single full strips; then a wide scan */
        for (int round = 0; round < 2 && !landed; round++) {
            if (round == 1)
                for (int ii = nth; ii < K && ii < nth + 3 && !landed; ii++) {
                    int k = order[ii];
                    int64_t P = SINGLE(k, base[k] + slack);
                    if (P >= 0) {
                        ch[k] = (Choice){P, 0};
                        landed = 1;
                        logmsg("  landed in strip %d alone (%lld probes)", k, (long long)probes);
                    }
                }
            for (int ii = 0, used = 0; ii < K && used < 3 && !landed; ii++) {
                int k = order[ii];
                if (!has_brk[k] || (round == 0 && ii >= nth))
                    continue;
                used++;
                for (int s0 = 1; s0 <= 32 && !landed; s0 += F->jobs) {
                    int ns = (s0 + F->jobs - 1 <= 32 ? F->jobs : 33 - s0);
                    SeedCtx *sc = amalloc(ns * sizeof(SeedCtx));
                    for (int i = 0; i < ns; i++)
                        sc[i] = (SeedCtx){k, s0 + i, base[k] + slack, brk_lo[k], brk_g[k], -1, 0};
                    parallel_for(F, ns, task_seed, sc);
                    double bw = INFINITY;
                    for (int i = 0; i < ns; i++)
                        if (sc[i].hit) {
                            double wv = cache_find(F, k, sc[i].hitP, sc[i].hit)->r.wsse;
                            if (wv < bw) {
                                bw = wv;
                                ch[k] = (Choice){sc[i].hitP, sc[i].hit};
                                landed = 1;
                            }
                        }
                    if (landed)
                        logmsg("  landed in strip %d alone (seed %d) (%lld probes)", k, ch[k].seed,
                               (long long)probes);
                }
            }
        }
        for (int ii = 0, used = 0; ii < K && used < 3 && !landed; ii++) {
            int k = order[ii];
            if (!has_brk[k])
                continue;
            used++;
            int64_t P = brk_lo[k], need = base[k] + slack;
            double wl = 3e7 / ((double)F->npx[k] * F->bpp);
            int width = wl < 24 ? 24 : wl > 2000 ? 2000 : (int)wl;
            for (int d = 24; d < width && !landed; d++) {
                int64_t cand[2] = {P - d, P + 1 + d};
                for (int t = 0; t < 2 && !landed; t++)
                    if (cand[t] >= 0 && cand[t] <= F->maxP[k] && PROBE(k, cand[t], 0) == need) {
                        ch[k] = (Choice){cand[t], 0};
                        landed = 1;
                        logmsg("  landed in strip %d alone, wide scan (%lld probes)", k, (long long)probes);
                    }
            }
        }
        if (!landed) {
            int64_t t2 = 0;
            for (int k = 0; k < K; k++)
                t2 += cache_find(F, k, ch[k].P, ch[k].seed)->r.R;
            if (!last_try && strip_h > 1) {
                logmsg("  no exact hit with %d strip(s), %lld B short", K, (long long)(T - t2));
                rv = NOT_LANDED;
                goto out;
            }
            logmsg("WARNING: could not land exactly, %lld B short", (long long)(T - t2));
            res->status = 3;
        }
    }

    write_result(F, g, ch, head, headn, size, dst, res);

out:
    res->filt = F->filt;
    res->sw = F->sw;
    res->strips = K;
    work_free(&F->mainw);
    pthread_mutex_destroy(&F->mu);
    return rv;
}

static int64_t probe_(Fit *F, int k, int64_t P, int seed, int64_t *probes)
{
    int fresh;
    Res r = eval(F, k, P, seed, &F->mainw, &fresh);
    if (fresh) {
        (*probes)++;
        prog_tick();
    }
    return r.R;
}

/* ------------------------------------------------------------- one file */

static void box5(const float *in, float *out, float *tmp, int W, int H) /* scipy uniform_filter(5), 'reflect' */
{
    for (int y = 0; y < H; y++) {
        const float *r = in + (size_t)y * W;
        float *o = tmp + (size_t)y * W;
        for (int x = 0; x < W; x++) {
            double s = 0;
            for (int d = -2; d <= 2; d++) {
                int xx = x + d;
                while (xx < 0 || xx >= W)
                    xx = xx < 0 ? -xx - 1 : 2 * W - xx - 1;
                s += r[xx];
            }
            o[x] = (float)(s / 5);
        }
    }
    for (int y = 0; y < H; y++) {
        float *o = out + (size_t)y * W;
        int ys[5];
        for (int d = -2; d <= 2; d++) {
            int yy = y + d;
            while (yy < 0 || yy >= H)
                yy = yy < 0 ? -yy - 1 : 2 * H - yy - 1;
            ys[d + 2] = yy;
        }
        for (int x = 0; x < W; x++) {
            double s = 0;
            for (int d = 0; d < 5; d++)
                s += tmp[(size_t)ys[d] * W + x];
            o[x] = (float)(s / 5);
        }
    }
}

static void activity(const Png *g, Plane *pl, double mask)
{
    size_t N = (size_t)pl->W * pl->H;
    float *l = amalloc(N * sizeof(float)), *l2 = amalloc(N * sizeof(float));
    for (size_t i = 0; i < N; i++) {
        const uint8_t *p = pl->px + i * g->bpp;
        float s[3];
        for (int c = 0; c < (g->C >= 3 ? 3 : 1); c++)
            s[c] = g->bps == 1 ? p[c] : ((p[2 * c] << 8) | p[2 * c + 1]) / 257.0f;
        l[i] = g->C >= 3 ? 0.299f * s[0] + 0.587f * s[1] + 0.114f * s[2] : s[0];
        l2[i] = l[i] * l[i];
    }
    /* temporaries live on the heap and go right away: they would otherwise sit in the
     * file's arena for the whole encode */
    float *m = xmalloc(N * sizeof(float)), *m2 = xmalloc(N * sizeof(float)), *tmp = xmalloc(N * sizeof(float));
    box5(l, m, tmp, pl->W, pl->H);
    box5(l2, m2, tmp, pl->W, pl->H);
    free(tmp);
    float *act = l, *wgt = l2; /* reuse */
    for (size_t i = 0; i < N; i++) {
        float v = m2[i] - m[i] * m[i];
        act[i] = sqrtf(v > 0 ? v : 0);
        /* optional perceptual weighting: errors in texture count less. Off by default:
         * measured at equal byte counts it lost up to 1.2 dB on photographs and turned
         * ±1 into ±28 on text, whose sharp edges look like "texture" to it */
        wgt[i] = mask > 0 ? (float)(1.0 / ((1.0 + act[i] / mask) * (1.0 + act[i] / mask))) : 1.0f;
    }
    free(m);
    free(m2);
    pl->act = act;
    pl->wgt = wgt;
}

static void fit_file(const Opts *o, const char *src, const char *dst, Result *res)
{
    memset(res, 0, sizeof *res);
    double t0 = now();
    prog.t0 = t0;
    Arena A = {NULL};
    arena = &A;
    if (setjmp(fail_jmp)) {
        prog_clear();
        res->status = -1;
        res->time = now() - t0;
        snprintf(res->msg, sizeof res->msg, "%s", fail_msg);
        arena_free();
        arena = NULL;
        return;
    }
    size_t n;
    uint8_t *d = read_file(src, &n);
    if (o->size >= 0 && (int64_t)n <= o->size) {
        logmsg("%s is already %zu B <= %lld B: left untouched, nothing written", src, n, (long long)o->size);
        res->status = 1;
        res->size = n;
        res->target = o->size;
        res->time = now() - t0;
        arena_free();
        arena = NULL;
        return;
    }
    if (same_file(src, dst))
        fail("refusing to overwrite the source; choose another output");
    Png g;
    png_parse(&g, d, n);
    res->depth = g.depth;
    for (int i = 0; i < g.nch; i++)
        if (!strcmp(g.ch[i].type, "tRNS"))
            fail("tRNS colour key is not supported: rounding could punch or fill transparent holes");
    png_decode(&g);
    int oldC = g.C, red = o->no_reduce ? 0 : reduce_format(&g);
    if (red) {
        static const char *CT[] = {"grey", "", "RGB", "", "grey+alpha", "", "RGBA"};
        logmsg("lossless format reduction: %s%s%s -> %d-bit %s, same pixels", red & R_ALPHA ? "alpha was opaque everywhere; " : "",
               red & R_GREY ? "colour was grey everywhere; " : "", red & R_16TO8 ? "16-bit samples were 8-bit values; " : "",
               g.depth, CT[g.ctype]);
    }

    if (o->free_alpha) {
        if (g.C == 2 || g.C == 4) {
            double inv = 0, all = 0;
            for (int p = 0; p < g.npl; p++) {
                const Plane *pl = &g.pl[p];
                size_t n = (size_t)pl->W * pl->H;
                all += n;
                for (size_t i = 0; i < n; i++)
                    inv += sample(&g, pl->px + i * g.bpp, g.C - 1) == 0;
            }
            logmsg("--alpha: colour is free under %.1f%% of the pixels (fully transparent)", 100 * inv / all);
        } else
            logmsg("--alpha: no alpha channel%s, nothing to free", red & R_ALPHA ? " left (it was opaque)" : "");
    }

    /* metadata: what to keep, and the fixed container overhead it costs */
    Buf head = {0}, kept = {0}, drop = {0};
    buf_put(&head, SIG, 8);
    uint8_t ih[13];
    put32(ih, g.W);
    put32(ih + 4, g.H);
    ih[8] = g.depth;
    ih[9] = g.ctype;
    ih[10] = ih[11] = ih[12] = 0;
    buf_chunk(&head, "IHDR", ih, 13);
    for (int i = 0; i < g.nch; i++) {
        const Chunk *c = &g.ch[i];
        if (!strcmp(c->type, "IHDR") || !strcmp(c->type, "IDAT") || !strcmp(c->type, "IEND") ||
            !strcmp(c->type, "PLTE") || (g.anim && (!strcmp(c->type, "acTL") || !strcmp(c->type, "fcTL") ||
                                                    !strcmp(c->type, "fdAT"))))
            continue; /* structure, rebuilt on output */
        int cat = chunk_category(c), keep = cat && (o->keep_mask & cat);
        for (int j = 0; j < o->nkeep_names && cat; j++)
            keep |= !strcmp(o->keep_names[j], c->type);
        for (int j = 0; j < o->nstrip_names; j++)
            keep &= !!strcmp(o->strip_names[j], c->type);
        char item[64];
        snprintf(item, sizeof item, "%s%s[%s] %u B", (keep ? kept.n : drop.n) ? ", " : "", c->type,
                 cat_name(cat), c->len + 12);
        buf_put(keep ? &kept : &drop, item, strlen(item));
        if (keep && red && (!strcmp(c->type, "sBIT") || !strcmp(c->type, "bKGD"))) {
            uint8_t nb[8];
            uint32_t nl = rewrite_for_reduction(c, red, oldC, g.C, g.depth, nb);
            if (nl)
                buf_chunk(&head, c->type, nb, nl);
            else
                logmsg("dropping %s: it cannot describe the reduced format", c->type);
        } else if (keep)
            buf_chunk(&head, c->type, c->data, c->len);
        else if (!strcmp(c->type, "iCCP")) {
            char nm[128];
            icc_name(c, nm, sizeof nm);
            char low[128];
            size_t k = 0;
            for (; nm[k] && k < sizeof low - 1; k++)
                low[k] = tolower((unsigned char)nm[k]);
            low[k] = 0;
            if (strncmp(low, "srgb", 4))
                logmsg("WARNING: dropping colour profile '%s': colours will look different", nm);
        } else if (!strcmp(c->type, "gAMA") || !strcmp(c->type, "cHRM") || !strcmp(c->type, "cICP"))
            logmsg("WARNING: dropping %s: colours may look different", c->type);
    }
    buf_put(&kept, "", 1);
    buf_put(&drop, "", 1);
    logmsg("metadata kept: %s; dropped: %s", kept.n > 1 ? (char *)kept.p : "nothing",
           drop.n > 1 ? (char *)drop.p : "nothing");
    if (g.anim) {
        buf_chunk(&head, "acTL", g.actl, 8);
        uint32_t plays = be32(g.actl + 4);
        char pl[32];
        snprintf(pl, sizeof pl, plays ? "plays %u time(s)" : "loops forever", plays);
        logmsg("animation: %u frames on a %dx%d canvas, %s", be32(g.actl), g.W, g.H, pl);
    }
    /* fixed bytes: header and kept chunks, then per plane its fcTL, chunk framing
     * (plus fdAT's sequence number) and zlib framing, then IEND */
    int64_t fixed = (int64_t)head.n + 12;
    for (int p = 0; p < g.npl; p++)
        fixed += (g.pl[p].fctl ? 12 + 26 : 0) + 12 + (g.pl[p].in_idat ? 0 : 4) + 2 + 2 + 4;
    int64_t raw = (int64_t)raw_bytes(&g);
    int64_t floor_ = fixed + (raw + 1031) / 1032;
    int extended = 0;
    if (o->size >= 0 && o->size < floor_) {
        logmsg("WARNING: %lld B is below what any %dx%d PNG can be: the container needs %lld B and DEFLATE "
               "cannot code %lld B of rows in under ~%lld B (about 1032:1). Writing the smallest file pngfit "
               "can make instead",
               (long long)o->size, g.W, g.H, (long long)fixed, (long long)raw, (long long)(raw / 1032));
        extended = 1;
    }

    prog_start("analysing", 0, NULL);
    for (int p = 0; p < g.npl; p++)
        activity(&g, &g.pl[p], o->mask);
    /* content class from the share of perfectly flat pixels: screenshots and other
     * synthetic images want thinner strips (finer allocation, +1.3 dB on a desktop
     * screenshot) and the final libdeflate level throughout (level 10 costs them ~2 dB);
     * photographs want 128-row strips and can explore at level 10 for free */
    double flat = 0, all = 0;
    for (int p = 0; p < g.npl; p++) {
        size_t n2 = (size_t)g.pl[p].W * g.pl[p].H;
        all += n2;
        for (size_t i = 0; i < n2; i++)
            flat += g.pl[p].act[i] == 0;
    }
    int synthetic = flat >= 0.3 * all;
    Opts o1 = *o;
    if (!o->strip_set)
        o1.strip = synthetic ? 64 : 128;
    if (!o->explore_set && synthetic)
        o1.explore_level = o->level;
    logmsg("content: %s (%.0f%% flat pixels): %d-row strips%s", synthetic ? "synthetic" : "photographic",
           100 * flat / all, o1.strip,
           o1.explore_level > 0 && o1.explore_level < o1.level ? ", rough search at level 10" : "");
    int strip_h = o1.strip, minimum = 0, onestrip = 0;
    for (;;) {
        int last = strip_h <= 8 || onestrip;
        int rv = fit_once(&o1, &g, head.p, head.n, fixed, strip_h, last, extended, minimum, dst, res);
        if (o1.size < 0 && res->target > 0) /* a percent target is resolved once: retries must not drift */
            o1.size = res->target;
        if (rv == NEED_EXTEND) {
            extended = 1;
            continue;
        }
        if (rv == NEED_MIN) {
            minimum = 1;
            continue;
        }
        if (rv == NEED_ONESTRIP) {
            logmsg("  a single strip without anchor rows reaches it: landing there");
            minimum = 0;
            onestrip = 1;
            strip_h = g.H;
            o1.filter = res->filt;
            o1.row_switch = 0;
            continue;
        }
        if (rv != NOT_LANDED)
            break;
        strip_h = strip_h / 2 > 8 ? strip_h / 2 : 8;
        logmsg("retrying with %d-row strips", strip_h);
    }
    prog_clear();
    res->time = now() - t0;
    arena_free();
    arena = NULL;
}

/* ------------------------------------------------------------------ CLI */

static int64_t parse_size(const char *s, double *pct)
{
    char *end;
    double v = strtod(s, &end);
    if (end == s || v < 0 || !isfinite(v))
        return -2;
    while (*end == ' ')
        end++;
    if (*end == '%' && !end[1]) {
        *pct = v / 100;
        return -1;
    }
    double mul = 1;
    if (*end && strchr("kKmMgG", *end)) {
        int e = strchr("kK", *end) ? 1 : strchr("mM", *end) ? 2 : 3;
        end++;
        double base = (*end == 'i') ? 1024 : 1000;
        if (*end == 'i')
            end++;
        mul = pow(base, e);
    }
    if (*end == 'B' || *end == 'b')
        end++;
    if (*end)
        return -2;
    return (int64_t)floor(v * mul); /* 0 = as small as possible */
}

/* --keep LIST (keep only these) and --strip LIST (drop these): categories, "meta"
 * (everything but colour), all, none, or raw chunk names */
static int parse_meta(Opts *o, const char *s, int strip)
{
    char buf[512];
    snprintf(buf, sizeof buf, "%s", s);
    for (char *t = strtok(buf, ","); t; t = strtok(NULL, ",")) {
        while (*t == ' ')
            t++;
        int m = -1;
        if (!strcmp(t, "all"))
            m = 255;
        else if (!strcmp(t, "none"))
            m = 0;
        else if (!strcmp(t, "meta"))
            m = 255 & ~K_COLOR;
        else
            for (int i = 0; i < 8; i++)
                if (!strcmp(t, CAT_NAMES[i]))
                    m = 1 << i;
        if (m >= 0) {
            if (strip)
                o->keep_mask &= ~m;
            else
                o->keep_mask |= m;
            continue;
        }
        int *cnt = strip ? &o->nstrip_names : &o->nkeep_names;
        if (strlen(t) != 4 || !isalpha((unsigned char)t[0]) || !isalpha((unsigned char)t[1]) ||
            !isalpha((unsigned char)t[2]) || !isalpha((unsigned char)t[3]) || *cnt >= 32) {
            fprintf(stderr,
                    "pngfit: unknown %s item '%s': use color, exif, xmp, iptc, text, phys, time, other, meta, "
                    "all, none or a chunk name like tEXt\n",
                    strip ? "--strip" : "--keep", t);
            return -1;
        }
        memcpy(strip ? o->strip_names[(*cnt)++] : o->keep_names[(*cnt)++], t, 5);
    }
    return 0;
}

static void usage(FILE *f)
{
    fputs("usage: pngfit [options] -s SIZE IN.png OUT.png\n"
          "       pngfit [options] -s SIZE -o DIR IN.png [IN.png ...]\n"
          "\n"
          "Make a truecolor PNG exactly SIZE bytes with the least visible loss.\n"
          "\n"
          "  -s, --size SIZE       exact output size: 10000000, 10MB (10^7), 10MiB (10*2^20),\n"
          "                        or 80% of pngfit's own lossless size. Below what the image can\n"
          "                        reach (0 included) it warns and writes the smallest it can make\n"
          "  -e, --max-error N     no sample moves by more than N (8-bit: levels of 255; 16-bit:\n"
          "                        of 65535), whatever the size; below that size: the smallest\n"
          "                        file within N. Default: no cap (best average quality)\n"
          "  -o, --outdir DIR      batch mode: write every input into DIR\n"
          "      --suffix STR      batch mode: add STR to output names (cover.png -> coverSTR.png)\n"
          "  -x, --strip LIST      metadata to drop (default: keep everything): color, exif, xmp,\n"
          "                        iptc, text, phys, time, other, meta (all but color), all, or\n"
          "                        chunk names like tEXt,pHYs. Kept chunks count in the budget\n"
          "  -k, --keep LIST       the other way round: keep only these (same names)\n"
          "  -p, --preserve        keep all metadata (the default)\n"
          "      --lossy-alpha     allow rounding in the alpha channel too\n"
          "      --alpha           colour under fully transparent pixels is free: spend no bytes\n"
          "                        on it (off by default: some uses read colour under alpha 0)\n"
          "      --no-reduce       keep the source format even when a smaller one holds the same\n"
          "                        pixels (opaque alpha, grey colour, 16-bit that is 8-bit)\n"
          "      --filter N        base PNG filter 0-4 (default: probe)\n"
          "      --row-switch F    a row leaves the base filter only if another is cheaper by\n"
          "                        fraction F (default: probe 0/0.05/0.1/0.2)\n"
          "      --ladder L        odd (default: 1,3,5..), all (1,2,3..) or a list like 1,3,5,9\n"
          "      --mask M          weight errors by local texture (visual masking), e.g. 4;\n"
          "                        smaller trusts texture more (default 0: off, plain error)\n"
          "      --level N         libdeflate level 1-12 (default 12)\n"
          "      --fast            level 10: photos come out the same (-0.02 dB) about 3x faster;\n"
          "                        screenshots lose about 2 dB, keep 12 for them\n"
          "      --explore-level N the rough search runs at this faster level, the final level\n"
          "                        decides (default 10; set it to --level to turn that off)\n"
          "      --strip-rows N    rows per independent strip (default: 128 for photographs,\n"
          "                        64 for screenshots and other flat, synthetic images)\n"
          "      --no-thin-strips  no thin landing strips at the bottom (slower exact landing)\n"
          "      --rounds N        rate-distortion refinement rounds (default 4)\n"
          "  -j, --jobs N          worker threads in total (default: all cores)\n"
          "  -P, --parallel N      batch mode: files encoded at once (default: as many as\n"
          "                        threads allow; each gets jobs/N threads; 1 = one by one)\n"
          "  -q, --quiet           no log, no progress bar\n"
          "      --json            one JSON object per file on stdout\n"
          "  -V, --version\n"
          "  -h, --help\n",
          f);
}

/* JSON wants valid UTF-8: pass well-formed sequences (Cyrillic paths and all),
 * replace any stray byte with U+FFFD */
static void json_str(FILE *f, const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    fputc('"', f);
    while (*p) {
        unsigned char c = *p;
        int n = c < 0x80 ? 0 : (c & 0xE0) == 0xC0 ? 1 : (c & 0xF0) == 0xE0 ? 2 : (c & 0xF8) == 0xF0 ? 3 : -1;
        int ok = n >= 0 && !(n == 1 && c < 0xC2);
        for (int i = 1; ok && i <= n; i++)
            ok = (p[i] & 0xC0) == 0x80;
        if (!ok) {
            fputs("\\ufffd", f);
            p++;
        } else if (n) {
            fwrite(p, 1, n + 1, f);
            p += n + 1;
        } else {
            if (c == '"' || c == '\\')
                fprintf(f, "\\%c", c);
            else if (c < 0x20)
                fprintf(f, "\\u%04x", c);
            else
                fputc(c, f);
            p++;
        }
    }
    fputc('"', f);
}

static void print_json(const char *src, const char *dst, const Result *r)
{
    static const char *st[] = {"exact", "skipped", "lossless", "short", "minimum"};
    printf("{\"src\":");
    json_str(stdout, src);
    printf(",\"dst\":");
    json_str(stdout, r->status == 1 || r->status < 0 ? "" : dst);
    printf(",\"status\":\"%s\"", r->status < 0 ? "error" : st[r->status]);
    if (r->status < 0) {
        printf(",\"error\":");
        json_str(stdout, r->msg);
    } else {
        printf(",\"size\":%lld,\"target\":%lld", (long long)r->size, (long long)r->target);
        if (r->status != 1) {
            printf(",\"lossless\":%lld,\"depth\":%d", (long long)r->lossless, r->depth);
            if (isfinite(r->psnr))
                printf(",\"psnr\":%.4f", r->psnr);
            else
                printf(",\"psnr\":null");
            printf(",\"max_error\":%d,\"changed\":%.5f,\"filter\":%d,\"row_switch\":%g,\"strips\":%d", r->maxe,
                   r->changed, r->filt, r->sw, r->strips);
        }
    }
    printf(",\"time\":%.2f}\n", r->time);
    fflush(stdout);
}

static void batch_dst(char *dst, size_t n, const char *outdir, const char *src, const char *suffix)
{
    const char *bn = strrchr(src, '/');
    bn = bn ? bn + 1 : src;
    const char *dot = strrchr(bn, '.');
    int stem = dot && dot != bn ? (int)(dot - bn) : (int)strlen(bn);
    snprintf(dst, n, "%s/%.*s%s.png", outdir, stem, bn, suffix);
}

/* one human-readable line per finished file (parallel batch mode) */
static void result_line(const char *tag, const char *src, const char *dst, const Result *r)
{
    char pe[64];
    if (r->status == 0 || r->status >= 2)
        snprintf(pe, sizeof pe, "PSNR %.2f dB%s, max error %d", r->psnr, r->depth == 16 ? " (16-bit)" : "", r->maxe);
    switch (r->status) {
    case 0:
        fprintf(stderr, "%s %s -> %s: EXACT %lld B, %s, %.0f s\n", tag, src, dst, (long long)r->size, pe, r->time);
        break;
    case 1:
        fprintf(stderr, "%s %s: already %lld B <= %lld B, left untouched\n", tag, src, (long long)r->size,
                (long long)r->target);
        break;
    case 2:
        fprintf(stderr, "%s %s -> %s: lossless re-encode %lld B fits (target %lld B)\n", tag, src, dst,
                (long long)r->size, (long long)r->target);
        break;
    case 3:
        fprintf(stderr, "%s %s -> %s: %lld B, %lld B short of the target, %s\n", tag, src, dst, (long long)r->size,
                (long long)(r->target - r->size), pe);
        break;
    case 4:
        fprintf(stderr, "%s %s -> %s: %lld B is the smallest possible (target %lld B unreachable), %s\n", tag, src,
                dst, (long long)r->size, (long long)r->target, pe);
        break;
    default:
        fprintf(stderr, "%s %s: FAILED: %s\n", tag, src, r->msg);
    }
}

typedef struct {
    Opts fo; /* per-file options: jobs = threads for one file */
    char **in;
    int n, next, done, cnt[5], fail;
    const char *outdir, *suffix;
    int quiet, json, bar;
    double t0;
    pthread_mutex_t mu;
} Batch;

static void batch_bar(Batch *b)
{
    if (!b->bar)
        return;
    char el[16];
    int fill = b->n ? b->done * 24 / b->n : 24;
    fputs("\r\x1b[Kfiles ", stderr);
    for (int i = 0; i < 24; i++)
        fputs(i < fill ? "\xe2\x96\x88" : "\xe2\x96\x91", stderr);
    fmt_time(el, now() - b->t0);
    fprintf(stderr, " %d/%d  [%s]", b->done, b->n, el);
    fflush(stderr);
}

static void *batch_worker(void *arg)
{
    Batch *b = arg;
    for (;;) {
        pthread_mutex_lock(&b->mu);
        int i = b->next++;
        pthread_mutex_unlock(&b->mu);
        if (i >= b->n)
            break;
        char dst[4096], tag[32];
        batch_dst(dst, sizeof dst, b->outdir, b->in[i], b->suffix);
        Result r;
        fit_file(&b->fo, b->in[i], dst, &r);
        pthread_mutex_lock(&b->mu);
        b->done++;
        if (r.status < 0)
            b->fail++;
        else
            b->cnt[r.status]++;
        if (b->json)
            print_json(b->in[i], dst, &r);
        if (!b->quiet || r.status < 0) {
            if (b->bar)
                fputs("\r\x1b[K", stderr);
            snprintf(tag, sizeof tag, "[%d/%d]", b->done, b->n);
            result_line(tag, b->in[i], dst, &r);
        }
        batch_bar(b);
        pthread_mutex_unlock(&b->mu);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    platform_init(&argc, &argv);
    for (int i = 0; i < 256; i++)
        cost_tab[i] = log2(1 + abs(i < 128 ? i : 256 - i));
    Opts o;
    memset(&o, 0, sizeof o);
    o.size = -2;
    o.keep_mask = 255; /* keep all metadata unless told otherwise */
    o.filter = -1;
    o.row_switch = -1;
    o.mask = 0;
    o.level = 12;
    o.strip = 128;
    o.rounds = 4;
    o.jobs = ncpu();
    const char *outdir = NULL, *suffix = "";
    enum { O_SUFFIX = 256, O_LALPHA, O_FILTER, O_RSW, O_LADDER, O_MASK, O_LEVEL, O_STRIP, O_ROUNDS, O_JSON,
           O_NOREDUCE, O_ALPHA, O_FAST, O_EXPLORE, O_NOTHIN };
    o.max_error = -1;
    o.explore_level = 10;
    static const struct option lo[] = {{"size", 1, 0, 's'},          {"outdir", 1, 0, 'o'},
                                       {"suffix", 1, 0, O_SUFFIX},   {"keep", 1, 0, 'k'},          {"strip", 1, 0, 'x'},
                                       {"preserve", 0, 0, 'p'},      {"lossy-alpha", 0, 0, O_LALPHA},
                                       {"filter", 1, 0, O_FILTER},   {"row-switch", 1, 0, O_RSW},
                                       {"ladder", 1, 0, O_LADDER},   {"mask", 1, 0, O_MASK},
                                       {"level", 1, 0, O_LEVEL},     {"strip-rows", 1, 0, O_STRIP},
                                       {"rounds", 1, 0, O_ROUNDS},   {"jobs", 1, 0, 'j'},
                                       {"parallel", 1, 0, 'P'},
                                       {"quiet", 0, 0, 'q'},         {"json", 0, 0, O_JSON},
                                       {"no-reduce", 0, 0, O_NOREDUCE}, {"alpha", 0, 0, O_ALPHA},
                                       {"max-error", 1, 0, 'e'},        {"fast", 0, 0, O_FAST},
                                       {"explore-level", 1, 0, O_EXPLORE},
                                       {"no-thin-strips", 0, 0, O_NOTHIN},
                                       {"version", 0, 0, 'V'},       {"help", 0, 0, 'h'},
                                       {0, 0, 0, 0}};
    int c, keep_set = 0;
    while ((c = getopt_long(argc, argv, "s:o:k:x:e:pj:P:qVh", lo, NULL)) != -1) {
        switch (c) {
        case 's':
            o.size = parse_size(optarg, &o.pct);
            if (o.size == -2) {
                fprintf(stderr, "pngfit: bad size '%s'\n", optarg);
                return 2;
            }
            break;
        case 'o': outdir = optarg; break;
        case O_SUFFIX: suffix = optarg; break;
        case 'k':
            if (!keep_set)
                o.keep_mask = 0;
            keep_set = 1;
            if (parse_meta(&o, optarg, 0))
                return 2;
            break;
        case 'x':
            if (parse_meta(&o, optarg, 1))
                return 2;
            break;
        case 'p': o.keep_mask = 255; keep_set = 1; break;
        case O_LALPHA: o.lossy_alpha = 1; break;
        case O_FILTER:
            o.filter = atoi(optarg);
            if (o.filter < -1 || o.filter > 4) {
                fputs("pngfit: --filter must be 0-4 (or -1 to probe)\n", stderr);
                return 2;
            }
            break;
        case O_RSW: o.row_switch = atof(optarg); break;
        case O_LADDER:
            if (!strcmp(optarg, "odd"))
                o.ladder_kind = 0;
            else if (!strcmp(optarg, "all"))
                o.ladder_kind = 1;
            else {
                o.ladder_kind = 2;
                char buf[1024];
                snprintf(buf, sizeof buf, "%s", optarg);
                for (char *t = strtok(buf, ","); t && o.nladder < 255; t = strtok(NULL, ",")) {
                    int q = atoi(t);
                    if (q < 1 || q > 255 || (o.nladder && q <= o.ladder[o.nladder - 1])) {
                        fputs("pngfit: --ladder list must rise from 1 within 1..255\n", stderr);
                        return 2;
                    }
                    o.ladder[o.nladder++] = q;
                }
                if (!o.nladder || o.ladder[0] != 1) {
                    fputs("pngfit: --ladder list must start with 1 (lossless)\n", stderr);
                    return 2;
                }
            }
            break;
        case O_MASK: o.mask = atof(optarg); break;
        case O_LEVEL: o.level = atoi(optarg); break;
        case O_STRIP: o.strip = atoi(optarg); o.strip_set = 1; break;
        case O_ROUNDS: o.rounds = atoi(optarg); break;
        case 'j': o.jobs = atoi(optarg); break;
        case 'P': o.parallel = atoi(optarg); break;
        case 'q': o.quiet = 1; break;
        case O_JSON: o.json = 1; break;
        case O_NOREDUCE: o.no_reduce = 1; break;
        case O_ALPHA: o.free_alpha = 1; break;
        case O_FAST: o.level = 10; break;
        case O_EXPLORE: o.explore_level = atoi(optarg); o.explore_set = 1; break;
        case O_NOTHIN: o.no_thin = 1; break;
        case 'e': {
            char *end;
            long v = strtol(optarg, &end, 10);
            if (*end || v < 0 || v > 65535) {
                fputs("pngfit: --max-error must be 0..65535\n", stderr);
                return 2;
            }
            o.max_error = (int)v;
            break;
        }
        case 'V': puts("pngfit " VERSION); return 0;
        case 'h': usage(stdout); return 0;
        default: usage(stderr); return 2;
        }
    }
    if (o.size == -2) {
        fputs("pngfit: -s/--size is required\n", stderr);
        usage(stderr);
        return 2;
    }
    if (o.level < 1 || o.level > 12 || o.strip < 1 || o.rounds < 0 || o.jobs < 1 || o.mask < 0) {
        fputs("pngfit: bad --level/--strip/--rounds/--jobs/--mask value\n", stderr);
        return 2;
    }
    int nin = argc - optind;
    if (outdir ? nin < 1 : nin != 2) {
        usage(stderr);
        return 2;
    }
    prog.quiet = o.quiet;
    prog.on = !o.quiet && stderr_tty();
    int nfail = 0, nexact = 0, nskip = 0, nother = 0, nmin = 0;
    int nfiles = outdir ? nin : 1;
    int par = o.parallel > 0 ? o.parallel : (outdir ? (nfiles < o.jobs ? nfiles : o.jobs) : 1);
    if (par > nfiles)
        par = nfiles;
    if (par > 64)
        par = 64;
    if (outdir && par > 1) {
        /* several files at once, each with its share of the threads; the per-file log
         * would interleave, so each file reports one line when it is done */
        Batch b = {.fo = o, .in = argv + optind, .n = nfiles, .outdir = outdir, .suffix = suffix,
                   .quiet = o.quiet, .json = o.json, .bar = !o.quiet && stderr_tty(), .t0 = now(),
                   .mu = PTHREAD_MUTEX_INITIALIZER};
        b.fo.jobs = o.jobs / par > 0 ? o.jobs / par : 1;
        prog.quiet = 1;
        prog.on = 0;
        if (!o.quiet)
            fprintf(stderr, "%d file(s), %d at once, %d thread(s) each\n", nfiles, par, b.fo.jobs);
        batch_bar(&b);
        pthread_t th[64];
        for (int i = 0; i < par; i++)
            if (pthread_create(&th[i], NULL, batch_worker, &b)) {
                fputs("pngfit: cannot start threads\n", stderr);
                return 2;
            }
        for (int i = 0; i < par; i++)
            pthread_join(th[i], NULL);
        if (b.bar)
            fputs("\r\x1b[K", stderr);
        if (!o.quiet)
            fprintf(stderr, "%d file(s): %d exact, %d already fit, %d smaller than target, %d at their minimum, "
                    "%d failed\n", nfiles, b.cnt[0], b.cnt[1], b.cnt[2] + b.cnt[3], b.cnt[4], b.fail);
        return b.fail ? 1 : 0;
    }
    for (int i = 0; i < nfiles; i++) {
        const char *src = argv[optind + i];
        char dst[4096];
        if (outdir) {
            batch_dst(dst, sizeof dst, outdir, src, suffix);
            snprintf(prog.prefix, sizeof prog.prefix, "[%d/%d] ", i + 1, nfiles);
            if (!o.quiet)
                fprintf(stderr, "[%d/%d] %s -> %s\n", i + 1, nfiles, src, dst);
        } else
            snprintf(dst, sizeof dst, "%s", argv[optind + 1]);
        Result r;
        fit_file(&o, src, dst, &r);
        if (o.json)
            print_json(src, dst, &r);
        if (r.status < 0 && !(o.quiet && o.json))
            fprintf(stderr, "%spngfit: %s: %s\n", prog.prefix, src, r.msg);
        if (r.status < 0)
            nfail++;
        else if (r.status == 0)
            nexact++;
        else if (r.status == 1)
            nskip++;
        else if (r.status == 4)
            nmin++;
        else
            nother++;
    }
    if (outdir && !o.quiet)
        fprintf(stderr, "%d file(s): %d exact, %d already fit, %d smaller than target, %d at their minimum, "
                "%d failed\n", nfiles, nexact, nskip, nother, nmin, nfail);
    return nfail ? 1 : 0;
}
