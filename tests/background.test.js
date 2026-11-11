const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { setImmediate } = require('node:timers/promises');

const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');
const pageUrl = 'https://example.com/watch';
const masterUrl = 'https://vixcloud.cc/playlist/772755?token=test&expires=1798129090&h=1';
const renditionUrl = 'https://vixcloud.cc/playlist/772755?type=video&rendition=720p&token=test&edge=au-u3-01';

function createEvent() {
  return {
    addListener(listener) { this.listener = listener; },
    async emit(...args) {
      await this.listener(...args);
      // Let asynchronous saves finish after the non-blocking webRequest listener.
      await setImmediate();
    }
  };
}

function createBackground() {
  const stored = {};
  const webRequest = Object.fromEntries([
    'onBeforeSendHeaders', 'onHeadersReceived', 'onCompleted', 'onErrorOccurred'
  ].map(name => [name, createEvent()]));
  const onMessage = createEvent();
  const chrome = {
    storage: { session: {
      async get() { return stored; },
      async set(value) { Object.assign(stored, JSON.parse(JSON.stringify(value))); }
    } },
    webRequest,
    tabs: { onUpdated: createEvent(), onRemoved: createEvent() },
    runtime: { onMessage }
  };
  vm.runInNewContext(source, { chrome, console, URL });
  return {
    webRequest,
    stored,
    async reportFromFrame(url, frameUrl) {
      return new Promise(resolve => onMessage.listener({
        type: 'M3U8_FOUND', url, pageUrl: frameUrl
      }, { tab: { id: 7, url: pageUrl }, frameId: 3 }, resolve));
    },
    async streams() {
      const result = await new Promise(resolve => onMessage.listener({
        type: 'GET_M3U8', tabId: 7, pageUrl
      }, {}, resolve));
      return JSON.parse(JSON.stringify(result));
    }
  };
}

async function receive(background, url, contentType, overrides = {}) {
  const details = { tabId: 7, requestId: url, type: 'xmlhttprequest', url, ...overrides };
  await background.webRequest.onBeforeSendHeaders.emit({
    ...details,
    requestHeaders: [
      { name: 'Referer', value: pageUrl },
      { name: 'Cookie', value: 'session=test' },
      { name: 'X-Unrelated', value: 'omit' }
    ]
  });
  await background.webRequest.onHeadersReceived.emit({
    ...details, responseHeaders: [{ name: 'Content-Type', value: contentType }]
  });
  await background.webRequest.onCompleted.emit(details);
}

test('keeps extensionless master and rendition playlists, including request headers', async () => {
  const background = createBackground();
  await receive(background, masterUrl, 'application/vnd.apple.mpegurl');
  await receive(background, renditionUrl, 'application/vnd.apple.mpegurl');

  const streams = await background.streams();
  assert.deepEqual(streams.map(stream => stream.url), [masterUrl, renditionUrl]);
  for (const stream of streams) {
    assert.equal(stream.via, 'content-type');
    assert.equal(stream.contentType, 'application/vnd.apple.mpegurl');
    assert.deepEqual(stream.headers, { Referer: pageUrl, Cookie: 'session=test' });
  }
  assert.ok(background.stored.mediaByTab[7].streams[masterUrl]);
});

test('accepts HLS MIME variants, case differences and charset parameters', async () => {
  const background = createBackground();
  const types = ['application/x-mpegURL', 'audio/mpegurl', ' Audio/X-MPEGURL; charset=UTF-8 '];
  for (const [index, contentType] of types.entries()) {
    await receive(background, `https://example.com/stream/${index}`, contentType, { type: 'media' });
  }
  assert.equal((await background.streams()).length, types.length);
});

test('still detects named m3u8 playlists with missing or generic MIME types', async () => {
  const background = createBackground();
  const url = 'https://example.com/master.m3u8?token=test';
  await receive(background, url, 'application/octet-stream', { type: 'other' });
  const [stream] = await background.streams();
  assert.equal(stream.url, url);
  assert.equal(stream.headers.Referer, pageUrl);
});

test('ignores segments, analytics, HTML playlists, local API responses and unowned requests', async () => {
  const background = createBackground();
  await receive(background, 'https://example.com/0001.ts?token=test', 'video/mp2t');
  await receive(background, 'https://analytics.vixcloud.cc/api/event', 'application/json');
  await receive(background, masterUrl, 'text/html');
  await receive(background, 'http://127.0.0.1:8000/master.m3u8', 'application/vnd.apple.mpegurl');
  await receive(background, masterUrl, 'application/vnd.apple.mpegurl', { tabId: -1 });
  assert.deepEqual(await background.streams(), []);
});

test('removes captured headers when a request fails', async () => {
  const background = createBackground();
  const details = { tabId: 7, requestId: 'failed-request', type: 'xmlhttprequest', url: masterUrl };
  await background.webRequest.onBeforeSendHeaders.emit({
    ...details, requestHeaders: [{ name: 'Cookie', value: 'stale-cookie' }]
  });
  await background.webRequest.onErrorOccurred.emit(details);
  await background.webRequest.onHeadersReceived.emit({
    ...details, responseHeaders: [{ name: 'Content-Type', value: 'application/vnd.apple.mpegurl' }]
  });
  const [stream] = await background.streams();
  assert.deepEqual(stream.headers, {});
});

test('captures a player URL with an encoded extensionless playlist and generic MIME type', async () => {
  const background = createBackground();
  const url = `https://ani3.api-webs.com/player.php?m3u8=${encodeURIComponent(masterUrl)}`;
  await receive(background, url, 'text/plain', { type: 'sub_frame' });
  const [stream] = await background.streams();
  assert.equal(stream.url, url);
  assert.equal(stream.headers.Referer, pageUrl);
});

test('associates iframe reports with the parent tab without clearing its other streams', async () => {
  const background = createBackground();
  await receive(background, masterUrl, 'application/vnd.apple.mpegurl');
  const wrapper = `https://ani3.api-webs.com/player.php?m3u8=${encodeURIComponent(renditionUrl)}`;
  await background.reportFromFrame(wrapper, wrapper);
  assert.equal((await background.streams()).length, 2);
  assert.equal(background.stored.mediaByTab[7].pageUrl, pageUrl);
});

test('ignores invalid embedded URLs and embedded local API URLs', async () => {
  const background = createBackground();
  for (const value of ['javascript:alert(1)', 'not-a-url', 'http://127.0.0.1:8000/playlist']) {
    await receive(background, `https://example.com/player?m3u8=${encodeURIComponent(value)}`, 'text/html');
  }
  assert.deepEqual(await background.streams(), []);
});
