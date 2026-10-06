# pngfit in the browser

A static page that runs pngfit as WebAssembly on the visitor's machine: no upload, no server
work, the image never leaves the computer.

| File | |
|---|---|
| `index.html`, `app.js` | the page |
| `worker.js` | runs the module off the page's thread, relays progress and the result |
| `build.sh` | Emscripten build: `pngfit.js/.wasm` (threaded) and `pngfit-st.js/.wasm` (fallback) |
| `_headers` | Cloudflare Pages headers for cross-origin isolation |
| `dev/` | a stand-in worker (`index.html?mock`) to check the page without a build, and a native check of the web entry point |

## Build

```sh
pacman -S emscripten                                                     # or the emsdk
git submodule add https://github.com/ebiggers/libdeflate vendor/libdeflate   # once
web/build.sh
```

libdeflate is a submodule, a link to its repository at a pinned commit, not a copy:
`git submodule update --remote vendor/libdeflate` moves it to the newest upstream, and the next
build picks it up. zlib comes from Emscripten's own port.

## Threads

The threaded build needs `SharedArrayBuffer`, which browsers give only to cross-origin isolated
pages: served with `Cross-Origin-Opener-Policy: same-origin` and
`Cross-Origin-Embedder-Policy: require-corp`. On Cloudflare Pages the `_headers` file does that.
Without the headers the page falls back to the single-threaded build by itself and says so.

## Deploy (Cloudflare Pages)

Upload `index.html`, `app.js`, `worker.js`, `_headers` and the four built files
(`pngfit.js`, `pngfit.wasm`, `pngfit-st.js`, `pngfit-st.wasm`). If the page lives under a path of a
larger site, scope the `_headers` rule to that path.

## Checks without a toolchain

- `index.html?mock` plays a run with a stand-in worker: layout, progress, result, download.
- `dev/check_web_entry.sh` compiles `pngfit.c`'s web entry point natively against a stand-in
  `emscripten.h` and runs it twice in a row, threaded and single-threaded.
