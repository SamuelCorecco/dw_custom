const API_URL = 'http://127.0.0.1:8000';

const dom = {
  serverStatus: document.getElementById('server-status'),
  serverLabel: document.getElementById('server-label'),
  loading: document.getElementById('loading'),
  error: document.getElementById('error'),
  empty: document.getElementById('empty-state'),
  videos: document.getElementById('videos-list'),
  streams: document.getElementById('m3u8-list'),
  activeDownloads: document.getElementById('active-downloads'),
  completedDownloads: document.getElementById('completed-downloads'),
  reset: document.getElementById('reset-btn'),
  clearHistory: document.getElementById('clear-history-btn'),
  mediaCount: document.getElementById('media-count'),
  downloadCount: document.getElementById('download-count'),
  activeCount: document.getElementById('active-count'),
  historyCount: document.getElementById('history-count'),
  sourceLabel: document.getElementById('source-label'),
  filenameDialog: document.getElementById('stream-name-dialog'),
  filenameInput: document.getElementById('stream-filename'),
  cancelFilename: document.getElementById('cancel-stream-name')
};

let currentTab = null;
let downloadsInterval = null;
let mediaTotal = 0;
let mediaSignature = null;
let mediaRefreshInProgress = false;
let mediaRefreshPending = false;

const icon = name => `<svg aria-hidden="true"><use href="#icon-${name}"></use></svg>`;

async function init() {
  setupTabs();
  setupActions();
  setupMediaUpdates();
  const online = await checkServer();
  if (!online) {
    showError('Il server locale non risponde. Avvia start.command e riprova.');
    return;
  }
  await loadMedia();
  startDownloadsMonitoring();
}

function setupTabs() {
  document.querySelectorAll('.tab-button').forEach(button => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.tab-button').forEach(item => item.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(item => item.classList.remove('active'));
      button.classList.add('active');
      document.getElementById(`tab-${button.dataset.tab}`).classList.add('active');
    });
  });
}

function setupActions() {
  dom.reset.addEventListener('click', resetDetectedMedia);
  dom.clearHistory.addEventListener('click', clearHistory);
  dom.cancelFilename.addEventListener('click', () => dom.filenameDialog.close());
}

function setupMediaUpdates() {
  chrome.storage.onChanged.addListener(changes => {
    if (changes.mediaByTab) void refreshDetectedMedia();
  });
}

async function refreshDetectedMedia(force = false) {
  if (!currentTab?.url || isYouTube(currentTab.url)) return;
  if (mediaRefreshInProgress) {
    mediaRefreshPending = true;
    return;
  }
  mediaRefreshInProgress = true;
  try {
    do {
      mediaRefreshPending = false;
      const streams = await sendRuntimeMessage({
        type: 'GET_M3U8', tabId: currentTab.id, pageUrl: currentTab.url
      });
      if (!Array.isArray(streams)) throw new Error(streams?.error || 'Impossibile leggere i media rilevati.');
      const signature = JSON.stringify(streams.map(stream => [
        stream.url, stream.contentType || '', Object.entries(stream.headers || {}).sort()
      ]).sort((a, b) => a[0].localeCompare(b[0])));
      if (!force && signature === mediaSignature) continue;
      force = false;
      resetMediaUI();
      setLoading(true);
      if (streams.length) await showStreams(streams);
      else showEmpty();
      mediaSignature = signature;
    } while (mediaRefreshPending);
  } catch (error) {
    showError(normalizeError(error));
  } finally {
    mediaRefreshInProgress = false;
    setLoading(false);
  }
}

async function fetchJson(path, options = {}, timeout = 20000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(`${API_URL}${path}`, { ...options, signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `Errore HTTP ${response.status}`);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function sendRuntimeMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, response => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError) reject(new Error(runtimeError.message));
      else resolve(response);
    });
  });
}

async function safeRuntimeMessage(message, fallback) {
  try {
    const response = await sendRuntimeMessage(message);
    return response?.error ? fallback : response;
  } catch (error) {
    console.warn('[Vela] Messaggio background non disponibile', error);
    return fallback;
  }
}

async function checkServer() {
  try {
    const state = await fetchJson('/', {}, 3000);
    dom.serverStatus.classList.add('online');
    dom.serverStatus.title = `${state.running || 0} attivi, ${state.queued || 0} in coda`;
    dom.serverLabel.textContent = 'Online';
    return true;
  } catch {
    dom.serverStatus.classList.remove('online');
    dom.serverStatus.title = 'Server non raggiungibile';
    dom.serverLabel.textContent = 'Offline';
    return false;
  }
}

