"use strict";
/* Node harness for the pure-JS pieces of the emailclient that are easy
   to get wrong against real servers: the SASL XOAUTH2 payload with its
   "+ ..." continuation abort, the UID listing (SORT vs SEARCH, with the
   UID line not necessarily first) and the FETCH header grouping.
   app.js is loaded into a vm with DOM stubs; ImapConn.send/readResponse
   are stubbed so each piece can be observed in isolation.
   Usage: node tests/js_emailclient_auth.js showcase/emailclient/app.js
   Prints JSON { ok, tests: [{name, ok}] }; exits 1 on failure. */

const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[2];
if (!appPath) {
  process.stdout.write(JSON.stringify({ ok: false, error: "no app.js path" }));
  process.exit(2);
}

let code = fs.readFileSync(appPath, "utf8");
code += "\n;globalThis.__ImapConn = ImapConn;" +
  "globalThis.__base64Encode = base64Encode;" +
  "globalThis.__loadUids = loadUids;" +
  "globalThis.__parseFetchHeaders = parseFetchHeaders;" +
  "globalThis.__autoConnectAfterOAuth = autoConnectAfterOAuth;" +
  "globalThis.__authMode = authMode;" +
  "globalThis.__onProvider = onProvider;" +
  "globalThis.__alignProviderWithAuth = alignProviderWithAuth;" +
  "globalThis.__mailbox = mailbox;";

const noop = function () {};
/* Minimal element stubs so $("id") works for the code under test. */
const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = { id: id, value: "", hidden: false, textContent: "",
      className: "", disabled: false, style: {}, appendChild: noop,
      addEventListener: noop };
  }
  return els[id];
}
const sandbox = {
  TextEncoder: TextEncoder,
  TextDecoder: TextDecoder,
  Promise: Promise,
  setTimeout: setTimeout,
  console: console,
  atob: atob,
  btoa: btoa,
  document: {
    addEventListener: noop,
    getElementById: el,
    createElement: function () {
      return { style: {}, appendChild: noop, addEventListener: noop,
        set innerHTML(v) { this.textContent = String(v).replace(/<[^>]*>/g, ""); },
        textContent: "" };
    },
  },
  window: {},
  localStorage: { getItem: function () { return null; }, setItem: noop },
  fetch: function () { return Promise.reject(new Error("no fetch in harness")); },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: appPath });

const ImapConn = sandbox.__ImapConn;
const base64Encode = sandbox.__base64Encode;
const results = [];
function check(name, cond) { results.push({ name: name, ok: !!cond }); }

function decodedSasl(line) {
  const m = line.match(/^([a-z]\d+) AUTHENTICATE XOAUTH2 (.*)\r\n$/);
  if (!m) { return null; }
  return Buffer.from(m[2], "base64").toString("binary");
}
function lit(s) { return new Uint8Array(Buffer.from(s, "binary")); }

