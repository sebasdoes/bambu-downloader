/* Bambu Downloader frontend */
'use strict';

let pendingTfaKey = null;
let loginEmail = '';
let loginRegion = 'global';
let eventTimer = null;

// ---------------------------------------------------------------- helpers
// API key (only needed when the server sets BND_API_KEY). Stored locally.
function getApiKey() { return localStorage.getItem('bnd_api_key') || ''; }
function setApiKey(k) { k ? localStorage.setItem('bnd_api_key', k) : localStorage.removeItem('bnd_api_key'); }

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const key = getApiKey();
  if (key) headers['X-API-Key'] = key;
  const res = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (res.status === 401 && !key && !path.includes('/api/auth')) {
    // Server requires an API key we don't have — ask once, then retry.
    const k = prompt('This server requires an API key (BND_API_KEY). Enter it:');
    if (k) { setApiKey(k.trim()); return api(path, opts); }
  }
  if (!res.ok) {
    throw new Error((data && data.detail) || `HTTP ${res.status}`);
  }
  return data;
}

function toast(msg, kind = '') {
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 8000 : 4000);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

function fmtBytes(n) {
  if (!n && n !== 0) return '';
  if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

function showTab(name) {
  for (const tab of document.querySelectorAll('.tab')) tab.hidden = true;
  document.getElementById('tab-' + name).hidden = false;
  for (const btn of document.querySelectorAll('nav.tabs button')) {
    btn.classList.toggle('active', btn.dataset.tab === name);
  }
  if (name === 'activity') startEventPolling();
  else stopEventPolling();
  if (name === 'library') { loadModels(); loadLabels(); }
  if (name === 'collections') { startMinePolling(); loadCollections(); }
  else stopMinePolling();
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  try {
    const s = await api('/api/status');
    const badge = document.getElementById('authBadge');
    if (s.authenticated) {
      badge.textContent = '✓ ' + (s.email || 'signed in');
      badge.className = 'auth-badge on';
    } else if (s.token_invalid) {
      badge.textContent = 'Sign-in expired';
      badge.className = 'auth-badge off';
    } else {
      badge.textContent = 'Not signed in';
      badge.className = 'auth-badge off';
    }
    document.getElementById('sysInfo').textContent =
      `Downloads directory: ${s.download_dir} · ${s.model_count} models · ` +
      `${s.collection_count} collections · scheduler ${s.scheduler.running ? 'running' : 'stopped'}`;
  } catch (e) {
    console.error('status failed', e);
  }
}

// --------------------------------------------------------------- downloads
async function doDownload() {
  const url = document.getElementById('modelUrl').value.trim();
  if (!url) return;
  const btn = document.getElementById('dlBtn');
  const result = document.getElementById('dlResult');
  btn.disabled = true;
  btn.textContent = 'Downloading…';
  result.innerHTML = '';
  try {
    const r = await api('/api/download', { method: 'POST', body: { url } });
    if (r.status === 'exists') {
      result.innerHTML = `<p class="muted">✓ Already in your library — not duplicated.</p>`;
    } else {
      result.innerHTML = `<p class="muted">✓ Downloaded <b>${esc(r.title)}</b> — ${fmtBytes(r.size)}<br>Saved to <code>${esc(r.path)}</code></p>`;
      toast(`Downloaded “${r.title}”`, 'ok');
    }
    document.getElementById('modelUrl').value = '';
    loadStatus();
  } catch (e) {
    result.innerHTML = `<p class="muted" style="color:var(--red)">✗ ${esc(e.message)}</p>`;
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Download';
  }
}

// ----------------------------------------------------------------- library
let libFilter = { label: null };  // null = All
// Library pagination: page size + "shown so far" (reset on filter change).
const LIB_PAGE = 60;
let libShown = LIB_PAGE;

async function loadLabels() {
  try {
    const labels = await api('/api/model-labels');
    const bar = document.getElementById('labelChips');
    const total = labels.reduce((s, l) => s + l.count, 0);
    const chips = [{ label: 'All', count: total, active: libFilter.label === null }]
      .concat(labels.map(l => ({ ...l, active: libFilter.label === l.label })));
    bar.innerHTML = chips.map(c =>
      `<button class="chip ${c.active ? 'active' : ''}" data-label="${esc(c.label)}" onclick="setLibFilter('${esc(c.label).replace(/'/g, "&#39;")}')">${esc(c.label)}<span class="n">${c.count}</span></button>`
    ).join('');
  } catch (e) { console.error(e); }
}

function setLibFilter(label) {
  libFilter.label = label === 'All' ? null : label;
  libShown = LIB_PAGE;  // new filter -> back to the first page
  loadModels();
  loadLabels();
}

function modelCard(m) {
  const mwUrl = `https://makerworld.com/en/models/${m.design_id}${m.slug ? '-' + m.slug : ''}`;
  const img = m.cover_url
    ? `<img loading="lazy" src="/thumb?url=${encodeURIComponent(m.cover_url)}" alt="" onerror="this.remove()">`
    : `<div class="cover-fallback">🖨</div>`;
  const origin = m.collection_title
    ? `<span class="tag origin" title="${esc(m.collection_title)}">🗂 ${esc(m.collection_title)}</span>`
    : `<span class="tag origin" title="Downloaded by URL, not via a collection">🏷 Manual download</span>`;
  const creator = m.creator ? `by ${esc(m.creator)}` : '';
  const date = m.created_at ? new Date(m.created_at).toLocaleDateString() : '';
  return `
  <div class="model-card">
    <a class="cover" href="${esc(mwUrl)}" target="_blank" rel="noopener noreferrer" title="View on MakerWorld">${img}</a>
    <div class="meta title" title="${esc(m.title)}">${esc(m.title)}</div>
    <div class="meta sub">
      ${creator ? `<div class="creator">${creator}</div>` : ''}
      ${date ? `<div class="date">${date}</div>` : ''}
      design #${m.design_id}${m.profile_id ? ' · plate #' + m.profile_id : ''}<br>
      ${fmtBytes(m.file_size)} · <span title="${esc(m.file_path)}">${esc(m.filename)}</span>
    </div>
    <div class="meta labels">
      ${origin}
    </div>
    <div class="meta actions">
      <a class="mw-link" href="${esc(mwUrl)}" target="_blank" rel="noopener noreferrer">↗ MakerWorld</a>
    </div>
  </div>`;
}

async function loadModels() {
  try {
    const params = new URLSearchParams();
    if (libFilter.label) params.set('label', libFilter.label);
    params.set('limit', String(LIB_PAGE));
    params.set('offset', '0');
    const r = await api('/api/models?' + params.toString());
    const grid = document.getElementById('modelGrid');
    const empty = document.getElementById('libEmpty');
    document.getElementById('libCount').textContent = `${r.total} models`;
    empty.hidden = r.models.length > 0;
    grid.innerHTML = r.models.map(modelCard).join('');
    updateLoadMore(r.total);
  } catch (e) {
    toast(e.message, 'err');
  }
}

// Re-fetch the next page and append its cards (Load more button).
async function loadMoreModels() {
  const btn = document.getElementById('libMore');
  if (!btn) return;
  btn.disabled = true;
  try {
    const params = new URLSearchParams();
    if (libFilter.label) params.set('label', libFilter.label);
    params.set('limit', String(LIB_PAGE));
    params.set('offset', String(libShown));
    const r = await api('/api/models?' + params.toString());
    const grid = document.getElementById('modelGrid');
    grid.insertAdjacentHTML('beforeend', r.models.map(modelCard).join(''));
    libShown += r.models.length;
    updateLoadMore(r.total);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function updateLoadMore(total) {
  const btn = document.getElementById('libMore');
  if (!btn) return;
  const done = libShown >= total;
  btn.hidden = done;
  if (!done) btn.textContent = `Load more (${libShown}/${total})`;
}

// ----------------------------------------------------------------- collections
let mineTimer = null;

async function addCollection() {
  const url = document.getElementById('collUrl').value.trim();
  if (!url) return;
  const interval = parseInt(document.getElementById('collInterval').value, 10);
  try {
    const c = await api('/api/collections', { method: 'POST', body: { url, sync_interval_minutes: interval } });
    toast(`Following “${c.title}”`, 'ok');
    document.getElementById('collUrl').value = '';
    loadCollections();
    loadMyCollections();
    loadStatus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// Own MakerWorld collections (hourly-refreshed cache; UI re-polls every 5 min).
async function loadMyCollections() {
  try {
    const r = await api('/api/my-collections');
    const el = document.getElementById('mineList');
    const empty = document.getElementById('mineEmpty');
    const meta = document.getElementById('mineMeta');
    const hint = document.getElementById('mineHint');
    if (!r.authenticated) {
      empty.hidden = false;
      empty.textContent = 'Sign in (Settings) to see your own collections here.';
      meta.textContent = '';
      hint.textContent = '';
      el.innerHTML = '';
      return;
    }
    empty.hidden = r.collections.length > 0;
    if (!r.collections.length) empty.textContent = 'Nothing cached yet — click Refresh, or wait for the next hourly fetch.';
    const when = r.fetched_at ? new Date(r.fetched_at).toLocaleString() : null;
    meta.textContent = when ? `updated ${when}` : 'not fetched yet';
    const rows = r.collections.map(c => {
      const pct = c.design_count ? Math.round(100 * c.downloaded_count / c.design_count) : 0;
      const state = c.downloaded
        ? '<span class="mine-check">✓ all downloaded</span>'
        : (c.downloaded_count > 0
            ? `<span class="mine-partial">✓ ${c.downloaded_count}/${c.design_count} downloaded</span>`
            : `<span class="mine-none">${c.design_count} models · none downloaded</span>`);
      const follow = c.followed
        ? `<span class="tag ok">following</span>`
        : `<button class="ghost" onclick="followMine(${c.collection_id}, '${esc(c.slug || String(c.collection_id))}')">Follow</button>`;
      const mw = `https://makerworld.com/en/collections/${c.collection_id}${c.slug ? '-' + c.slug : ''}`;
      return `
      <div class="mine-item">
        <a class="title" href="${esc(mw)}" target="_blank" rel="noopener noreferrer">${c.title ? esc(c.title) : 'Collection ' + c.collection_id}</a>
        ${state}
        <div class="mine-progress" title="${pct}% downloaded"><div style="width:${pct}%"></div></div>
        ${follow}
      </div>`;
    }).join('');
    el.innerHTML = rows;
  } catch (e) {
    toast(e.message, 'err');
  }
}

// Follow one of your own collections without typing its URL.
async function followMine(cid, slug) {
  try {
    const url = `https://makerworld.com/en/collections/${cid}-${slug}`;
    await api('/api/collections', { method: 'POST', body: { url, sync_interval_minutes: 360 } });
    toast('Collection followed', 'ok');
    loadCollections();
    loadMyCollections();
    loadStatus();
  } catch (e) { toast(e.message, 'err'); }
}

// Manual refresh of the own-collections cache (normally fetched hourly).
async function refreshMyCollections() {
  try {
    await api('/api/my-collections/refresh', { method: 'POST' });
    toast('Your collections refreshed', 'ok');
    loadMyCollections();
  } catch (e) { toast(e.message, 'err'); }
}

function startMinePolling() {
  loadMyCollections();
  if (!mineTimer) mineTimer = setInterval(loadMyCollections, 5 * 60 * 1000);
}
function stopMinePolling() {
  if (mineTimer) { clearInterval(mineTimer); mineTimer = null; }
}

async function loadCollections() {
  try {
    const list = await api('/api/collections');
    const el = document.getElementById('collList');
    const empty = document.getElementById('collEmpty');
    empty.hidden = list.length > 0;
    el.innerHTML = list.map(c => `
      <div class="coll-item" data-id="${c.collection_id}">
        <span class="title">${esc(c.title || 'Collection ' + c.collection_id)}</span>
        <span class="tag ${c.enabled ? 'ok' : 'warn'}">${c.enabled ? 'active' : 'paused'}</span>
        <span class="muted">${c.last_sync_at ? 'last sync ' + new Date(c.last_sync_at).toLocaleString() : 'never synced'}</span>
        <span class="muted">${c.last_sync_new ?? 0} new last time</span>
        <span class="interval">
          <input type="number" value="${c.sync_interval_minutes}" min="15" step="15" onchange="setInterval_(${c.collection_id}, this.value)"> min
        </span>
        <button class="ghost" onclick="syncNow(${c.collection_id})">Sync now</button>
        <button class="ghost" onclick="toggleColl(${c.collection_id}, ${c.enabled ? 'false' : 'true'})">${c.enabled ? 'Pause' : 'Resume'}</button>
        <button class="danger" onclick="removeColl(${c.collection_id})">Remove</button>
      </div>`).join('');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function setInterval_(cid, minutes) {
  try {
    await api(`/api/collections/${cid}`, { method: 'PATCH', body: { sync_interval_minutes: parseInt(minutes, 10) || 360 } });
    toast('Interval updated', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function toggleColl(cid, enabled) {
  try {
    await api(`/api/collections/${cid}`, { method: 'PATCH', body: { enabled } });
    loadCollections();
  } catch (e) { toast(e.message, 'err'); }
}

// Unfollow dialog: three-way choice (this is destructive, so no bare confirm()).
let pendingRemoveCid = null;

async function removeColl(cid) {
  pendingRemoveCid = cid;
  let text = 'Stop following this collection?';
  try {
    const r = await api(`/api/models?collection_id=${cid}`);
    text = `Stop following this collection? It has ${r.total} downloaded model${r.total === 1 ? '' : 's'} in your library.`;
  } catch (e) { /* count is cosmetic */ }
  document.getElementById('removeModalText').textContent = text;
  document.getElementById('removeModal').hidden = false;
}

function closeRemoveModal() {
  document.getElementById('removeModal').hidden = true;
  pendingRemoveCid = null;
}

async function doRemoveColl(deleteFiles) {
  const cid = pendingRemoveCid;
  if (!cid) return;
  closeRemoveModal();
  try {
    const r = await api(`/api/collections/${cid}?delete_files=${deleteFiles}`, { method: 'DELETE' });
    toast(deleteFiles
      ? (r.deleted_files
          ? `Unfollowed — deleted ${r.deleted_files} file${r.deleted_files === 1 ? '' : 's'} from disk`
          : 'Unfollowed — no files found to delete')
      : 'Collection unfollowed — files kept', 'ok');
    loadCollections();
    loadStatus();
  } catch (e) { toast(e.message, 'err'); }
}

async function syncNow(cid) {
  try {
    const r = await api(`/api/collections/${cid}/sync`, { method: 'POST' });
    toast(r.started ? 'Sync started — watch Activity' : 'Sync already running', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

// ---------------------------------------------------------------- activity
async function loadEvents() {
  try {
    const r = await api('/api/events?limit=100');
    const el = document.getElementById('eventList');
    const empty = document.getElementById('evEmpty');
    empty.hidden = r.events.length > 0;
    el.innerHTML = r.events.map(ev => `
      <div class="event ${ev.kind === 'error' ? 'error' : ''}">
        <time>${fmtTime(ev.ts)}</time>${esc(ev.message)}
      </div>`).join('');
  } catch (e) {
    console.error(e);
  }
}

function startEventPolling() {
  loadEvents();
  eventTimer = setInterval(loadEvents, 4000);
}
function stopEventPolling() {
  if (eventTimer) { clearInterval(eventTimer); eventTimer = null; }
}

// -------------------------------------------------------------------- auth
async function doLogin() {
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  const region = document.getElementById('loginRegion').value;
  const btn = document.getElementById('loginBtn');
  if (!email || !password) { toast('Email and password required', 'err'); return; }
  btn.disabled = true;
  try {
    const r = await api('/api/auth/login', { method: 'POST', body: { email, password, region } });
    if (r.step === 'done') {
      toast('Signed in to MakerWorld', 'ok');
      resetLogin();
      loadStatus();
    } else if (r.step === 'email_code') {
      loginEmail = email; loginRegion = region;
      showStep2('Check your email for a 6-digit code.');
      pendingTfaKey = null;
    } else if (r.step === 'totp') {
      loginEmail = email; loginRegion = region;
      pendingTfaKey = r.tfa_key;
      showStep2('Enter the code from your authenticator app.');
    }
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function showStep2(hint) {
  document.getElementById('loginStep1').hidden = true;
  document.getElementById('loginStep2').hidden = false;
  document.getElementById('stepBadge2').classList.add('on');
  document.getElementById('step2Hint').textContent = hint;
  document.getElementById('otpCode').focus();
}

function resetLogin() {
  document.getElementById('loginStep1').hidden = false;
  document.getElementById('loginStep2').hidden = true;
  document.getElementById('stepBadge2').classList.remove('on');
  document.getElementById('loginPassword').value = '';
  document.getElementById('otpCode').value = '';
  pendingTfaKey = null;
}

async function doVerify() {
  const code = document.getElementById('otpCode').value.trim();
  if (!code) return;
  try {
    await api('/api/auth/verify', {
      method: 'POST',
      body: { email: loginEmail, code, tfa_key: pendingTfaKey || '', region: loginRegion },
    });
    toast('Signed in to MakerWorld', 'ok');
    resetLogin();
    loadStatus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function doTokenLogin() {
  const token = document.getElementById('tokenInput').value.trim();
  if (!token) return;
  try {
    await api('/api/auth/token', { method: 'POST', body: { access_token: token } });
    toast('Token accepted — signed in', 'ok');
    document.getElementById('tokenInput').value = '';
    loadStatus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ---------------------------------------------------------------- startup
function saveApiKey() {
  setApiKey(document.getElementById('apiKeyInput').value.trim());
  toast('API key saved', 'ok');
  loadStatus();
}
function clearApiKey() {
  setApiKey('');
  document.getElementById('apiKeyInput').value = '';
  toast('API key cleared', 'ok');
}

async function loadAll() {
  await loadStatus();
}
loadAll();
setInterval(loadStatus, 30000);

// PWA share-target: ?url=... shared from Bambu Handy / Android
const params = new URLSearchParams(location.search);
const sharedUrl = params.get('url');
if (sharedUrl) {
  showTab('download');
  document.getElementById('modelUrl').value = sharedUrl;
  doDownload();
}