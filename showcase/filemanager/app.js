"use strict";
/* Showcase file manager: pure JS client of the low-level webservice.
   Only these API functions are used: GET /, GET /api/help,
   GET /api/dir, GET /api/file, POST /api/file, PUT /api/file,
   DELETE /api/file, DELETE /api/dir, GET /api/sysinfo. */

var SERVER = "http://127.0.0.1:1423";
var caps = {};   // route name -> true, from /api/help
var online = false;
var cwd = "/";

function $(id) { return document.getElementById(id); }

function showError(msg) {
  var el = $("error");
  el.textContent = msg;
  el.hidden = false;
}
function clearError() { $("error").hidden = true; $("error").textContent = ""; }

function authHeaders() {
  var t = $("apiToken").value.trim();
  return t ? { "Authorization": "Bearer " + t } : {};
}

function setControls(ids) {
  ids.forEach(function (id) { $(id).disabled = !online; });
}

function setOnline(ok, detail) {
  online = ok;
  $("dot").className = ok ? "on" : "off";
  $("statusText").textContent = ok ? ("Online" + (detail ? " (" + detail + ")" : "")) : "Offline";
  setControls(["upBtn", "refreshBtn", "pathInput", "goBtn", "newName",
    "createBtn", "deleteDirBtn", "viewText", "saveBtn", "downloadBtn",
    "deleteFileBtn", "closeViewBtn", "retryBtn"]);
  if (!ok) {
    showError(detail || ("Server not available at " + SERVER +
      ". Start it with: ./start.sh --runtime go"));
  }
}

/* Wire tags: "i:5" -> 5, "f:1.5" -> 1.5, "b:true" -> true,
   "null" -> null. Raw strings pass through. */
function tv(s) {
  if (typeof s !== "string") { return s; }
  if (s === "null") { return null; }
  if (s === "b:true") { return true; }
  if (s === "b:false") { return false; }
  if (s.length > 2 && s.charAt(1) === ":") {
    var head = s.charAt(0), body = s.slice(2);
    if (head === "i") { return parseInt(body, 10); }
    if (head === "f") { return parseFloat(body); }
  }
  return s;
}

function fmtSize(n) {
  if (n === null || n === undefined || isNaN(n)) { return "-"; }
  var units = ["B", "KB", "MB", "GB"], i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? String(v) : v.toFixed(1)) + " " + units[i];
}

function fmtTime(t) {
  if (t === null || t === undefined) { return "-"; }
  var d = new Date(t * 1000);
  return isNaN(d.getTime()) ? "-" : d.toLocaleString();
}

function apiPath(route, params) {
  var q = Object.keys(params || {}).map(function (k) {
    return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]);
  }).join("&");
  return route + (q ? "?" + q : "");
}

function request(url, options) {
  options = options || {};
  options.headers = Object.assign({}, authHeaders(), options.headers || {});
  return fetch(SERVER + url, options).then(function (res) {
    var isJson = (res.headers.get("Content-Type") || "").indexOf("application/json") !== -1;
    if (isJson) {
      return res.json().then(function (data) { return { status: res.status, data: data }; });
    }
    return res.blob().then(function (blob) { return { status: res.status, blob: blob }; });
  }).then(function (out) {
    if (out.status === 401) {
      online = false;
      setOnline(false, "Unauthorized");
      showError("Invalid or missing API token. Paste the token printed " +
        "by the server (or start it with --no-auth) and press Connect.");
    }
    if (out.status < 200 || out.status >= 300) {
      var msg = (out.data && (out.data.message || out.data.error)) || ("HTTP " + out.status);
      throw new Error(msg);
    }
    return out.data !== undefined ? out.data : out.blob;
  });
}

function joinPath(base, name) {
  if (base.charAt(base.length - 1) !== "/") { base += "/"; }
  return base + name;
}

function parentDir(p) {
  if (p === "/") { return "/"; }
  return p.replace(/\/+$/, "").split("/").slice(0, -1).join("/") || "/";
}

function renderCrumbs() {
  var box = $("crumbs");
  box.textContent = "";
  var parts = cwd.replace(/\/+$/, "").split("/").filter(function (x) { return x !== ""; });
  var acc = "/";
  var root = document.createElement("button");
  root.textContent = "/";
  root.addEventListener("click", function () { listDir("/"); });
  box.appendChild(root);
  parts.forEach(function (part) {
    box.appendChild(document.createTextNode(" / "));
    acc = acc === "/" ? "/" + part : acc + "/" + part;
    (function (target) {
      var b = document.createElement("button");
      b.textContent = part;
      b.addEventListener("click", function () { listDir(target); });
      box.appendChild(b);
    })(acc);
  });
}

function entryRow(entry) {
  var tr = document.createElement("tr");
  if (entry.type === "dir") { tr.className = "dir"; }
  var nameTd = document.createElement("td");
  var link = document.createElement("button");
  link.className = "linklike";
  link.textContent = entry.name;
  link.addEventListener("click", function () {
    if (entry.type === "dir") { listDir(entry.path); }
    else { openFile(entry.path); }
  });
  nameTd.appendChild(link);
  tr.appendChild(nameTd);
  function td(text, num) {
    var c = document.createElement("td");
    c.textContent = text;
    if (num) { c.className = "num"; }
    tr.appendChild(c);
    return c;
  }
  td(entry.type);
  td(fmtSize(tv(entry.size)), true);
  td(fmtTime(tv(entry.mtime)));
  td(fmtTime(tv(entry.atime)));
  td(fmtTime(tv(entry.ctime)));
  td(fmtTime(tv(entry.crtime)));
  td(entry.mode || "-");
  return tr;
}

