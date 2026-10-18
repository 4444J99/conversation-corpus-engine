"""The static site's fixed assets. File names are placeholders (`__STYLE__` and the like) that
`fill_names` replaces with the names `archive_layout` mints, so the templates spell none."""

from __future__ import annotations

from . import archive_layout as layout

NAME_PLACEHOLDERS = {
    "__STYLE__": layout.SITE_STYLE_FILENAME,
    "__HOME__": layout.SITE_HOME_FILENAME,
    "__GRAPH_PAGE__": layout.SITE_GRAPH_PAGE_FILENAME,
    "__HOME_SCRIPT__": layout.SITE_HOME_SCRIPT_FILENAME,
    "__GRAPH_SCRIPT__": layout.SITE_GRAPH_SCRIPT_FILENAME,
    "__INDEX_DATA__": layout.SITE_INDEX_DATA_FILENAME,
    "__TOPIC_DATA__": layout.SITE_TOPIC_DATA_FILENAME,
    "__PAGES__": layout.SITE_PAGES_DIRNAME,
    "__PAGE_PREFIX__": layout.CONVERSATION_PREFIX,
}


def fill_names(template: str) -> str:
    """Replace every name placeholder in a template with its minted name."""
    for placeholder, name in NAME_PLACEHOLDERS.items():
        template = template.replace(placeholder, name)
    return template


CSP_PAGE = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'"
)
CSP_APP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'"
)

STYLE_CSS = """\
:root {
  --bg: #fbfaf8; --fg: #1d1c1a; --muted: #6a665f; --line: #e2ded6; --card: #ffffff;
  --accent: #2b5fd9; --mark: #fff0a8;
  --chatgpt: #10a37f; --claude: #d97757; --gemini: #4285f4; --notebooklm: #8e6bd6;
  --grok: #7a7a7a; --perplexity: #20b8cd;
}
:root[data-theme="dark"] {
  --bg: #15151a; --fg: #ece9e2; --muted: #9a968d; --line: #2c2c34; --card: #1d1d24;
  --accent: #8fb0ff; --mark: #594a00;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15151a; --fg: #ece9e2; --muted: #9a968d; --line: #2c2c34; --card: #1d1d24;
    --accent: #8fb0ff; --mark: #594a00;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--accent); }
.wrap { max-width: 980px; margin: 0 auto; padding: 0 16px 64px; }
.top { padding: 28px 0 12px; border-bottom: 1px solid var(--line); margin-bottom: 16px; }
.top h1 { margin: 0 0 4px; font-size: 1.6rem; }
.top p, .muted { color: var(--muted); margin: 0; font-size: .92rem; }
.top nav { margin-top: 8px; display: flex; gap: 16px; flex-wrap: wrap; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.controls { display: grid; gap: 10px; margin-bottom: 8px; }
.controls .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
input[type="search"], select { font: inherit; color: inherit; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
input[type="search"] { flex: 1 1 280px; min-width: 0; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip { font: inherit; font-size: .85rem; color: inherit; background: var(--card);
  border: 1px solid var(--line); border-radius: 999px; padding: 3px 10px; cursor: pointer; }
.chip[aria-pressed="true"] { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.dot { display: inline-block; width: .6em; height: .6em; border-radius: 50%; margin-right: 6px;
  background: var(--muted); }
.dot.chatgpt { background: var(--chatgpt); } .dot.claude { background: var(--claude); }
.dot.gemini { background: var(--gemini); } .dot.notebooklm { background: var(--notebooklm); }
.dot.grok { background: var(--grok); } .dot.perplexity { background: var(--perplexity); }
.results { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.result { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; }
.result h3 { margin: 0 0 2px; font-size: 1.02rem; }
.result .meta { color: var(--muted); font-size: .82rem; margin-bottom: 4px; }
.result p { margin: 4px 0 0; font-size: .92rem; overflow-wrap: anywhere; }
.topic { font-size: .78rem; color: var(--muted); border: 1px solid var(--line); border-radius: 6px;
  padding: 0 6px; margin-right: 4px; text-decoration: none; display: inline-block; }
mark { background: var(--mark); color: inherit; border-radius: 3px; padding: 0 2px; }
button.more { margin: 16px 0; font: inherit; padding: 8px 16px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--card); color: inherit; cursor: pointer; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; display: block; overflow-x: auto; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 10px; text-align: left; }
td.num, th.num { text-align: right; }
details { margin: 6px 0; } summary { cursor: pointer; }
.page article.msg { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  margin: 14px 0; padding: 4px 14px 10px; }
.page article.msg > header { color: var(--muted); font-size: .8rem; padding-top: 8px; text-transform: uppercase; letter-spacing: .04em; }
.page article.user { border-left: 4px solid var(--accent); }
.page .body { overflow-wrap: anywhere; }
.page .body pre { background: rgba(127,127,127,.12); padding: 10px; border-radius: 8px; overflow-x: auto; }
.page .body code { font: .88em ui-monospace, SFMono-Regular, Menlo, monospace; }
.page .related li { margin: 2px 0; }
.page h1 { font-size: 1.4rem; margin: 18px 0 6px; }
.attach { color: var(--muted); font-size: .85rem; margin: 6px 0 0; }
.banner { color: var(--muted); font-size: .85rem; border-left: 3px solid var(--line); padding-left: 10px; }
canvas#map { width: 100%; height: 70vh; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; }
#tip { position: fixed; pointer-events: none; background: var(--card); border: 1px solid var(--line);
  border-radius: 6px; padding: 4px 8px; font-size: .85rem; display: none; }
"""

INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="__CSP__">
<meta name="referrer" content="no-referrer">
<title>AI Archive</title>
<link rel="stylesheet" href="__STYLE__">
</head>
<body>
<div class="wrap">
<header class="top">
  <h1>AI Archive</h1>
  <p id="stats">Loading…</p>
  <nav><a href="__GRAPH_PAGE__">Topic map</a><a href="#coverage">Coverage</a></nav>
</header>
<noscript><p>This archive needs JavaScript for search. Conversation pages under
<code>__PAGES__/</code> work without it.</p></noscript>
<section class="controls" aria-label="Search and filters">
  <div class="row">
    <label class="sr-only" for="q">Search</label>
    <input id="q" type="search" placeholder="Search titles, snippets and topics…" autocomplete="off">
    <label class="muted"><input type="checkbox" id="fts" disabled> Full text
      <span id="ftsnote"></span></label>
  </div>
  <div id="providers" class="chips" role="group" aria-label="Providers"></div>
  <div class="row">
    <label class="sr-only" for="kind">Kind</label><select id="kind"></select>
    <label class="sr-only" for="year">Year</label><select id="year"></select>
    <label class="sr-only" for="sort">Sort</label>
    <select id="sort">
      <option value="new">Newest first</option>
      <option value="old">Oldest first</option>
      <option value="long">Most messages</option>
    </select>
  </div>
</section>
<p id="count" class="muted" role="status" aria-live="polite"></p>
<ol id="results" class="results"></ol>
<button id="more" class="more" hidden>Show more</button>
<section id="coverage" aria-label="Coverage"><h2>Coverage</h2><div id="coverage-body"></div></section>
<p class="banner">Conversation text is a record of speech acts, not of verified events.</p>
</div>
<script src="__INDEX_DATA__"></script>
<script src="__HOME_SCRIPT__"></script>
</body>
</html>
"""

GRAPH_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="__CSP__">
<title>AI Archive — topic map</title>
<link rel="stylesheet" href="__STYLE__">
</head>
<body>
<div class="wrap">
<header class="top"><h1>Topic map</h1>
<p>Each node is a topic hub; size is the number of conversations, colour the dominant
provider, lines link topics that share conversations. Click a node to open its conversations.</p>
<nav><a href="__HOME__">← Archive</a></nav></header>
<canvas id="map" role="img" aria-label="Topic co-occurrence map"></canvas>
<div id="tip"></div>
<noscript><p>The topic map needs JavaScript.</p></noscript>
</div>
<script src="__TOPIC_DATA__"></script>
<script src="__GRAPH_SCRIPT__"></script>
</body>
</html>
"""

