/* pngfit web page: picks a file, builds the command line, talks to worker.js. */
'use strict';

const $ = (id) => document.getElementById(id);
const dev = new URLSearchParams(location.search).has('mock'); // UI check without the wasm build
const worker = new Worker(dev ? 'dev/mock-worker.js' : 'worker.js');

let file = null;       // {name, bytes, url}
let resultUrl = null;
let started = 0;
let timer = 0;

const fmtBytes = (n) => n >= 1e6 ? (n / 1e6).toFixed(2) + ' MB' : n >= 1e3 ? (n / 1e3).toFixed(1) + ' KB' : n + ' B';
const group = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');

worker.onmessage = ({ data: m }) => {
  if (m.type === 'ready') {
    $('cores').textContent = m.threads > 1
      ? `Running on ${m.threads} threads.`
      : 'Running single-threaded (this page is not cross-origin isolated, or the browser has no SharedArrayBuffer).';
  } else if (m.type === 'progress') {
    $('phase').textContent = m.label || 'working';
    const bar = $('bar');
    if (m.total > 0) { bar.max = m.total; bar.value = m.done; } else bar.removeAttribute('value');
  } else if (m.type === 'log') {
    const log = $('log');
    log.textContent += m.line + '\n';
    log.scrollTop = log.scrollHeight;
  } else if (m.type === 'result') {
    finish(m);
  } else if (m.type === 'error') {
    stopTimer();
    $('phase').textContent = 'error: ' + m.message;
    $('phase').className = 'warn';
    $('go').disabled = !file;
  }
};

worker.onerror = (e) => {
  $('cores').textContent = 'Could not start pngfit: ' + (e.message || 'the worker failed to load') + '.';
  $('cores').className = 'hint warn';
};

function pick(f) {
  if (!f) return;
  f.arrayBuffer().then((buf) => {
    if (resultUrl && resultUrl !== (file && file.url)) URL.revokeObjectURL(resultUrl);
    if (file && file.url) URL.revokeObjectURL(file.url);
    resultUrl = null;
    file = { name: f.name, bytes: buf, url: URL.createObjectURL(new Blob([buf], { type: 'image/png' })) };
    const img = new Image();
    img.onload = () => { $('fileinfo').textContent = `${f.name} · ${img.naturalWidth}×${img.naturalHeight} · ${fmtBytes(buf.byteLength)}`; };
    img.onerror = () => { $('fileinfo').textContent = `${f.name} · ${fmtBytes(buf.byteLength)} · the browser cannot show it, pngfit may still read it`; };
    img.src = file.url;
    $('go').disabled = false;
    $('result').hidden = true;
  });
}

