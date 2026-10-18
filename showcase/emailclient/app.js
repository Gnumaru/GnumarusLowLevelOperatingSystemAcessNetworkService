"use strict";
/* Read-only IMAP inbox over the low-level socket proxy.
   Only these webservice functions are used: GET /api/help (for the
   socket_ws capability), POST /api/socket/open, POST /api/socket/send,
   GET /api/socket/recv, POST /api/socket/close and
   GET /api/socket/ws (WebSocket transport, preferred). To sign in
   with Google the service also does the OAuth2 dance on our behalf:
   POST /api/oauth2/start, GET /api/oauth2/poll, POST
   /api/oauth2/refresh and POST /api/oauth2/close (the Google
   endpoints and the redirect never appear in this page; only the
   local service is contacted). When the poll reports that the consent
   is done the page connects on its own, so a Google sign-in lands on
   the inbox just like a password one.
   Read-only by design: EXAMINE (never SELECT), BODY.PEEK (never
   setting \Seen), and no send/delete/flag commands exist here. */

var SERVER = "http://127.0.0.1:1423";
var PAGE_DEFAULT = 25;

/* Well-known providers. Ports 993 need implicit TLS: the proxy
   service terminates it (verified against the system roots, with
   SNI) when the session opens with tls:b:true — nothing in the
   IMAP engine changes. */
var PROVIDERS = {
  proton: { host: "127.0.0.1", port: 1143, tls: false,
    note: "Proton Bridge on this machine (plaintext 127.0.0.1:1143)." },
  gmail: { host: "imap.gmail.com", port: 993, tls: true,
    note: "Gmail: use an app password (LOGIN) or OAuth2 (XOAUTH2 via " +
      "the service, no app password needed). TLS is terminated by " +
      "the proxy service (verified, SNI imap.gmail.com)." },
  outlook: { host: "outlook.office365.com", port: 993, tls: true,
    note: "TLS is terminated by the proxy service (verified). " +
      "Modern accounts may need an app password." },
  icloud: { host: "imap.mail.me.com", port: 993, tls: true,
    note: "iCloud needs an app-specific password. TLS is terminated " +
      "by the proxy service (verified)." },
  yahoo: { host: "imap.mail.yahoo.com", port: 993, tls: true,
    note: "Yahoo needs an app password. TLS is terminated by the " +
      "proxy service (verified)." },
  custom: { host: "", port: 143, tls: false, note: "" }
};

var conn = null;      // ImapConn
var mailbox = { uids: [], exists: 0, page: 0, perPage: PAGE_DEFAULT };
var oauth = { sessionId: null, accessToken: null, obtainedAt: 0, win: null,
  navigated: false };

function $(id) { return document.getElementById(id); }
function showError(msg) {
  var el = $("error"); el.textContent = msg; el.hidden = false;
}
function clearError() { $("error").hidden = true; $("error").textContent = ""; }
function authHeaders() {
  var t = $("apiToken").value.trim();
  return t ? { "Authorization": "Bearer " + t } : {};
}
function apiPath(route, params) {
  var q = Object.keys(params || {}).map(function (k) {
    return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]);
  }).join("&");
  return route + (q ? "?" + q : "");
}
function proxyFetch(url, options) {
  options = options || {};
  options.headers = Object.assign({}, authHeaders(), options.headers || {});
  return fetch(SERVER + url, options).then(function (res) {
    return res.json().then(function (data) {
      if (!res.ok) { throw new Error((data && data.message) || ("HTTP " + res.status)); }
      return data;
    });
  });
}

/* Byte buffer: IMAP literals count BYTES, so framing stays on bytes
   (lines decode latin1 1:1; literal payloads decode per-part later). */
function ByteBuf() { this.bytes = []; }
ByteBuf.prototype.push = function (u8) {
  for (var i = 0; i < u8.length; i++) { this.bytes.push(u8[i]); }
};
ByteBuf.prototype.crlf = function () {
  for (var i = 0; i + 1 < this.bytes.length; i++) {
    if (this.bytes[i] === 13 && this.bytes[i + 1] === 10) { return i; }
  }
  return -1;
};
ByteBuf.prototype.takeLine = function () {
  var at = this.crlf();
  if (at < 0) { return null; }
  var out = this.bytes.slice(0, at);
  this.bytes = this.bytes.slice(at + 2);
  return out;
};
ByteBuf.prototype.take = function (n) {
  if (this.bytes.length < n) { return null; }
  var out = this.bytes.slice(0, n);
  this.bytes = this.bytes.slice(n);
  return out;
};
function latin1(u8) {
  var s = "", i, CH = 8192;
  for (i = 0; i < u8.length; i += CH) {
    s += String.fromCharCode.apply(null, u8.slice(i, i + CH));
  }
  return s;
}
function utf8bytes(str) { return new TextEncoder().encode(str); }
/* Base64 of a byte string (SASL XOAUTH2 payloads are ASCII + \x01). */
function base64Encode(str) {
  var bin = "", i;
  for (i = 0; i < str.length; i++) { bin += String.fromCharCode(str.charCodeAt(i) & 255); }
  return btoa(bin);
}

