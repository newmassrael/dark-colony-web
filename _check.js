
// Data overlay path: defaults to 'data' (serves custom UI files alongside ISO/folder mode)
// ?datapath=/gamedata overrides for dev/test with serve.py
var serverDataPath = new URLSearchParams(window.location.search).get('datapath') || 'data';

// Server mode: pre-loaded file list to avoid 404 fetch errors in browser console
var _serverFileSet = null;

if (serverDataPath) {
  var xhr = new XMLHttpRequest();
  xhr.open('GET', serverDataPath + '/_filelist?t=' + Date.now(), false);
  try {
    xhr.send();
    if (xhr.status === 200) {
      var list = JSON.parse(xhr.responseText);
      _serverFileSet = {};
      for (var i = 0; i < list.length; i++) _serverFileSet[list[i]] = true;
      console.log('File list loaded: ' + list.length + ' files');
    }
  } catch(e) {}
}

/*============================================================================
 * IndexedDB persistence for save games (esave/*.dcg)
 *
 * Save files live in Emscripten MEMFS (volatile). This layer syncs them
 * to IndexedDB so they survive page reloads and browser restarts.
 *============================================================================*/
var saveDB = null;
var _pendingSaves = [];

// Open IDB and pre-load saves at page load (completes while WASM downloads)
(function() {
  var req = indexedDB.open('darkcolony-saves', 1);
  req.onupgradeneeded = function(e) {
    var db = e.target.result;
    if (!db.objectStoreNames.contains('saves')) {
      db.createObjectStore('saves', { keyPath: 'path' });
    }
  };
  req.onsuccess = function(e) {
    saveDB = e.target.result;
    var tx = saveDB.transaction('saves', 'readonly');
    tx.objectStore('saves').getAll().onsuccess = function(ev) {
      _pendingSaves = ev.target.result || [];
      if (_pendingSaves.length > 0) {
        console.log('Loaded ' + _pendingSaves.length + ' save(s) from IndexedDB');
      }
    };
  };
  req.onerror = function() { console.error('Failed to open save database'); };
})();

// Called from C++ (__wrap_close/__wrap_fclose) when an esave/ file is closed after writing.
// path is "esave/name.dcg" — game writes directly to esave/ (exp/ write falls through).
function persistSave(path) {
  if (!saveDB) return;
  try {
    var data = FS.readFile(path);
    var tx = saveDB.transaction('saves', 'readwrite');
    tx.objectStore('saves').put({ path: path, data: data });
    console.log('Save persisted: ' + path + ' (' + data.length + ' bytes)');
  } catch(e) {
    console.error('Failed to persist save:', path, e);
  }
}

/*============================================================================
 * ISO9660 CD Image VFS
 *
 * Mounts BIN/CUE and MDF/MDS disc images, providing direct file access
 * without game installation. Supports base game + expansion overlay.
 *============================================================================*/
var _iso = {
  base: null,          // { file, sectorSize, dataOffset, volumeId, rootSector, rootSize }
  expansion: null,
  baseDirHandle: null,
  expDirHandle: null,
  fileMap: {},          // UPPER(gamePath) → { mount, sector, size }
  dirMap: {},           // UPPER(dirPath)  → [ {name, isDir} ]
  cuePath: null,        // CUE path written to MEMFS for CDDA
};

/* Read raw bytes from a File object */
async function isoSlice(file, offset, length) {
  var blob = file.slice(offset, offset + length);
  return new Uint8Array(await blob.arrayBuffer());
}

/* Detect ISO9660 sector format by looking for CD001 at sector 16 */
async function isoDetect(file) {
  var fmts = [
    { sz: 2048, off: 0 },   // Plain ISO
    { sz: 2352, off: 16 },  // MODE1/2352
    { sz: 2352, off: 24 },  // MODE2/2352
  ];
  for (var i = 0; i < fmts.length; i++) {
    var f = fmts[i];
    var pos = 16 * f.sz + f.off;
    if (pos + 2048 > file.size) continue;
    var pvd = await isoSlice(file, pos, 2048);
    if (pvd[0] === 1 && pvd[1] === 0x43 && pvd[2] === 0x44 &&
        pvd[3] === 0x30 && pvd[4] === 0x30 && pvd[5] === 0x31) {
      return { sectorSize: f.sz, dataOffset: f.off, pvd: pvd };
    }
  }
  return null;
}