async function loadMedia() {
  setLoading(true);
  resetMediaUI();
  try {
    [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!currentTab?.url) throw new Error('La pagina corrente non è accessibile.');
    dom.sourceLabel.textContent = hostname(currentTab.url);

    if (isYouTube(currentTab.url)) {
      const dismissed = await safeRuntimeMessage({
        type: 'IS_DISMISSED',
        tabId: currentTab.id,
        pageUrl: currentTab.url,
        url: currentTab.url
      }, false);
      if (dismissed) showEmpty();
      else await loadYouTube(currentTab.url);
    } else {
      await refreshDetectedMedia(true);
    }
  } catch (error) {
    showError(normalizeError(error));
  } finally {
    setLoading(false);
  }
}

async function loadYouTube(url) {
  const info = await fetchJson('/video-info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  }, 45000);
  if (info.error) throw new Error(info.error);
  dom.videos.appendChild(createYouTubeCard(info, url));
  dom.videos.classList.remove('hidden');
  setMediaCount(1);
}

function createYouTubeCard(info, url) {
  const card = document.createElement('article');
  card.className = 'media-card';
  const format = info.best_format || {};
  const playlist = info.playlist || {};
  const playlistLabel = playlist.count ? `Playlist · ${playlist.count}` : 'Playlist';
  const thumbnail = info.thumbnail
    ? `<img src="${escapeHtml(info.thumbnail)}" alt="">`
    : icon('media');
  const cacheBadge = info.cached ? '<span class="meta-pill cache-pill">Cache</span>' : '';

  card.innerHTML = `
    <div class="media-main">
      <div class="media-thumb">
        ${thumbnail}
        <span class="media-badge">YOUTUBE</span>
        <span class="media-duration">${escapeHtml(info.duration_string || '--:--')}</span>
      </div>
      <div class="media-copy">
        <div class="media-title">${escapeHtml(info.title || 'Video YouTube')}</div>
        <div class="media-meta">
          <span class="meta-pill">${escapeHtml(format.resolution || 'Migliore')}</span>
          ${info.uploader ? `<span>${escapeHtml(truncate(info.uploader, 24))}</span>` : ''}
          ${cacheBadge}
        </div>
      </div>
    </div>
    <div class="media-actions ${playlist.available ? 'three' : ''}">
      <button class="action-button primary" data-action="video" type="button">${icon('download')}<span>Video</span></button>
      <button class="action-button" data-action="audio" type="button">${icon('audio')}<span>Audio</span></button>
      ${playlist.available ? `<button class="action-button playlist" data-action="playlist" type="button">${icon('playlist')}<span>${escapeHtml(playlistLabel)}</span></button>` : ''}
    </div>`;

  card.querySelector('[data-action="video"]').addEventListener('click', event => queueYouTube(event.currentTarget, url, format.format_id || 'best', 'video', false));
  card.querySelector('[data-action="audio"]').addEventListener('click', event => queueYouTube(event.currentTarget, url, 'best', 'audio', false));
  card.querySelector('[data-action="playlist"]')?.addEventListener('click', event => queueYouTube(event.currentTarget, url, 'best', 'video', true));
  return card;
}

async function showStreams(streams) {
  const analyzed = await runWithConcurrency(selectStreams(streams), 2, async stream => {
    try {
      const info = await fetchJson('/m3u8-info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: stream.url, headers: stream.headers || {} })
      }, 45000);
      return { stream, info };
    } catch (error) {
      return { stream, info: { error: normalizeError(error) } };
    }
  });
  const selected = selectAnalyzedStreams(analyzed);
  if (!selected.length) {
    showEmpty();
    return;
  }
  selected.forEach(({ stream, info }, index) => {
    const card = createStreamCard(stream, index);
    dom.streams.appendChild(card);
    applyStreamMetadata(card, stream, info);
  });
  dom.streams.classList.remove('hidden');
  setMediaCount(selected.length);
}

function selectStreams(streams) {
  const unique = [...new Map(streams.filter(item => item?.url).map(item => [item.url, item])).values()];
  return unique
    .sort((a, b) => streamScore(b) - streamScore(a) || (b.detectedAt || 0) - (a.detectedAt || 0));
}

