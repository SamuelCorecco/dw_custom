const seen = new Set();
const isLocalApi = (url = '') => /^https?:\/\/(?:127\.0\.0\.1|localhost):8000(?:\/|$)/i.test(url);
function hasEmbeddedHls(url) {
  try {
    const value = new URL(url).searchParams.get('m3u8');
    return Boolean(value && /^https?:$/.test(new URL(value).protocol) && !isLocalApi(value));
  } catch {
    return false;
  }
}
const isM3U8 = (url = '') => !isLocalApi(url) && (
  /(?:\.m3u8)(?:$|[?#&])/i.test(url) ||
  /(?:format|type|mime)[=/:%-]*(?:application\/)?(?:x-)?mpegurl/i.test(url) ||
  hasEmbeddedHls(url)
);

function report(url) {
  if (!url || !isM3U8(url) || seen.has(url)) return;
  seen.add(url);
  chrome.runtime.sendMessage({ type: 'M3U8_FOUND', url, pageUrl: location.href });
}

function scanResources(entries = performance.getEntriesByType('resource')) {
  entries.forEach(entry => report(entry.name));
}

function scanMediaElements(root = document) {
  root.querySelectorAll?.('video[src], audio[src], source[src], iframe[src]').forEach(element => report(element.currentSrc || element.src));
}

report(location.href);
scanResources();
scanMediaElements();

const resourceObserver = new PerformanceObserver(list => scanResources(list.getEntries()));
resourceObserver.observe({ type: 'resource', buffered: true });

const domObserver = new MutationObserver(records => {
  records.forEach(record => {
    if (record.type === 'attributes') {
      report(record.target.currentSrc || record.target.src);
      return;
    }
    record.addedNodes.forEach(node => {
    if (node.nodeType === Node.ELEMENT_NODE) {
      report(node.currentSrc || node.src);
      scanMediaElements(node);
    }
    });
  });
});
domObserver.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });

window.addEventListener('pagehide', () => {
  resourceObserver.disconnect();
  domObserver.disconnect();
});
