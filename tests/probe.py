"""probe.py - BHT/1 conformance suite, written from SPEC.md alone.

It carries its own tiny encoder/decoder (a third one: it imports nothing from
bserve or bcurl), so a pass means "agrees with the spec", not "agrees with
itself".

    python tests/probe.py                 test ./bserve and ./bcurl
    python tests/probe.py --server H:P    test someone else's server instead
                                          (it must serve a root made by
                                          --make-fixtures)
    python tests/probe.py --client "CMD"  test someone else's client instead
    python tests/probe.py --make-fixtures DIR
                                          write the fixture tree and exit;
                                          serve DIR/root
Extra arguments go to unittest, e.g.  -k traversal  or  -v.
"""

import argparse
import hashlib
import os
import random
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
CONFIG = {"server": None, "client": None, "root": None, "custom_client": False}

# ------------------------------------------------------------------ spec

PREFACE = b"BHT\x01"
DATA, HEADERS = 0x00, 0x01
END_STREAM = 0x01
CODES = {":method": 1, ":path": 2, ":status": 3, "host": 4, "user-agent": 5,
         "accept": 6, "content-type": 7, "content-length": 8, "server": 9,
         "date": 10}
NAMES = {v: k for k, v in CODES.items()}


def u16(n):
    return n.to_bytes(2, "big")


def field(name, value):
    if name in CODES:
        return bytes([CODES[name]]) + u16(len(value)) + value
    raw = name.encode()
    return b"\x00" + u16(len(raw)) + raw + u16(len(value)) + value


def frame(ftype, flags, stream, payload):
    return (len(payload).to_bytes(3, "big") + bytes([ftype, flags])
            + stream.to_bytes(3, "big") + payload)


def request(stream, path, method=b"GET", flags=END_STREAM, extra=()):
    block = (field(":method", method) + field(":path", path)
             + field("host", b"localhost") + field("user-agent", b"probe/1")
             + field("accept", b"*/*") + b"".join(field(n, v) for n, v in extra))
    return frame(HEADERS, flags, stream, block)


def decode_block(p):
    out, i = [], 0
    while i < len(p):
        code = p[i]
        i += 1
        if code == 0:
            n = int.from_bytes(p[i:i + 2], "big")
            name = p[i + 2:i + 2 + n].decode()
            i += 2 + n
        else:
            name = NAMES[code]  # KeyError = server sent a reserved code
        n = int.from_bytes(p[i:i + 2], "big")
        out.append((name, p[i + 2:i + 2 + n]))
        i += 2 + n
    assert i == len(p), "header block overran its frame"
    return out


class Response:
    def __init__(self):
        self.headers, self.body, self.frames = {}, b"", []

    @property
    def status(self):
        return self.headers.get(":status")


class Conn:
    """One raw BHT/1 connection to the server under test."""

    def __init__(self, preface=True):
        host, port = CONFIG["server"]
        self.sock = socket.create_connection((host, port), timeout=10)
        self.reader = self.sock.makefile("rb")
        if preface:
            self.send(PREFACE)

    def send(self, data):
        self.sock.sendall(data)

    def read(self, n):
        data = self.reader.read(n)
        if len(data) < n:
            raise EOFError("server closed the connection")
        return data

    def response(self, stream):
        """Read frames until END_STREAM on `stream`. Frames for any other
        stream, or of unknown type, are an error from a v1 server."""
        r = Response()
        while True:
            h = self.read(8)
            length, ftype, flags = int.from_bytes(h[:3], "big"), h[3], h[4]
            fstream = int.from_bytes(h[5:], "big")
            payload = self.read(length)
            r.frames.append((ftype, flags, fstream, length))
            if fstream != stream or ftype not in (DATA, HEADERS):
                raise AssertionError(f"unexpected frame {r.frames[-1]}")
            if ftype == HEADERS:
                if r.headers:
                    raise AssertionError("two HEADERS frames in one response")
                r.headers = dict(decode_block(payload))
            else:
                if not r.headers:
                    raise AssertionError("DATA before HEADERS")
                r.body += payload
            if flags & END_STREAM:
                return r

    def get(self, stream, path, **kw):
        self.send(request(stream, path, **kw))
        return self.response(stream)

    def close(self):
        self.reader.close()
        self.sock.close()


# -------------------------------------------------------------- fixtures