/* Read sector data (2048-byte data portions) from ISO */
async function isoReadSectors(file, ss, doff, startSector, count) {
  if (ss === 2048) {
    return await isoSlice(file, startSector * 2048, count * 2048);
  }
  var raw = await isoSlice(file, startSector * ss, count * ss);
  var result = new Uint8Array(count * 2048);
  for (var i = 0; i < count; i++) {
    result.set(raw.subarray(i * ss + doff, i * ss + doff + 2048), i * 2048);
  }
  return result;
}

/* Parse ISO9660 directory records */
function isoParseDirRecords(data, size) {
  var entries = [];
  var offset = 0;
  while (offset < size) {
    var recLen = data[offset];
    if (recLen === 0) {
      var next = (Math.floor(offset / 2048) + 1) * 2048;
      if (next < size) { offset = next; continue; }
      break;
    }
    if (offset + recLen > size) break;
    var nameLen = data[offset + 32];
    if (nameLen === 1 && (data[offset + 33] === 0 || data[offset + 33] === 1)) {
      offset += recLen; continue;
    }
    var name = '';
    for (var i = 0; i < nameLen; i++) name += String.fromCharCode(data[offset + 33 + i]);
    var sector = data[offset+2]|(data[offset+3]<<8)|(data[offset+4]<<16)|(data[offset+5]<<24);
    var fsize = data[offset+10]|(data[offset+11]<<8)|(data[offset+12]<<16)|(data[offset+13]<<24);
    var isDir = !!(data[offset + 25] & 0x02);
    var semi = name.indexOf(';');
    if (semi >= 0) name = name.substring(0, semi);
    if (name.endsWith('.')) name = name.substring(0, name.length - 1);
    entries.push({ name: name, sector: sector, size: fsize, isDir: isDir });
    offset += recLen;
  }
  return entries;
}

/* Read and parse an ISO directory */
async function isoReadDir(mount, sector, size) {
  var data = await isoReadSectors(mount.file, mount.sectorSize, mount.dataOffset,
                                   sector, Math.ceil(size / 2048));
  return isoParseDirRecords(data, size);
}

/* Walk ISO tree recursively, calling cb(gamePath, entry) */
async function isoWalkTree(mount, sector, size, prefix, cb) {
  var entries = await isoReadDir(mount, sector, size);
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    var path = prefix ? prefix + '/' + e.name : e.name;
    cb(path, e);
    if (e.isDir) await isoWalkTree(mount, e.sector, e.size, path, cb);
  }
}

/* Read file data from ISO */
async function isoReadFileData(mount, sector, size) {
  if (size === 0) return null;
  var ss = mount.sectorSize, doff = mount.dataOffset;
  if (ss === 2048) return await isoSlice(mount.file, sector * 2048, size);
  var count = Math.ceil(size / 2048);
  var raw = await isoSlice(mount.file, sector * ss, count * ss);
  var result = new Uint8Array(size);
  var rem = size;
  for (var i = 0; i < count; i++) {
    var n = Math.min(2048, rem);
    result.set(raw.subarray(i * ss + doff, i * ss + doff + n), i * 2048);
    rem -= n;
  }
  return result;
}

/* Mount an ISO9660 image */
async function isoMount(file) {
  var det = await isoDetect(file);
  if (!det) return null;
  var pvd = det.pvd;
  var vol = '';
  for (var i = 40; i < 72; i++) vol += String.fromCharCode(pvd[i]);
  vol = vol.trim();
  var rootSec = pvd[158]|(pvd[159]<<8)|(pvd[160]<<16)|(pvd[161]<<24);
  var rootSz  = pvd[166]|(pvd[167]<<8)|(pvd[168]<<16)|(pvd[169]<<24);
  console.log('ISO mounted: ' + vol + ' (' + file.name + ', ' +
              det.sectorSize + '/' + det.dataOffset + ')');
  return { file:file, sectorSize:det.sectorSize, dataOffset:det.dataOffset,
           volumeId:vol, rootSector:rootSec, rootSize:rootSz };
}