function selectAnalyzedStreams(analyzed) {
  const candidates = analyzed
    .filter(({ info }) => !(info?.has_video === false && info?.has_audio === true && !info?.error))
    .sort((a, b) => (b.info?.qualities?.length || 0) - (a.info?.qualities?.length || 0)
      || (b.info?.height || 0) - (a.info?.height || 0)
      || streamScore(b.stream) - streamScore(a.stream));
  const selected = [];
  const hidden = new Set();
  for (const candidate of candidates) {
    const identity = streamIdentity(candidate.info?.source_url || candidate.stream.url);
    if (hidden.has(identity)) continue;
    selected.push(candidate);
    hidden.add(identity);
    // Only a visible, verified video playlist can replace its child renditions.
    if (!candidate.info?.error && candidate.info?.qualities?.length) {
      (candidate.info.child_playlists || []).forEach(url => hidden.add(streamIdentity(url)));
    }
  }
  return selected;
}

function streamIdentity(url) {
  try {
    const embedded = new URL(url).searchParams.get('m3u8');
    return embedded && /^https?:$/.test(new URL(embedded).protocol) ? embedded : url;
  } catch {
    return url;
  }
}

function streamScore(stream) {
  const url = stream.url.toLowerCase();
  return (url.includes('master') ? 8 : 0) + (url.includes('index') ? 4 : 0) + (stream.via === 'content-type' ? 2 : 0);
}

function createStreamCard(stream, index) {
  const card = document.createElement('article');
  card.className = 'media-card';
  card.innerHTML = `
    <div class="media-main">
      <div class="media-thumb skeleton" data-role="thumb">
        ${icon('stream')}
        <span class="media-badge">HLS</span>
        <span class="media-duration" data-role="duration">--:--</span>
      </div>
      <div class="media-copy">
        <div class="media-title">Stream ${index + 1} · ${escapeHtml(hostname(stream.url))}</div>
        <div class="media-meta">
          <span class="meta-pill" data-role="resolution">Analisi</span>
          <select class="quality-select hidden" data-role="quality" aria-label="Qualità del video"></select>
          <span data-role="source-status">${escapeHtml(stream.via === 'content-type' ? 'Manifest verificato' : 'Rilevato nella pagina')}</span>
          <span class="meta-pill cache-pill hidden" data-role="cache">Cache</span>
        </div>
      </div>
    </div>
    <div class="media-actions">
      <button class="action-button primary" data-action="stream" type="button">${icon('download')}<span>Scarica stream</span></button>
      <button class="action-button" data-action="copy" type="button">${icon('stream')}<span>Copia URL</span></button>
    </div>
    <details class="stream-error hidden" data-role="error-details">
      <summary>Dettagli errore</summary>
      <pre data-role="error-text"></pre>
      <button class="text-button" data-action="copy-error" type="button">Copia errore</button>
    </details>`;
  card.querySelector('[data-action="stream"]').addEventListener('click', event => queueStream(event.currentTarget, stream));
  card.querySelector('[data-action="copy"]').addEventListener('click', async event => {
    await navigator.clipboard.writeText(stream.url);
    const label = event.currentTarget.querySelector('span');
    label.textContent = 'Copiato';
    setTimeout(() => { label.textContent = 'Copia URL'; }, 1200);
  });
  card.querySelector('[data-action="copy-error"]').addEventListener('click', async event => {
    await navigator.clipboard.writeText(card.querySelector('[data-role="error-text"]').textContent);
    const button = event.currentTarget;
    button.textContent = 'Copiato';
    setTimeout(() => { button.textContent = 'Copia errore'; }, 1200);
  });
  return card;
}

function applyStreamMetadata(card, stream, info) {
  try {
    if (!info) throw new Error('Metadati non disponibili');
    stream.downloadUrl = info.source_url || stream.url;
    stream.downloadHeaders = info.source_headers || stream.headers || {};
    card.querySelector('[data-role="duration"]').textContent = info.duration_string || '--:--';
    card.querySelector('[data-role="resolution"]').textContent = info.resolution || 'Auto';
    const qualities = info.qualities || [];
    if (info.error) {
      const status = card.querySelector('[data-role="source-status"]');
      status.textContent = 'Analisi non riuscita';
      status.title = info.error;
      card.querySelector('[data-role="error-text"]').textContent = info.error;
      card.querySelector('[data-role="error-details"]').classList.remove('hidden');
    }
    stream.quality = qualities[0] || null;
    if (qualities.length > 1) {
      const selector = card.querySelector('[data-role="quality"]');
      selector.innerHTML = qualities.map((quality, index) => {
        const resolution = quality.width && quality.height ? `${quality.width}×${quality.height}` : 'Auto';
        const bitrate = quality.bandwidth ? ` · ${(quality.bandwidth / 1_000_000).toLocaleString('it-IT', { maximumFractionDigits: 1 })} Mb/s` : '';
        return `<option value="${index}">${escapeHtml(resolution + bitrate)}</option>`;
      }).join('');
      selector.classList.remove('hidden');
      card.querySelector('[data-role="resolution"]').classList.add('hidden');
      selector.addEventListener('change', () => { stream.quality = qualities[Number(selector.value)]; });
    }
    if (info.cached) card.querySelector('[data-role="cache"]').classList.remove('hidden');
    if (info.thumbnail) {
      const thumb = card.querySelector('[data-role="thumb"]');
      const duration = escapeHtml(info.duration_string || '--:--');
      thumb.innerHTML = `<img src="${info.thumbnail}" alt=""><span class="media-badge">HLS</span><span class="media-duration">${duration}</span>`;
    }
  } catch {
    card.querySelector('[data-role="resolution"]').textContent = 'Auto';
  } finally {
    card.querySelector('[data-role="thumb"]').classList.remove('skeleton');
  }
}

