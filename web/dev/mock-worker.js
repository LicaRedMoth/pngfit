/* Stand-in for worker.js (page loaded with ?mock): plays a run without the wasm build,
 * so the page can be checked in a browser before Emscripten is installed. */
'use strict';
postMessage({ type: 'ready', threads: 2 });
const phases = [['analysing', 0], ['filter probe', 16], ['lossless', 24], ['ladder q=3', 48],
                ['RD round 1/4', 30], ['RD round 2/4', 30], ['exact landing', 0], ['writing', 24]];
onmessage = (event) => {
  const { bytes, args } = event.data;
  postMessage({ type: 'log', line: 'mock run: ' + args.join(' ') });
  let i = 0, done = 0;
  const step = () => {
    if (i === phases.length) {
      const json = { status: 'exact', size: bytes.byteLength, target: bytes.byteLength, lossless: bytes.byteLength,
                     depth: 8, psnr: 53.79, max_error: 1, changed: 0.392, time: 4.2 };
      postMessage({ type: 'result', rc: 0, json, bytes }, [bytes]);
      return;
    }
    const [label, total] = phases[i];
    postMessage({ type: 'progress', label, done, total, seconds: i });
    done += Math.max(1, Math.ceil(total / 4));
    if (done > total) { i++; done = 0; postMessage({ type: 'log', line: 'phase done: ' + label }); }
    setTimeout(step, 60);
  };
  step();
};