/* Find and mount ALL disc images in a directory. Returns array of mounts. */
async function findAllDiscMounts(dirHandle) {
  var cueFiles = [], mdfFiles = [], isoFiles = [];
  var allFiles = {};
  for await (var [name, handle] of dirHandle.entries()) {
    if (handle.kind !== 'file') continue;
    allFiles[name] = handle;
    var lo = name.toLowerCase();
    if (lo.endsWith('.cue')) cueFiles.push({ name: name, handle: handle });
    else if (lo.endsWith('.mdf')) mdfFiles.push({ name: name, handle: handle });
    else if (lo.endsWith('.iso')) isoFiles.push({ name: name, handle: handle });
  }

  var mounts = [];

  // Mount from each CUE (find data track BIN)
  for (var ci = 0; ci < cueFiles.length; ci++) {
    var cf = await cueFiles[ci].handle.getFile();
    var cueText = await cf.text();
    var lines = cueText.split('\n');
    var curFile = null, dataFile = null;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      var fm = line.match(/^FILE\s+"([^"]+)"\s+BINARY/i);
      if (fm) curFile = fm[1];
      if (line.match(/TRACK\s+\d+\s+MODE[12]/i) && curFile) {
        for (var fn in allFiles) {
          if (fn.toLowerCase() === curFile.toLowerCase()) {
            dataFile = await allFiles[fn].getFile(); break;
          }
        }
        break;
      }
    }
    if (dataFile) {
      var m = await isoMount(dataFile);
      if (m) mounts.push(m);
    }
  }

  // Mount each MDF
  for (var mi = 0; mi < mdfFiles.length; mi++) {
    var f = await mdfFiles[mi].handle.getFile();
    var m = await isoMount(f);
    if (m) mounts.push(m);
  }

  // Mount each ISO
  for (var ii = 0; ii < isoFiles.length; ii++) {
    var f = await isoFiles[ii].handle.getFile();
    var m = await isoMount(f);
    if (m) mounts.push(m);
  }

  // Fallback: try standalone .bin files as ISO
  if (mounts.length === 0) {
    for (var fn in allFiles) {
      if (fn.toLowerCase().endsWith('.bin')) {
        var f = await allFiles[fn].getFile();
        var m = await isoMount(f);
        if (m) { mounts.push(m); break; }
      }
    }
  }

  return mounts;
}

/* Add entry to _iso.fileMap and _iso.dirMap */
function isoAddEntry(gamePath, entry, mount) {
  var upper = gamePath.toUpperCase();
  if (!entry.isDir) {
    _iso.fileMap[upper] = { mount: mount, sector: entry.sector, size: entry.size };
  }
  var lastSlash = upper.lastIndexOf('/');
  var parentKey = lastSlash >= 0 ? upper.substring(0, lastSlash) : '';
  var childName = lastSlash >= 0 ? gamePath.substring(lastSlash + 1) : gamePath;
  if (!_iso.dirMap[parentKey]) _iso.dirMap[parentKey] = [];
  var existing = _iso.dirMap[parentKey].findIndex(function(e) {
    return e.name.toUpperCase() === childName.toUpperCase();
  });
  if (existing >= 0) {
    _iso.dirMap[parentKey][existing] = { name: childName, isDir: entry.isDir };
  } else {
    _iso.dirMap[parentKey].push({ name: childName, isDir: entry.isDir });
  }
  if (entry.isDir && !_iso.dirMap[upper]) _iso.dirMap[upper] = [];
}

