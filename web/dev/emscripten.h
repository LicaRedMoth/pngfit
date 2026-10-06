/* Stand-in for <emscripten.h>, only to compile and run the web entry point natively
 * (web/dev/check_web_entry.sh). Not used by the real web build. */
#pragma once
#define EMSCRIPTEN_KEEPALIVE
void web_done_stub(int rc);
#define MAIN_THREAD_ASYNC_EM_ASM(code, ...) web_done_stub(__VA_ARGS__)
/* threaded like the main web build, unless checking the single-threaded fallback */
#ifndef STUB_NO_THREADS
#define __EMSCRIPTEN_PTHREADS__ 1
#endif