/* One IMAP connection over the socket proxy (WS preferred). */
function ImapConn() {
  this.id = null; this.ws = null; this.mode = null;
  this.buf = new ByteBuf(); this.waiters = [];
  this.tagN = 0; this.closed = false;
}
ImapConn.prototype._fetch = proxyFetch;

ImapConn.prototype.open = function (host, port, useTls) {
  var self = this;
  var body = { host: host, port: "i:" + port };
  if (useTls) { body.tls = "b:true"; }
  return this._fetch("/api/socket/open", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).then(function (data) { self.id = data.id; });
};

/* Read one full response (untagged lines + tagged completion),
   honouring {n} literals. Resolves {lines, literals, ok, tail}.
   literals[i] is the literal payload (Uint8Array) attached to
   lines[i], or null. A "+ ..." continuation line invokes onPlus once
   (used by AUTHENTICATE to send the empty abort line) and the read
   keeps going until the tagged completion arrives. */
ImapConn.prototype.readResponse = function (tag, timeoutMs, onPlus) {
  var self = this;
  var lines = [], lits = [];
  var plusSeen = false;
  var deadline = Date.now() + (timeoutMs || 30000);
  function pump() {
    for (;;) {
      var raw = self.buf.takeLine();
      if (raw === null) { return false; }
      var line = latin1(raw);
      var m = line.match(/\{(\d+)\}$/);
      if (m) {
        var n = parseInt(m[1], 10);
        var lit = self.buf.take(n);
        if (lit === null) {
          // Put the line back and wait for more bytes.
          self.buf.bytes = raw.concat([13, 10]).concat(self.buf.bytes);
          return false;
        }
        lines.push(line);
        lits.push(lit);
        continue;
      }
      lines.push(line);
      lits.push(null);
      if (line.charAt(0) === "+") {
        if (onPlus && !plusSeen) { plusSeen = true; onPlus(line); }
        continue;
      }
      if (line.indexOf(tag + " ") === 0 || line === tag) { return true; }
    }
  }
  return new Promise(function (resolve, reject) {
    function step() {
      if (self.closed) { reject(new Error("connection lost")); return; }
      var done;
      try { done = pump(); } catch (e) { reject(e); return; }
      if (done) {
        var last = lines[lines.length - 1] || "";
        var ok = new RegExp("^" + tag + " OK").test(last);
        resolve({ lines: lines, literals: lits, ok: ok, tail: last });
        return;
      }
      if (Date.now() > deadline) { reject(new Error("IMAP command timed out")); return; }
      self.waiters.push(step);
      self._kick();
    }
    step();
  });
};

ImapConn.prototype._kick = function () {
  // HTTP mode: one long-poll per kick; WS mode pushes wake waiters.
  var self = this;
  if (this.mode !== "http" || this.kicking || this.closed) { return; }
  this.kicking = true;
  this._fetch(apiPath("/api/socket/recv", { id: this.id, timeout: "5" }), {})
    .then(function (data) {
      self.kicking = false;
      var bin = atob(data.content || "");
      var u8 = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) { u8[i] = bin.charCodeAt(i); }
      var eof = data.eof === "b:true";
      var timedOut = data.timed_out === "b:true";
      if (u8.length) { self.buf.push(u8); }
      if (eof && !u8.length) { self._fail(new Error("server closed the connection")); return; }
      void timedOut;
      var w = self.waiters; self.waiters = [];
      w.forEach(function (fn) { fn(); });
      if (!self.closed && (self.waiters.length || u8.length)) { self._kick(); }
    }).catch(function (err) {
      self.kicking = false;
      if (!self.closed) { self._fail(err); }
    });
};

ImapConn.prototype._fail = function () {
  this.closed = true;
  var w = this.waiters; this.waiters = [];
  w.forEach(function (fn) { fn(); }); // steps see closed -> reject
};

/* Greeting: first line starting with "* " (any tag). */
ImapConn.prototype.readGreeting = function (timeoutMs) {
  var self = this;
  var deadline = Date.now() + (timeoutMs || 15000);
  return new Promise(function (resolve, reject) {
    function step() {
      if (self.closed) { reject(new Error("connection lost")); return; }
      for (;;) {
        var raw = self.buf.takeLine();
        if (raw === null) { break; }
        var line = latin1(raw);
        if (line.indexOf("* ") === 0) { resolve(line); return; }
        // Pre-greeting noise: ignore and keep reading.
      }
      if (Date.now() > deadline) {
        reject(new Error("No IMAP greeting (wrong host/port, or a TLS port without the provider's TLS preset)."));
        return;
      }
      self.waiters.push(step);
      self._kick();
    }
    step();
  });
};

