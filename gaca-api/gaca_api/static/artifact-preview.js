/* GACA artifact PREVIEW shim — only in pages from POST /api/artifacts/preview, never in a real page.
 * It stands in for the app: an in-memory `GacaBridge` serving the seed data (writes work but are never saved),
 * and a reporter that records what went wrong so a headless browser (the skill's `preview` command) can read it
 * back: errors, CSP violations, console errors, the calls the page made, and layout problems. The report is
 * kept as JSON in a script element with id gaca-preview-report, and posted to the parent window when the
 * page runs inside the helper's phone-width frame. */
(function () {
  'use strict';
  var seed = window.__GACA_PREVIEW_SEED || {};
  var state = clone(seed.state || {}), stateVersion = Object.keys(state).length ? 1 : 0;
  var cols = {}, rev = 1, seq = 0;
  var report = { errors: [], warnings: [], calls: {}, toasts: [], emits: [], asks: [], urls: [], layout: null };

  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  function now() { return new Date().toISOString(); }
  function mkdoc(id, data, created) { return { id: id, version: 1, data: data, created_at: created || now(), updated_at: now() }; }
  Object.keys(seed.collections || {}).forEach(function (name) {
    cols[name] = {};
    var docs = seed.collections[name] || {};
    Object.keys(docs).forEach(function (id) { cols[name][id] = mkdoc(id, clone(docs[id])); });
  });

  function merge(target, patch) {   // the API's rule: objects merge, null removes, arrays (and the rest) replace
    Object.keys(patch).forEach(function (k) {
      var v = patch[k];
      if (v === null) delete target[k];
      else if (v && typeof v === 'object' && !Array.isArray(v) && target[k] && typeof target[k] === 'object' && !Array.isArray(target[k])) merge(target[k], v);
      else target[k] = clone(v);
    });
    return target;
  }
  function fail(status, message) { var e = new Error(message); e.status = status; throw e; }
  function col(name) { return (cols[name] = cols[name] || {}); }
  function list(name) { return Object.values(col(name)).sort(function (a, b) { return a.created_at < b.created_at ? -1 : 1; }); }
  function snapshot(since) {
    if (since != null && since === rev) return { rev: rev, changed: false };
    var out = {};
    Object.keys(cols).forEach(function (n) { if (Object.keys(cols[n]).length) out[n] = list(n); });
    return { rev: rev, state: state, state_version: stateVersion, collections: out };
  }
  function write(fn) { var r = fn(); rev++; return r; }
  function checkVersion(cur, expected) { if (expected != null && expected !== cur) fail(409, 'version conflict (preview)'); }

  function handle(method, a) {
    switch (method) {
      case 'data': return snapshot(a.since_rev);
      case 'state.get': return { state: state, version: stateVersion };
      case 'state.set': return write(function () { checkVersion(stateVersion, a.expected_version); state = clone(a.state || {}); stateVersion++; return { state: state, version: stateVersion }; });
      case 'state.patch': return write(function () { checkVersion(stateVersion, a.expected_version); merge(state, a.patch || {}); stateVersion++; return { state: state, version: stateVersion }; });
      case 'doc.list': return { collection: a.collection, rev: rev, docs: list(a.collection) };
      case 'doc.add': return write(function () {
        var id = a.id || (Math.random().toString(36).slice(2, 10));
        if (col(a.collection)[id]) fail(409, a.collection + '/' + id + ' already exists');
        return (col(a.collection)[id] = mkdoc(id, clone(a.data || {})));
      });
      case 'doc.set': return write(function () {
        var cur = col(a.collection)[a.id];
        checkVersion(cur ? cur.version : 0, a.expected_version);
        var d = mkdoc(a.id, clone(a.data || {}), cur && cur.created_at); d.version = cur ? cur.version + 1 : 1;
        return (col(a.collection)[a.id] = d);
      });
      case 'doc.patch': return write(function () {
        var cur = col(a.collection)[a.id];
        if (!cur) fail(404, a.collection + '/' + a.id + ' not found');
        checkVersion(cur.version, a.expected_version);
        merge(cur.data, a.patch || {}); cur.version++; cur.updated_at = now(); return cur;
      });
      case 'doc.remove': return write(function () { if (!col(a.collection)[a.id]) fail(404, a.collection + '/' + a.id + ' not found'); delete cols[a.collection][a.id]; return null; });
      case 'doc.clear': return write(function () { cols[a.collection] = {}; return null; });
      case 'batch': return write(function () {
        (a.ops || []).forEach(function (op) {
          var m = { set: 'doc.set', patch: 'doc.patch', delete: 'doc.remove', clear: 'doc.clear' }[op.op];
          if (!m) fail(422, 'unknown batch op ' + op.op);
          handle(m, { collection: op.collection, id: op.id, data: op.data, patch: op.data });
        });
        return { rev: rev };
      });
      case 'emit': report.emits.push({ name: a.name, payload: a.payload }); return { seq: ++seq };
      case 'ask': report.asks.push(String(a.text)); return null;
      case 'openUrl': report.urls.push(String(a.url)); return null;
      case 'toast': report.toasts.push(String(a.message)); return null;
      default: fail(400, 'unknown method ' + method);
    }
  }

  window.GacaBridge = {
    call: function (id, method, argsJson) {
      report.calls[method] = (report.calls[method] || 0) + 1;
      var ok = true, out;
      try { out = handle(method, JSON.parse(argsJson || '{}')); }
      catch (e) { ok = false; out = { status: e.status || 500, message: e.message }; }
      setTimeout(function () { window.__gaca && window.__gaca.resolve(id, ok, JSON.stringify(out == null ? null : out)); }, 0);
    }
  };

  // ── the reporter ──
  function text(v) { try { return typeof v === 'string' ? v : (v && v.message) || JSON.stringify(v); } catch (e) { return String(v); } }
  function push(list, msg) { msg = String(msg).slice(0, 400); if (list.length < 30 && list.indexOf(msg) < 0) list.push(msg); save(); }
  window.addEventListener('error', function (e) { push(report.errors, 'Uncaught ' + (e.message || 'error') + (e.lineno ? ' (line ' + e.lineno + ')' : '')); });
  window.addEventListener('unhandledrejection', function (e) { push(report.errors, 'Unhandled promise rejection: ' + text(e.reason)); });
  document.addEventListener('securitypolicyviolation', function (e) {
    push(report.errors, 'Blocked by the page sandbox (' + e.violatedDirective + '): ' + (e.blockedURI || 'inline') + '. Pages have no network; use the library shelf or data: URIs.');
  });
  ['error', 'warn'].forEach(function (level) {
    var orig = console[level];
    console[level] = function () {
      push(level === 'error' ? report.errors : report.warnings, 'console.' + level + ': ' + Array.prototype.map.call(arguments, text).join(' '));
      return orig.apply(console, arguments);
    };
  });

  function describe(el) {
    var s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    else if (el.className && typeof el.className === 'string' && el.className.trim()) s += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.');
    return s;
  }
  function layout() {
    var de = document.documentElement, body = document.body, w = window.innerWidth, issues = [];
    if (!body) return { issues: ['no <body>'] };
    if (de.scrollWidth > w + 1) {
      var wide = [];
      Array.prototype.forEach.call(body.querySelectorAll('*'), function (el) {
        var r = el.getBoundingClientRect();
        if (r.right > w + 1 && wide.length < 5 && getComputedStyle(el).position !== 'fixed') {
          var p = el.parentElement, clipped = false;
          while (p && p !== body) { var ox = getComputedStyle(p).overflowX; if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') { clipped = true; break; } p = p.parentElement; }
          if (!clipped) wide.push(describe(el) + ' (' + Math.round(r.width) + 'px wide)');
        }
      });
      issues.push('The page scrolls sideways: ' + de.scrollWidth + 'px wide in a ' + w + 'px screen' + (wide.length ? '. Too wide: ' + wide.join(', ') : ''));
    }
    Array.prototype.forEach.call(body.querySelectorAll('canvas, svg, #root, #app, main, [id]'), function (el) {
      var r = el.getBoundingClientRect();
      if (r.height < 2 && r.width > 0 && el.childElementCount + (el.tagName === 'CANVAS' ? 1 : 0) > 0 && issues.length < 10)
        issues.push(describe(el) + ' has no height (0px): size it with var(--app-h, 600px), not vh/dvh/height:100%');
    });
    var visible = (body.innerText || '').trim().length;
    var drawn = body.querySelectorAll('canvas, svg, img').length;
    if (!visible && !drawn) issues.push('Nothing visible rendered (no text, canvas, svg or image).');
    return { width: w, height: de.scrollHeight, text_chars: visible, drawn_elements: drawn, issues: issues };
  }
  function save() {
    var node = document.getElementById('gaca-preview-report');
    if (!node) {
      if (!document.head) return;
      node = document.createElement('script');
      node.type = 'application/json';
      node.id = 'gaca-preview-report';
      document.head.appendChild(node);
    }
    node.textContent = JSON.stringify(report).replace(/</g, '\\u003c');
    if (window.parent !== window) { try { window.parent.postMessage({ gacaPreviewReport: report }, '*'); } catch (e) { /* no parent */ } }
  }
  function final() { try { report.layout = layout(); } catch (e) { report.layout = { issues: ['layout check failed: ' + e.message] }; } save(); }
  document.addEventListener('DOMContentLoaded', save);
  window.addEventListener('load', function () { setTimeout(final, 400); setTimeout(final, 1500); });
  window.__gacaPreviewReport = function () { final(); return report; };
})();
