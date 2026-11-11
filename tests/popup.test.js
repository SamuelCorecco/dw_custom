const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function loadPopup(extraGlobals = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../extension/popup.js'), 'utf8');
  const elements = new Map();
  const context = vm.createContext({
    document: { getElementById(id) {
      if (!elements.has(id)) {
        const classes = new Set();
        elements.set(id, {
          classList: {
            add: name => classes.add(name), remove: name => classes.delete(name),
            contains: name => classes.has(name),
            toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }
          },
          replaceChildren() {}, addEventListener() {}
        });
      }
      return elements.get(id);
    } },
    window: { addEventListener() {} },
    console, URL, ...extraGlobals
  });
  vm.runInContext(source.replace(/^init\(\);$/m, ''), context);
  return context;
}

const master = { url: 'https://cdn.example.com/playlist/123?token=test', detectedAt: 1 };
const low = { url: 'https://cdn.example.com/video/480p/index.m3u8', detectedAt: 2 };
const qualities = [
  { program_id: 2, width: 1920, height: 1080, bandwidth: 4500000 },
  { program_id: 1, width: 1280, height: 720, bandwidth: 1800000 },
  { program_id: 0, width: 854, height: 480, bandwidth: 1080000 }
];

test('analyzes every captured playlist instead of keeping only the most recent variant', () => {
  const popup = loadPopup();
  const selected = popup.selectStreams([master, low, master]);
  assert.equal(selected.length, 2);
  assert.ok(selected.some(stream => stream.url === master.url));
});

test('shows the master with its qualities and removes only its known child playlists', () => {
  const popup = loadPopup();
  const unrelated = { url: 'https://other.example.com/index.m3u8' };
  const selected = popup.selectAnalyzedStreams([
    { stream: low, info: { height: 480, qualities: [qualities[2]] } },
    { stream: master, info: { height: 1080, qualities, child_playlists: [low.url] } },
    { stream: unrelated, info: { height: 720, qualities: [qualities[1]] } }
  ]);
  assert.equal(selected.length, 2);
  assert.equal(selected[0].stream.url, master.url);
  assert.ok(selected.some(item => item.stream.url === unrelated.url));
});

test('keeps independently detected renditions when no master was captured', () => {
  const popup = loadPopup();
  const high = { url: 'https://cdn.example.com/video/1080p/index.m3u8' };
  const selected = popup.selectAnalyzedStreams([
    { stream: low, info: { height: 480, qualities: [qualities[2]] } },
    { stream: high, info: { height: 1080, qualities: [qualities[0]] } }
  ]);
  assert.equal(selected.length, 2);
  assert.equal(selected[0].stream.url, high.url);
});

test('groups an encoded player rendition under its master playlist', () => {
  const popup = loadPopup();
  const wrappedLow = { url: `https://ani3.api-webs.com/player.php?m3u8=${encodeURIComponent(low.url)}` };
  const selected = popup.selectAnalyzedStreams([
    { stream: master, info: { height: 1080, qualities, child_playlists: [low.url] } },
    { stream: wrappedLow, info: { height: 480, qualities: [qualities[2]] } }
  ]);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].stream.url, master.url);
});

test('does not hide a verified rendition behind a parent that failed video analysis', () => {
  const popup = loadPopup();
  const selected = popup.selectAnalyzedStreams([
    { stream: master, info: { qualities: [], child_playlists: [low.url] } },
    { stream: low, info: { height: 480, qualities: [qualities[2]] } }
  ]);
  assert.ok(selected.some(item => item.stream.url === low.url));
  assert.ok(selected.some(item => item.stream.url === master.url));
});

test('keeps a visible playlist when proxy references include itself or form a cycle', () => {
  const popup = loadPopup();
  const selected = popup.selectAnalyzedStreams([
    { stream: master, info: { height: 1080, qualities, child_playlists: [master.url, low.url] } },
    { stream: low, info: { height: 480, qualities: [qualities[2]], child_playlists: [master.url] } }
  ]);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].stream.url, master.url);
});

test('does not let a confirmed audio playlist hide a video rendition', () => {
  const popup = loadPopup();
  const selected = popup.selectAnalyzedStreams([
    { stream: master, info: { qualities: [], has_video: false, has_audio: true, child_playlists: [low.url] } },
    { stream: low, info: { height: 480, qualities: [qualities[2]] } }
  ]);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].stream.url, low.url);
});