/* Build file/dir maps from mounted ISOs with path remapping */
async function isoBuildMaps() {
  _iso.fileMap = {};
  _iso.dirMap = {};

  // Base game: DC/* -> *
  if (_iso.base) {
    var root = _iso.base.rootEntries || await isoReadDir(_iso.base, _iso.base.rootSector, _iso.base.rootSize);
    for (var i = 0; i < root.length; i++) {
      if (root[i].isDir && root[i].name.toUpperCase() === 'DC') {
        await isoWalkTree(_iso.base, root[i].sector, root[i].size, '',
          function(p, e) { isoAddEntry(p, e, _iso.base); });
        break;
      }
    }
  }

  // Expansion: DC/* -> * (overwrites base), EXP*/EXP* -> exp/
  if (_iso.expansion) {
    var root = _iso.expansion.rootEntries || await isoReadDir(_iso.expansion, _iso.expansion.rootSector, _iso.expansion.rootSize);
    for (var i = 0; i < root.length; i++) {
      if (root[i].isDir && root[i].name.toUpperCase() === 'DC') {
        await isoWalkTree(_iso.expansion, root[i].sector, root[i].size, '',
          function(p, e) { isoAddEntry(p, e, _iso.expansion); });
      }
    }
    for (var i = 0; i < root.length; i++) {
      var rn = root[i].name.toUpperCase();
      if (root[i].isDir && rn.startsWith('EXP') && rn.length > 3) {
        var langEntries = await isoReadDir(_iso.expansion, root[i].sector, root[i].size);
        for (var j = 0; j < langEntries.length; j++) {
          var en = langEntries[j].name.toUpperCase();
          if (langEntries[j].isDir && en.startsWith('EXP')) {
            await isoWalkTree(_iso.expansion, langEntries[j].sector, langEntries[j].size, 'exp',
              function(p, e) { isoAddEntry(p, e, _iso.expansion); });
          } else if (!langEntries[j].isDir && (en.endsWith('EXP16.EXE') || en.endsWith('EXP.EXE'))) {
            isoAddEntry(langEntries[j].name, langEntries[j], _iso.expansion);
            if (en !== 'ENGEXP16.EXE') {
              isoAddEntry('ENGEXP16.EXE', langEntries[j], _iso.expansion);
            }
          }
        }
        break;
      }
    }
  }

  console.log('ISO VFS: ' + Object.keys(_iso.fileMap).length + ' files, ' +
              Object.keys(_iso.dirMap).length + ' directories');
}

/* Read a game file from ISO mounts */
async function readFromISO(gamePath) {
  var upper = gamePath.toUpperCase().replace(/\\/g, '/');
  while (upper.startsWith('./')) upper = upper.substring(2);
  var entry = _iso.fileMap[upper];
  if (!entry) return null;
  return await isoReadFileData(entry.mount, entry.sector, entry.size);
}

/* List directory entries from ISO (synchronous, for wasm_fill_entries) */
function isoListDir(gamePath) {
  var upper = gamePath.toUpperCase().replace(/\\/g, '/');
  while (upper.startsWith('./')) upper = upper.substring(2);
  if (upper.endsWith('/')) upper = upper.substring(0, upper.length - 1);
  return _iso.dirMap[upper] || null;
}

/* Scan a directory (and subdirs up to depth levels) for disc images */
async function scanForDiscImages(dirHandle, depth) {
  if (typeof depth === 'undefined') depth = 2;
  var found = [];
  var hasImageFiles = false;
  var subdirs = [];

  for await (var [name, handle] of dirHandle.entries()) {
    if (handle.kind === 'directory') {
      subdirs.push(handle);
    } else if (/\.(cue|mdf|iso|bin)$/i.test(name)) {
      hasImageFiles = true;
    }
  }

  if (hasImageFiles) found.push(dirHandle);

  if (depth > 0) {
    for (var i = 0; i < subdirs.length; i++) {
      var sub = await scanForDiscImages(subdirs[i], depth - 1);
      found = found.concat(sub);
    }
  }
  return found;
}

/* Classify a mounted ISO as base game or expansion by checking root for EXP* dirs.
   Caches root entries on mount object to avoid re-reading in isoBuildMaps. */
async function isoClassify(mount) {
  mount.rootEntries = await isoReadDir(mount, mount.rootSector, mount.rootSize);
  for (var i = 0; i < mount.rootEntries.length; i++) {
    if (mount.rootEntries[i].isDir && mount.rootEntries[i].name.toUpperCase().startsWith('EXP') &&
        mount.rootEntries[i].name.length > 3) {
      return 'expansion';
    }
  }
  return 'base';
}

