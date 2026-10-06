# pngfit — one C file, two system libraries.
#   make            portable optimised build (what a package should ship)
#   make native     tuned for this very CPU (-march=native), stripped
#   make check      self-test: builds test images, verifies every output (Python 3, numpy, Pillow)
#   make install    PREFIX=/usr/local by default, DESTDIR supported
#   make windows    pngfit.exe, static, from an MSYS2 MINGW64 shell with
#                   pacman -S make mingw-w64-x86_64-gcc mingw-w64-x86_64-libdeflate mingw-w64-x86_64-zlib
CC      ?= cc
CFLAGS  ?= -O3 -flto=auto -pipe
CFLAGS  += -std=c11 -Wall -Wextra
LDFLAGS ?= -flto=auto
LDLIBS  := -ldeflate -lz -lpthread -lm
PREFIX  ?= /usr/local

pngfit: pngfit.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

native:
	$(MAKE) -B pngfit CFLAGS="-O3 -march=native -flto=auto -pipe" LDFLAGS="-flto=auto -s"

check: pngfit
	python3 tools/selftest.py ./pngfit

windows:
	$(CC) -O3 -pipe -std=c11 -Wall -Wextra -static -s -o pngfit.exe pngfit.c -ldeflate -lz -lpthread -lm -lshell32

install: pngfit
	install -Dm755 pngfit $(DESTDIR)$(PREFIX)/bin/pngfit
	install -Dm644 LICENSE $(DESTDIR)$(PREFIX)/share/licenses/pngfit/LICENSE
	install -Dm644 README.md $(DESTDIR)$(PREFIX)/share/doc/pngfit/README.md
	install -Dm644 pngfit.1 $(DESTDIR)$(PREFIX)/share/man/man1/pngfit.1

clean:
	rm -f pngfit pngfit.exe

.PHONY: native check windows install clean
