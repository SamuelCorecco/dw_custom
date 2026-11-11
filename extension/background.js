const STORAGE_KEY = 'mediaByTab';
const mediaByTab = new Map();
const requestHeaders = new Map();
const storageArea = chrome.storage.session || chrome.storage.local;

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
const isHlsContentType = (contentType = '') => /^(?:application|audio)\/(?:vnd\.apple\.|x-)?mpegurl(?:\s*;|$)/i.test(contentType.trim());

async function restoreState() {
  try {
    const stored = await storageArea.get(STORAGE_KEY);
    Object.entries(stored[STORAGE_KEY] || {}).forEach(([tabId, state]) => {
      mediaByTab.set(Number(tabId), state);
    });
  } catch (error) {
    console.warn('[Vela] Impossibile ripristinare la sessione media', error);
  }
}

const ready = restoreState();

function serializableState() {
  return Object.fromEntries(mediaByTab.entries());
}

async function persistState() {
  try {
    await storageArea.set({ [STORAGE_KEY]: serializableState() });
  } catch (error) {
    console.warn('[Vela] Impossibile salvare la sessione media', error);
  }
}

function cleanHeaders(headers = []) {
  const allowed = new Set(['authorization', 'cookie', 'origin', 'referer', 'user-agent', 'accept', 'accept-language']);
  return Object.fromEntries(
    headers
      .filter(header => allowed.has(header.name.toLowerCase()) && header.value)
      .map(header => [header.name, header.value])
  );
}

async function save(tabId, url, meta = {}) {
  if (tabId == null || tabId < 0 || isLocalApi(url)) return;
  if (!isM3U8(url) && !isHlsContentType(meta.contentType)) return;
  await ready;

  const current = mediaByTab.get(tabId) || { pageUrl: meta.pageUrl || '', streams: {}, dismissed: {}, dismissAll: false };
  if (meta.pageUrl && current.pageUrl && current.pageUrl !== meta.pageUrl) {
    current.streams = {};
    current.dismissed = {};
    current.dismissAll = false;
  }
  current.pageUrl = meta.pageUrl || current.pageUrl;
  current.streams[url] = {
    ...(current.streams[url] || {}),
    url,
    detectedAt: Date.now(),
    via: meta.via || 'network',
    contentType: meta.contentType || current.streams[url]?.contentType || '',
    headers: { ...(current.streams[url]?.headers || {}), ...(meta.headers || {}) }
  };
  mediaByTab.set(tabId, current);
  await persistState();
}

async function clearTab(tabId, pageUrl = '') {
  await ready;
  mediaByTab.set(tabId, { pageUrl, streams: {}, dismissed: {}, dismissAll: false });
  await persistState();
}

async function dismissMedia(tabId, pageUrl, url, all = false) {
  await ready;
  const current = mediaByTab.get(tabId) || { pageUrl, streams: {}, dismissed: {}, dismissAll: false };
  current.dismissed = current.dismissed || {};
  if (all) current.dismissAll = true;
  else current.dismissed[url] = true;
  mediaByTab.set(tabId, current);
  await persistState();
}

chrome.webRequest.onBeforeSendHeaders.addListener(
  details => {
    // The response MIME type can identify HLS even when the URL has no extension.
    if (details.tabId >= 0 && !isLocalApi(details.url) && (
      isM3U8(details.url) || ['xmlhttprequest', 'media'].includes(details.type)
    )) {
      requestHeaders.set(details.requestId, cleanHeaders(details.requestHeaders));
    }
  },
  { urls: ['<all_urls>'] },
  ['requestHeaders', 'extraHeaders']
);

chrome.webRequest.onCompleted.addListener(
  details => {
    if (isM3U8(details.url)) {
      save(details.tabId, details.url, {
        via: 'network',
        headers: requestHeaders.get(details.requestId) || {}
      });
    }
    requestHeaders.delete(details.requestId);
  },
  { urls: ['<all_urls>'] }
);

chrome.webRequest.onHeadersReceived.addListener(
  details => {
    const contentType = details.responseHeaders?.find(header => header.name.toLowerCase() === 'content-type')?.value || '';
    if (isHlsContentType(contentType)) {
      save(details.tabId, details.url, {
        via: 'content-type',
        contentType,
        headers: requestHeaders.get(details.requestId) || {}
      });
    }
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders', 'extraHeaders']
);

chrome.webRequest.onErrorOccurred.addListener(
  details => requestHeaders.delete(details.requestId),
  { urls: ['<all_urls>'] }
);

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'loading' || changeInfo.url) {
    clearTab(tabId, changeInfo.url || tab.url || '');
  }
});

chrome.tabs.onRemoved.addListener(async tabId => {
  await ready;
  mediaByTab.delete(tabId);
  await persistState();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      await ready;
      if (message.type === 'M3U8_FOUND') {
        await save(sender.tab?.id, message.url, {
          via: 'page',
          pageUrl: sender.tab?.url || message.pageUrl || ''
        });
        sendResponse({ ok: true });
        return;
      }

      if (message.type === 'GET_M3U8') {
        const state = mediaByTab.get(message.tabId);
        const samePage = state && (!message.pageUrl || !state.pageUrl || state.pageUrl === message.pageUrl);
        sendResponse(samePage && !state.dismissAll
          ? Object.values(state.streams || {}).filter(stream => !state.dismissed?.[stream.url])
          : []);
        return;
      }

      if (message.type === 'IS_DISMISSED') {
        const state = mediaByTab.get(message.tabId);
        const samePage = state && (!message.pageUrl || !state.pageUrl || state.pageUrl === message.pageUrl);
        sendResponse(Boolean(samePage && (state.dismissAll || state.dismissed?.[message.url])));
        return;
      }

      if (message.type === 'DISMISS_MEDIA') {
        await dismissMedia(message.tabId, message.pageUrl || '', message.url, message.all);
        sendResponse({ ok: true });
        return;
      }

      if (message.type === 'RESET_M3U8') {
        await clearTab(message.tabId, message.pageUrl || '');
        sendResponse({ ok: true });
        return;
      }

      sendResponse({ ok: false, error: 'Messaggio non riconosciuto' });
    } catch (error) {
      console.error('[Vela] Errore messaggio background', error);
      sendResponse({ ok: false, error: error?.message || 'Errore background' });
    }
  })();
  return true;
});
