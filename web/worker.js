/* Runs pngfit's WebAssembly build off the page's thread.
 *
 * in:  {type: 'run', bytes: ArrayBuffer, args: [...]}
 * out: {type: 'ready', threads}             module loaded
 *      {type: 'progress', label, done, total, seconds}
 *      {type: 'log', line}
 *      {type: 'result', rc, json, bytes}    bytes: the output file, or null
 *      {type: 'error', message}
 */
'use strict';

const threaded = self.crossOriginIsolated === true; // SharedArrayBuffer is there only then
const script = threaded ? 'pngfit.js' : 'pngfit-st.js';
importScripts(script);

let json = null;
let busy = false;

const ready = createPngfit({
  // pthread workers must load the module script, not this file
  mainScriptUrlOrBlob: new URL(script, self.location.href).href,
  print(line) {
    if (line.startsWith('{')) {
      try { json = JSON.parse(line); return; } catch (_) { /* not ours: show it */ }
    }
    postMessage({ type: 'log', line });
  },
  printErr(line) {
    if (line.startsWith('@progress ')) {
      const [label, done, total, seconds] = line.slice(10).split('\t');
      postMessage({ type: 'progress', label, done: +done, total: +total, seconds: +seconds });
    } else {
      postMessage({ type: 'log', line });
    }
  },
}).then((mod) => {
  postMessage({ type: 'ready', threads: threaded ? navigator.hardwareConcurrency : 1 });
  return mod;
}, (err) => {
  postMessage({ type: 'error', message: 'could not load the WebAssembly module: ' + err });
  throw err;
});

onmessage = async (event) => {
  const msg = event.data;
  if (msg.type !== 'run') return;
  const mod = await ready;
  if (busy) {
    postMessage({ type: 'error', message: 'already running' });
    return;
  }
  busy = true;
  json = null;
  const inPath = '/in.png', outPath = '/out.png';
  mod.FS.writeFile(inPath, new Uint8Array(msg.bytes));
  try { mod.FS.unlink(outPath); } catch (_) { /* no previous output */ }

  const args = ['pngfit', ...msg.args, ...(threaded ? [] : ['-j', '1']),
                '--json', '--progress-lines', inPath, outPath];
  const ptrs = args.map((a) => mod.stringToNewUTF8(a));
  const argv = mod._malloc(4 * ptrs.length);
  ptrs.forEach((p, i) => mod.setValue(argv + 4 * i, p, 'i32'));

  mod.onDone = (rc) => {
    let out = null;
    try { out = mod.FS.readFile(outPath); } catch (_) { /* nothing written: skipped or failed */ }
    try { mod.FS.unlink(inPath); } catch (_) { /* already gone */ }
    try { mod.FS.unlink(outPath); } catch (_) { /* never written */ }
    busy = false;
    postMessage({ type: 'result', rc, json, bytes: out ? out.buffer : null }, out ? [out.buffer] : []);
  };
  const started = mod._pngfit_start(args.length, argv);
  ptrs.forEach((p) => mod._free(p)); // pngfit_start keeps its own copies
  mod._free(argv);
  if (started !== 0) {
    busy = false;
    postMessage({ type: 'error', message: 'could not start a thread' });
  }
};