ImapConn.prototype._onBytes = function (u8) {
  this.buf.push(u8);
  var w = this.waiters; this.waiters = [];
  w.forEach(function (fn) { fn(); });
};

ImapConn.prototype.send = function (text) {
  var self = this;
  if (this.ws && this.ws.readyState === 1) {
    this.ws.send(text);
    return Promise.resolve();
  }
  return this._fetch(apiPath("/api/socket/send", { id: this.id }), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: text, encoding: "text" })
  }).then(function () { self._kick(); });
};

/* A short, secret-free command label for error messages: never the
   LOGIN password, never the XOAUTH2 token. */
function cmdLabel(command) {
  return String(command).split(" ").slice(0, 2).join(" ");
}

ImapConn.prototype.cmd = function (command) {
  var self = this;
  this.tagN++;
  var tag = "a" + String(this.tagN).padStart(3, "0");
  return this.send(tag + " " + command + "\r\n").then(function () {
    return self.readResponse(tag, 30000);
  }).then(function (r) {
    if (!r.ok) {
      throw new Error("IMAP error (" + cmdLabel(command) + "): " + r.tail);
    }
    return r;
  });
};

/* Gmail OAuth2 (SASL XOAUTH2). The initial response carries
   "user=<addr>\x01auth=Bearer <token>\x01\x01", base64-encoded; on
   rejection the server sends a "+ <error>" continuation and expects
   an empty line before its tagged NO, which readResponse handles via
   the onPlus hook. */
ImapConn.prototype.authXoauth2 = function (user, token) {
  var self = this;
  this.tagN++;
  var tag = "a" + String(this.tagN).padStart(3, "0");
  var sasl = base64Encode("user=" + user + "\x01auth=Bearer " + token + "\x01\x01");
  var aborted = false;
  return this.send(tag + " AUTHENTICATE XOAUTH2 " + sasl + "\r\n").then(function () {
    return self.readResponse(tag, 30000, function () {
      if (!aborted) { aborted = true; self.send("\r\n"); }
    });
  }).then(function (r) {
    if (!r.ok) {
      throw new Error("IMAP error (AUTHENTICATE XOAUTH2): " + r.tail);
    }
    return r;
  });
};

ImapConn.prototype.attachWs = function (token) {
  var self = this;
  return new Promise(function (resolve, reject) {
    var base = SERVER.replace(/^http(s?):/, "ws$1:");
    var params = { id: self.id };
    if (token) { params.token = token; }
    var ws;
    try { ws = new WebSocket(base + apiPath("/api/socket/ws", params)); }
    catch (e) { reject(e); return; }
    ws.binaryType = "arraybuffer";
    var settled = false;
    ws.onopen = function () { settled = true; self.ws = ws; self.mode = "ws"; resolve(); };
    ws.onerror = function () {
      if (!settled) { settled = true; reject(new Error("WebSocket failed")); }
    };
    ws.onmessage = function (ev) {
      var u8;
      if (typeof ev.data === "string") { u8 = utf8bytes(ev.data); }
      else { u8 = new Uint8Array(ev.data); }
      self._onBytes(u8);
    };
    ws.onclose = function () {
      self.closed = true;
      var w = self.waiters; self.waiters = [];
      w.forEach(function (fn) { fn(); });
    };
  });
};

ImapConn.prototype.startHttp = function () { this.mode = "http"; this._kick(); };

ImapConn.prototype.close = function () {
  var self = this;
  this.closed = true;
  try { if (this.ws) { this.ws.close(); } } catch (e) { /* ignore */ }
  this.ws = null;
  if (!this.id) { return Promise.resolve(); }
  var id = this.id; this.id = null;
  return this._fetch(apiPath("/api/socket/close", { id: id }), { method: "POST" })
    .catch(function () { /* already gone */ });
};

function imapQuote(s) {
  return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
}