async function runWithConcurrency(items, limit, operation) {
  let nextIndex = 0;
  const results = new Array(items.length);
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (nextIndex < items.length) {
      const index = nextIndex++;
      results[index] = await operation(items[index], index);
    }
  });
  await Promise.all(workers);
  return results;
}

async function queueYouTube(button, url, formatId, downloadType, playlist) {
  await queueRequest(button, '/download', {
    url,
    format_id: formatId,
    download_type: downloadType,
    playlist
  });
}

async function queueStream(button, stream) {
  const filename = await requestStreamFilename();
  if (filename === null) return;
  await queueRequest(button, '/download-m3u8', {
    url: stream.downloadUrl || stream.url, headers: stream.downloadHeaders || stream.headers || {}, filename,
    program_id: stream.quality?.program_id ?? null,
    height: stream.quality?.height ?? null
  });
}

function requestStreamFilename() {
  dom.filenameInput.value = currentTab?.title || 'Stream HLS';
  dom.filenameDialog.returnValue = '';
  return new Promise(resolve => {
    dom.filenameDialog.addEventListener('close', () => {
      resolve(dom.filenameDialog.returnValue === 'download' ? dom.filenameInput.value.trim() : null);
    }, { once: true });
    dom.filenameDialog.showModal();
    dom.filenameInput.focus();
    dom.filenameInput.select();
  });
}