test('updates an already open empty popup when storage receives detected streams', async () => {
  let onChanged;
  let streams = [];
  const popup = loadPopup({ chrome: {
    storage: { onChanged: { addListener(callback) { onChanged = callback; } } },
    runtime: { sendMessage(message, callback) { callback(streams); } }
  } });
  vm.runInContext("currentTab = { id: 7, url: 'https://example.com/watch' }; globalThis.renders = []; showStreams = async streams => renders.push(streams);", popup);
  popup.setupMediaUpdates();
  await popup.refreshDetectedMedia(true);
  assert.equal(popup.document.getElementById('empty-state').classList.contains('hidden'), false);
  streams = [master, low];
  onChanged({ mediaByTab: {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(popup.renders.length, 1);
  assert.equal(popup.renders[0].length, 2);
  assert.equal(popup.document.getElementById('empty-state').classList.contains('hidden'), true);
  streams = [{ ...master, detectedAt: 999 }, low];
  onChanged({ mediaByTab: {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(popup.renders.length, 1, 'unchanged manifests should not be analyzed again');
});

test('applies detections that arrive while the previous metadata is still loading', async () => {
  let onChanged;
  let streams = [master];
  let release;
  const popup = loadPopup({ chrome: {
    storage: { onChanged: { addListener(callback) { onChanged = callback; } } },
    runtime: { sendMessage(message, callback) { callback(streams); } }
  }, waitForFirstRender: () => new Promise(resolve => { release = resolve; }) });
  vm.runInContext("currentTab = { id: 7, url: 'https://example.com/watch' }; globalThis.renders = []; showStreams = async streams => { renders.push(streams); if (renders.length === 1) await waitForFirstRender(); };", popup);
  popup.setupMediaUpdates();
  const initial = popup.refreshDetectedMedia(true);
  await new Promise(resolve => setImmediate(resolve));
  streams = [master, low];
  onChanged({ mediaByTab: {} });
  release();
  await initial;
  assert.equal(popup.renders.length, 2);
  assert.equal(popup.renders[1].length, 2);
});

test('shows background communication errors instead of claiming that no streams exist', async () => {
  const popup = loadPopup({ chrome: { runtime: {
    lastError: { message: 'Service worker non disponibile' },
    sendMessage(message, callback) { callback(); }
  } } });
  vm.runInContext("currentTab = { id: 7, url: 'https://example.com/watch' };", popup);
  await popup.refreshDetectedMedia(true);
  const error = popup.document.getElementById('error');
  assert.equal(error.textContent, 'Service worker non disponibile');
  assert.equal(error.classList.contains('hidden'), false);
});

test('shows a copyable error detail instead of relying only on a tooltip', () => {
  const popup = loadPopup();
  const card = { querySelector: selector => popup.document.getElementById(selector) };
  card.querySelector('[data-role="error-details"]').classList.add('hidden');
  popup.applyStreamMetadata(card, { ...master }, { error: 'PHP segment format rejected' });
  assert.equal(card.querySelector('[data-role="source-status"]').textContent, 'Analisi non riuscita');
  assert.equal(card.querySelector('[data-role="error-text"]').textContent, 'PHP segment format rejected');
  assert.equal(card.querySelector('[data-role="error-details"]').classList.contains('hidden'), false);
});

test('defaults to 1080p and sends the chosen 720p quality with the requested filename', async () => {
  const popup = loadPopup();
  const nodes = new Map();
  const card = { querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      classList: { add() {}, remove() {} },
      addEventListener(event, callback) { this[event] = callback; }
    });
    return nodes.get(selector);
  } };
  const stream = { ...master, headers: { Referer: 'https://example.com/watch' } };
  popup.applyStreamMetadata(card, stream, { height: 1080, resolution: '1920x1080', qualities });
  assert.equal(stream.quality.height, 1080);
  const selector = card.querySelector('[data-role="quality"]');
  assert.ok(selector.innerHTML.indexOf('1920×1080') < selector.innerHTML.indexOf('854×480'));
  selector.value = '1';
  selector.change();
  vm.runInContext("requestStreamFilename = async () => 'Episodio scelto'; queueRequest = async (button, path, payload) => { globalThis.queued = { path, payload }; };", popup);
  await popup.queueStream({}, stream);
  assert.equal(popup.queued.path, '/download-m3u8');
  assert.equal(popup.queued.payload.program_id, 1);
  assert.equal(popup.queued.payload.height, 720);
  assert.equal(popup.queued.payload.filename, 'Episodio scelto');
  assert.equal(popup.queued.payload.headers.Referer, stream.headers.Referer);
});
