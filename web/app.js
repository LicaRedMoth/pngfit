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

function pick(f) {
  if (!f) return;
  f.arrayBuffer().then((buf) => {
    if (file && file.url) URL.revokeObjectURL(file.url);
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
  if (resultUrl) URL.revokeObjectURL(resultUrl);
  resultUrl = m.bytes ? URL.createObjectURL(new Blob([m.bytes], { type: 'image/png' })) : file.url;
  $('before').src = file.url;
  $('after').src = resultUrl;
  const base = file.name.replace(/\.a?png$/i, '');
  $('download').href = resultUrl;
  $('download').download = `${base}_${j.size}B.png`;
  $('download').hidden = !m.bytes;
  $('result').hidden = false;
}

function setSplit(v) {
  $('after').style.clipPath = `inset(0 0 0 ${v}%)`;
  $('handle').style.left = v + '%';
}
$('split').addEventListener('input', (e) => setSplit(e.target.value));
$('compare').addEventListener('pointermove', (e) => {
  if (e.buttons !== 1) return;
  const r = e.currentTarget.getBoundingClientRect();
  const v = Math.min(100, Math.max(0, ((e.clientX - r.left) / r.width) * 100));
  $('split').value = v;
  setSplit(v);
});
$('zoom').addEventListener('change', (e) => {
  // 1:1: the images at their natural size, the panel scrolls
  for (const id of ['before', 'after']) $(id).style.width = e.target.checked ? 'auto' : '100%';
  $('compare').style.overflow = e.target.checked ? 'auto' : 'hidden';
});