function listDir(path) {
  clearError();
  if (!online) { showError("Offline. Press Connect after starting the server."); return; }
  request(apiPath("/api/dir", { path: path }), {}).then(function (data) {
    cwd = data.absolute_path || path;
    $("pathInput").value = cwd;
    $("dirCount").textContent = "(" + tv(data.count) + ")";
    renderCrumbs();
    var body = $("dirBody");
    body.textContent = "";
    (data.entries || []).forEach(function (e) { body.appendChild(entryRow(e)); });
    hideViewer();
  }).catch(function (err) { showError(err.message); });
}

function fileMetaLine(data) {
  return "size " + fmtSize(tv(data.size)) +
    " · mtime " + fmtTime(tv(data.mtime)) +
    " · mime " + (data.mime_type || "-") +
    " · binary " + String(tv(data.is_binary)) +
    " · encoding " + (data.encoding || "-");
}

function openFile(path) {
  clearError();
  request(apiPath("/api/file", { path: path }), {}).then(function (data) {
    $("viewer").hidden = false;
    $("viewTitle").textContent = data.path || path;
    $("viewTitle").dataset.path = path;
    $("viewMeta").textContent = fileMetaLine(data);
    if (tv(data.is_binary)) {
      $("viewText").value = "";
      $("viewText").disabled = true;
      $("saveBtn").disabled = true;
      $("binaryNote").hidden = false;
    } else {
      $("viewText").disabled = !online;
      $("saveBtn").disabled = !online;
      $("binaryNote").hidden = true;
      $("viewText").value = data.content || "";
    }
  }).catch(function (err) { showError(err.message); });
}

function hideViewer() {
  $("viewer").hidden = true;
  $("viewText").value = "";
}

function saveFile() {
  var path = $("viewTitle").dataset.path;
  if (!path) { return; }
  clearError();
  request(apiPath("/api/file", { path: path }), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: $("viewText").value, encoding: "text" })
  }).then(function () { openFile(path); })
    .catch(function (err) { showError(err.message); });
}

function downloadFile() {
  var path = $("viewTitle").dataset.path;
  if (!path) { return; }
  clearError();
  request(apiPath("/api/file", { path: path, raw: "1" }), {})
    .then(function (blob) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = path.split("/").pop() || "download";
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    }).catch(function (err) { showError(err.message); });
}

function createFile() {
  var name = $("newName").value.trim();
  if (!name) { showError("Type a file name first."); return; }
  clearError();
  request(apiPath("/api/file", { path: joinPath(cwd, name) }), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: "", encoding: "text" })
  }).then(function () {
    $("newName").value = "";
    listDir(cwd);
  }).catch(function (err) { showError(err.message); });
}

function deleteFile() {
  var path = $("viewTitle").dataset.path;
  if (!path) { return; }
  clearError();
  request(apiPath("/api/file", { path: path }), { method: "DELETE" })
    .then(function () { listDir(cwd); })
    .catch(function (err) { showError(err.message); });
}

function deleteDir() {
  clearError();
  request(apiPath("/api/dir", { path: cwd }), { method: "DELETE" })
    .then(function () { listDir(parentDir(cwd)); })
    .catch(function (err) { showError(err.message); });
}

function connect() {
  clearError();
  SERVER = $("serverUrl").value.trim().replace(/\/+$/, "") || SERVER;
  try {
    localStorage.setItem("showcase_server", SERVER);
    localStorage.setItem("showcase_token", $("apiToken").value.trim());
  } catch (e) { /* private mode: keep going without persistence */ }
  request("/api/help", {}).then(function (data) {
    caps = {};
    (data.routes || []).forEach(function (r) { caps[r.name] = true; });
    if (!caps.read_directory) { throw new Error("Server lacks read_directory."); }
    online = true;
    setOnline(true, data.version);
    request("/api/sysinfo", {}).then(function (sys) {
      $("sysInfo").textContent = (sys.system_name || sys.system || "?") +
        " · " + (sys.hostname || "?");
    }, function () { /* sysinfo optional for navigation */ });
    listDir(cwd);
  }).catch(function (err) {
    online = false;
    setOnline(false);
    if (err.message !== "Unauthorized") { showError(err.message); }
  });
}

function init() {
  try {
    $("serverUrl").value = localStorage.getItem("showcase_server") || $("serverUrl").value;
    $("apiToken").value = localStorage.getItem("showcase_token") || "";
  } catch (e) { /* ignore */ }
  $("retryBtn").addEventListener("click", connect);
  $("upBtn").addEventListener("click", function () { listDir(parentDir(cwd)); });
  $("refreshBtn").addEventListener("click", function () { listDir(cwd); });
  $("goBtn").addEventListener("click", function () { listDir($("pathInput").value.trim() || "/"); });
  $("createBtn").addEventListener("click", createFile);
  $("deleteDirBtn").addEventListener("click", deleteDir);
  $("saveBtn").addEventListener("click", saveFile);
  $("downloadBtn").addEventListener("click", downloadFile);
  $("deleteFileBtn").addEventListener("click", deleteFile);
  $("closeViewBtn").addEventListener("click", hideViewer);
  setOnline(false);
  connect();
}

document.addEventListener("DOMContentLoaded", init);
