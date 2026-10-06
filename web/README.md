# pngfit in the browser

A static page that runs pngfit as WebAssembly on the visitor's machine: no upload, no server
work, the image never leaves the computer.

| File | |
|---|---|
| `index.html`, `app.js` | the page |
| `worker.js` | runs the module off the page's thread, relays progress and the result |
| `build.sh` | Emscripten build: `pngfit.js/.wasm` (threaded) and `pngfit-st.js/.wasm` (fallback) |
| `_headers` | Cloudflare Pages headers for cross-origin isolation |
| `dev/` | a local server with the headers, checks against the native binary, a stand-in worker (`index.html?mock`) |

## Build

```sh
git submodule update --init          # libdeflate sources, once (or clone with --recursive)
web/build.sh                         # needs Emscripten: the emsdk, or pacman -S emscripten
```

libdeflate is a submodule, a link to its repository at a pinned commit, not a copy:
`git submodule update --remote vendor/libdeflate` moves it to the newest upstream, and the next
build picks it up. zlib comes from Emscripten's own port. The native build does not use the
submodule; it links the system libdeflate.

## Threads

The threaded build needs `SharedArrayBuffer`, which browsers give only to cross-origin isolated
pages: served with `Cross-Origin-Opener-Policy: same-origin` and
`Cross-Origin-Embedder-Policy: require-corp`. On Cloudflare Pages the `_headers` file does that.
Without the headers the page falls back to the single-threaded build by itself and says so.

Emscripten starts its threads from the script that created the module, here `worker.js`; a worker
named `em-pthread` therefore only loads `pngfit.js` and leaves the rest to it.

## Deploy (Cloudflare Pages)

Upload `index.html`, `app.js`, `worker.js`, `_headers` and the four built files
(`pngfit.js`, `pngfit.wasm`, `pngfit-st.js`, `pngfit-st.wasm`). If the page lives under a path of a
larger site, scope the `_headers` rule to that path.

## Checks

- `dev/serve.py [port]` serves `web/` with the isolation headers (default port 8000), as
  Cloudflare will; `python3 -m http.server` without them shows the single-threaded fallback.
- `dev/node_check.sh image.png ...` builds a Node.js variant and checks that WebAssembly pngfit
  writes byte-for-byte the same files as the native binary.
- `index.html?mock` plays a run with a stand-in worker: layout, progress, result, download,
  without a build.
- `dev/check_web_entry.sh` compiles `pngfit.c`'s web entry point natively against a stand-in
  `emscripten.h` and runs it twice in a row, threaded and single-threaded.