/* --- MIME --- */
function parseHeaders(text) {
  var headers = {}, last = null;
  text.split("\r\n").forEach(function (ln) {
    if (/^[ \t]/.test(ln) && last) { headers[last] += " " + ln.trim(); return; }
    var c = ln.indexOf(":");
    if (c < 0) { return; }
    last = ln.slice(0, c).trim().toLowerCase();
    var v = ln.slice(c + 1).trim();
    headers[last] = headers[last] ? headers[last] + ", " + v : v;
  });
  Object.keys(headers).forEach(function (k) { headers[k] = rfc2047(headers[k]); });
  return headers;
}
function rfc2047(s) {
  return String(s).replace(/=\?([^?]+)\?([bBqQ])\?([^?]*)\?=/g, function (m, cs, enc, data) {
    try {
      var bytes;
      if (enc.toUpperCase() === "B") {
        var bin = atob(data);
        bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) { bytes[i] = bin.charCodeAt(i); }
      } else {
        var raw = data.replace(/_/g, " ");
        var out = [];
        for (var j = 0; j < raw.length; j++) {
          if (raw[j] === "=" && j + 2 < raw.length) {
            out.push(parseInt(raw.slice(j + 1, j + 3), 16)); j += 2;
          } else { out.push(raw.charCodeAt(j)); }
        }
        bytes = new Uint8Array(out);
      }
      return new TextDecoder(cs).decode(bytes);
    } catch (e) { return data; }
  });
}
function decodeBody(bytes, encoding, charset) {
  var text = latin1(bytes);
  var raw;
  if (encoding === "base64") {
    var clean = text.replace(/\s/g, "");
    try {
      var bin = atob(clean);
      raw = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) { raw[i] = bin.charCodeAt(i); }
    } catch (e) { raw = utf8bytes(text); }
  } else if (encoding === "quoted-printable") {
    var out = [];
    for (var j = 0; j < text.length; j++) {
      if (text[j] === "=" && text[j + 1] === "\r" && text[j + 2] === "\n") { j += 2; }
      else if (text[j] === "=" && j + 2 < text.length) {
        out.push(parseInt(text.slice(j + 1, j + 3), 16)); j += 2;
      } else { out.push(text.charCodeAt(j)); }
    }
    raw = new Uint8Array(out);
  } else {
    raw = utf8bytes(text);
  }
  try { return new TextDecoder(charset || "utf-8").decode(raw); }
  catch (e) { return new TextDecoder("utf-8").decode(raw); }
}
function splitMultipart(body, boundary) {
  return body.split("--" + boundary).slice(1).map(function (p) {
    return p.replace(/^\r\n/, "").replace(/\r\n--\s*$/, "").replace(/\r\n$/, "");
  }).filter(function (p) { return p !== "" && p !== "--"; });
}
function pickTextPart(rawBytes) {
  // Returns {text, attachments:[{name,size}]} — text/plain preferred,
  // otherwise text/html stripped to text. Attachments listed only.
  var text = latin1(rawBytes);
  var headEnd = text.indexOf("\r\n\r\n");
  var topH = parseHeaders(headEnd >= 0 ? text.slice(0, headEnd) : "");
  var topB = headEnd >= 0 ? text.slice(headEnd + 4) : text;
  var ct = (topH["content-type"] || "text/plain").toLowerCase();
  var bm = ct.match(/boundary="?([^";]+)"?/);
  function partOf(h, bodyBytes) {
    var c = (h["content-type"] || "text/plain").toLowerCase();
    var enc = (h["content-transfer-encoding"] || "7bit").toLowerCase().trim();
    var csm = c.match(/charset="?([^";]+)"?/);
    var cs = csm ? csm[1].trim() : "utf-8";
    return { ct: c.split(";")[0].trim(), text: decodeBody(bodyBytes, enc, cs) };
  }
  function toBytes(s) {
    var u = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) { u[i] = s.charCodeAt(i) & 255; }
    return u;
  }
  if (!bm) {
    if (ct.indexOf("text/html") === 0) {
      return { text: htmlToText(partOf(topH, toBytes(topB)).text), attachments: [] };
    }
    return { text: partOf(topH, toBytes(topB)).text, attachments: [] };
  }
  var plain = null, html = null, attachments = [];
  splitMultipart(topB, bm[1].trim()).forEach(function (p) {
    var e = p.indexOf("\r\n\r\n");
    var h = parseHeaders(e >= 0 ? p.slice(0, e) : "");
    var b = toBytes(e >= 0 ? p.slice(e + 4) : p);
    var c = (h["content-type"] || "text/plain").toLowerCase();
    var disp = (h["content-disposition"] || "").toLowerCase();
    var fn = null, fm = disp.match(/filename="?([^";]+)"?/) || c.match(/name="?([^";]+)"?/);
    if (fm) { fn = rfc2047(fm[1]); }
    if (disp.indexOf("attachment") === 0 || (fn && c.indexOf("text/") !== 0)) {
      attachments.push({ name: fn || "unnamed", size: b.length });
      return;
    }
    var part = partOf(h, b);
    if (part.ct === "text/plain" && plain === null) { plain = part.text; }
    else if (part.ct === "text/html" && html === null) { html = part.text; }
    else if (fn) { attachments.push({ name: fn, size: b.length }); }
  });
  return { text: plain !== null ? plain : (html !== null ? htmlToText(html) : ""),
    attachments: attachments };
}
function htmlToText(html) {
  var div = document.createElement("div");
  div.innerHTML = html;
  return div.textContent || "";
}