/* Mount CD images: single folder selection, auto-detect base + expansion */
async function mountCDImages() {
  var btn = document.getElementById('mount-disc-btn');
  var status = document.getElementById('disc-status');
  var errorMsg = document.getElementById('error-msg');

  var dirHandle;
  try { dirHandle = await window.showDirectoryPicker({ mode: 'read' }); }
  catch(e) { return; }

  btn.disabled = true;
  btn.textContent = 'Scanning...';
  errorMsg.style.display = 'none';
  status.style.display = 'block';
  status.textContent = 'Scanning for disc images...';

  try {
    var discDirs = await scanForDiscImages(dirHandle);
    if (discDirs.length === 0) {
      errorMsg.textContent = 'No disc images (.cue/.bin, .mdf, .iso) found in selected folder or subfolders.';
      errorMsg.style.display = 'block';
      status.style.display = 'none';
      btn.disabled = false;
      btn.textContent = 'Select CD Image Folder';
      return;
    }

    status.textContent = 'Mounting disc image(s)...';

    // Mount all disc images from each directory and classify
    for (var i = 0; i < discDirs.length; i++) {
      var mounts = [];
      try {
        mounts = await findAllDiscMounts(discDirs[i]);
      } catch(e) {
        console.error('Mount failed for ' + discDirs[i].name + ':', e);
      }

      for (var j = 0; j < mounts.length; j++) {
        var type = await isoClassify(mounts[j]);
        console.log('Disc: ' + mounts[j].volumeId + ' → ' + type);

        if (type === 'expansion') {
          _iso.expansion = mounts[j];
          _iso.expDirHandle = discDirs[i];
        } else {
          _iso.base = mounts[j];
          _iso.baseDirHandle = discDirs[i];
        }
      }
    }

    if (!_iso.base && !_iso.expansion) {
      errorMsg.textContent = 'Found disc images but could not read ISO9660 filesystem.';
      errorMsg.style.display = 'block';
      status.style.display = 'none';
      btn.disabled = false;
      btn.textContent = 'Select CD Image Folder';
      return;
    }

    // If only expansion found, use it as base too (expansion DC/ has base files)
    if (!_iso.base && _iso.expansion) {
      _iso.base = _iso.expansion;
      _iso.baseDirHandle = _iso.expDirHandle;
    }

    var statusText = 'Base: ' + _iso.base.volumeId;
    if (_iso.expansion && _iso.expansion !== _iso.base) {
      statusText += ' | Expansion: ' + _iso.expansion.volumeId;
    }
    status.textContent = statusText;

    btn.textContent = 'Building file map...';
    await isoBuildMaps();

    // Write CUE file to MEMFS for CDDA
    if (_iso.baseDirHandle) {
      for await (var [name, handle] of _iso.baseDirHandle.entries()) {
        if (handle.kind === 'file' && name.toLowerCase().endsWith('.cue')) {
          var file = await handle.getFile();
          var bytes = new Uint8Array(await file.arrayBuffer());
          try { FS.writeFile(name, bytes); _iso.cuePath = name; } catch(e) {}
          break;
        }
      }
    }

    startGame();

  } catch(e) {
    console.error('mountCDImages failed:', e);
    errorMsg.textContent = 'Error: ' + e.message;
    errorMsg.style.display = 'block';
    status.style.display = 'none';
    btn.disabled = false;
    btn.textContent = 'Select CD Image Folder';
  }
}

var Module = {
  canvas: (function() { return document.getElementById('canvas'); })(),
  print: function(text) { console.log(text); },
  printErr: function(text) { console.error(text); },
  noInitialRun: true,
  onRuntimeInitialized: function() {
    console.log('WASM runtime ready');
    // ?datapath= mode: skip folder dialog, start immediately
    if (serverDataPath) {
      console.log('Server data path: ' + serverDataPath);
      startGame();
      return;
    }
    // Check browser support on load
    if (!('showDirectoryPicker' in window)) {
      document.getElementById('unsupported-msg').style.display = 'block';
      document.getElementById('select-folder-btn').disabled = true;
      document.getElementById('mount-disc-btn').disabled = true;
    }
  }
};

// Store the directory handle globally for on-demand file reads
var gameDirHandle = null;

