// Runs the Node.js variant of the web build once: node node_run.cjs module.cjs in.png out.png [args...]
'use strict';
const fs = require('fs');
const [modPath, inFile, outFile, ...args] = process.argv.slice(2);
require(require('path').resolve(modPath))({ print: (l) => console.log(l), printErr: () => {} }).then((mod) => {
  mod.FS.writeFile('/in.png', fs.readFileSync(inFile));
  const argv = ['pngfit', ...args, '--json', '/in.png', '/out.png'];
  const ptrs = argv.map((a) => mod.stringToNewUTF8(a));
  const p = mod._malloc(4 * ptrs.length);
  ptrs.forEach((x, i) => mod.setValue(p + 4 * i, x, 'i32'));
  mod.onDone = (rc) => {
    try { fs.writeFileSync(outFile, mod.FS.readFile('/out.png')); } catch (_) { /* nothing written */ }
    process.exitCode = rc;
  };
  mod._pngfit_start(argv.length, p);
});
