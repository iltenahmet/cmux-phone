#!/usr/bin/env python3
"""Checks the live install the way the phone uses it, through the tailnet URL.

Each check asserts what you would see, not a step on the way: your panes in the list, the
screen you're looking at on the terminal stream, the right words from dictation.

It only reads. It never creates, types into or resizes anything in cmux, so
opening, sending, photos and scrolling are not covered here.

Usage: ./smoke_test.py https://<this-mac>.<tailnet>.ts.net
"""
import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

ORIGIN = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else sys.exit(__doc__)
HOST = urlparse(ORIGIN).hostname
# Escape sequences the stream draws with; stripped so it can be compared with the pane's text.
ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78DEHMc]")
failures = []


def check(name, fn):
    try:
        fn()
        print(f"✓ {name}")
    except Exception as e:
        failures.append(name)
        print(f"✗ {name}: {e}")


def request(path, body=None, content_type="application/json", origin=ORIGIN):
    headers = {"Origin": origin}
    if body is not None:
        headers["Content-Type"] = content_type
        if content_type == "application/json":
            body = json.dumps(body).encode()
    req = urllib.request.Request(ORIGIN + path, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


CMUX_SOCKET = os.environ.get("CMUX_SOCKET_PATH") or os.path.expanduser("~/.local/state/cmux/cmux.sock")


def cmux(method, params=None):
    """One call on cmux's socket, to compare the phone's view with cmux's own."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(CMUX_SOCKET)
        s.sendall(json.dumps({"id": 1, "method": method, "params": params or {}}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf)
    assert reply.get("ok"), f"cmux {method}: {reply.get('error')}"
    return reply.get("result") or {}


def first_frame(pane):
    """Opens the pane's terminal stream like the page does and returns the text of its first frame."""
    req = urllib.request.Request(f"{ORIGIN}/api/panes/{pane}/stream", headers={"Origin": ORIGIN})
    with urllib.request.urlopen(req, timeout=10) as r:
        for line in r:
            if line.startswith(b"data: "):
                return ESCAPES.sub("", json.loads(line[6:])["frame"])
    return ""


def main():
    def page():
        status, body = request("/")
        assert status == 200 and b"<title>cmux</title>" in body, f"GET / returned {status}"
    check("page loads", page)

    listed = []

    def pane_list():
        status, body = request("/api/panes")
        assert status == 200, f"GET /api/panes returned {status}: {body[:200]!r}"
        data = json.loads(body)
        current = [s for s in data["sessions"] if s["current"]]
        assert len(current) == 1, f"expected one current session, got {current}"
        listed.extend(p["id"] for p in data["panes"])
        window = next(w for w in cmux("system.tree", {"all_windows": True})["windows"] if w["id"] == current[0]["id"])
        real = sorted(s["id"] for ws in window["workspaces"] for pane in ws["panes"] for s in pane["surfaces"] if s["type"] == "terminal")
        assert sorted(listed) == real, f"list shows {sorted(listed)}, cmux window {current[0]['name']} has {real}"
    check("pane list matches the cmux window", pane_list)

    def terminal():
        # Reading the stream does not resize the pane; only opening it on the phone does.
        assert listed, "no panes to look at"
        for pane in listed:
            lines = [l for l in base64.b64decode(cmux("surface.read_text", {"surface_id": pane}).get("base64", "")).decode(errors="replace").splitlines() if len(l.split()) >= 2]
            if lines:
                break
        assert lines, "every pane is blank, nothing to compare"
        text = "".join(first_frame(pane).split())
        assert text, "connected, but no terminal output arrived"
        # cmux joins a wrapped line when it hands over text, and the stream places words by column: compare the words alone.
        line = max(lines[-10:], key=len)
        assert "".join(line.split())[:30] in text, f"stream does not show the screen's line {line.strip()!r}"
    check("terminal shows the pane's screen", terminal)

    def dictation():
        with tempfile.TemporaryDirectory() as d:
            clip = os.path.join(d, "clip.m4a")
            subprocess.run(["say", "-o", clip, "--data-format=aac", "The quick brown fox jumps over the lazy dog."], check=True)
            with open(clip, "rb") as f:
                status, body = request("/api/transcribe", f.read(), content_type="audio/mp4")
        assert status == 200, f"{status}: {body[:200]!r}"
        text = json.loads(body)["text"].lower()
        assert "quick brown fox" in text, f"got {text!r}"
    check("dictation returns the spoken words", dictation)

    def other_sites_blocked():
        # The pane needn't exist: the origin is refused before anything reaches cmux.
        pane = "00000000-0000-0000-0000-000000000000"
        for action, body in (("key", {"key": "enter"}), ("input", {"data": "x"})):
            status, _ = request(f"/api/panes/{pane}/{action}", body, origin="https://evil.example")
            assert status == 403, f"POST {action} from another origin returned {status}"
    check("other websites can't type into the terminal", other_sites_blocked)

    if failures:
        sys.exit(f"\n{len(failures)} failed: {', '.join(failures)}")
    print("\nall checks passed")


if __name__ == "__main__":
    main()