(async function () {
  check("base64Encode roundtrip",
    base64Encode("abc") === Buffer.from("abc", "binary").toString("base64"));

  // --- SASL XOAUTH2 -------------------------------------------------
  // Success: no continuation, resolves after the tagged OK.
  const okConn = new ImapConn();
  const okSent = [];
  okConn.send = function (text) { okSent.push(text); return Promise.resolve(); };
  okConn.readResponse = function (tag, timeout, onPlus) {
    void onPlus;  // success has no continuation
    return Promise.resolve({ ok: true, tail: tag + " OK authenticated" });
  };
  const okRes = await okConn.authXoauth2("ada@example.com", "tok-123");
  check("xoauth2 resolves on OK", okRes.ok === true);
  check("xoauth2 sends one line", okSent.length === 1);
  check("xoauth2 SASL payload",
    decodedSasl(okSent[0] || "") ===
      "user=ada@example.com\u0001auth=Bearer tok-123\u0001\u0001");
  check("xoauth2 no abort without continuation",
    okSent.indexOf("\r\n") === -1);

  // Rejection: continuation, then the empty abort line, then tagged NO.
  const koConn = new ImapConn();
  const koSent = [];
  koConn.send = function (text) { koSent.push(text); return Promise.resolve(); };
  koConn.readResponse = function (tag, timeout, onPlus) {
    if (onPlus) { onPlus("+ " + base64Encode('{"status":"401"}')); }
    return Promise.resolve({ ok: false, tail: tag + " NO [AUTHENTICATIONFAILED] nope" });
  };
  let err = null;
  try { await koConn.authXoauth2("ada@example.com", "bad-token"); }
  catch (e) { err = e; }
  check("xoauth2 rejects on NO", err !== null && /IMAP error/.test(err.message));
  check("xoauth2 error names the command",
    err !== null && /AUTHENTICATE XOAUTH2/.test(err.message));
  check("xoauth2 error hides the token",
    err !== null && err.message.indexOf("bad-token") === -1);
  check("xoauth2 sends abort empty line", koSent[1] === "\r\n");
  check("xoauth2 abort sent once", koSent.length === 2);

  // --- command error label ------------------------------------------
  const eConn = new ImapConn();
  eConn.send = function () { return Promise.resolve(); };
  eConn.readResponse = function (tag) {
    return Promise.resolve({ ok: false, tail: tag + " NO [ALERT] nope" });
  };
  let cErr = null;
  try { await eConn.cmd('LOGIN "ada@example.com" "s3cret"'); }
  catch (e) { cErr = e; }
  check("cmd error names the verb",
    cErr !== null && /IMAP error \(LOGIN /.test(cErr.message));
  check("cmd error hides the password",
    cErr !== null && cErr.message.indexOf("s3cret") === -1);

  // --- loadUids -----------------------------------------------------
  // SORT advertised, but the UID line is NOT the first line (Gmail
  // piggybacks an unsolicited "* CAPABILITY" on the auth response).
  sandbox.conn = {
    cmd: function (command) {
      if (/SORT/.test(command)) {
        return Promise.resolve({ ok: true, lines: [
          "* CAPABILITY IMAP4rev1 UNSELECT X-GM-EXT-1",
          "* SORT 105 104 103"
        ] });
      }
      return Promise.reject(new Error("unexpected: " + command));
    }
  };
  await sandbox.__loadUids();
  check("loadUids reads SORT past unsolicited lines",
    JSON.stringify(sandbox.__mailbox.uids) === JSON.stringify([105, 104, 103]));

  // No SORT (Gmail): BAD -> UID SEARCH ALL, reversed to newest-first,
  // again with the UID line not first.
  sandbox.conn = {
    cmd: function (command) {
      if (/SORT/.test(command)) { return Promise.reject(new Error("bad sort")); }
      if (/SEARCH/.test(command)) {
        return Promise.resolve({ ok: true, lines: [
          "* CAPABILITY IMAP4rev1 X-GM-EXT-1",
          "* SEARCH 7 9 11"
        ] });
      }
      return Promise.reject(new Error("unexpected: " + command));
    }
  };
  await sandbox.__loadUids();
  check("loadUids SEARCH fallback is newest-first",
    JSON.stringify(sandbox.__mailbox.uids) === JSON.stringify([11, 9, 7]));

  // SORT answers OK but without UIDs -> still fall back to SEARCH.
  sandbox.conn = {
    cmd: function (command) {
      if (/SORT/.test(command)) {
        return Promise.resolve({ ok: true, lines: ["* SORT"] });
      }
      if (/SEARCH/.test(command)) {
        return Promise.resolve({ ok: true, lines: ["* SEARCH 3 5"] });
      }
      return Promise.reject(new Error("unexpected: " + command));
    }
  };
  await sandbox.__loadUids();
  check("loadUids empty SORT falls back to SEARCH",
    JSON.stringify(sandbox.__mailbox.uids) === JSON.stringify([5, 3]));

  // --- parseFetchHeaders --------------------------------------------
  const h1 = "From: Ada <ada@example.com>\r\nSubject: Hi\r\n\r\n";
  const h2 = "From: Bob <bob@example.com>\r\nSubject: Re\r\n\r\n";
  // Record A: UID appears only AFTER the header literal (attribute
  // order is not guaranteed by the protocol).
  // Record B: UID on the opening line (the common shape).
  sandbox.__mailbox.uids = [42, 41];
  const rows = sandbox.__parseFetchHeaders({
    lines: [
      "* 1 FETCH (BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] {" +
        Buffer.byteLength(h1, "binary") + "}",
      " UID 42 FLAGS (\\Seen))",
      "* 2 FETCH (UID 41 FLAGS () INTERNALDATE \"15-Oct-2026 09:00:00 +0000\" " +
        "BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] {" +
        Buffer.byteLength(h2, "binary") + "}",
      ")"
    ],
    literals: [lit(h1), null, lit(h2), null]
  });
  check("parseFetchHeaders keeps the requested order",
    rows.length === 2 && rows[0].uid === 42 && rows[1].uid === 41);
  check("parseFetchHeaders finds UID after the literal",
    rows[0].headers.subject === "Hi");
  check("parseFetchHeaders finds UID on the opening line",
    rows[1].headers.subject === "Re");
  check("parseFetchHeaders reads flags", /\\Seen/.test(rows[0].flags));

  // --- autoConnectAfterOAuth ----------------------------------------
  let connectCalls = 0;
  sandbox.doConnect = function () { connectCalls++; };
  el("imapHost").value = "imap.gmail.com";
  el("imapUser").value = "ada@example.com";
  el("authMode").value = "oauth2";
  sandbox.conn = null;
  sandbox.__autoConnectAfterOAuth();
  check("autoConnect connects after OAuth", connectCalls === 1);

  sandbox.conn = { mode: "ws" };
  sandbox.__autoConnectAfterOAuth();
  check("autoConnect skips when already connected", connectCalls === 1);

  sandbox.conn = null;
  el("authMode").value = "login";
  sandbox.__autoConnectAfterOAuth();
  check("autoConnect skips for password mode", connectCalls === 1);

  el("authMode").value = "oauth2";
  el("imapHost").value = "";
  sandbox.__autoConnectAfterOAuth();
  check("autoConnect skips without a host", connectCalls === 1);

  // --- provider/auth alignment --------------------------------------
  // Picking Google OAuth2 must not leave the form on another preset
  // (the Proton Bridge default at 127.0.0.1:1143 was the real bug).
  el("provider").value = "proton";
  el("authMode").value = "oauth2";
  sandbox.__onProvider();
  check("proton preset fills 127.0.0.1:1143",
    el("imapHost").value === "127.0.0.1" && el("imapPort").value === "1143");
  sandbox.__alignProviderWithAuth(true);
  check("choosing OAuth2 switches the provider to gmail",
    el("provider").value === "gmail");
  check("choosing OAuth2 fills the gmail host",
    el("imapHost").value === "imap.gmail.com");
  check("choosing OAuth2 fills the gmail port 993",
    el("imapPort").value === "993");

  // On load a deliberately chosen custom host is kept.
  el("provider").value = "custom";
  el("imapHost").value = "imap.example.com";
  el("authMode").value = "oauth2";
  sandbox.__alignProviderWithAuth(false);
  check("load keeps a custom host under OAuth2",
    el("provider").value === "custom" &&
    el("imapHost").value === "imap.example.com");

  // Password mode is never touched.
  el("provider").value = "proton";
  el("authMode").value = "login";
  sandbox.__alignProviderWithAuth(true);
  check("password mode keeps the provider",
    el("provider").value === "proton");

  const ok = results.every(function (r) { return r.ok; });
  process.stdout.write(JSON.stringify({ ok: ok, tests: results }));
  process.exit(ok ? 0 : 1);
})().catch(function (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e) }));
  process.exit(2);
});