APP_JS = """\
(function () {
  'use strict';
  var D = window.CCE;
  var COL = { uid: 0, provider: 1, kind: 2, title: 3, created: 4, msgs: 5, snippet: 6, topics: 7 };
  var PAGE = 100;
  var rows = D.index;
  var state = { q: '', p: [], k: '', y: '', t: '', s: 'new', fts: false };
  var shown = 0;
  var current = [];
  var ftsAvailable = false;
  var ftsTimer = 0;
  var els = {};
  ['q', 'fts', 'ftsnote', 'providers', 'kind', 'year', 'sort', 'count', 'results', 'more',
   'stats', 'coverage-body'].forEach(function (id) { els[id] = document.getElementById(id); });

  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === 'text') { node.textContent = attrs[key]; } else { node.setAttribute(key, attrs[key]); }
    });
    (kids || []).forEach(function (kid) { node.appendChild(kid); });
    return node;
  }
  function pageHref(uid) { return '__PAGES__/__PAGE_PREFIX__' + encodeURIComponent(uid) + '.html'; }

  function readHash() {
    var out = { q: '', p: [], k: '', y: '', t: '', s: 'new', fts: false };
    location.hash.replace(/^#/, '').split('&').forEach(function (pair) {
      if (!pair) { return; }
      var i = pair.indexOf('=');
      var key = decodeURIComponent(i < 0 ? pair : pair.slice(0, i));
      var val = i < 0 ? '' : decodeURIComponent(pair.slice(i + 1));
      if (key === 'p') { out.p = val ? val.split(',') : []; }
      else if (key === 'fts') { out.fts = val === '1'; }
      else if (key in out) { out[key] = val; }
    });
    return out;
  }
  function writeHash() {
    var parts = [];
    ['q', 'k', 'y', 't', 's'].forEach(function (key) {
      if (state[key] && !(key === 's' && state.s === 'new')) { parts.push(key + '=' + encodeURIComponent(state[key])); }
    });
    if (state.p.length) { parts.push('p=' + encodeURIComponent(state.p.join(','))); }
    if (state.fts) { parts.push('fts=1'); }
    history.replaceState(null, '', parts.length ? '#' + parts.join('&') : location.pathname + location.search);
  }

  function matches(row) {
    if (state.p.length && state.p.indexOf(row[COL.provider]) < 0) { return false; }
    if (state.k && row[COL.kind] !== state.k) { return false; }
    if (state.y && row[COL.created].slice(0, 4) !== state.y) { return false; }
    if (state.t && row[COL.topics].indexOf(state.t) < 0) { return false; }
    if (!state.q) { return true; }
    var hay = (row[COL.title] + ' ' + row[COL.snippet] + ' ' + row[COL.topics].join(' ') + ' ' + row[COL.provider]).toLowerCase();
    return state.q.toLowerCase().split(/\\s+/).every(function (token) { return !token || hay.indexOf(token) >= 0; });
  }
  function order(a, b) {
    if (state.s === 'long') { return b[COL.msgs] - a[COL.msgs] || (a[COL.uid] < b[COL.uid] ? -1 : 1); }
    var cmp = a[COL.created] < b[COL.created] ? -1 : a[COL.created] > b[COL.created] ? 1 : 0;
    if (cmp === 0) { cmp = a[COL.uid] < b[COL.uid] ? -1 : 1; }
    return state.s === 'old' ? cmp : -cmp;
  }

  function badge(provider) { return el('span', { 'class': 'dot ' + provider }); }
  function topicLinks(topics) {
    var wrap = el('div', {});
    topics.slice(0, 6).forEach(function (term) {
      var a = el('a', { 'class': 'topic', href: '#t=' + encodeURIComponent(term), text: term });
      a.addEventListener('click', function () { setTimeout(function () { state = readHash(); sync(); }, 0); });
      wrap.appendChild(a);
    });
    return wrap;
  }
  function renderRow(row) {
    var title = el('h3', {}, [el('a', { href: pageHref(row[COL.uid]), text: row[COL.title] || 'Untitled' })]);
    var meta = el('div', { 'class': 'meta' }, [badge(row[COL.provider]),
      document.createTextNode(row[COL.provider] + ' · ' + row[COL.kind] + ' · ' + (row[COL.created].slice(0, 10) || 'undated') + ' · ' + row[COL.msgs] + ' msg')]);
    return el('li', { 'class': 'result' }, [title, meta, el('p', { text: row[COL.snippet] }), topicLinks(row[COL.topics])]);
  }
  function renderExcerpt(text) {
    var p = el('p', {});
    text.split(/(\\[[^\\]]*\\])/).forEach(function (piece) {
      if (piece.length > 2 && piece.charAt(0) === '[' && piece.charAt(piece.length - 1) === ']') {
        p.appendChild(el('mark', { text: piece.slice(1, -1) }));
      } else if (piece) { p.appendChild(document.createTextNode(piece)); }
    });
    return p;
  }
  function renderHit(hit) {
    return el('li', { 'class': 'result' }, [
      el('h3', {}, [el('a', { href: pageHref(hit.uid), text: hit.title || 'Untitled' })]),
      el('div', { 'class': 'meta' }, [badge(hit.provider), document.createTextNode(hit.provider + ' · ' + hit.kind + ' · ' + (hit.created || '').slice(0, 10) + ' · ' + hit.role + ' message ' + (hit.idx + 1))]),
      renderExcerpt(hit.excerpt)]);
  }

  function renderPage() {
    var end = Math.min(current.length, shown + PAGE);
    for (; shown < end; shown++) { els.results.appendChild(renderRow(current[shown])); }
    els.more.hidden = shown >= current.length;
  }
  function renderRows() {
    els.results.textContent = '';
    current = rows.filter(matches).sort(order);
    shown = 0;
    els.count.textContent = current.length + ' of ' + rows.length + ' conversations';
    renderPage();
  }
  function renderFulltext() {
    var seq = ++ftsTimer;
    var url = '/api/search?q=' + encodeURIComponent(state.q) + '&limit=50' +
      (state.p.length ? '&provider=' + encodeURIComponent(state.p.join(',')) : '') +
      (state.k ? '&kind=' + encodeURIComponent(state.k) : '');
    els.count.textContent = 'Searching…';
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (seq !== ftsTimer) { return; }
      els.results.textContent = '';
      els.more.hidden = true;
      els.count.textContent = data.results.length + ' conversations with matching messages (best hit each)';
      data.results.forEach(function (hit) { els.results.appendChild(renderHit(hit)); });
    }).catch(function () { els.count.textContent = 'Full-text search is unavailable.'; });
  }
  function sync() {
    els.q.value = state.q; els.kind.value = state.k; els.year.value = state.y; els.sort.value = state.s;
    els.fts.checked = state.fts && ftsAvailable;
    Array.prototype.forEach.call(els.providers.children, function (chip) {
      chip.setAttribute('aria-pressed', state.p.indexOf(chip.getAttribute('data-p')) >= 0 ? 'true' : 'false');
    });
    writeHash();
    if (state.fts && ftsAvailable && state.q) { renderFulltext(); } else { renderRows(); }
  }

  function fillSelect(select, label, values) {
    select.textContent = '';
    select.appendChild(el('option', { value: '', text: label }));
    values.forEach(function (v) { select.appendChild(el('option', { value: v, text: v })); });
  }
  function init() {
    var counts = {}; var kinds = {}; var years = {};
    rows.forEach(function (row) {
      counts[row[COL.provider]] = (counts[row[COL.provider]] || 0) + 1;
      kinds[row[COL.kind]] = 1;
      years[row[COL.created].slice(0, 4)] = 1;
    });
    Object.keys(counts).sort().forEach(function (name) {
      var chip = el('button', { 'class': 'chip', type: 'button', 'data-p': name, 'aria-pressed': 'false' }, [badge(name), document.createTextNode(name + ' ' + counts[name])]);
      chip.addEventListener('click', function () {
        var i = state.p.indexOf(name);
        if (i < 0) { state.p.push(name); } else { state.p.splice(i, 1); }
        sync();
      });
      els.providers.appendChild(chip);
    });
    fillSelect(els.kind, 'All kinds', Object.keys(kinds).sort());
    fillSelect(els.year, 'All years', Object.keys(years).filter(Boolean).sort().reverse());
    els.q.addEventListener('input', function () { state.q = els.q.value.trim(); sync(); });
    els.kind.addEventListener('change', function () { state.k = els.kind.value; sync(); });
    els.year.addEventListener('change', function () { state.y = els.year.value; sync(); });
    els.sort.addEventListener('change', function () { state.s = els.sort.value; sync(); });
    els.fts.addEventListener('change', function () { state.fts = els.fts.checked; sync(); });
    els.more.addEventListener('click', renderPage);
    window.addEventListener('hashchange', function () { state = readHash(); sync(); });
    var total = D.totals;
    els.stats.textContent = total.conversations + ' conversations · ' + total.messages + ' messages · fingerprint ' + D.fingerprint.slice(0, 12);
    renderCoverage();
    state = readHash();
    sync();
    if (location.protocol.indexOf('http') === 0) {
      fetch('/api/ping').then(function (r) { return r.json(); }).then(function (info) {
        if (info && info.ok) {
          ftsAvailable = true; els.fts.disabled = false; els.ftsnote.textContent = '';
          sync();
        }
      }).catch(function () {});
    }
    if (!ftsAvailable) { els.ftsnote.textContent = '(run `cce archive serve` to enable)'; }
  }

  function renderCoverage() {
    var table = el('table', {});
    var head = el('tr', {});
    ['Provider', 'Status', 'Conversations', 'Messages', 'First', 'Last activity'].forEach(function (h, i) {
      head.appendChild(el('th', { 'class': i > 1 && i < 4 ? 'num' : '', text: h }));
    });
    table.appendChild(head);
    D.providers.forEach(function (p) {
      var tr = el('tr', {});
      [p.display, p.status, p.conversations, p.messages, p.first, p.last].forEach(function (v, i) {
        tr.appendChild(el('td', { 'class': i === 2 || i === 3 ? 'num' : '', text: String(v === undefined || v === '' ? '—' : v) }));
      });
      table.appendChild(tr);
    });
    els['coverage-body'].appendChild(table);
    D.providers.forEach(function (p) {
      var read = el('ul', {}); p.read.forEach(function (x) { read.appendChild(el('li', { text: x })); });
      var unread = el('ul', {}); p.not_read.forEach(function (x) { unread.appendChild(el('li', { text: x })); });
      var box = el('details', {}, [el('summary', { text: p.display + ' — channels read / not read' }),
        el('strong', { text: 'Read' }), read, el('strong', { text: 'Not read' }), unread]);
      els['coverage-body'].appendChild(box);
    });
  }
  init();
})();
"""

