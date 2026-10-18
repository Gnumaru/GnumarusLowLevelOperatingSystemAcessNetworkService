"""Tests for showcase/emailclient (read-only IMAP inbox over proxy).

The app uses ONLY the socket proxy (+ /api/help for the socket_ws
capability), so this module checks the static side (files exist,
offline-pure, route allowlist, read-only IMAP verbs) and the live
side: a scripted fake IMAP server (greeting, LOGIN, EXAMINE,
SEARCH/SORT fallback, literal FETCH headers, literal BODY.PEEK[],
LOGOUT) driven through the Go proxy over BOTH transports the app
supports (HTTP send/recv polling and the WebSocket upgrade), plus a
Gmail-shaped variant (no SORT capability, an unsolicited CAPABILITY
line after AUTHENTICATE).
"""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import unittest

from tests.helpers import base_url, json_request, quote, ti
from tests.test_socket import sock_close, sock_open, sock_recv, sock_send

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOW = os.path.join(ROOT, "showcase", "emailclient")

HDRS = ("From: \"Doe, Ada\" <ada@example.com>\r\n"
        "To: bob@example.com\r\n"
        "Subject: =?UTF-8?B?aGk=?=\r\n"
        "Date: Thu, 16 Oct 2026 10:00:00 +0000\r\n"
        "Message-ID: <1@example.com>\r\n"
        "\r\n")
BODY = ("From: =?UTF-8?Q?Bob?= <bob@example.com>\r\n"
        "To: ada@example.com\r\n"
        "Subject: Re: hi\r\n"
        "Date: Thu, 16 Oct 2026 11:00:00 +0000\r\n"
        "Message-ID: <2@example.com>\r\n"
        "Content-Type: multipart/mixed; boundary=\"b1\"\r\n"
        "\r\n"
        "--b1\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "hello back\r\n"
        "--b1\r\n"
        "Content-Type: application/octet-stream; name=\"a.bin\"\r\n"
        "Content-Disposition: attachment; filename=\"a.bin\"\r\n"
        "\r\n"
        "QUJD\r\n"
        "--b1--\r\n")