/* --- session flow --- */
function setStatus(text, on) {
  $("statusText").textContent = text;
  $("dot").className = on ? "on" : "off";
}

function setConnected(on) {
  ["connectBtn"].forEach(function (id) { $(id).disabled = on; });
  ["disconnectBtn", "prevBtn", "nextBtn", "reloadBtn"].forEach(function (id) {
    $(id).disabled = !on;
  });
  $("mailbox").hidden = !on;
  if (!on) { $("viewer").hidden = true; }
}

/* --- OAuth2 (delegated to the local service) --- */
function delay(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}
function authMode() { return $("authMode").value === "oauth2" ? "oauth2" : "login"; }
function setOauthStatus(text, ok) {
  var el = $("oauthStatus");
  el.textContent = text;
  el.className = ok ? "on" : "";
  $("refreshTokenBtn").disabled = !(oauth.sessionId && oauth.accessToken);
  $("signOutBtn").disabled = !oauth.sessionId;
}
function setOauthLink(url) {
  var box = $("oauthLink");
  box.textContent = "";
  if (!url) { return; }
  var a = document.createElement("a");
  a.href = url; a.textContent = "Open the consent page";
  box.appendChild(document.createTextNode("Popup blocked? "));
  box.appendChild(a);
}
function startOAuth() {
  clearError();
  setOauthLink("");
  var clientId = $("oauthClientId").value.trim();
  if (!clientId) { showError("Fill in the OAuth client id."); return; }
  var clientSecret = $("oauthClientSecret").value;
  try {
    localStorage.setItem("showcase_oauth_client_id", clientId);
    localStorage.setItem("showcase_oauth_client_secret", clientSecret);
  } catch (e) { /* ignore */ }
  // Hold a blank popup across the async start call so it is not treated
  // as an unrequested popup and blocked.
  var win = window.open("", "gnumarus_oauth", "width=520,height=680");
  oauth.win = win;
  oauth.navigated = false;   // true once the popup left for the provider
  setOauthStatus("Starting sign-in…", false);
  var body = { client_id: clientId };
  if (clientSecret) { body.client_secret = clientSecret; }
  proxyFetch("/api/oauth2/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).then(function (data) {
    oauth.sessionId = data.id;
    oauth.accessToken = null;
    oauth.obtainedAt = 0;
    if (win) {
      win.location = data.auth_url;
      oauth.navigated = true;
    } else { setOauthLink(data.auth_url); }
    setOauthStatus("Waiting for consent…", false);
    return pollOAuth(0);
  }).then(function () {
    autoConnectAfterOAuth();
  }).catch(function (err) {
    // Only the blank, not-yet-navigated popup can be closed from here;
    // once it sits on the provider (or the callback page, which closes
    // itself) the cross-origin COOP would just block the call.
    if (win && !oauth.navigated) { try { win.close(); } catch (e) { /* ignore */ } }
    oauth.win = null;
    setOauthStatus("Not signed in", false);
    showError(err.message);
  });
}
function pollOAuth(attempt) {
  if (!oauth.sessionId) { return Promise.reject(new Error("No OAuth session.")); }
  return proxyFetch(apiPath("/api/oauth2/poll", { id: oauth.sessionId }), {})
    .then(function (data) {
      if (data.status === "authorized") {
        oauth.accessToken = data.access_token;
        oauth.obtainedAt = Date.now();
        // The callback page closes itself (calling window.close from
        // here would only trip Chrome's COOP warning, because the
        // consent page is cross-origin).
        oauth.win = null;
        setOauthStatus("Signed in (" + String(data.expires_in).replace("i:", "") + "s token)", true);
        return data.access_token;
      }
      if (data.status === "error") {
        throw new Error("Google sign-in failed: " + (data.error || "unknown error"));
      }
      return delay(Math.min(500 + 400 * attempt, 3000)).then(function () { return pollOAuth(attempt + 1); });
    });
}
function refreshOAuth() {
  if (!oauth.sessionId) { return Promise.reject(new Error("Sign in with Google first.")); }
  return proxyFetch(apiPath("/api/oauth2/refresh", { id: oauth.sessionId }), { method: "POST" })
    .then(function (data) {
      oauth.accessToken = data.access_token;
      oauth.obtainedAt = Date.now();
      setOauthStatus("Token refreshed (" + String(data.expires_in).replace("i:", "") + "s)", true);
      return data.access_token;
    });
}
function forgetOAuth() {
  var sid = oauth.sessionId;
  oauth.sessionId = null; oauth.accessToken = null; oauth.obtainedAt = 0;
  setOauthStatus("Not signed in", false);
  if (sid) {
    proxyFetch(apiPath("/api/oauth2/close", { id: sid }), { method: "POST" })
      .catch(function () { /* already gone */ });
  }
}
/* A fresh-enough access token for the IMAP SASL exchange. */
function ensureOAuthToken() {
  if (oauth.accessToken && (Date.now() - oauth.obtainedAt) < 3000000) {
    return Promise.resolve(oauth.accessToken);
  }
  if (oauth.sessionId) { return refreshOAuth(); }
  return Promise.reject(new Error('Not signed in with Google. Click "Sign in with Google" first.'));
}