// Resolve a file handle from a relative path (case-insensitive, recursive)
async function resolveFile(dirHandle, relativePath) {
  var parts = relativePath.replace(/\\/g, '/').split('/').filter(p => p.length > 0);
  var current = dirHandle;

  for (var i = 0; i < parts.length; i++) {
    var part = parts[i];
    if (i < parts.length - 1) {
      // Directory lookup — try getDirectoryHandle first (fast O(1)), fall back to iteration
      try { current = await current.getDirectoryHandle(part); } catch(e1) {
        try { current = await current.getDirectoryHandle(part.toUpperCase()); } catch(e2) {
          try { current = await current.getDirectoryHandle(part.toLowerCase()); } catch(e3) {
            return null;
          }
        }
      }
    } else {
      // File lookup — try getFileHandle first (fast O(1)), fall back to iteration
      try { return await current.getFileHandle(part); } catch(e1) {
        try { return await current.getFileHandle(part.toUpperCase()); } catch(e2) {
          try { return await current.getFileHandle(part.toLowerCase()); } catch(e3) {
            return null;
          }
        }
      }
    }
  }
  return null;
}

// Read a file from the game directory (called from C++ via JS)
async function readGameFile(relativePath) {
  // ISO disc image mode
  if (_iso.base || _iso.expansion) {
    var data = await readFromISO(relativePath);
    if (data) return data;
    // Fallback: raw disc file access (e.g., CUE-referenced audio BIN files)
    if (_iso.baseDirHandle) {
      var fh = await resolveFile(_iso.baseDirHandle, relativePath);
      if (fh) { var f = await fh.getFile(); return new Uint8Array(await f.arrayBuffer()); }
    }
    if (_iso.expDirHandle) {
      var fh = await resolveFile(_iso.expDirHandle, relativePath);
      if (fh) { var f = await fh.getFile(); return new Uint8Array(await f.arrayBuffer()); }
    }
    return null;
  }
  // Server fetch mode (dev/test)
  if (serverDataPath) {
    // Check file list before fetching to avoid 404 errors in browser console
    // Normalize: backslash→slash, remove leading or mid-path ./ segments, collapse //
    var key = relativePath.toLowerCase().replace(/\\/g, '/').replace(/(^|\/).\//g, '$1').replace(/\/\//g, '/');
    if (_serverFileSet && !_serverFileSet[key]) return null;
    try {
      var resp = await fetch(serverDataPath + '/' + relativePath);
      if (resp.ok) {
        // Skip directory listings: Python http.server returns HTML for directories.
        // Without this check, HTML gets saved as a file in MEMFS, corrupting the
        // directory structure and breaking opendir() for map/scenario listing.
        var ct = resp.headers.get('content-type') || '';
        if (ct.startsWith('text/html')) return null;
        return new Uint8Array(await resp.arrayBuffer());
      }
    } catch(e) {}
    return null;
  }
  // Directory handle mode (production)
  if (!gameDirHandle) return null;
  var fileHandle = await resolveFile(gameDirHandle, relativePath);
  if (!fileHandle) return null;
  var file = await fileHandle.getFile();
  var buffer = await file.arrayBuffer();
  return new Uint8Array(buffer);
}

// Extract icon from PE executable resources → ICO file bytes
function extractPEIcon(buf) {
  if (buf.length < 0x40) return null;
  var dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  if (dv.getUint16(0, true) !== 0x5A4D) return null;
  var peOff = dv.getUint32(0x3C, true);
  if (peOff + 0x18 > buf.length || dv.getUint32(peOff, true) !== 0x4550) return null;
  var numSec = dv.getUint16(peOff + 6, true);
  var secOff = peOff + 0x18 + dv.getUint16(peOff + 0x14, true);
  var rsrcRva = 0, rsrcRaw = 0;
  for (var i = 0; i < numSec; i++) {
    var s = secOff + i * 40, nm = '';
    for (var j = 0; j < 8 && buf[s+j]; j++) nm += String.fromCharCode(buf[s+j]);
    if (nm === '.rsrc') { rsrcRva = dv.getUint32(s+12,true); rsrcRaw = dv.getUint32(s+20,true); break; }
  }
  if (!rsrcRva) return null;
  function r2r(rva) { return rva - rsrcRva + rsrcRaw; }
  function parseDir(off) {
    var entries = [], n = dv.getUint16(off+12,true) + dv.getUint16(off+14,true);
    for (var i = 0; i < n; i++) {
      var e = off + 16 + i * 8, id = dv.getUint32(e,true), od = dv.getUint32(e+4,true);
      entries.push({ id: id & 0x7FFFFFFF, isDir: !!(od & 0x80000000), off: rsrcRaw + (od & 0x7FFFFFFF) });
    }
    return entries;
  }
  function getDataEntry(entry) {
    var o = entry.off;
    if (entry.isDir) { var sub = parseDir(o); if (!sub.length) return null; o = sub[0].off;
      if (sub[0].isDir) { var lang = parseDir(o); if (!lang.length) return null; o = lang[0].off; }
    }
    return { raw: r2r(dv.getUint32(o,true)), size: dv.getUint32(o+4,true) };
  }
  var types = parseDir(rsrcRaw), icons = {}, groupData = null;
  for (var t = 0; t < types.length; t++) {
    if (!types[t].isDir) continue;
    if (types[t].id === 3) { // RT_ICON
      var items = parseDir(types[t].off);
      for (var k = 0; k < items.length; k++) { var de = getDataEntry(items[k]); if (de) icons[items[k].id] = de; }
    } else if (types[t].id === 14) { // RT_GROUP_ICON
      var items = parseDir(types[t].off); if (items.length) {
        var de = getDataEntry(items[0]); if (de) groupData = buf.slice(de.raw, de.raw + de.size);
      }
    }
  }
  if (!groupData || !Object.keys(icons).length) return null;
  var gdv = new DataView(groupData.buffer, groupData.byteOffset, groupData.byteLength);
  var count = gdv.getUint16(4, true);
  if (count === 0) return null;
  var entries = [], parts = [], dataOff = 6 + count * 16;
  for (var i = 0; i < count; i++) {
    var e = 6 + i * 14, iconId = gdv.getUint16(e+12,true);
    if (!icons[iconId]) continue;
    var d = icons[iconId], idata = buf.slice(d.raw, d.raw + d.size);
    entries.push({ w:groupData[e], h:groupData[e+1], c:groupData[e+2],
      planes:gdv.getUint16(e+4,true), bpp:gdv.getUint16(e+6,true), data:idata, offset:dataOff });
    dataOff += idata.length; parts.push(idata);
  }
  var ico = new Uint8Array(dataOff), idv = new DataView(ico.buffer);
  idv.setUint16(2, 1, true); idv.setUint16(4, entries.length, true);
  for (var i = 0; i < entries.length; i++) {
    var o = 6 + i * 16;
    ico[o] = entries[i].w; ico[o+1] = entries[i].h; ico[o+2] = entries[i].c;
    idv.setUint16(o+4, entries[i].planes, true); idv.setUint16(o+6, entries[i].bpp, true);
    idv.setUint32(o+8, entries[i].data.length, true); idv.setUint32(o+12, entries[i].offset, true);
  }
  for (var i = 0; i < parts.length; i++) ico.set(new Uint8Array(parts[i].buffer || parts[i]), entries[i].offset);
  return ico;
}

// Load favicon dynamically from game EXE's PE resources
async function loadFavicon() {
  var data = await readGameFile('ENGEXP16.EXE');
  if (!data) data = await readGameFile('DC16.EXE');
  if (!data) data = await readGameFile('DKCOLONY.EXE');
  if (!data) return;
  var ico = extractPEIcon(data);
  if (!ico) return;
  var blob = new Blob([ico], {type: 'image/x-icon'});
  var link = document.querySelector('link[rel="icon"]');
  if (link) link.href = URL.createObjectURL(blob);
}

async function selectGameFolder() {
  var btn = document.getElementById('select-folder-btn');
  var loadingInfo = document.getElementById('loading-info');
  var errorMsg = document.getElementById('error-msg');
  var progressSpan = document.getElementById('loading-progress');

  try {
    gameDirHandle = await window.showDirectoryPicker({ mode: 'read' });
  } catch (e) {
    // User cancelled
    return;
  }

  // Validate: check required files exist
  var required = ['ANIM.DAT', 'FADE.DAT', 'COLOUR.SET'];
  var missing = [];

  for (var i = 0; i < required.length; i++) {
    var handle = await resolveFile(gameDirHandle, required[i]);
    if (!handle) missing.push(required[i]);
  }

  if (missing.length > 0) {
    errorMsg.textContent = 'Invalid folder. Missing: ' + missing.join(', ');
    errorMsg.style.display = 'block';
    gameDirHandle = null;
    return;
  }

  errorMsg.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Starting...';

  // VFS layer in C++ will call readGameFile() on-demand via ASYNCIFY.
  // No pre-loading needed — files are fetched when the game opens them.
  startGame();
}

function startGame() {
  var overlay = document.getElementById('overlay');
  var canvas = document.getElementById('canvas');

  overlay.style.display = 'none';
  canvas.style.display = 'block';
  canvas.focus();

  // Restore save games from IndexedDB pre-load into MEMFS
  if (_pendingSaves.length > 0) {
    try { FS.mkdir('esave'); } catch(e) {}
    for (var i = 0; i < _pendingSaves.length; i++) {
      try {
        var parts = _pendingSaves[i].path.split('/');
        var dir = '';
        for (var j = 0; j < parts.length - 1; j++) {
          dir += (dir ? '/' : '') + parts[j];
          try { FS.mkdir(dir); } catch(e) {}
        }
        FS.writeFile(_pendingSaves[i].path, _pendingSaves[i].data);
        console.log('Restored save: ' + _pendingSaves[i].path);
      } catch(e) {}
    }
    _pendingSaves = [];
  }

  // Load favicon from game EXE (fire-and-forget)
  loadFavicon();

  // Game receives "." as data path — VFS fetches files on-demand from gameDirHandle
  Module.arguments = ['.'];
  Module.callMain(Module.arguments);
}

// Programmatic fullscreen with navigationUI:"hide" + Keyboard Lock API.
// This hides the browser's "exit fullscreen" bar that blocks mouse events at the top edge,
// which prevents map scrolling in-game. navigator.keyboard.lock() puts Chrome into immersive
// mode where the notification bar is fully suppressed.
// The SDL event still propagates so F11's game function (camera/view reset) also fires.
document.addEventListener('keydown', function(e) {
  if (e.code === 'F11') {
    e.preventDefault();
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      var canvas = document.getElementById('canvas');
      if (canvas.requestFullscreen) {
        canvas.requestFullscreen({ navigationUI: 'hide' }).catch(function() {});
      }
    }
  }
}, true);

