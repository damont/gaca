/* GACA artifact SDK — injected into every artifact page before its own scripts run
 * (by the API: GET /api/artifacts/{id}/page, services/artifact_page.py).
 * The page is a stateless view: everything it remembers goes through `app.*`, which the
 * app forwards to the GACA API (so an agent can read and write the same data). The page
 * never sees a token and can't make network requests of its own. */
(function () {
  'use strict';
  var init = window.__GACA_INIT || {};

  // Browser storage is switched off in this WebView (the page's data belongs in `app.*`, where the
  // agent can see it). A page that reaches for localStorage anyway gets a throwaway in-memory one
  // instead of crashing — it won't survive closing the page.
  ['localStorage', 'sessionStorage'].forEach(function (name) {
    try { void window[name].length; return; } catch (e) { /* unavailable: shim it */ }
    var data = {};
    var shim = {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
      setItem: function (k, v) { data[k] = String(v); },
      removeItem: function (k) { delete data[k]; },
      clear: function () { data = {}; },
      key: function (i) { return Object.keys(data)[i] || null; },
      get length() { return Object.keys(data).length; }
    };
    try { Object.defineProperty(window, name, { value: shim, configurable: true }); } catch (e) { /* leave it */ }
  });
  var pending = {};
  var nextId = 1;
  var subs = [];
  var lastRev = null;
  var lastSnap = null;
  var inFlight = 0;
  var polling = false;
  var pollAgain = false;

  // The app's side of the bridge: Android injects `GacaBridge` (addJavascriptInterface); iOS
  // registers a `gaca` WKScriptMessageHandler. Either way the answer comes back through
  // `window.__gaca.resolve(id, ok, json)` below.
  function send(id, method, args) {
    if (window.GacaBridge) {
      window.GacaBridge.call(id, method, JSON.stringify(args));
      return;
    }
    var ios = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.gaca;
    if (ios) {
      ios.postMessage({ id: id, method: method, args: args });
      return;
    }
    throw new Error('no app bridge');
  }

  function call(method, args) {
    return new Promise(function (resolve, reject) {
      var id = String(nextId++);
      pending[id] = { resolve: resolve, reject: reject };
      try {
        send(id, method, args || {});
      } catch (e) {
        delete pending[id];
        reject(e);
      }
    });
  }

  // A write changes the data on the server, but the page only learns of it by fetching again. So the moment a
  // write finishes (or fails: the page may be out of date), fetch right away instead of waiting for the next
  // timer tick; a user's own edit then shows up in about one round trip rather than up to 4 seconds later.
  function write(method, args) {
    inFlight++;
    return call(method, args).then(
      function (v) { inFlight--; poll(true); return v; },
      function (e) { inFlight--; poll(true); throw e; }
    );
  }

  function doc(d) {
    return { id: d.id, version: d.version, createdAt: d.created_at, updatedAt: d.updated_at, data: d.data };
  }

  function snapshot(raw) {
    var collections = {};
    Object.keys(raw.collections || {}).forEach(function (name) {
      collections[name] = raw.collections[name].map(doc);
    });
    return {
      rev: raw.rev,
      state: raw.state || {},
      stateVersion: raw.state_version || 0,
      collections: collections,
      /** The documents in a collection as plain objects, `{id, ...data}`, oldest first. */
      docs: function (name) {
        return (collections[name] || []).map(function (d) {
          return Object.assign({}, d.data, { id: d.id });
        });
      }
    };
  }

  function load() {
    return call('data', {}).then(function (raw) {
      lastRev = raw.rev;
      lastSnap = snapshot(raw);
      return lastSnap;
    });
  }

  function poll(force) {
    if (subs.length === 0) return;
    // A fetch already under way may have been answered before the write that asked for this one, so ask again
    // once it lands. And while a write is in flight don't fetch: the write's own completion will.
    if (polling) { if (force) pollAgain = true; return; }
    if (inFlight > 0) return;
    if (!force && document.visibilityState === 'hidden') return;
    polling = true;
    call('data', { since_rev: lastRev }).then(function (raw) {
      polling = false;
      if (raw.changed !== false) {
        lastRev = raw.rev;
        lastSnap = snapshot(raw);
        subs.slice().forEach(function (cb) { try { cb(lastSnap); } catch (e) { console.error(e); } });
      }
      again();
    }, function () { polling = false; again(); });
  }
  function again() { if (pollAgain) { pollAgain = false; poll(true); } }
  setInterval(poll, 4000);
  document.addEventListener('visibilitychange', function () { poll(true); });

  function collection(name) {
    return {
      list: function () {
        return call('doc.list', { collection: name }).then(function (r) { return r.docs.map(doc); });
      },
      /** Add a document (id generated unless given). Resolves to {id, version, data}. */
      add: function (data, id) {
        return write('doc.add', { collection: name, data: data, id: id }).then(doc);
      },
      /** Create or replace a document. Pass {ifVersion: n} to fail instead of overwriting a newer one. */
      set: function (id, data, opts) {
        return write('doc.set', { collection: name, id: id, data: data, expected_version: opts && opts.ifVersion }).then(doc);
      },
      /** Merge fields into a document; a null value removes that field. */
      patch: function (id, patch, opts) {
        return write('doc.patch', { collection: name, id: id, patch: patch, expected_version: opts && opts.ifVersion }).then(doc);
      },
      remove: function (id) { return write('doc.remove', { collection: name, id: id }); },
      clear: function () { return write('doc.clear', { collection: name }); }
    };
  }

  window.__gaca = {
    resolve: function (id, ok, json) {
      var p = pending[id];
      if (!p) return;
      delete pending[id];
      var v = json ? JSON.parse(json) : null;
      if (ok) { p.resolve(v); return; }
      var err = new Error((v && v.message) || 'request failed');
      err.status = v && v.status;
      p.reject(err);
    },
    resume: function () { poll(true); }
  };

  // The page's dark tokens (see _THEME_CSS) switch on when the client reports a dark theme.
  if (init.theme && init.theme.dark) document.documentElement.setAttribute('data-dark', '');

  window.app = {
    artifact: init.artifact || {},
    theme: init.theme || {},
    /** Read everything once: {state, stateVersion, collections, docs(name), rev}. */
    load: load,
    /** Call cb now with the current data and again whenever it changes (also when the agent
     * changes it). Re-render from the snapshot; keep the user's focus and typing intact.
     * Returns an unsubscribe function. */
    subscribe: function (cb) {
      subs.push(cb);
      if (lastSnap) { try { cb(lastSnap); } catch (e) { console.error(e); } }
      else load().then(function (s) { subs.indexOf(cb) >= 0 && cb(s); });
      return function () { subs = subs.filter(function (x) { return x !== cb; }); };
    },
    state: {
      get: function () {
        return call('state.get', {}).then(function (r) { return r.state; });
      },
      /** Replace the whole state. Pass {ifVersion: n} to fail instead of overwriting a newer one. */
      set: function (state, opts) {
        return write('state.set', { state: state, expected_version: opts && opts.ifVersion }).then(function (r) { return r.state; });
      },
      /** Merge into the state (objects merge, null removes a key, arrays replace). */
      patch: function (patch, opts) {
        return write('state.patch', { patch: patch, expected_version: opts && opts.ifVersion }).then(function (r) { return r.state; });
      }
    },
    collection: collection,
    /** Several document writes, all or nothing: [{op:'set'|'patch'|'delete'|'clear', collection, id, data}]. */
    batch: function (ops) { return write('batch', { ops: ops }); },
    /** Tell the agent something happened (it reads these). */
    emit: function (name, payload) { return call('emit', { name: name, payload: payload || {} }); },
    /** Open the chat with this text as an unsent draft for the user to review and send. */
    ask: function (text) { return call('ask', { text: String(text) }); },
    /** Open a web address in the browser. */
    openUrl: function (url) { return call('openUrl', { url: String(url) }); },
    toast: function (message) { return call('toast', { message: String(message) }); },
    /** A short unique id for new documents. */
    uid: function () { return Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4); }
  };
})();