GRAPH_JS = """\
(function () {
  'use strict';
  var G = window.CCE_GRAPH;
  var canvas = document.getElementById('map');
  var tip = document.getElementById('tip');
  var COLORS = { chatgpt: '#10a37f', claude: '#d97757', gemini: '#4285f4', notebooklm: '#8e6bd6',
                 grok: '#7a7a7a', perplexity: '#20b8cd' };
  var nodes = G.nodes.map(function (n, i) {
    var a = (i / G.nodes.length) * Math.PI * 2;
    return { id: n.id, label: n.label, n: n.n, color: COLORS[n.dominant] || '#888',
             x: Math.cos(a) * 200 + (i % 7), y: Math.sin(a) * 200 + (i % 5), vx: 0, vy: 0,
             r: 4 + Math.sqrt(n.n) * 2.2 };
  });
  var edges = G.edges;
  function layout() {
    for (var step = 0; step < 260; step++) {
      var cool = 1 - step / 260;
      for (var i = 0; i < nodes.length; i++) {
        var a = nodes[i];
        for (var j = i + 1; j < nodes.length; j++) {
          var b = nodes[j];
          var dx = a.x - b.x, dy = a.y - b.y;
          var d2 = dx * dx + dy * dy + 0.01;
          var f = 900 / d2;
          var d = Math.sqrt(d2);
          a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
        }
        a.vx -= a.x * 0.002; a.vy -= a.y * 0.002;
      }
      edges.forEach(function (e) {
        var a = nodes[e[0]], b = nodes[e[1]];
        var dx = b.x - a.x, dy = b.y - a.y;
        var d = Math.sqrt(dx * dx + dy * dy) + 0.01;
        var f = (d - 70) * 0.012 * Math.min(3, e[2]);
        a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
      });
      nodes.forEach(function (n) {
        n.x += Math.max(-8, Math.min(8, n.vx)) * cool; n.y += Math.max(-8, Math.min(8, n.vy)) * cool;
        n.vx *= 0.6; n.vy *= 0.6;
      });
    }
  }
  var view = { s: 1, ox: 0, oy: 0 };
  function fit() {
    var xs = nodes.map(function (n) { return n.x; }), ys = nodes.map(function (n) { return n.y; });
    var minx = Math.min.apply(null, xs), maxx = Math.max.apply(null, xs);
    var miny = Math.min.apply(null, ys), maxy = Math.max.apply(null, ys);
    var w = canvas.width, h = canvas.height;
    view.s = Math.min(w / (maxx - minx + 80), h / (maxy - miny + 80)) || 1;
    view.ox = w / 2 - ((minx + maxx) / 2) * view.s; view.oy = h / 2 - ((miny + maxy) / 2) * view.s;
  }
  function draw(hover) {
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.lineWidth = 1;
    edges.forEach(function (e) {
      var a = nodes[e[0]], b = nodes[e[1]];
      ctx.strokeStyle = 'rgba(127,127,127,' + Math.min(0.5, 0.08 + e[2] * 0.05) + ')';
      ctx.beginPath(); ctx.moveTo(a.x * view.s + view.ox, a.y * view.s + view.oy);
      ctx.lineTo(b.x * view.s + view.ox, b.y * view.s + view.oy); ctx.stroke();
    });
    nodes.forEach(function (n) {
      ctx.fillStyle = n.color; ctx.globalAlpha = hover && hover !== n ? 0.55 : 0.95;
      ctx.beginPath(); ctx.arc(n.x * view.s + view.ox, n.y * view.s + view.oy, n.r * Math.max(0.7, view.s * 0.8), 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalAlpha = 1;
    ctx.fillStyle = getComputedStyle(document.body).color; ctx.font = '11px system-ui, sans-serif';
    nodes.forEach(function (n) {
      if (n.n >= 8 || hover === n) { ctx.fillText(n.label, n.x * view.s + view.ox + n.r + 3, n.y * view.s + view.oy + 3); }
    });
  }
  function resize() {
    var rect = canvas.getBoundingClientRect();
    var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(300, rect.width * ratio); canvas.height = Math.max(300, rect.height * ratio);
    fit(); draw(null);
  }
  function pick(ev) {
    var rect = canvas.getBoundingClientRect();
    var ratio = canvas.width / rect.width;
    var mx = (ev.clientX - rect.left) * ratio, my = (ev.clientY - rect.top) * ratio;
    var best = null, bestD = 1e9;
    nodes.forEach(function (n) {
      var dx = n.x * view.s + view.ox - mx, dy = n.y * view.s + view.oy - my;
      var d = Math.sqrt(dx * dx + dy * dy);
      if (d < Math.max(10, n.r * view.s * 1.2) && d < bestD) { best = n; bestD = d; }
    });
    return best;
  }
  canvas.addEventListener('mousemove', function (ev) {
    var n = pick(ev);
    draw(n);
    if (n) {
      tip.style.display = 'block'; tip.style.left = ev.clientX + 12 + 'px'; tip.style.top = ev.clientY + 12 + 'px';
      tip.textContent = n.label + ' — ' + n.n + ' conversations';
    } else { tip.style.display = 'none'; }
  });
  canvas.addEventListener('mouseleave', function () { tip.style.display = 'none'; draw(null); });
  canvas.addEventListener('click', function (ev) {
    var n = pick(ev);
    if (n) { location.href = '__HOME__#t=' + encodeURIComponent(n.label); }
  });
  window.addEventListener('resize', resize);
  layout();
  resize();
})();
"""
