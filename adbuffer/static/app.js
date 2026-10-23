// adbuffer app: keyboard-first UI over the local adbuffer daemon.
// Up/down are i/k (and arrows), back/forward are j/l, matching the ijkl keys used elsewhere on this desktop.

const $ = (s) => document.querySelector(s);
const omni = $('#omni'), video = $('#video');

const S = {
  view: 'home',
  section: localGet('section') || 'bookmarks',
  lib: { history: [], bookmarks: [] },
  results: [], query: '',
  sel: { home: 0, results: 0 },
  session: undefined, hls: null, hlsFor: null, theme: null,
  rows: [], rowEls: [],
};

function localGet(k) { try { return localStorage.getItem('adbuffer.' + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem('adbuffer.' + k, v); } catch {} }

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

let toastTimer;
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 1800);
}

const fmtDur = (s) => {
  if (s == null) return '';
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`;
};
const fmtAgo = (ts) => {
  const d = Date.now() / 1000 - ts;
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
};
const host = (u) => { try { return new URL(u).host.replace(/^www\./, ''); } catch { return u; } };

// ---------------------------------------------------------------- views

function setView(v, push = true) {
  if (!['home', 'results', 'player'].includes(v)) v = 'home';
  S.view = v;
  if (push && location.hash !== '#' + v) history.pushState(null, '', '#' + v);
  document.querySelectorAll('.view').forEach((el) => el.classList.toggle('active', el.id === 'view-' + v));
  document.querySelectorAll('#tabs a').forEach((a) => a.classList.toggle('active', a.dataset.view === v));
  renderKeys();
  if (v !== 'player') render();
}
window.addEventListener('popstate', () => setView(location.hash.slice(1), false));

function items(view = S.view) {
  if (view === 'home') return S.lib[S.section] || [];
  if (view === 'results') return S.results;
  return [];
}
function selected() { const l = items(); return l[S.sel[S.view]]; }

function render() {
  document.querySelectorAll('.sections button').forEach((b) => b.classList.toggle('active', b.dataset.section === S.section));
  renderList('home', $('#home-list'), S.section === 'bookmarks'
    ? 'No bookmarks yet. Press a on anything to keep it here.'
    : 'Nothing watched yet. Press / to search.');
  $('#results-title').textContent = S.query ? `YouTube results for “${S.query}”` : '';
  renderList('results', $('#results-list'), S.query ? 'No results.' : 'Press / and type to search.');
}

function renderList(view, ol, emptyMsg) {
  const list = items(view);
  S.sel[view] = Math.min(S.sel[view], Math.max(0, list.length - 1));
  const marks = new Set(S.lib.bookmarks.map((b) => b.url));
  if (!list.length) { ol.innerHTML = ''; const li = document.createElement('li'); li.className = 'empty'; li.textContent = emptyMsg; ol.append(li); return; }
  ol.replaceChildren(...list.map((it, i) => {
    const li = document.createElement('li');
    li.className = i === S.sel[view] ? 'sel' : '';
    if (it.thumb) { const img = document.createElement('img'); img.loading = 'lazy'; img.src = it.thumb; img.alt = ''; li.append(img); }
    const text = document.createElement('div'); text.className = 'text';
    const title = document.createElement('div'); title.className = 'title'; title.textContent = it.title || it.url;
    const sub = document.createElement('div'); sub.className = 'sub';
    sub.textContent = view === 'results'
      ? [it.channel, it.live ? 'LIVE' : fmtDur(it.duration), it.views ? `${(+it.views).toLocaleString()} views` : ''].filter(Boolean).join(' · ')
      : [host(it.url), it.ts ? fmtAgo(it.ts) : ''].filter(Boolean).join(' · ');
    text.append(title, sub); li.append(text);
    if (marks.has(it.url)) { const b = document.createElement('span'); b.className = 'badge'; b.textContent = '★'; li.append(b); }
    li.addEventListener('click', () => { S.sel[view] = i; render(); });
    li.addEventListener('dblclick', () => watch(it));
    return li;
  }));
  ol.children[S.sel[view]]?.scrollIntoView({ block: 'nearest' });
}

function move(delta) {
  const n = items().length; if (!n) return;
  S.sel[S.view] = Math.max(0, Math.min(n - 1, S.sel[S.view] + delta));
  render();
}

function renderKeys() {
  const k = (key, label) => `<span><span class="key">${key}</span>${label}</span>`;
  const common = [k('/', 'search'), k('j l', 'back/fwd'), k('?', 'help')];
  const list = [k('i k', 'move'), k('↵', 'watch clean'), k('b', 'open in Brave'), k('a', 'bookmark')];
  const by = {
    home: [...list, k('d', 'remove'), k('Tab', 'bookmarks/history')],
    results: list,
    player: [k('Space', 'pause'), k('j l ← →', 'seek'), k('End', 'live'), k('f', 'fullscreen'), k('m', 'mute'),
      k('t', 'transcript'), k('s', 'stop'), k('b', 'open in Brave'), k('Esc', 'back')],
  };
  const tail = S.view === 'player' ? [k('/', 'search'), k('?', 'help')] : common;
  $('#keys').innerHTML = [...by[S.view], ...tail].join('');
}

// ---------------------------------------------------------------- actions

async function loadLibrary() { S.lib = await api('/api/library'); render(); }

async function runSearch(q) {
  S.query = q; S.results = []; S.sel.results = 0; setView('results');
  $('#results-title').textContent = `Searching YouTube for “${q}”…`;
  try { S.results = await api('/api/search?q=' + encodeURIComponent(q)); }
  catch (e) { toast('Search failed: ' + e.message.slice(0, 80)); }
  render();
}

async function watch(it) {
  try {
    await api('/api/watch', { url: it.url, title: it.title || '' });
    resetPlayer();
    setView('player');
    toast('Buffering — ads get softened before you see them');
    loadLibrary(); poll();
  } catch (e) { toast('Could not start: ' + e.message.slice(0, 80)); }
}

async function openInBrave(urlOrQuery) {
  try { await api('/api/open', { url: urlOrQuery }); toast('Opened in Brave'); }
  catch (e) { toast('Could not open: ' + e.message.slice(0, 80)); }
}

async function toggleBookmark(it) {
  if (!it) return;
  const r = await api('/api/bookmark', { url: it.url, title: it.title || '' });
  toast(r.bookmarked ? 'Bookmarked' : 'Bookmark removed');
  loadLibrary();
}

async function removeSelected() {
  const it = selected(); if (!it || S.view !== 'home') return;
  await api('/api/remove', { kind: S.section, url: it.url });
  loadLibrary();
}

function looksLikeUrl(t) { return /^https?:\/\//i.test(t) || (!/\s/.test(t) && /^[^/]+\.[a-z]{2,}(\/|$)/i.test(t)); }

function submitOmni(shift) {
  const t = omni.value.trim(); if (!t) return;
  omni.blur();
  if (shift) return openInBrave(t);
  if (looksLikeUrl(t)) return watch({ url: t });
  runSearch(t);
}

function nowPlaying() { return S.session ? { url: S.session.source, title: S.session.title } : null; }

// ---------------------------------------------------------------- player

function resetPlayer() {
  if (S.hls) { S.hls.destroy(); S.hls = null; }
  S.hlsFor = null; video.removeAttribute('src'); video.load();
}

// Stream time of what's on screen. The playlist stamps each segment with 1970-01-01 + its stream
// time (EXT-X-PROGRAM-DATE-TIME), which hls.js turns into playingDate.
function playerPts() {
  const d = S.hls && S.hls.playingDate;
  return d ? d.getTime() / 1000 : null;
}
function streamOffset() { const p = playerPts(); return p == null ? null : p - video.currentTime; }

function seekable() {
  const r = video.seekable;
  return r.length ? [r.start(r.length - 1), r.end(r.length - 1)] : null;
}

function seekTo(t) {
  const r = seekable(); if (!r) return;
  video.currentTime = Math.max(r[0], Math.min(r[1] - 1, t));
  renderPosition();
}
const seekBy = (d) => seekTo(video.currentTime + d);
const goLive = () => { const r = seekable(); if (r) seekTo(r[1] - 6); };

function attach(id) {
  resetPlayer(); S.hlsFor = id;
  const src = `/hls/${id}/live.m3u8`;
  if (window.Hls && Hls.isSupported()) {
    S.hls = new Hls({ liveSyncDurationCount: 3, manifestLoadingMaxRetry: 30, levelLoadingMaxRetry: 30 });
    S.hls.on(Hls.Events.ERROR, (_, d) => { if (d.fatal && S.hlsFor === id) setTimeout(() => attach(id), 2000); });
    S.hls.loadSource(src); S.hls.attachMedia(video);
  } else { video.src = src; }
  video.play().catch(() => {});
}

function renderSession() {
  const s = S.session, ov = $('#overlay');
  $('#live-dot').textContent = s && !s.ended ? '●' : '';
  if (!s) {
    if (S.hlsFor) resetPlayer();
    ov.innerHTML = '<div>Nothing playing.</div><div class="hint">Press / to search, or Super Alt Y in Brave to send a tab here.</div>';
    $('#np-title').textContent = ''; $('#np-status').textContent = ''; $('#lines').replaceChildren();
    return;
  }
  if (s.published_segments > 0 && S.hlsFor !== s.id) attach(s.id);
  if (s.failed) ov.innerHTML = '<div>Couldn’t read that source.</div><div class="hint">It may not be a video page. Press b to open it in Brave.</div>';
  else if (s.waiting_for_source) ov.innerHTML = '<div>Connecting to source…</div>';
  else if (!s.published_segments) ov.innerHTML = `<div class="big">${Math.ceil(s.starts_in)}s</div><div>Buffering and scanning for ads</div>`;
  else ov.innerHTML = '';

  $('#np-title').textContent = s.title;
  const parts = [
    s.ended ? 'Ended' : s.ingest_done ? 'Source finished, draining buffer' : 'Live',
    `<b>${Math.round(s.buffered_seconds)}s</b> buffered`,
    `<b>${s.censored_segments * 2}s</b> softened`,
    host(s.source),
  ];
  $('#np-status').innerHTML = parts.map((p) => `<span>${p}</span>`).join('');

  renderTranscript(s);
  renderPosition();
}

function renderTranscript(s) {
  const key = JSON.stringify(s.lines);
  if (key === S.rowsKey) return;
  S.rowsKey = key;
  S.rows = s.lines;
  S.rowEls = s.lines.map((l) => {
    const d = document.createElement('div');
    d.className = 'line' + (l.ad ? ' ad' : '');
    const t = document.createElement('span'); t.className = 't'; t.textContent = fmtDur(l.start - s.origin);
    d.append(t, l.text);
    if (l.ad) { const k = document.createElement('span'); k.className = 'kind'; k.textContent = l.kind; d.append(k); }
    d.addEventListener('click', () => { const o = streamOffset(); if (o != null) seekTo(l.start - o); });
    return d;
  });
  $('#lines').replaceChildren(...S.rowEls);
  S.nowRow = -1;
}

// Follows the player, not the transcriber: highlights the sentence being played and scrolls to it.
function renderPosition() {
  const s = S.session, pts = playerPts();
  let now = -1;
  if (pts != null) S.rows.forEach((l, i) => { if (l.start <= pts) now = i; });
  S.rowEls.forEach((el, i) => {
    el.classList.toggle('now', i === now);
    el.classList.toggle('future', pts == null || S.rows[i].start > pts);
  });
  if (now !== S.nowRow && now >= 0) S.rowEls[now].scrollIntoView({ block: 'center', behavior: 'smooth' });
  S.nowRow = now;

  const r = seekable(), track = $('#scrub-track');
  if (!s || !r || r[1] - r[0] < 1) {
    $('#scrub-played').style.width = '0'; $('#scrub-head').style.left = '0'; $('#scrub-ads').replaceChildren();
    $('#scrub-time').textContent = ''; return;
  }
  const span = r[1] - r[0], pct = (t) => `${Math.max(0, Math.min(100, (t - r[0]) / span * 100))}%`;
  $('#scrub-played').style.width = pct(video.currentTime);
  $('#scrub-head').style.left = pct(video.currentTime);
  const off = streamOffset();
  if (off != null) {
    $('#scrub-ads').replaceChildren(...s.spans.filter((x) => x.start - off < r[1]).map((x) => {
      const el = document.createElement('div'); el.className = 'ad';
      const a = x.start - off, b = x.end - off;
      el.style.left = pct(a); el.style.width = `calc(${pct(b)} - ${pct(a)})`;
      el.title = `${x.kind} (${x.source})`;
      return el;
    }));
  }
  const behind = r[1] - video.currentTime, live = !s.ended && behind < 12;
  const tm = $('#scrub-time');
  tm.classList.toggle('live', live);
  tm.textContent = s.ended ? `${fmtDur(video.currentTime - r[0])} / ${fmtDur(span)}` : live ? '● LIVE' : `−${fmtDur(behind)}`;
}
video.addEventListener('timeupdate', () => S.view === 'player' && renderPosition());
$('#scrub-track').addEventListener('click', (e) => {
  const r = seekable(); if (!r) return;
  const box = e.currentTarget.getBoundingClientRect();
  seekTo(r[0] + (e.clientX - box.left) / box.width * (r[1] - r[0]));
});

let pollTimer;
async function poll() {
  clearTimeout(pollTimer);
  try {
    const pts = playerPts();
    const st = await api('/api/status' + (pts != null ? `?around=${pts.toFixed(1)}` : ''));
    if (S.theme !== null && st.theme !== S.theme) $('#theme').href = '/theme.css?v=' + st.theme;
    S.theme = st.theme;
    // a session started elsewhere (Brave passthrough, CLI) brings the player forward
    if (st.session && st.session.id !== S.session?.id && S.session !== undefined) setView('player');
    S.session = st.session;
    renderSession();
  } catch {}
  pollTimer = setTimeout(poll, S.view === 'player' ? 1000 : 3000);
}

// ---------------------------------------------------------------- keys

document.addEventListener('keydown', (e) => {
  const help = $('#help');
  if (!help.hidden) { if (e.key === 'Escape' || e.key === '?') { help.hidden = true; e.preventDefault(); } return; }

  if (e.target === omni) {
    if (e.key === 'Enter') { e.preventDefault(); submitOmni(e.shiftKey); }
    else if (e.key === 'Escape') omni.blur();
    else if (e.key === 'ArrowDown') { omni.blur(); if (S.view === 'player') setView('results'); }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  const inList = S.view !== 'player';
  const it = inList ? selected() : nowPlaying();
  const keys = {
    '/': () => { omni.focus(); omni.select(); },
    o: () => { omni.focus(); omni.select(); },
    '?': () => { help.hidden = false; },
    1: () => setView('home'), 2: () => setView('results'), 3: () => setView('player'),
    j: () => history.back(), l: () => history.forward(),
    b: () => it && openInBrave(it.url),
    a: () => toggleBookmark(it),
    y: () => it && navigator.clipboard.writeText(it.url).then(() => toast('Copied URL')),
  };
  if (inList) Object.assign(keys, {
    i: () => move(-1), k: () => move(1), ArrowUp: () => move(-1), ArrowDown: () => move(1),
    I: () => move(-5), K: () => move(5), PageUp: () => move(-5), PageDown: () => move(5),
    g: () => move(-1e9), G: () => move(1e9), Home: () => move(-1e9), End: () => move(1e9),
    Enter: () => it && watch(it),
    d: removeSelected, Delete: removeSelected,
    Tab: () => {
      if (S.view !== 'home') return setView('home');
      S.section = S.section === 'bookmarks' ? 'history' : 'bookmarks'; localSet('section', S.section); render();
    },
  });
  else Object.assign(keys, {
    ' ': () => (video.paused ? video.play() : video.pause()),
    f: () => (document.fullscreenElement ? document.exitFullscreen() : video.requestFullscreen()),
    m: () => { video.muted = !video.muted; toast(video.muted ? 'Muted' : 'Unmuted'); },
    t: () => document.body.classList.toggle('no-transcript'),
    '+': () => { video.volume = Math.min(1, video.volume + 0.1); toast(`Volume ${Math.round(video.volume * 100)}%`); },
    '=': () => keys['+'](),
    '-': () => { video.volume = Math.max(0, video.volume - 0.1); toast(`Volume ${Math.round(video.volume * 100)}%`); },
    s: async () => { await api('/api/stop', {}); resetPlayer(); toast('Stopped'); poll(); },
    Tab: () => setView('home'),
    Escape: () => history.back(),
    j: () => seekBy(-10), l: () => seekBy(10),
    ArrowLeft: () => seekBy(e.shiftKey ? -30 : -5), ArrowRight: () => seekBy(e.shiftKey ? 30 : 5),
    Home: () => { const r = seekable(); if (r) seekTo(r[0]); },
    End: goLive,
  });
  const fn = keys[e.key];
  if (fn) { e.preventDefault(); fn(); }
});

document.querySelectorAll('#tabs a').forEach((a) => a.addEventListener('click', () => setView(a.dataset.view)));
document.querySelectorAll('.sections button').forEach((b) => b.addEventListener('click', () => {
  S.section = b.dataset.section; localSet('section', S.section); render();
}));

// ---------------------------------------------------------------- boot

setView(location.hash.slice(1) || 'home', false);
history.replaceState(null, '', '#' + S.view);
loadLibrary();
poll();