async function queueRequest(button, path, payload) {
  const previous = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon('queue')}<span>In coda</span>`;
  try {
    const data = await fetchJson(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (data.status !== 'queued') throw new Error('Il download non è stato accodato.');
    await dismissCurrentMedia(payload.url);
    removeMediaCard(button.closest('.media-card'));
    await updateDownloadsList();
    document.querySelector('[data-tab="downloads"]').click();
  } catch (error) {
    button.disabled = false;
    button.innerHTML = previous;
    showError(normalizeError(error));
  }
}

async function dismissCurrentMedia(url) {
  if (!currentTab || !url) return;
  try {
    await sendRuntimeMessage({
      type: 'DISMISS_MEDIA',
      tabId: currentTab.id,
      pageUrl: currentTab.url || '',
      url,
      all: !isYouTube(currentTab.url)
    });
  } catch {
    // Il download è già in coda: la rimozione locale resta comunque valida.
  }
}

function removeMediaCard(card) {
  if (!card) return;
  card.classList.add('is-removing');
  setMediaCount(Math.max(0, mediaTotal - 1));
  setTimeout(() => {
    const list = card.parentElement;
    card.remove();
    if (list && !list.children.length) {
      list.classList.add('hidden');
      showEmpty();
    }
  }, 180);
}

function startDownloadsMonitoring() {
  updateDownloadsList();
  downloadsInterval = setInterval(updateDownloadsList, 1000);
}

async function updateDownloadsList() {
  try {
    const [active, history] = await Promise.all([
      fetchJson('/downloads/active', {}, 4000),
      fetchJson('/downloads/history', {}, 4000)
    ]);
    displayActiveDownloads(active.downloads || []);
    displayHistory(history.downloads || []);
    const activeTotal = active.downloads?.length || 0;
    dom.downloadCount.textContent = activeTotal;
    dom.activeCount.textContent = activeTotal;
    dom.historyCount.textContent = history.downloads?.length || 0;
  } catch {
    dom.serverStatus.classList.remove('online');
    dom.serverLabel.textContent = 'Offline';
  }
}

function displayActiveDownloads(downloads) {
  if (!downloads.length) {
    dom.activeDownloads.innerHTML = '<div class="list-empty">Nessun download attivo</div>';
    return;
  }
  dom.activeDownloads.innerHTML = downloads.map(download => {
    const progress = Number(download.progress || 0);
    const queuedDetail = `Posizione ${download.queue_position || 1}`;
    const itemDetail = download.item_count ? `${download.item_index || 1} di ${download.item_count}` : '';
    return `
      <article class="download-card">
        <div class="download-top">
          <div class="download-title">${escapeHtml(download.title || 'Download')}</div>
          <span class="download-state ${escapeHtml(download.status)}">${statusLabel(download.status)}</span>
        </div>
        <div class="progress-track"><div class="progress-value" style="width:${Math.max(1.5, progress)}%"></div></div>
        <div class="download-detail">
          <span>${download.status === 'queued' ? queuedDetail : `${progress.toFixed(1)}% ${itemDetail}`}</span>
          <span>${escapeHtml(download.speed || (download.status === 'queued' ? 'Attesa slot' : 'Preparazione'))}${download.eta ? ` · ${escapeHtml(download.eta)}` : ''}</span>
        </div>
      </article>`;
  }).join('');
}

function displayHistory(downloads) {
  if (!downloads.length) {
    dom.completedDownloads.innerHTML = '<div class="list-empty">La cronologia è vuota</div>';
    return;
  }
  dom.completedDownloads.innerHTML = downloads.slice().reverse().slice(0, 12).map(download => `
    <article class="download-card">
      <div class="download-top">
        <div class="download-title">${escapeHtml(download.title || 'Download')}</div>
        <span class="download-state ${escapeHtml(download.status)}">${statusLabel(download.status)}</span>
      </div>
      <div class="download-detail">
        <span>${escapeHtml(kindLabel(download.kind))}</span>
        <span>${download.status === 'error' ? escapeHtml(truncate(download.error, 52)) : formatTime(download.completed_at)}</span>
      </div>
    </article>`).join('');
}

async function clearHistory() {
  try {
    await fetchJson('/downloads/history', { method: 'DELETE' });
    await updateDownloadsList();
  } catch (error) {
    showError(normalizeError(error));
  }
}

async function resetDetectedMedia() {
  if (!currentTab) [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  dom.reset.classList.add('is-resetting');
  try {
    await sendRuntimeMessage({ type: 'RESET_M3U8', tabId: currentTab.id, pageUrl: currentTab.url || '' });
    if (isYouTube(currentTab.url)) await dismissCurrentMedia(currentTab.url);
    resetMediaUI();
    setMediaCount(0);
    showEmpty();
  } finally {
    setTimeout(() => dom.reset.classList.remove('is-resetting'), 500);
  }
}

function resetMediaUI() {
  dom.videos.replaceChildren();
  dom.streams.replaceChildren();
  dom.videos.classList.add('hidden');
  dom.streams.classList.add('hidden');
  dom.error.classList.add('hidden');
  dom.empty.classList.add('hidden');
  setMediaCount(0);
}

function setLoading(active) {
  dom.loading.classList.toggle('hidden', !active);
}

function setMediaCount(count) {
  mediaTotal = count;
  dom.mediaCount.textContent = String(mediaTotal);
}

function showEmpty() {
  dom.empty.classList.remove('hidden');
}

function showError(message) {
  setLoading(false);
  dom.error.textContent = message;
  dom.error.classList.remove('hidden');
}

function statusLabel(status) {
  return ({ queued: 'Coda', downloading: 'Attivo', completed: 'Finito', error: 'Errore' })[status] || status;
}

function kindLabel(kind = '') {
  return ({ video: 'Video', audio: 'Audio', hls: 'Stream HLS', 'playlist-video': 'Playlist video', 'playlist-audio': 'Playlist audio' })[kind] || 'Media';
}

function isYouTube(url = '') {
  try {
    const host = new URL(url).hostname.replace(/^www\./, '');
    return host === 'youtube.com' || host.endsWith('.youtube.com') || host === 'youtu.be';
  } catch {
    return false;
  }
}

function hostname(url = '') {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return 'Sorgente web';
  }
}

function truncate(value, length) {
  const text = String(value || '');
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function formatTime(isoString) {
  if (!isoString) return '';
  try {
    return new Date(isoString).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

function normalizeError(error) {
  if (error?.name === 'AbortError') return 'La richiesta ha impiegato troppo tempo.';
  return error?.message || 'Si è verificato un errore.';
}

init();
window.addEventListener('unload', () => downloadsInterval && clearInterval(downloadsInterval));
