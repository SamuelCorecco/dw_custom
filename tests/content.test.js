const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../extension/content.js'), 'utf8');
const wrapper = rendition => `https://ani3.api-webs.com/player.php?m3u8=${encodeURIComponent(`https://vixcloud.cc/playlist/793409?token=test&rendition=${rendition}`)}`;

function loadContent(url, frames = []) {
  const messages = [];
  let mutations;
  vm.runInNewContext(source, {
    URL, Node: { ELEMENT_NODE: 1 },
    location: { href: url },
    document: { documentElement: {}, querySelectorAll: () => frames },
    performance: { getEntriesByType: () => [] },
    chrome: { runtime: { sendMessage(message) { messages.push(message); } } },
    PerformanceObserver: class { observe() {} disconnect() {} },
    MutationObserver: class {
      constructor(callback) { mutations = callback; }
      observe() {} disconnect() {}
    },
    window: { addEventListener() {} }
  });
  return { messages, mutate: records => mutations(records) };
}

test('finds encoded playlists in iframe sources and later src changes', () => {
  const content = loadContent('https://animeunity.at/watch', [{ src: wrapper('master') }]);
  assert.equal(content.messages[0].url, wrapper('master'));
  content.mutate([{ type: 'attributes', target: { src: wrapper('1080p') } }]);
  assert.equal(content.messages[1].url, wrapper('1080p'));
});

test('reports the current iframe document even before the media is requested', () => {
  const content = loadContent(wrapper('master'));
  assert.equal(content.messages.length, 1);
  assert.equal(content.messages[0].url, wrapper('master'));
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../extension/manifest.json'), 'utf8'));
  assert.equal(manifest.content_scripts[0].all_frames, true);
});