class FakeImap:
    """Scripted single-connection IMAP server (plaintext)."""

    def __init__(self, gmail=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.gmail = gmail        # Gmail-shaped: no SORT, unsolicited CAPABILITY
        self.seen = []
        self.auth_seen = []      # decoded XOAUTH2 SASL payloads
        self.abort_seen = []     # empty abort lines after a rejection
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _readline(self, conn):
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = conn.recv(1)
            if not chunk:
                raise ConnectionError("eof")
            buf += chunk
        return buf.decode("utf-8", "replace")

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            conn.sendall(b"* OK fake ready\r\n")
            while True:
                try:
                    line = self._readline(conn)
                except ConnectionError:
                    return
                if not line.strip():
                    continue
                self.seen.append(line)
                tag, _, rest = line.strip().partition(" ")
                verb = rest.split(" ", 1)[0].upper()
                if verb == "CAPABILITY":
                    caps = ("IMAP4rev1 UNSELECT IDLE NAMESPACE X-GM-EXT-1 "
                            "AUTH=XOAUTH2" if self.gmail
                            else "IMAP4rev1 UIDPLUS")
                    conn.sendall(("* CAPABILITY " + caps + "\r\n"
                                 + ("%s OK done\r\n" % tag)).encode())
                elif verb == "LOGIN":
                    conn.sendall(("%s OK logged in\r\n" % tag).encode())
                elif verb == "AUTHENTICATE":
                    parts = rest.split(" ", 2)
                    mech = parts[1].upper() if len(parts) > 1 else ""
                    blob = parts[2] if len(parts) > 2 else ""
                    if mech == "XOAUTH2":
                        try:
                            decoded = base64.b64decode(blob).decode("utf-8")
                        except Exception:
                            decoded = ""
                        self.auth_seen.append(decoded)
                        if "bad" in decoded.lower():
                            err = base64.b64encode(json.dumps(
                                {"status": "401",
                                 "schemes": ["bearer"],
                                 "scope": "https://mail.google.com/"}
                            ).encode()).decode()
                            conn.sendall(("+ %s\r\n" % err).encode())
                            try:
                                abort = self._readline(conn)
                            except ConnectionError:
                                return
                            self.abort_seen.append(abort)
                            conn.sendall(
                                ("%s NO [AUTHENTICATIONFAILED] "
                                 "invalid credentials\r\n" % tag).encode())
                        else:
                            if self.gmail:
                                # Gmail rides an unsolicited capability
                                # line along with the auth completion.
                                conn.sendall(
                                    b"* CAPABILITY IMAP4rev1 UNSELECT "
                                    b"X-GM-EXT-1 AUTH=XOAUTH2\r\n")
                            conn.sendall(
                                ("%s OK authenticated\r\n" % tag).encode())
                    else:
                        conn.sendall(
                            ("%s NO unsupported mechanism\r\n" % tag).encode())
                elif verb == "EXAMINE":
                    conn.sendall(b"* 2 EXISTS\r\n"
                                b"* OK [UIDVALIDITY 1]\r\n"
                                + ("%s OK examined\r\n" % tag).encode())
                elif verb == "UID":
                    sub = rest.split(" ", 2)[1].upper()
                    if sub.startswith("SORT"):
                        conn.sendall(("%s %s sort unavailable\r\n"
                                     % (tag, "BAD" if self.gmail
                                        else "NO")).encode())
                    elif sub.startswith("SEARCH"):
                        conn.sendall(b"* SEARCH 101 102\r\n"
                                    + ("%s OK searched\r\n" % tag).encode())
                    elif "HEADER.FIELDS" in rest:
                        for uid in ("102", "101"):
                            payload = HDRS.encode()
                            conn.sendall(
                                ("* 1 FETCH (UID %s FLAGS (\\Seen) "
                                 "INTERNALDATE \"16-Oct-2026 10:00:00 +0000\" "
                                 "RFC822.SIZE %d BODY[HEADER.FIELDS "
                                 "(FROM TO CC SUBJECT DATE MESSAGE-ID)] "
                                 "{%d}\r\n" % (uid, len(payload),
                                               len(payload))).encode()
                                + payload + b"\r\n)\r\n")
                        conn.sendall(("%s OK fetched\r\n" % tag).encode())
                    elif "BODY" in rest:
                        payload = BODY.encode()
                        conn.sendall(
                            ("* 1 FETCH (UID 101 BODY[] {%d}\r\n"
                             % len(payload)).encode()
                            + payload + b"\r\n)\r\n"
                            + ("%s OK fetched\r\n" % tag).encode())
                    else:
                        conn.sendall(("%s BAD unknown\r\n" % tag).encode())
                elif verb == "LOGOUT":
                    try:
                        conn.sendall(b"* BYE gone\r\n"
                                    + ("%s OK logout\r\n" % tag).encode())
                    except OSError:
                        pass
                    return
                else:
                    conn.sendall(("%s BAD unknown\r\n" % tag).encode())


class TestShowcaseFiles(unittest.TestCase):
    def test_files_exist(self):
        for name in ("index.html", "styles.css", "app.js"):
            path = os.path.join(SHOW, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertGreater(os.path.getsize(path), 0, name)

    def test_html_wires_css_and_js(self):
        html = open(os.path.join(SHOW, "index.html"),
                    encoding="utf-8").read()
        self.assertIn('href="styles.css"', html)
        self.assertIn('src="app.js"', html)

    def test_element_ids_wired(self):
        js = open(os.path.join(SHOW, "app.js"), encoding="utf-8").read()
        html = open(os.path.join(SHOW, "index.html"),
                    encoding="utf-8").read()
        used = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', js))
        have = set(re.findall(r'id="([A-Za-z0-9_]+)"', html))
        self.assertEqual(sorted(used - have), [],
                         "app.js references element ids missing from index.html")

    def test_offline_pure(self):
        for name in ("index.html", "styles.css", "app.js"):
            text = open(os.path.join(SHOW, name), encoding="utf-8").read()
            for bad in ("https://", "http://cdn", "cdn.jsdelivr",
                        "unpkg.com", "googleapis", "@import",
                        "<script src=\"http"):
                self.assertNotIn(bad, text, "%s contains %s" % (name, bad))
        html = open(os.path.join(SHOW, "index.html"),
                    encoding="utf-8").read()
        for bad in ("src=\"http", "href=\"http"):
            self.assertNotIn(bad, html)

    def test_js_syntax(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not available")
        proc = subprocess.run(
            [node, "--check", os.path.join(SHOW, "app.js")],
            capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))

    def test_js_oauth2_harness(self):
        """Unit-test the SASL XOAUTH2 payload + continuation abort."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not available")
        harness = os.path.join(ROOT, "tests", "js_emailclient_auth.js")
        proc = subprocess.run(
            [node, harness, os.path.join(SHOW, "app.js")],
            capture_output=True, timeout=60)
        try:
            out = json.loads(proc.stdout.decode("utf-8"))
        except ValueError:
            self.fail("harness produced no JSON: %r / %r" % (
                proc.stdout[:400], proc.stderr[:400]))
        failed = [t["name"] for t in out.get("tests", []) if not t["ok"]]
        self.assertTrue(out.get("ok"),
                        "harness failures: %r (%s)" % (failed, out))
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))

    def test_proxy_only_routes(self):
        js = open(os.path.join(SHOW, "app.js"), encoding="utf-8").read()
        for route in ("/api/help", "/api/socket/open",
                      "/api/socket/send", "/api/socket/recv",
                      "/api/socket/close", "/api/socket/ws",
                      "/api/oauth2/start", "/api/oauth2/poll",
                      "/api/oauth2/refresh", "/api/oauth2/close"):
            self.assertIn(route, js)
        for route in ("/api/shell", "/api/sqlite", "/api/file",
                      "/api/dir", "/api/sysinfo"):
            self.assertNotIn(route, js)

    def test_read_only_verbs(self):
        js = open(os.path.join(SHOW, "app.js"), encoding="utf-8").read()
        for verb in ("LOGIN", "AUTHENTICATE XOAUTH2", "EXAMINE",
                     "UID SEARCH", "UID SORT",
                     "UID FETCH", "BODY.PEEK", "LOGOUT", "CAPABILITY"):
            self.assertIn(verb, js)
        for verb in ("SELECT INBOX", "STORE", "APPEND", "EXPUNGE",
                     "SEND", "DELETE"):
            self.assertNotIn(verb, js)

    def test_provider_presets(self):
        js = open(os.path.join(SHOW, "app.js"), encoding="utf-8").read()
        for host in ("imap.gmail.com", "outlook.office365.com",
                     "imap.mail.me.com", "imap.mail.yahoo.com",
                     "127.0.0.1"):
            self.assertIn(host, js)
        # TLS presets open the proxy session with the tls flag.
        self.assertIn('"b:true"', js)
        self.assertIn("tls", js)


def recv_text(sid, timeout="5"):
    """One proxy recv decoded to text (like the app's HTTP path)."""
    status, _, data = sock_recv(sid, timeout=timeout)
    assert status == 200, data
    raw = base64.b64decode(data["content"] or "")
    return raw.decode("utf-8", "replace")


class ImapScript:
    """The app's exact command sequence against a proxy session."""

    def __init__(self, send, recv):
        self.send = send
        self.recv = recv
        self.buf = ""
        self.tag = 0

    def _drain(self, tag, timeout=30):
        """Pull recvs until the tagged completion line arrives."""
        import re
        import time
        out = []
        deadline = time.time() + timeout
        lit_re = re.compile(r".*\{(\d+)\}$")
        while True:
            while "\r\n" in self.buf:
                line, _, self.buf = self.buf.partition("\r\n")
                out.append(line)
                m = lit_re.match(line)
                if m:
                    n = int(m.group(1))
                    while len(self.buf) < n:
                        self.buf += self.recv()
                    out.append(self.buf[:n])
                    self.buf = self.buf[n:]
                    if self.buf.startswith("\r\n"):
                        self.buf = self.buf[2:]
                if line.startswith(tag + " ") or line == tag:
                    return out
            if time.time() > deadline:
                raise AssertionError("no tagged completion: %r" % (out,))
            self.buf += self.recv()

    def greeting(self):
        import time
        deadline = time.time() + 15
        while True:
            while "\r\n" in self.buf:
                line, _, self.buf = self.buf.partition("\r\n")
                if line.startswith("* "):
                    assert line.startswith("* OK"), line
                    return line
            if time.time() > deadline:
                raise AssertionError(
                    "no greeting: %r" % (self.buf,))
            self.buf += self.recv()

    def cmd(self, command):
        self.tag += 1
        tag = "a%03d" % self.tag
        self.send(tag + " " + command + "\r\n")
        lines = self._drain(tag)
        tagged = [ln for ln in lines
                  if ln.startswith(tag + " ")]
        assert tagged, lines
        assert tagged[0].startswith(tag + " OK"), tagged
        return lines

    def new_tag(self):
        self.tag += 1
        return "a%03d" % self.tag

    def drain_tag(self, tag, timeout=30):
        return self._drain(tag, timeout)

    def read_continuation(self, timeout=30):
        """Wait for a '+' continuation line and answer it (empty line)."""
        import time
        deadline = time.time() + timeout
        while True:
            while "\r\n" in self.buf:
                line, _, self.buf = self.buf.partition("\r\n")
                if line.startswith("+"):
                    self.send("\r\n")
                    return line
            if time.time() > deadline:
                raise AssertionError("no continuation: %r" % (self.buf,))
            self.buf += self.recv()


class TestShowcaseImapFlow(unittest.TestCase):
    def test_imap_over_http_proxy(self):
        srv = FakeImap().start()
        status, _, data = sock_open("127.0.0.1", ti(srv.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            convo = ImapScript(
                lambda text: sock_send(sid, text),
                lambda: recv_text(sid))
            convo.greeting()
            convo.cmd("CAPABILITY")
            convo.cmd('LOGIN "user" "pass"')
            lines = convo.cmd("EXAMINE INBOX")
            self.assertTrue(any("EXISTS" in ln for ln in lines))
            try:
                convo.cmd("UID SORT (REVERSE DATE) UTF-8 ALL")
                self.fail("fake must reject SORT (fallback path)")
            except AssertionError as err:
                self.assertIn("NO", str(err))
            lines = convo.cmd("UID SEARCH ALL")
            self.assertTrue(any("* SEARCH 101 102" in ln
                                for ln in lines))
            lines = convo.cmd(
                "UID FETCH 102,101 (UID FLAGS INTERNALDATE "
                "RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
                "(FROM TO CC SUBJECT DATE MESSAGE-ID)])")
            blob = "\n".join(lines)
            self.assertIn("Subject:", blob)
            self.assertIn("= ?UTF-8?B?aGk=?=".replace(" ", ""), blob)
            lines = convo.cmd("UID FETCH 101 (UID BODY.PEEK[])")
            blob = "\n".join(lines)
            self.assertIn("multipart/mixed", blob)
            self.assertIn("a.bin", blob)
            convo.cmd("LOGOUT")
        finally:
            sock_close(sid)

    def _xoauth2_session(self, token):
        """Open a proxy session, greet + CAPABILITY, send AUTHENTICATE."""
        srv = FakeImap().start()
        status, _, data = sock_open("127.0.0.1", ti(srv.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        convo = ImapScript(lambda text: sock_send(sid, text),
                           lambda: recv_text(sid))
        convo.greeting()
        convo.cmd("CAPABILITY")
        payload = "user=ada@example.com\x01auth=Bearer " + token + "\x01\x01"
        blob = base64.b64encode(payload.encode()).decode()
        tag = convo.new_tag()
        convo.send(tag + " AUTHENTICATE XOAUTH2 " + blob + "\r\n")
        return srv, convo, sid, tag, payload

    def test_imap_xoauth2_over_http_proxy(self):
        srv, convo, sid, tag, payload = self._xoauth2_session("tok-123")
        try:
            lines = convo.drain_tag(tag)
            self.assertTrue(any(" OK " in ln for ln in lines), lines)
            self.assertEqual(srv.auth_seen, [payload])
            convo.cmd("EXAMINE INBOX")
            convo.cmd("LOGOUT")
        finally:
            sock_close(sid)

    def test_imap_xoauth2_rejection_continuation(self):
        srv, convo, sid, tag, _ = self._xoauth2_session("bad-token")
        try:
            plus = convo.read_continuation()
            self.assertTrue(plus.startswith("+ "), plus)
            lines = convo.drain_tag(tag)
            self.assertTrue(any(" NO " in ln for ln in lines), lines)
            self.assertEqual(srv.abort_seen, ["\r\n"])
        finally:
            sock_close(sid)

    def test_imap_gmail_shaped_session(self):
        """Gmail has no SORT capability and answers AUTHENTICATE with an
        unsolicited "* CAPABILITY" line; the session must still walk
        through EXAMINE -> SEARCH -> FETCH to the headers."""
        srv = FakeImap(gmail=True).start()
        status, _, data = sock_open("127.0.0.1", ti(srv.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            convo = ImapScript(lambda text: sock_send(sid, text),
                               lambda: recv_text(sid))
            convo.greeting()
            caps = convo.cmd("CAPABILITY")
            self.assertTrue(any("X-GM-EXT-1" in ln for ln in caps), caps)
            self.assertFalse(any("SORT" in ln for ln in caps), caps)
            payload = "user=ada@example.com\x01auth=Bearer tok-123\x01\x01"
            blob = base64.b64encode(payload.encode()).decode()
            tag = convo.new_tag()
            convo.send(tag + " AUTHENTICATE XOAUTH2 " + blob + "\r\n")
            lines = convo.drain_tag(tag)
            # The unsolicited capability line rides along with the OK.
            self.assertTrue(any(ln.startswith("* CAPABILITY")
                                for ln in lines), lines)
            self.assertTrue(any(" OK " in ln for ln in lines), lines)
            self.assertEqual(srv.auth_seen, [payload])
            convo.cmd("EXAMINE INBOX")
            try:
                convo.cmd("UID SORT (REVERSE DATE) UTF-8 ALL")
                self.fail("gmail fake must reject SORT (BAD)")
            except AssertionError as err:
                self.assertIn("BAD", str(err))
            lines = convo.cmd("UID SEARCH ALL")
            self.assertTrue(any("* SEARCH 101 102" in ln for ln in lines),
                            lines)
            lines = convo.cmd(
                "UID FETCH 102,101 (UID FLAGS INTERNALDATE "
                "RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
                "(FROM TO CC SUBJECT DATE MESSAGE-ID)])")
            blob = "\n".join(lines)
            self.assertIn("UID 102", blob)
            self.assertIn("UID 101", blob)
            self.assertIn("Subject:", blob)
            convo.cmd("LOGOUT")
        finally:
            sock_close(sid)

    def test_imap_over_ws_proxy(self):
        try:
            from tests.test_socket_ws import upgrade, ws_capable
        except ImportError:
            self.skipTest("no ws helper module")
            return
        if not ws_capable():
            self.skipTest("no socket_ws route on this port")
        srv = FakeImap().start()
        status, _, data = sock_open("127.0.0.1", ti(srv.port))
        self.assertEqual(status, 201, data)
        sid = data["id"]
        try:
            ws = upgrade(sid)

            def send(text):
                ws.send_frame(0x1, text.encode())

            def recv():
                kind, op, payload = ws.recv_message()
                assert kind == "msg", (kind, op)
                return payload.decode("utf-8", "replace")

            convo = ImapScript(send, recv)
            convo.greeting()
            convo.cmd('LOGIN "user" "pass"')
            convo.cmd("EXAMINE INBOX")
            lines = convo.cmd("UID SEARCH ALL")
            self.assertTrue(any("* SEARCH" in ln for ln in lines))
            convo.cmd("LOGOUT")
            ws.close()
        finally:
            sock_close(sid)


if __name__ == "__main__":
    unittest.main()