function doConnect() {
  clearError();
  SERVER = $("serverUrl").value.trim().replace(/\/+$/, "") || SERVER;
  try {
    localStorage.setItem("showcase_imap_server", SERVER);
    localStorage.setItem("showcase_imap_token", $("apiToken").value.trim());
  } catch (e) { /* ignore */ }
  var host = $("imapHost").value.trim();
  var port = parseInt($("imapPort").value.trim(), 10);
  var user = $("imapUser").value, pass = $("imapPass").value;
  if (!host || !(port >= 1 && port <= 65535) || !user) {
    showError("Fill in IMAP host, port and user.");
    return;
  }
  if (authMode() === "login" && !pass) {
    showError("Fill in the password, or switch Auth to Google OAuth2.");
    return;
  }
  setStatus("Opening proxy session…", false);
  conn = new ImapConn();
  var useWs = false;
  var preset = PROVIDERS[$("provider").value] || PROVIDERS.custom;
  proxyFetch("/api/help", {}).then(function (help) {
    useWs = (help.routes || []).some(function (r) { return r.name === "socket_ws"; });
    return conn.open(host, port, preset.tls === true);
  }).then(function () {
    if (useWs && typeof WebSocket !== "undefined") {
      var t = $("apiToken").value.trim();
      return conn.attachWs(t).then(function () { conn.mode = "ws"; });
    }
    conn.startHttp();
  }).then(function () {
    setStatus("Waiting for IMAP greeting…", false);
    return conn.readGreeting(15000);
  }).then(function (greeting) {
    if (greeting.indexOf("* OK") !== 0) {
      throw new Error("Unexpected greeting: " + greeting.slice(0, 80));
    }
    return conn.cmd("CAPABILITY");
  }).then(function () {
    if (authMode() === "oauth2") {
      return ensureOAuthToken().then(function (token) {
        return conn.authXoauth2(user, token);
      });
    }
    return conn.cmd("LOGIN " + imapQuote(user) + " " + imapQuote(pass));
  }).then(function () {
    return conn.cmd("EXAMINE INBOX");
  }).then(function (ex) {
    var exists = 0;
    ex.lines.forEach(function (ln) {
      var m = ln.match(/^\* (\d+) EXISTS/);
      if (m) { exists = parseInt(m[1], 10); }
    });
    mailbox.exists = exists;
    return loadUids();
  }).then(function () {
    setStatus("Connected (" + conn.mode + ") — " + mailbox.exists + " messages", true);
    setConnected(true);
    mailbox.page = 0;
    return showPage();
  }).catch(function (err) {
    setStatus("Offline", false);
    showError(err.message);
    if (conn) { conn.close(); conn = null; }
  });
}

/* A finished Google sign-in brings the inbox up by itself, so the
   OAuth flow ends on the list exactly like a password Connect. */
function autoConnectAfterOAuth() {
  if (conn) { return; }                       // a session is already up
  if (authMode() !== "oauth2") { return; }
  if (!$("imapHost").value.trim() || !$("imapUser").value.trim()) { return; }
  doConnect();
}

/* UIDs from any untagged "* SORT ..." / "* SEARCH ..." line. Servers
   may piggyback unsolicited untagged lines (Gmail answers AUTHENTICATE
   with "* CAPABILITY ..."), so the UID line is not necessarily the
   first one. */
function scanUids(lines, keyword) {
  var re = new RegExp("^\\* " + keyword + "(?: (.*))?$");
  var uids = [];
  lines.forEach(function (ln) {
    var m = ln.match(re);
    if (!m || !m[1]) { return; }
    m[1].trim().split(/\s+/).forEach(function (tok) {
      if (/^\d+$/.test(tok)) { uids.push(parseInt(tok, 10)); }
    });
  });
  return uids;
}