$('file').addEventListener('change', (e) => pick(e.target.files[0]));
const drop = $('drop');
['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => pick(e.dataTransfer.files[0]));

function args() {
  const a = ['-s', $('size').value.trim() + $('unit').value];
  if ($('cap').checked) a.push('-e', String(Math.max(0, parseInt($('caperr').value, 10) || 0)));
  const meta = $('meta').value;
  if (meta === 'meta') a.push('--strip', 'meta');
  if (meta === 'none') a.push('-k', 'none');
  if ($('alpha').checked) a.push('--alpha');
  if ($('fast').checked) a.push('--fast');
  return a;
}

function stopTimer() { clearInterval(timer); timer = 0; }

$('go').addEventListener('click', () => {
  if (!file) return;
  const size = parseFloat($('size').value);
  if (!(size >= 0)) { $('size').focus(); return; }
  $('go').disabled = true;
  $('result').hidden = true;
  $('work').hidden = false;
  $('log').textContent = '';
  $('phase').textContent = 'starting';
  $('phase').className = '';
  $('bar').removeAttribute('value');
  started = performance.now();
  timer = setInterval(() => {
    const s = (performance.now() - started) / 1000;
    $('elapsed').textContent = `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  }, 500);
  const copy = file.bytes.slice(0); // the worker gets its own copy; we keep ours for "before"
  worker.postMessage({ type: 'run', bytes: copy, args: args() }, [copy]);
});

function stat(value, label, cls) {
  return `<div class="stat"><b class="${cls || ''}">${value}</b><span>${label}</span></div>`;
}

function finish(m) {
  stopTimer();
  $('go').disabled = false;
  const j = m.json || {};
  if (j.status === 'error' || (!m.bytes && j.status !== 'skipped')) {
    $('phase').textContent = 'failed: ' + (j.error || 'see the log');
    $('phase').className = 'warn';
    return;
  }
  $('work').hidden = true;
  const verdict = {
    exact: ['exact', 'exact'],
    minimum: ['smallest possible', 'warn'],
    lossless: ['lossless, under the target', 'exact'],
    short: [`${group(j.target - j.size)} B short`, 'warn'],
    skipped: ['already fits: nothing to do', 'exact'],
  }[j.status] || [j.status, ''];
  let html = stat(group(j.size) + ' B', `target ${group(j.target)} B · ${verdict[0]}`, verdict[1]);
  if (j.status !== 'skipped') {
    const peak = j.depth === 16 ? 65535 : 255;
    html += stat(j.psnr == null ? '∞' : j.psnr.toFixed(2) + ' dB', 'PSNR (higher is better)');
    html += stat('±' + j.max_error, `worst pixel, levels of ${peak}`);
    html += stat((j.changed * 100).toFixed(1) + ' %', 'pixels changed');
    html += stat(j.time.toFixed(1) + ' s', 'time');
  }
  $('stats').innerHTML = html;
  // a run that left the file alone showed the original itself as the result: keep that one
  if (resultUrl && resultUrl !== file.url) URL.revokeObjectURL(resultUrl);
  resultUrl = m.bytes ? URL.createObjectURL(new Blob([m.bytes], { type: 'image/png' })) : file.url;
  $('before').src = file.url;
  $('after').src = resultUrl;
  const base = file.name.replace(/\.a?png$/i, '');
  $('download').href = resultUrl;
  $('download').download = `${base}_${j.size}B.png`;
  $('download').hidden = !m.bytes;
  $('diffrow').hidden = !m.bytes;
  if (!m.bytes) $('diffmode').checked = false;
  diffFor = null;
  $('result').hidden = false;
  showDiff();
}

// The difference view: for every pixel the largest change in any channel, scaled so the
// largest change in the image is the brightest colour, black where nothing changed. The images
// are decoded without colour management, so a dropped colour profile does not show up as a
// change, and pixels transparent in both are left black.
let diffFor = null; // the result the canvas shows
const HEAT = (() => { // black, violet, red, orange, pale yellow
  const stops = [[0, 0, 4], [87, 16, 110], [188, 55, 84], [249, 142, 9], [252, 255, 164]];
  const lut = new Uint8Array(256 * 3);
  for (let v = 0; v < 256; v++) {
    const x = (v / 255) * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(x)), f = x - i;
    for (let c = 0; c < 3; c++) lut[v * 3 + c] = Math.round(stops[i][c] + (stops[i + 1][c] - stops[i][c]) * f);
  }
  return lut;
})();

async function pixelsOf(url) {
  const blob = await (await fetch(url)).blob();
  const bmp = await createImageBitmap(blob, { colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
  const c = document.createElement('canvas');
  c.width = bmp.width;
  c.height = bmp.height;
  const g = c.getContext('2d', { willReadFrequently: true });
  g.drawImage(bmp, 0, 0);
  bmp.close();
  return g.getImageData(0, 0, c.width, c.height);
}

async function drawDiff() {
  if (diffFor === resultUrl) return;
  const want = resultUrl;
  $('legend').textContent = 'working out the difference…';
  let a, b;
  try {
    [a, b] = await Promise.all([pixelsOf(file.url), pixelsOf(want)]);
  } catch (_) {
    $('legend').textContent = 'This browser cannot decode the images to compare them.';
    return;
  }
  if (want !== resultUrl) return; // a newer result arrived meanwhile
  if (a.width !== b.width || a.height !== b.height) {
    $('legend').textContent = 'The two images differ in size, so there is nothing to compare.';
    return;
  }
  const n = a.width * a.height, d = new Uint8Array(n), A = a.data, B = b.data;
  let max = 0, changed = 0;
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    if (A[p + 3] === 0 && B[p + 3] === 0) continue;
    const m = Math.max(Math.abs(A[p] - B[p]), Math.abs(A[p + 1] - B[p + 1]),
                       Math.abs(A[p + 2] - B[p + 2]), Math.abs(A[p + 3] - B[p + 3]));
    d[i] = m;
    if (m) changed++;
    if (m > max) max = m;
  }
  const canvas = $('diff');
  canvas.width = a.width;
  canvas.height = a.height;
  const g = canvas.getContext('2d');
  const out = g.createImageData(a.width, a.height), O = out.data;
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    const v = max ? Math.round((d[i] * 255) / max) * 3 : 0;
    O[p] = HEAT[v];
    O[p + 1] = HEAT[v + 1];
    O[p + 2] = HEAT[v + 2];
    O[p + 3] = 255;
  }
  g.putImageData(out, 0, 0);
  diffFor = want;
  $('legend').textContent = max === 0
    ? 'No pixel changed.'
    : `Black: unchanged. Brightest: the largest change, ±${max} of 255 as the browser shows it. ` +
      `${((changed / n) * 100).toFixed(1)} % of the pixels changed.`;
}

async function showDiff() {
  const on = $('diffmode').checked;
  $('after').hidden = on;
  $('diff').hidden = !on;
  $('tagr').textContent = on ? 'difference' : 'pngfit';
  $('legend').hidden = !on;
  if (on) await drawDiff();
}
$('diffmode').addEventListener('change', showDiff);

// Fitted, the whole image is on screen: as wide as the panel, no taller than 75 % of the window.
// At 1:1 it has its own size: the window shrinks around a small image and scrolls over a large
// one. Either way the viewer is centred and no wider than the image. Pixels are drawn as squares
// only when enlarged; a reduced image is smoothed.
function layout() {
  const img = $('before'), stage = $('stage'), c = $('compare'), viewer = $('viewer');
  const zoomed = $('zoom').checked, w = img.naturalWidth, h = img.naturalHeight;
  c.classList.toggle('zoomed', zoomed);
  viewer.style.maxWidth = '';
  if (w && h) {
    if (zoomed) { // the image, its border, and the vertical scrollbar if it has one
      const bar = c.offsetWidth - c.clientWidth - 2;
      viewer.style.maxWidth = `${w + 2 + bar}px`;
    } else {
      viewer.style.maxWidth = `min(100%, calc(75vh * ${w / h}))`;
    }
  }
  stage.classList.toggle('pixels', zoomed || stage.clientWidth > w);
}

function setSplit(v) {
  $('after').style.clipPath = $('diff').style.clipPath = `inset(0 0 0 ${v}%)`;
  $('handle').style.left = v + '%';
}

// at 1:1, bring the boundary into view (and, the first time, the middle of the image)
function reveal(middle) {
  const c = $('compare'), stage = $('stage');
  c.scrollLeft = stage.offsetWidth * $('split').value / 100 - c.clientWidth / 2;
  if (middle) c.scrollTop = (stage.offsetHeight - c.clientHeight) / 2;
}

$('before').addEventListener('load', () => {
  $('viewer').hidden = false;
  $('noview').hidden = true;
  layout();
});
$('before').addEventListener('error', () => { // a PNG pngfit reads but this browser cannot show
  $('viewer').hidden = true;
  $('noview').hidden = false;
});
window.addEventListener('resize', layout);
$('split').addEventListener('input', (e) => {
  setSplit(e.target.value);
  if ($('zoom').checked) reveal(false);
});
// press anywhere on the image and drag: the boundary follows, even outside the panel
// (scrollbars are not part of the stage, so they still scroll)
function splitAt(e) {
  const r = $('stage').getBoundingClientRect(); // the image itself, wherever it is scrolled
  const v = Math.min(100, Math.max(0, ((e.clientX - r.left) / r.width) * 100));
  $('split').value = v;
  setSplit(v);
}
$('stage').addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  $('stage').setPointerCapture(e.pointerId);
  splitAt(e);
});
$('stage').addEventListener('pointermove', (e) => {
  if ($('stage').hasPointerCapture(e.pointerId)) splitAt(e);
});
$('stage').addEventListener('dragstart', (e) => e.preventDefault());
$('zoom').addEventListener('change', (e) => {
  layout();
  if (e.target.checked) reveal(true);
});