document.addEventListener('fullscreenchange', function() {
  if (document.fullscreenElement) {
    if (navigator.keyboard && navigator.keyboard.lock) {
      navigator.keyboard.lock().catch(function() {});
    }
    if (Module._setWasmFullscreen) Module._setWasmFullscreen(1);
  } else {
    if (navigator.keyboard && navigator.keyboard.unlock) {
      navigator.keyboard.unlock();
    }
    if (Module._setWasmFullscreen) Module._setWasmFullscreen(0);
    if (_edgeScrollTimer) { clearInterval(_edgeScrollTimer); _edgeScrollTimer = null; }
  }
});

// Edge scroll: when mouse leaves canvas toward the top in fullscreen,
// the browser's exit button captures pointer events. Send synthetic
// mousemove at y=0 so the game's edge-scroll detection keeps working.
var _lastMouseX = 320;
var _edgeScrollTimer = null;

document.addEventListener('pointermove', function(e) {
  _lastMouseX = e.clientX;
}, true);

document.getElementById('canvas').addEventListener('pointerleave', function(e) {
  if (!document.fullscreenElement) return;
  if (_edgeScrollTimer) { clearInterval(_edgeScrollTimer); _edgeScrollTimer = null; }
  if (e.clientY <= 5) {
    _edgeScrollTimer = setInterval(function() {
      if (!document.fullscreenElement) {
        clearInterval(_edgeScrollTimer);
        _edgeScrollTimer = null;
        return;
      }
      var c = document.getElementById('canvas');
      c.dispatchEvent(new PointerEvent('pointermove', {
        clientX: _lastMouseX, clientY: 1,
        bubbles: true, pointerType: 'mouse'
      }));
    }, 50);
  }
});

document.getElementById('canvas').addEventListener('pointerenter', function() {
  if (_edgeScrollTimer) { clearInterval(_edgeScrollTimer); _edgeScrollTimer = null; }
});