function loadUids() {
  // Newest first. UID SORT when the server advertises it (Zoho,
  // Dovecot, ...); otherwise UID SEARCH ALL reversed — Gmail has no
  // SORT capability and answers UID SORT with BAD.
  function bySearch() {
    return conn.cmd("UID SEARCH ALL").then(function (r) {
      mailbox.uids = scanUids(r.lines, "SEARCH").sort(function (a, b) {
        return b - a;
      });
    });
  }
  return conn.cmd("UID SORT (REVERSE DATE) UTF-8 ALL").then(function (r) {
    var uids = scanUids(r.lines, "SORT");
    if (uids.length) { mailbox.uids = uids; return; }
    return bySearch();
  }, bySearch);
}

function showPage() {
  var total = Math.max(1, Math.ceil(mailbox.uids.length / mailbox.perPage));
  mailbox.page = Math.min(Math.max(0, mailbox.page), total - 1);
  var slice = mailbox.uids.slice(mailbox.page * mailbox.perPage,
    (mailbox.page + 1) * mailbox.perPage);
  $("mboxInfo").textContent = "(" + mailbox.exists + " in INBOX)";
  $("pageInfo").textContent = "Page " + (mailbox.page + 1) + " of " + total;
  $("prevBtn").disabled = mailbox.page <= 0;
  $("nextBtn").disabled = mailbox.page >= total - 1;
  var body = $("msgBody");
  body.textContent = "";
  if (!slice.length) { return Promise.resolve(); }
  var set = slice.join(",");
  return conn.cmd("UID FETCH " + set +
    " (UID FLAGS INTERNALDATE RFC822.SIZE " +
    "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"
  ).then(function (r) {
    parseFetchHeaders(r).forEach(function (m) {
      body.appendChild(msgRow(m));
    });
  });
}

function parseFetchHeaders(r) {
  // One record per "* <n> FETCH ..." line. Attributes are read from
  // every line of the record (servers differ in attribute order, so
  // the UID is not guaranteed to sit on the opening line) and the
  // header literal is attached where it appears.
  var out = [], cur = null;
  r.lines.forEach(function (ln, i) {
    if (/^\* \d+ FETCH /.test(ln)) {
      cur = { uid: null, flags: "", date: "", headers: {}, size: 0, raw: null };
      out.push(cur);
    }
    if (!cur) { return; }
    var mu = ln.match(/\bUID (\d+)/);
    if (mu) { cur.uid = parseInt(mu[1], 10); }
    var mf = ln.match(/FLAGS \(([^)]*)\)/);
    if (mf) { cur.flags = mf[1]; }
    var md = ln.match(/INTERNALDATE "([^"]+)"/);
    if (md) { cur.date = md[1]; }
    var ms = ln.match(/RFC822\.SIZE (\d+)/);
    if (ms) { cur.size = parseInt(ms[1], 10); }
    if (r.literals[i]) { cur.raw = latin1(r.literals[i]); }
  });
  var byUid = {};
  out.forEach(function (m) {
    if (m.uid === null) { return; }
    m.headers = parseHeaders(m.raw || "");
    byUid[m.uid] = m;
  });
  // Keep the requested (newest-first) order.
  return mailbox.uids.filter(function (u) { return byUid[u]; })
    .map(function (u) { return byUid[u]; })
    .filter(function (m, i, a) { return a.indexOf(m) === i; });
}

function msgRow(m) {
  var tr = document.createElement("tr");
  if (!/\\Seen/.test(m.flags)) { tr.className = "unseen"; }
  function td(t) { var c = document.createElement("td"); c.textContent = t; tr.appendChild(c); }
  td(/\\Seen/.test(m.flags) ? "·" : "●");
  td(m.headers.from || "?");
  var st = document.createElement("td");
  var link = document.createElement("button");
  link.className = "linklike";
  link.textContent = m.headers.subject || "(no subject)";
  link.addEventListener("click", function () { openMsg(m.uid); });
  st.appendChild(link);
  tr.appendChild(st);
  td(m.headers.date || m.date || "");
  return tr;
}

function openMsg(uid) {
  clearError();
  conn.cmd("UID FETCH " + uid + " (UID BODY.PEEK[])").then(function (r) {
    var raw = null;
    for (var i = r.literals.length - 1; i >= 0; i--) {
      if (r.literals[i]) { raw = r.literals[i]; break; }
    }
    if (!raw) { throw new Error("Empty message body."); }
    var picked = pickTextPart(raw);
    var h = parseHeaders(latin1(raw).split("\r\n\r\n")[0]);
    $("viewer").hidden = false;
    $("viewSubject").textContent = h.subject || "(no subject)";
    $("viewHeaders").textContent = "From: " + (h.from || "?") + " · To: " +
      (h.to || "?") + (h.cc ? " · Cc: " + h.cc : "") + " · Date: " + (h.date || "?");
    $("viewText").textContent = picked.text || "(empty body)";
    $("attachList").textContent = picked.attachments.length ?
      "Attachments: " + picked.attachments.map(function (a) {
        return a.name + " (" + a.size + " B)";
      }).join(", ") : "";
  }).catch(function (err) { showError(err.message); });
}