def big_bytes():
    return bytes(random.Random(1).randrange(256) for _ in range(100_000))


def make_fixtures(base):
    """base/root is what the server serves; base/secret.txt must never leak."""
    root = os.path.join(base, "root")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    files = {"index.html": b"<h1>hi</h1>\n", "sub/index.html": b"<h1>sub</h1>\n",
             "empty.txt": b"", "big.bin": big_bytes(), "a b.txt": b"space\n"}
    for name, data in files.items():
        with open(os.path.join(root, name), "wb") as f:
            f.write(data)
    with open(os.path.join(base, "secret.txt"), "wb") as f:
        f.write(b"TOP SECRET\n")
    try:
        os.symlink(os.path.join(base, "secret.txt"), os.path.join(root, "escape.txt"))
    except (OSError, NotImplementedError):
        pass  # Windows without developer mode: that test is skipped
    return root


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ======================================================== server tests

class Server(unittest.TestCase):
    def setUp(self):
        self.c = Conn()

    def tearDown(self):
        self.c.close()

    def assertStatus(self, r, status):
        self.assertEqual(r.status, status, f"body={r.body!r}")

    # --- SPEC 5 basics
    def test_get_index(self):
        r = self.c.get(1, b"/index.html")
        self.assertStatus(r, b"200")
        self.assertEqual(r.body, b"<h1>hi</h1>\n")
        self.assertEqual(r.headers.get("content-length"), b"12")
        self.assertEqual(r.headers.get("content-type"), b"text/html")
        self.assertEqual([f[:3] for f in r.frames], [(HEADERS, 0, 1), (DATA, END_STREAM, 1)])

    def test_directory_maps_to_index(self):
        self.assertEqual(self.c.get(1, b"/").body, b"<h1>hi</h1>\n")
        self.assertEqual(self.c.get(2, b"/sub/").body, b"<h1>sub</h1>\n")
        self.assertEqual(self.c.get(3, b"/sub").body, b"<h1>sub</h1>\n")

    def test_query_and_fragment_stripped(self):
        self.assertStatus(self.c.get(1, b"/index.html?x=1#top"), b"200")

    def test_percent_decoding(self):
        self.assertEqual(self.c.get(1, b"/a%20b.txt").body, b"space\n")

    def test_empty_file_is_headers_only(self):
        r = self.c.get(1, b"/empty.txt")
        self.assertStatus(r, b"200")
        self.assertEqual(r.frames, [(HEADERS, END_STREAM, 1, r.frames[0][3])])

    def test_big_file_split_and_intact(self):
        r = self.c.get(1, b"/big.bin")
        self.assertEqual(hashlib.sha256(r.body).digest(), hashlib.sha256(big_bytes()).digest())
        self.assertEqual(r.headers.get("content-length"), b"100000")
        data = [f for f in r.frames if f[0] == DATA]
        self.assertGreater(len(data), 1, "100 kB should not fit one frame comfortably")
        self.assertTrue(all(f[1] == 0 for f in data[:-1]) and data[-1][1] == END_STREAM)

    def test_head(self):
        r = self.c.get(1, b"/big.bin", method=b"HEAD")
        self.assertStatus(r, b"200")
        self.assertEqual((r.body, len(r.frames)), (b"", 1))
        self.assertEqual(r.headers.get("content-length"), b"100000")

    def test_literal_header_name_accepted(self):
        r = self.c.get(1, b"/index.html", extra=[("x-trace", b"abc")])
        self.assertStatus(r, b"200")

    # --- SPEC 1: one connection, many requests
    def test_many_requests_one_connection(self):
        for s in range(1, 51):
            self.assertStatus(self.c.get(s, b"/index.html"), b"200")

    def test_pipelined_in_order(self):
        self.c.send(request(1, b"/index.html") + request(2, b"/nope") + request(3, b"/sub/"))
        self.assertEqual([self.c.response(s).status for s in (1, 2, 3)],
                         [b"200", b"404", b"200"])

    def test_stream_ids_may_skip(self):
        self.assertStatus(self.c.get(5, b"/"), b"200")
        self.assertStatus(self.c.get(100, b"/"), b"200")

    def test_request_split_one_byte_at_a_time(self):
        for b in request(1, b"/index.html"):
            self.c.send(bytes([b]))
            time.sleep(0.0005)
        self.assertStatus(self.c.response(1), b"200")

    def test_half_close_after_request_still_answered(self):
        self.c.send(request(1, b"/index.html"))
        self.c.sock.shutdown(socket.SHUT_WR)
        self.assertStatus(self.c.response(1), b"200")

    def test_connections_are_independent(self):
        stalled = Conn()
        stalled.send(request(1, b"/index.html")[:5])  # half a frame header
        try:
            self.assertStatus(self.c.get(1, b"/index.html"), b"200")
        finally:
            stalled.close()

    # --- SPEC 3: extension rules
    def test_unknown_frame_type_skipped(self):
        self.c.send(frame(0x7F, 0, 0, b"\xAA" * 1000))
        self.c.send(frame(0xFF, 0xFF, 1, b""))
        self.assertStatus(self.c.get(1, b"/index.html"), b"200")

    def test_unknown_frame_type_large_payload_skipped(self):
        self.c.send(frame(0x42, 0, 0, b"\x00" * (1 << 20)))
        self.assertStatus(self.c.get(1, b"/index.html"), b"200")

    def test_unknown_flag_bits_ignored(self):
        self.assertStatus(self.c.get(1, b"/index.html", flags=0xF1), b"200")

    def test_client_data_frames_discarded(self):
        self.c.send(frame(DATA, END_STREAM, 7, b"ignored body"))
        self.assertStatus(self.c.get(1, b"/index.html"), b"200")

    # --- SPEC 6: 400s keep the connection
    def bad(self, payload, flags=END_STREAM, stream=1, status=b"400"):
        self.c.send(frame(HEADERS, flags, stream, payload))
        self.assertStatus(self.c.response(stream), status)
        nxt = max(stream, 1) + 1
        self.assertStatus(self.c.get(nxt, b"/index.html"), b"200")  # still usable

    def test_400_truncated_block(self):
        self.bad(b"\x01\x00\x09GE")

    def test_400_trailing_garbage(self):
        self.bad(field(":method", b"GET") + field(":path", b"/") + b"\x02")

    def test_400_reserved_name_code(self):
        self.bad(bytes([0x0B]) + u16(1) + b"x" + field(":method", b"GET") + field(":path", b"/"))

    def test_400_uppercase_literal_name(self):
        self.bad(field(":method", b"GET") + field(":path", b"/") + field("X-Up", b"1"))

    def test_400_empty_literal_name(self):
        self.bad(field(":method", b"GET") + field(":path", b"/") + b"\x00\x00\x00\x00\x00")

    def test_400_missing_path(self):
        self.bad(field(":method", b"GET"))

    def test_400_missing_method(self):
        self.bad(field(":path", b"/"))

    def test_400_repeated_path(self):
        self.bad(field(":method", b"GET") + field(":path", b"/") + field(":path", b"/"))

    def test_400_status_in_request(self):
        self.bad(field(":method", b"GET") + field(":path", b"/") + field(":status", b"200"))

    def test_400_headers_without_end_stream(self):
        self.bad(field(":method", b"GET") + field(":path", b"/"), flags=0)

    def test_400_stream_zero(self):
        self.bad(field(":method", b"GET") + field(":path", b"/"), stream=0)

    def test_400_stream_not_increasing(self):
        self.assertStatus(self.c.get(5, b"/"), b"200")
        self.c.send(request(5, b"/"))
        self.assertStatus(self.c.response(5), b"400")
        self.assertStatus(self.c.get(6, b"/"), b"200")

    def test_405_other_methods(self):
        for s, m in enumerate([b"POST", b"PUT", b"DELETE", b"get"], start=1):
            self.assertStatus(self.c.get(s, b"/index.html", method=m), b"405")

    def test_error_responses_have_content_length(self):
        r = self.c.get(1, b"/nope")
        self.assertEqual(r.headers.get("content-length"), str(len(r.body)).encode())

    # --- SPEC 5: path mapping must not escape the root
    def test_traversal_all_404(self):
        attacks = [b"/nope.html", b"/../secret.txt", b"/../../secret.txt",
                   b"/%2e%2e/secret.txt", b"/%2E%2E/secret.txt", b"/..%2fsecret.txt",
                   b"/..%2Fsecret.txt", b"/sub/../../secret.txt", b"/./index.html",
                   b"/%00", b"/index.html%00.txt", b"/..\\secret.txt",
                   b"/..%5csecret.txt", b"/C:/Windows/win.ini", b"/c:%5cwindows",
                   b"/%ff", b"/%", b"/%zz", b"index.html", b"", b"//../secret.txt"]
        got = {}
        for s, path in enumerate(attacks, start=1):
            r = self.c.get(s, path)
            self.assertNotIn(b"TOP SECRET", r.body, path)
            got[path] = r.status
        self.assertEqual({p: st for p, st in got.items() if st != b"404"}, {})

    def test_symlink_out_of_root_refused(self):
        if CONFIG["root"] is None or not os.path.islink(os.path.join(CONFIG["root"], "escape.txt")):
            self.skipTest("no symlink fixture (needs symlink rights / local server)")
        r = self.c.get(1, b"/escape.txt")
        self.assertStatus(r, b"404")
        self.assertNotIn(b"TOP SECRET", r.body)

    # --- SPEC 1: preface
    def test_bad_preface_closed_without_reply(self):
        raw = Conn(preface=False)
        raw.send(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        try:
            got = raw.sock.recv(100)
        except ConnectionResetError:
            got = b""  # unread input at close -> reset on some stacks
        raw.close()
        self.assertEqual(got, b"")

    def test_wrong_version_closed(self):
        raw = Conn(preface=False)
        raw.send(b"BHT\x02" + request(1, b"/"))
        try:
            got = raw.sock.recv(100)
        except ConnectionResetError:
            got = b""
        raw.close()
        self.assertEqual(got, b"")


# ======================================================== client tests

class Mock:
    """A toy BHT/1 server. reply(stream, path) returns the bytes to send back,
    or None to hang up. Records every byte received and every accept()."""

    def __init__(self, reply, trickle=False, hangup=False):
        self.hangup = hangup
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.reply, self.trickle = reply, trickle
        self.accepts, self.received = 0, bytearray()
        threading.Thread(target=self._accept, daemon=True).start()

    def url(self, path):
        return f"127.0.0.1:{self.port}{path}"

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.accepts += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        f = c.makefile("rb")
        try:
            self.received += f.read(4)
            while True:
                h = f.read(8)
                if len(h) < 8:
                    return
                p = f.read(int.from_bytes(h[:3], "big"))
                self.received += h + p
                if h[3] != HEADERS:
                    continue
                path = dict(decode_block(p)).get(":path", b"")
                out = self.reply(int.from_bytes(h[5:], "big"), path)
                if out is None:
                    return
                step = 3 if self.trickle else len(out) or 1
                for k in range(0, len(out), step):
                    c.sendall(out[k:k + step])
                if self.hangup:
                    return
        except OSError:
            pass
        finally:
            f.close()
            c.close()

    def close(self):
        self.srv.close()


def ok_reply(stream, path, status=b"200"):
    body = b"mock:" + path + b"\n"
    return (frame(HEADERS, 0, stream, field(":status", status)
                  + field("content-length", str(len(body)).encode()))
            + frame(DATA, END_STREAM, stream, body))


def hostile_reply(stream, path):
    """Legal-but-awkward: unknown types, foreign streams, odd flags, an empty
    DATA frame, a literal header name, body split in three."""
    body = b"mock:" + path + b"\n"
    return (frame(0x42, 0, 0, b"future frame type")
            + frame(DATA, END_STREAM, stream + 1000, b"not your stream")
            + frame(HEADERS, 0x80, stream, field(":status", b"200") + field("x-extra", b"yes"))
            + frame(0x99, 0xFF, stream, b"\x00" * 64)
            + frame(DATA, 0x80, stream, body[:3])
            + frame(DATA, 0, stream, b"")
            + frame(DATA, END_STREAM, stream, body[3:]))


class Client(unittest.TestCase):
    def run_client(self, *args):
        p = subprocess.run(CONFIG["client"] + list(args), capture_output=True, timeout=30)
        err = p.stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", err, "client crashed instead of reporting an error")
        return p.returncode, p.stdout, err

    def mock(self, reply, **kw):
        m = Mock(reply, **kw)
        self.addCleanup(m.close)
        return m

    def test_request_bytes_follow_spec(self):
        m = self.mock(ok_reply)
        self.run_client(m.url("/index.html"))
        data = bytes(m.received)
        self.assertEqual(data[:4], PREFACE)
        h = data[4:12]
        self.assertEqual((h[3], h[4] & END_STREAM, int.from_bytes(h[5:], "big")), (HEADERS, 1, 1))
        block = data[12:12 + int.from_bytes(h[:3], "big")]
        fields = decode_block(block)
        names = [n for n, _ in fields]
        self.assertEqual(dict(fields)[":method"], b"GET")
        self.assertEqual(dict(fields)[":path"], b"/index.html")
        self.assertEqual(names.count(":method") + names.count(":path"), 2)
        self.assertNotEqual(block[0], 0x00, "table names should use their code (SHOULD)")

    def test_body_to_stdout_exit_0(self):
        m = self.mock(ok_reply)
        code, out, _ = self.run_client(m.url("/x"))
        self.assertEqual((code, out), (0, b"mock:/x\n"))

    def test_tolerates_unknown_frames_flags_and_streams(self):
        m = self.mock(hostile_reply)
        code, out, err = self.run_client(m.url("/x"))
        self.assertEqual((code, out), (0, b"mock:/x\n"), err)

    def test_trickled_response(self):
        m = self.mock(hostile_reply, trickle=True)
        code, out, err = self.run_client(m.url("/x"))
        self.assertEqual((code, out), (0, b"mock:/x\n"), err)

    def test_binary_body_byte_exact(self):
        blob = big_bytes()
        m = self.mock(lambda s, p: frame(HEADERS, 0, s, field(":status", b"200"))
                      + b"".join(frame(DATA, 0, s, blob[i:i + 30000]) for i in range(0, len(blob), 30000))
                      + frame(DATA, END_STREAM, s, b""))
        code, out, _ = self.run_client(m.url("/b"))
        self.assertEqual(code, 0)
        self.assertEqual(hashlib.sha256(out).digest(), hashlib.sha256(blob).digest())

    def test_one_connection_for_many_urls(self):
        m = self.mock(ok_reply, trickle=True)
        code, out, err = self.run_client(m.url("/a"), m.url("/b"), m.url("/c"))
        self.assertEqual(m.accepts, 1, "client opened a second connection")
        self.assertEqual(out, b"mock:/a\nmock:/b\nmock:/c\n", err)

    def test_4xx_exits_nonzero(self):
        m = self.mock(lambda s, p: ok_reply(s, p, b"404"))
        self.assertNotEqual(self.run_client(m.url("/x"))[0], 0)

    def test_5xx_exits_nonzero(self):
        m = self.mock(lambda s, p: ok_reply(s, p, b"503"))
        self.assertNotEqual(self.run_client(m.url("/x"))[0], 0)

    def test_3xx_exits_zero(self):
        m = self.mock(lambda s, p: ok_reply(s, p, b"304"))
        self.assertEqual(self.run_client(m.url("/x"))[0], 0)

    def test_hangup_mid_response_exits_nonzero(self):
        m = self.mock(lambda s, p: frame(HEADERS, 0, s, field(":status", b"200")) + frame(DATA, 0, s, b"part"),
                      hangup=True)  # closes with no END_STREAM
        self.assertNotEqual(self.run_client(m.url("/x"))[0], 0)

    def test_nothing_listening_exits_nonzero(self):
        self.assertNotEqual(self.run_client(f"127.0.0.1:{free_port()}/x")[0], 0)

    def test_verbose_dumps_frames_to_stderr_only(self):
        m = self.mock(ok_reply)
        code, out, err = self.run_client("-v", m.url("/x"))
        self.assertEqual((code, out), (0, b"mock:/x\n"), "-v must not touch stdout")
        hexonly = "".join(ch for ch in err.lower() if ch in "0123456789abcdef")
        self.assertIn("42485401", hexonly, "preface not dumped")
        self.assertIn(frame(DATA, END_STREAM, 1, b"mock:/x\n")[:8].hex(), hexonly,
                      "DATA frame header not dumped")

    # --- bcurl's own documented choices (skipped for --client)
    def test_bcurl_exit_codes(self):
        if CONFIG["custom_client"]:
            self.skipTest("bcurl-specific")
        m = self.mock(lambda s, p: ok_reply(s, p, b"500" if p == b"/boom" else b"404" if p == b"/x" else b"200"))
        self.assertEqual(self.run_client(m.url("/x"))[0], 4)
        self.assertEqual(self.run_client(m.url("/boom"))[0], 5)
        self.assertEqual(self.run_client(m.url("/x"), m.url("/boom"), m.url("/ok"))[0], 5)
        self.assertEqual(self.run_client(m.url("/x"), m.url("/ok"))[0], 4)

    def test_bcurl_protocol_errors_exit_1(self):
        if CONFIG["custom_client"]:
            self.skipTest("bcurl-specific")
        for reply in (lambda s, p: None,
                      lambda s, p: frame(DATA, END_STREAM, s, b"x"),
                      lambda s, p: frame(HEADERS, END_STREAM, s, field(":status", b"2x0")),
                      lambda s, p: frame(HEADERS, END_STREAM, s, b"\x03\x00\x09200"),
                      lambda s, p: frame(HEADERS, END_STREAM, s, b"\x0b\x00\x00")):
            m = self.mock(reply)
            self.assertEqual(self.run_client(m.url("/x"))[0], 1)

    def test_bcurl_refuses_two_servers(self):
        if CONFIG["custom_client"]:
            self.skipTest("bcurl-specific")
        code, _, err = self.run_client("127.0.0.1:1/a", "127.0.0.2:1/b")
        self.assertEqual(code, 1)
        self.assertIn("Usage:", err)

    def test_bcurl_usage_errors(self):
        if CONFIG["custom_client"]:
            self.skipTest("bcurl-specific")
        for args in ([], ["--bogus", "127.0.0.1:1/x"], ["127.0.0.1:99999/x"]):
            code, out, err = self.run_client(*args)
            self.assertEqual((code, out), (1, b""), args)
            self.assertIn("Usage:", err, args)


# ======================================================== interop

class Interop(unittest.TestCase):
    """The client under test against the server under test."""

    def run_client(self, *paths):
        host, port = CONFIG["server"]
        args = [f"{host}:{port}{p}" for p in paths]
        p = subprocess.run(CONFIG["client"] + args, capture_output=True, timeout=60)
        return p.returncode, p.stdout

    def test_fetch_index(self):
        self.assertEqual(self.run_client("/index.html"), (0, b"<h1>hi</h1>\n"))

    def test_fetch_big_file(self):
        code, out = self.run_client("/big.bin")
        self.assertEqual(code, 0)
        self.assertEqual(hashlib.sha256(out).digest(), hashlib.sha256(big_bytes()).digest())

    def test_404_nonzero(self):
        self.assertNotEqual(self.run_client("/nope")[0], 0)


# ================================================================ main

def wait_for_port(host, port, proc, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"server exited early with {proc.returncode}")
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listening on {host}:{port}")


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--server")
    ap.add_argument("--client")
    ap.add_argument("--make-fixtures")
    ap.add_argument("-h", "--help", action="store_true")
    opts, rest = ap.parse_known_args()
    if opts.help:
        print(__doc__)
        return 0
    if opts.make_fixtures:
        print("serve:", make_fixtures(os.path.abspath(opts.make_fixtures)))
        return 0

    if opts.client:
        CONFIG["client"] = shlex.split(opts.client, posix=os.name != "nt")
        CONFIG["custom_client"] = True
    else:
        CONFIG["client"] = [sys.executable, os.path.join(PROJECT, "bcurl")]

    proc, tmp = None, None
    try:
        if opts.server:
            host, _, port = opts.server.rpartition(":")
            CONFIG["server"] = (host.strip("[]") or "127.0.0.1", int(port))
        else:
            tmp = tempfile.TemporaryDirectory(prefix="bht-probe-")
            CONFIG["root"] = make_fixtures(tmp.name)
            port = free_port()
            log = open(os.path.join(tmp.name, "bserve.log"), "wb")
            proc = subprocess.Popen([sys.executable, os.path.join(PROJECT, "bserve"),
                                     CONFIG["root"], str(port)],
                                    stdout=log, stderr=subprocess.STDOUT)
            CONFIG["server"] = ("127.0.0.1", port)
        wait_for_port(*CONFIG["server"], proc)
        prog = unittest.main(argv=[sys.argv[0], *rest], exit=False, verbosity=2)
        return 0 if prog.result.wasSuccessful() else 1
    finally:
        if proc is not None:
            proc.kill()  # direct child, not a shell wrapper: this really stops it
            proc.wait()
            log.close()
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
