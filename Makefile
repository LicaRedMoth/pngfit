# pngfit — one C file, two system libraries.
#   make            portable optimised build (what a package should ship)
#   make native     tuned for this very CPU (-march=native), stripped
#   make check      self-test: builds test images, verifies every output (Python 3, numpy, Pillow)
#   make install    PREFIX=/usr/local by default, DESTDIR supported
#   make windows    pngfit.exe, static, from an MSYS2 MINGW64 shell with
#                   pacman -S make mingw-w64-x86_64-gcc mingw-w64-x86_64-libdeflate mingw-w64-x86_64-zlib
#   make windows-cross  the same pngfit.exe from Linux: llvm-mingw (github.com/mstorsjo/llvm-mingw)
#                   on PATH, libdeflate and zlib from the submodules (git submodule update --init)
CC      ?= cc
CFLAGS  ?= -O3 -flto=auto -pipe
CFLAGS  += -std=c11 -Wall -Wextra
LDFLAGS ?= -flto=auto
LDLIBS  := -ldeflate -lz -lpthread -lm
PREFIX  ?= /usr/local
WINCC   ?= x86_64-w64-mingw32-clang
ZSRC    := $(addprefix vendor/zlib/,adler32.c crc32.c inflate.c inffast.c inftrees.c uncompr.c zutil.c)

pngfit: pngfit.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

native:
	$(MAKE) -B pngfit CFLAGS="-O3 -march=native -flto=auto -pipe" LDFLAGS="-flto=auto -s"

check: pngfit
	python3 tools/selftest.py ./pngfit

windows:
	$(CC) -O3 -pipe -std=c11 -Wall -Wextra -static -s -o pngfit.exe pngfit.c -ldeflate -lz -lpthread -lm -lshell32

windows-cross:
	$(WINCC) -O3 -pipe -std=gnu11 -static -s -Ivendor/libdeflate -Ivendor/zlib -o pngfit.exe pngfit.c \
		vendor/libdeflate/lib/*.c vendor/libdeflate/lib/x86/*.c $(ZSRC) -lpthread -lshell32

install: pngfit
	install -Dm755 pngfit $(DESTDIR)$(PREFIX)/bin/pngfit
	install -Dm644 LICENSE $(DESTDIR)$(PREFIX)/share/licenses/pngfit/LICENSE
	install -Dm644 README.md $(DESTDIR)$(PREFIX)/share/doc/pngfit/README.md
	install -Dm644 pngfit.1 $(DESTDIR)$(PREFIX)/share/man/man1/pngfit.1

clean:
	rm -f pngfit pngfit.exe

.PHONY: native check windows windows-cross install clean