function doDisconnect() {
  setConnected(false);
  setStatus("Offline", false);
  if (conn) {
    var c = conn; conn = null;
    c.cmd("LOGOUT").catch(function () { /* go away quietly */ })
      .then(function () { return c.close(); });
  }
}

function onProvider() {
  var pv = $("provider").value;
  var p = PROVIDERS[pv] || PROVIDERS.custom;
  var custom = pv === "custom";
  if (!custom) {
    $("imapHost").value = p.host;
    $("imapPort").value = String(p.port);
  }
  var note = $("tlsNote");
  if (p.tls) {
    note.hidden = false;
    note.textContent = "TLS: " + p.note;
  } else if (p.note) {
    note.hidden = false;
    note.textContent = p.note;
  } else { note.hidden = true; }
  try { localStorage.setItem("showcase_imap_provider", pv); } catch (e) { /* ignore */ }
}

/* Google OAuth2 only makes sense against Gmail, so keep the Provider in
   step with the Auth mode: picking the OAuth2 mode must not leave the
   form on another preset (e.g. the Proton Bridge default at
   127.0.0.1:1143). `force` is the user changing the Auth selector; on
   load it is skipped for a deliberately chosen Custom host. */
function alignProviderWithAuth(force) {
  if (authMode() !== "oauth2") { return; }
  var pv = $("provider").value;
  if (pv === "gmail" || (!force && pv === "custom")) { return; }
  $("provider").value = "gmail";
  onProvider();
}

function onAuthMode(userChanged) {
  var oauth2 = authMode() === "oauth2";
  $("passLabel").hidden = oauth2;
  $("oauthPanel").hidden = !oauth2;
  if (oauth2 && userChanged) { alignProviderWithAuth(true); }
  try { localStorage.setItem("showcase_imap_auth", authMode()); } catch (e) { /* ignore */ }
}

function init() {
  try {
    $("serverUrl").value = localStorage.getItem("showcase_imap_server") || $("serverUrl").value;
    $("apiToken").value = localStorage.getItem("showcase_imap_token") || "";
    var savedProvider = localStorage.getItem("showcase_imap_provider");
    if (savedProvider && PROVIDERS[savedProvider]) {
      $("provider").value = savedProvider;
    }
    var savedAuth = localStorage.getItem("showcase_imap_auth");
    if (savedAuth === "oauth2") { $("authMode").value = "oauth2"; }
    $("oauthClientId").value = localStorage.getItem("showcase_oauth_client_id") || "";
    $("oauthClientSecret").value = localStorage.getItem("showcase_oauth_client_secret") || "";
  } catch (e) { /* ignore */ }
  $("provider").addEventListener("change", onProvider);
  $("authMode").addEventListener("change", function () { onAuthMode(true); });
  $("connectBtn").addEventListener("click", doConnect);
  $("disconnectBtn").addEventListener("click", doDisconnect);
  $("signInBtn").addEventListener("click", startOAuth);
  $("refreshTokenBtn").addEventListener("click", function () {
    refreshOAuth().catch(function (err) { showError(err.message); });
  });
  $("signOutBtn").addEventListener("click", forgetOAuth);
  $("prevBtn").addEventListener("click", function () {
    if (!conn) { return; }
    mailbox.page--; showPage().catch(function (e) { showError(e.message); });
  });
  $("nextBtn").addEventListener("click", function () {
    if (!conn) { return; }
    mailbox.page++; showPage().catch(function (e) { showError(e.message); });
  });
  $("reloadBtn").addEventListener("click", function () {
    if (!conn) { return; }
    conn.cmd("EXAMINE INBOX").then(loadUids).then(showPage).catch(function (e) { showError(e.message); });
  });
  $("pageSize").addEventListener("change", function () {
    if (!conn) { return; }
    mailbox.perPage = parseInt($("pageSize").value, 10) || PAGE_DEFAULT;
    mailbox.page = 0;
    showPage().catch(function (e) { showError(e.message); });
  });
  $("closeViewBtn").addEventListener("click", function () { $("viewer").hidden = true; });
  onProvider();
  onAuthMode(false);
  alignProviderWithAuth(false);   // a saved OAuth2 mode must not sit on another preset
  setOauthStatus("Not signed in", false);
  setConnected(false);
  setStatus("Offline", false);
}

document.addEventListener("DOMContentLoaded", init);
