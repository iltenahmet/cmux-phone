#!/usr/bin/env python3
"""Phone UI for the cmux panes on this Mac, served on the tailnet via `tailscale serve`."""
import base64
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOST, PORT = "127.0.0.1", int(os.environ.get("CMUX_PHONE_PORT", "8768"))
# Set by install.sh in the launchd job: this Mac's tailnet URL, and the one Tailscale login allowed in.
PUBLIC_ORIGIN = os.environ.get("CMUX_PHONE_ORIGIN", "")
ALLOWED_LOGIN = os.environ.get("CMUX_PHONE_LOGIN", "")
DEV = "--dev" in sys.argv  # no Tailscale login check and no dictation; for testing on localhost only
CMUX_SOCKET = os.environ.get("CMUX_SOCKET_PATH") or str(Path.home() / ".local/state/cmux/cmux.sock")
CMUX_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"
STATE = Path(os.environ.get("CMUX_PHONE_STATE") or Path.home() / ".local/state/cmux-phone")  # the browser test sets its own
TARGET_FILE = STATE / "target"  # id of the cmux window the list shows
FITTED_FILE = STATE / "fitted.json"  # panes sized to the phone, and the size policy each had before
STATIC = Path(__file__).resolve().parent / "static"
INDEX = STATIC / "index.html"
SERVER_SOURCE = Path(__file__).read_bytes()  # a change to this file needs a restart, so reading it once is enough
# The terminal library, web app manifest and icons the page loads; only these paths are served.
STATIC_FILES = {"/vendor/xterm.js": "text/javascript; charset=utf-8", "/vendor/xterm.css": "text/css; charset=utf-8",
                "/vendor/addon-fit.js": "text/javascript; charset=utf-8", "/vendor/codemirror.js": "text/javascript; charset=utf-8",
                "/manifest.json": "application/manifest+json", "/icon-180.png": "image/png", "/icon-512.png": "image/png"}
MAX_BODY = 64 * 1024
MAX_FILE = 2 * 1024 * 1024  # bigger files are not shown on the phone
MAX_AUDIO = 25 * 1024 * 1024
MAX_IMAGES = 20 * 1024 * 1024  # all images in one paste, before base64
UPLOADS = STATE / "uploads"
UPLOAD_DAYS = 7
IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"GIF87a": "gif", b"GIF89a": "gif"}
WHISPER_MODEL = STATE / "models/ggml-large-v3-turbo-q5_0.bin"
WHISPER_HOST, WHISPER_PORT = "127.0.0.1", int(os.environ.get("CMUX_PHONE_WHISPER_PORT", "8769"))

# Ids are cmux's own: a pane is a terminal surface, a window is a workspace, a session is a cmux window.
ID = r"([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})"
PANE_ROUTE = re.compile(rf"^/api/panes/{ID}/(open|back|fit|input|scroll|paste|key|close|rename)$")
STREAM_ROUTE = re.compile(rf"^/api/panes/{ID}/stream$")
WINDOW_ROUTE = re.compile(rf"^/api/windows/{ID}/(rename|panes|close)$")
SESSION_ROUTE = re.compile(rf"^/api/sessions/{ID}/(switch|close)$")
MAX_NAME = 100
# Phone button name -> cmux send_key name. Digits answer Claude's numbered menus; cmux has no digit keys, so they are typed.
KEYS = {"enter": "enter", "escape": "escape", "up": "up", "down": "down", "left": "left", "right": "right",
        "tab": "tab", "shift+tab": "shift+tab", "ctrl+c": "ctrl+c", "alt+p": "alt+p", "0": "0", "1": "1", "2": "2", "3": "3"}
MIN_COLS, MAX_COLS, MIN_ROWS, MAX_ROWS = 20, 300, 5, 200
FIT_LEASE_SECONDS = 15  # an open terminal stream renews it; a phone that vanishes hands the pane back to the Mac
STREAM_SECONDS = 0.08  # how often an open pane's screen is read
MOUSE_MODES = {1000, 1002, 1003}  # the app in the pane asked for mouse events, so it gets the wheel


# Claude Code draws its conversation, a rule, the ❯ input line, then a footer.
RULE = re.compile(r"^\s*─{8,}")
BULLET = re.compile(r"^[⏺●•] ")
CLAUDE_NOTICE = re.compile(r"new task\? /clear|Compacted\s+while\s+idle|^[⏺●•] Compacted\b|Update installed · /restart|0: Dismiss\s*$|^\s*1: Learn more\s*$")
# Claude Code's spinner line ("✻ Thinking…"), or Cursor's (" ⠰⠳ Working").
SPINNER = re.compile(r"^[✻✶✳✢✽·*+] \S.*…|^ [⠀-⣿]+ \S")
# The coding agents a pane can run, by process name. Cursor's CLI is started as `agent` or `cursor-agent`.
AGENT_PROCESS = {"claude": "claude", "codex": "codex", "agent": "cursor", "cursor-agent": "cursor"}
# For each agent: its input box, matched against a line and the next one joined by "\n", and a line it shows
# while it works.
# Claude Code: a rule, then the ❯ line. Cursor: a bar of ▄, then the → line. Codex: the › line, with no rule.
INPUT_BOX = {"claude": re.compile(r"\s*─{8,}.*\n\s*❯"), "cursor": re.compile(r"\s*▄{8,}.*\n\s*→"),
             "codex": re.compile(r"›(\s|$)")}
WORKING = {"claude": SPINNER, "cursor": re.compile(r"ctrl\+c to stop\s*$"), "codex": re.compile(r"to interrupt\)")}
# Claude Code and Cursor hide the input box while they ask you something. Codex keeps its › line, so its
# question or approval is known by the hint under it.
CODEX_ASK = re.compile(r"to confirm\b|to submit (answer|all)\b|enter continue")


class CmuxError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class WhisperError(Exception):
    pass


def rpc(method, params=None):
    """One call on cmux's socket: a JSON line out, a JSON line back."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(10)
            s.connect(CMUX_SOCKET)
            s.sendall(json.dumps({"id": 1, "method": method, "params": params or {}}).encode() + b"\n")
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        raise CmuxError("unavailable", f"cmux is not reachable ({e})") from e
    try:
        reply = json.loads(buf)
    except ValueError as e:
        raise CmuxError("bad_reply", "cmux sent an unreadable reply") from e
    if not reply.get("ok"):
        err = reply.get("error") or {}
        raise CmuxError(err.get("code", "error"), err.get("message", "cmux refused the request"))
    return reply.get("result") or {}


def cmux_cli(*args):
    """The cmux CLI, for making workspaces and splits: its flags are documented, the socket's parameters are not."""
    r = subprocess.run([CMUX_CLI, *args], capture_output=True, text=True, timeout=30,
                       env={**os.environ, "CMUX_SOCKET_PATH": CMUX_SOCKET, "CMUX_QUIET": "1"})
    if r.returncode:
        raise CmuxError("cli", (r.stderr or r.stdout).strip() or "cmux refused the request")
    return r.stdout


def windows():
    """Every cmux window with its workspaces, panes and surfaces."""
    return rpc("system.tree", {"all_windows": True}).get("windows", [])


def surfaces(window):
    """The terminal surfaces of one cmux window, in workspace order. Browser tabs are not panes."""
    out = []
    for ws in window.get("workspaces", []):
        for pane in ws.get("panes", []):
            for s in pane.get("surfaces", []):
                if s.get("type") == "terminal":
                    out.append({"id": s["id"], "ref": s["ref"], "title": s.get("title") or "", "tty": s.get("tty") or "",
                                "active": bool(s.get("focused") and ws.get("selected")), "workspace": ws})
    return out


def target_window(tree):
    """The cmux window the list shows: the one picked on the phone while it is open, else the first.
    None when cmux has no window."""
    try:
        saved = TARGET_FILE.read_text().strip()
    except OSError:
        saved = ""
    return next((w for w in tree if w["id"] == saved), tree[0] if tree else None)


def sessions_for_phone(tree, target):
    """cmux windows have no names, so they are numbered as cmux lists them."""
    return [{"id": w["id"], "name": str(i + 1), "windows": len(w.get("workspaces", [])), "attached": 1,
             "current": bool(target) and w["id"] == target["id"]} for i, w in enumerate(tree)]


def find_pane(pane):
    """The surface with this id in any window. Raises the error the page takes as "this pane is gone"."""
    for w in windows():
        for s in surfaces(w):
            if s["id"].lower() == pane.lower():
                return s
    raise CmuxError("not_found", f"can't find pane {pane}")


def find_workspace(workspace):
    for w in windows():
        for ws in w.get("workspaces", []):
            if ws["id"].lower() == workspace.lower():
                return ws
    raise CmuxError("not_found", f"can't find window {workspace}")


def grid(pane):
    return rpc("terminal.replay", {"terminal_id": pane}).get("render_grid") or {}


def rows_of(spans, count):
    """Spans grouped by row, each row's spans left to right."""
    rows = [[] for _ in range(count)]
    for span in spans:
        if 0 <= span["row"] < count:
            rows[span["row"]].append(span)
    for row in rows:
        row.sort(key=lambda s: s["column"])
    return rows


def plain(row):
    """One row of the screen as text, each span at its column."""
    out = ""
    for span in row:
        out += " " * (span["column"] - len(out)) + span["text"]
    return out.rstrip()


def screen_lines(g):
    return [plain(row) for row in rows_of(g.get("row_spans", []), g.get("rows", 0))]


def reads_mouse(g):
    return any(m.get("on") and not m.get("ansi") and m.get("code") in MOUSE_MODES for m in g.get("modes", []))


def rgb(color):
    return f"2;{int(color[1:3], 16)};{int(color[3:5], 16)};{int(color[5:7], 16)}"


def sgr(style):
    """The escape sequence that sets one cmux style. A palette color goes by its number, so the phone's theme colors it."""
    codes = ["0"]
    for flag, code in (("bold", "1"), ("faint", "2"), ("italic", "3"), ("underline", "4"), ("blink", "5"),
                       ("inverse", "7"), ("invisible", "8"), ("strikethrough", "9"), ("overline", "53")):
        if style.get(flag):
            codes.append(code)
    for side, base in (("foreground", "38"), ("background", "48")):
        source = style.get(f"{side}_source")
        if source == "palette" and isinstance(style.get(f"{side}_palette_index"), int):
            codes.append(f"{base};5;{style[f'{side}_palette_index']}")
        elif source not in (None, "default") and re.fullmatch(r"#[0-9A-Fa-f]{6}", style.get(side) or ""):
            codes.append(f"{base};{rgb(style[side])}")
    return f"\x1b[{';'.join(codes)}m"


def frame(g, offset):
    """The pane's screen as escape sequences that redraw a terminal of the same size from the top left.
    offset is how many rows the phone has scrolled back into the pane's history."""
    rows, history = g.get("rows", 0), g.get("scrollback_rows", 0)
    offset = max(0, min(offset, history))
    lines = rows_of(g.get("scrollback_spans", []), history)[history - offset:] + rows_of(g.get("row_spans", []), rows)
    styles = {s["id"]: sgr(s) for s in g.get("styles", []) if "id" in s}
    out = ["\x1b[?2026h\x1b[?25l"]
    for i, row in enumerate(lines[:rows]):
        out.append(f"\x1b[{i + 1};1H\x1b[0m\x1b[2K")
        for span in row:
            out.append(f"\x1b[{span['column'] + 1}G{styles.get(span.get('style_id', 0), chr(27) + '[0m')}{span['text']}")
    cursor = g.get("cursor") or {}
    if cursor.get("visible") and not offset:
        out.append(f"\x1b[0m\x1b[{cursor.get('row', 0) + 1};{cursor.get('column', 0) + 1}H\x1b[?25h")
    out.append("\x1b[0m\x1b[?2026l")
    return "".join(out), offset


# While a pane is open on the phone it is sized to the phone, so its text wraps at the phone's width.
# The pane's own size policy is kept and put back when the phone leaves, or stops renewing the lease.
fit_lock = threading.Lock()
fitted = {}  # pane id -> {"policy": the pane's own size policy, "size": [cols, rows], "until": lease expiry}
scrolled = {}  # pane id -> rows the phone has scrolled back


def save_fitted():
    FITTED_FILE.write_text(json.dumps({pane: f["policy"] for pane, f in fitted.items()}))


def restore(pane, policy):
    try:
        rpc("terminal.size_policy.set", {"surface_id": pane, "policy": policy})
    except CmuxError as e:
        if e.code != "not_found":
            print(f"restore {pane} failed: {e}", flush=True)
            return False
    return True


def fit(pane, cols, rows):
    cols, rows = min(max(cols, MIN_COLS), MAX_COLS), min(max(rows, MIN_ROWS), MAX_ROWS)
    with fit_lock:
        if pane not in fitted:
            policy = rpc("terminal.size_state", {"surface_id": pane}).get("size_state", {}).get("policy")
            fitted[pane] = {"policy": policy or {"mode": "latest", "fixed": None, "priority": []}, "size": None, "until": 0}
            save_fitted()
        entry = fitted[pane]
        if entry["size"] != [cols, rows]:
            rpc("terminal.size_policy.set", {"surface_id": pane, "policy": {"mode": "fixed", "fixed": {"cols": cols, "rows": rows}, "priority": []}})
            entry["size"] = [cols, rows]
        entry["until"] = time.time() + FIT_LEASE_SECONDS


def renew(pane):
    """Keep the pane at the phone's size for another lease. Returns that size, or None if the phone has let go."""
    with fit_lock:
        if pane not in fitted:
            return None
        fitted[pane]["until"] = time.time() + FIT_LEASE_SECONDS
        return fitted[pane]["size"]


def refit(pane):
    """cmux drops a size set on a pane that has not started yet, and then takes the same size again as no change.
    Asking for one row less first makes it apply the phone's size."""
    with fit_lock:
        size = (fitted.get(pane) or {}).get("size")
    if not size:
        return
    for rows in (size[1] - 1, size[1]):
        rpc("terminal.size_policy.set", {"surface_id": pane, "policy": {"mode": "fixed", "fixed": {"cols": size[0], "rows": rows}, "priority": []}})
        time.sleep(0.5)


def release(pane=None):
    """Hand one pane, or every pane, back to the Mac at its own size."""
    with fit_lock:
        for p in [p for p in fitted if pane in (None, p)]:
            if restore(p, fitted[p]["policy"]):
                del fitted[p]
                scrolled.pop(p, None)
        save_fitted()


def housekeeping():
    while True:
        time.sleep(3)
        try:
            with fit_lock:
                expired = [p for p, f in fitted.items() if f["until"] < time.time()]
            for p in expired:
                release(p)
        except Exception:
            traceback.print_exc()


def to_bottom(pane):
    """Typing into a pane the phone has scrolled back shows the bottom again."""
    scrolled.pop(pane, None)


def scroll(pane, up, count, col, row):
    """A swipe on the phone, as mouse wheel input. An app that reads the mouse gets the wheel itself;
    otherwise the phone scrolls through the pane's history, which cmux hands over with the screen."""
    g = grid(pane)
    if reads_mouse(g):
        rpc("terminal.input", {"terminal_id": pane, "text": f"\x1b[<{64 if up else 65};{col};{row}M" * count})
    else:
        scrolled[pane] = max(0, min(scrolled.get(pane, 0) + (count if up else -count), g.get("scrollback_rows", 0)))


def list_panes(window):
    agents = agent_ttys()

    def summary(numbered):
        index, s = numbered
        try:
            lines = screen_lines(grid(s["id"]))
        except CmuxError:
            lines = []
        while lines and not lines[-1]:
            lines.pop()
        agent = agents.get(s["tty"])
        return {
            "id": s["id"],
            "window": index,
            "window_id": s["workspace"]["id"],
            "window_name": s["workspace"].get("title") or "",
            "title": s["title"],
            "command": "terminal",
            "active": s["active"],
            "status": status(lines, agent) if agent else None,
            "last_line": last_line(lines, agent) if agent else next((l.strip() for l in reversed(lines) if l.strip()), ""),
        }
    index = {ws["id"]: i + 1 for i, ws in enumerate(window.get("workspaces", []))}
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(summary, [(index[s["workspace"]["id"]], s) for s in surfaces(window)]))


def new_ref(out, kind):
    m = re.search(rf"\b{kind}:\d+", out)
    if not m:
        raise CmuxError("cli", f"cmux did not name the new {kind}: {out.strip()}")
    return m[0]


def wait_for(find, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        found = find()
        if found:
            return found
        time.sleep(0.1)
    raise CmuxError("not_found", "cmux didn't show what was just made")


def add_window(window):
    """A new workspace in this cmux window, with one shell in the home folder. Returns its pane's id. The Mac keeps its focus."""
    ref = new_ref(cmux_cli("new-workspace", "--cwd", str(Path.home()), "--window", window["id"], "--focus", "false"), "workspace")
    return wait_for(lambda: next((s["id"] for w in windows() for s in surfaces(w) if s["workspace"].get("ref") == ref), None))


def add_pane(workspace):
    """Split the workspace and return the new pane's id: beside its one pane, else under its last pane."""
    ws = find_workspace(workspace)
    terminals = [s for pane in ws.get("panes", []) for s in pane.get("surfaces", []) if s.get("type") == "terminal"]
    if not terminals:
        raise CmuxError("not_found", f"can't find window {workspace}")
    side = "right" if len(ws.get("panes", [])) == 1 else "down"
    ref = new_ref(cmux_cli("new-split", side, "--workspace", ws["id"], "--surface", terminals[-1]["id"], "--focus", "false"), "surface")
    return wait_for(lambda: next((s["id"] for w in windows() for s in surfaces(w) if s["ref"] == ref), None))


def close_pane(s):
    """Close a pane and whatever runs in it. cmux won't close a workspace's last surface, so then the workspace closes."""
    release(s["id"])
    try:
        rpc("surface.close", {"surface_id": s["id"], "workspace_id": s["workspace"]["id"], "force": True})
    except CmuxError as e:
        if "last surface" not in str(e):
            raise
        rpc("workspace.close", {"workspace_id": s["workspace"]["id"], "force": True})


def new_session():
    """A new cmux window on the Mac. Returns its id."""
    before = {w["id"] for w in windows()}
    rpc("window.create")
    return wait_for(lambda: next((w["id"] for w in windows() if w["id"] not in before), None))


def agent_ttys():
    """tty -> the coding agent in its foreground: "claude", "codex" or "cursor"."""
    out = subprocess.run(["ps", "-A", "-o", "tty=,stat=,comm="], capture_output=True, text=True, env={"LC_ALL": "C"}).stdout
    return {parts[0]: AGENT_PROCESS[os.path.basename(parts[2])] for parts in (line.split(None, 2) for line in out.splitlines())
            if len(parts) == 3 and "+" in parts[1] and os.path.basename(parts[2]) in AGENT_PROCESS}


def body_end(lines, agent):
    """Index of the top of the agent's input box; everything from there down is prompt and footer."""
    for i in range(len(lines) - 2, -1, -1):
        if INPUT_BOX[agent].match(lines[i] + "\n" + lines[i + 1]):
            return i
    return len(lines)


def last_line(lines, agent):
    lines = lines[:body_end(lines, agent)]
    # A line with no letter or digit is blank or a box border.
    content = [l for l in lines if re.search(r"\w", l) and not RULE.match(l) and not CLAUDE_NOTICE.search(l)]
    spinner = next((l.strip() for l in content[-4:] if SPINNER.match(l)), None)
    if spinner:
        return spinner
    # Otherwise the start of the latest reply, rejoined across its wrapped lines. Claude Code and Codex start
    # each reply with a bullet; Cursor does not, so for Cursor this is mostly the last line above the box.
    for i in range(len(lines) - 1, -1, -1):
        if BULLET.match(lines[i]) and not CLAUDE_NOTICE.search(lines[i]):
            parts = [lines[i][2:].strip()]
            for l in lines[i + 1:]:
                if not l.startswith("  ") or not l.strip() or "⎿" in l:
                    break
                parts.append(l.strip())
            return " ".join(parts)
    return content[-1].strip() if content else ""


def status(lines, agent):
    end = body_end(lines, agent)
    if end == len(lines) or agent == "codex" and any(CODEX_ASK.search(l) for l in lines[end:]):
        return "needs_you"  # no input box on screen means a question or menu is open
    # The two lines at the top of the box too: Cursor shows "ctrl+c to stop" in its → line.
    if any(WORKING[agent].search(l) for l in lines[:end + 2][-30:]):
        return "working"
    return "idle"


def transcribe(audio):
    """Speech to text on this Mac with whisper-server, which keeps the model loaded; the audio never leaves the Mac."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio\"\r\n\r\n".encode()
            + audio + f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson"
            f"\r\n--{boundary}--\r\n".encode())
    conn = HTTPConnection(WHISPER_HOST, WHISPER_PORT, timeout=120)
    try:
        conn.request("POST", "/inference", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        r = conn.getresponse()
        data = r.read()
    finally:
        conn.close()
    if r.status != 200:
        raise WhisperError(f"whisper-server {r.status}: {data[:300].decode(errors='replace')}")
    return " ".join(json.loads(data)["text"].split())


def save_upload(data):
    """Store a phone image where Claude Code can read it; only real image bytes are accepted. Old ones are deleted."""
    ext = next((e for magic, e in IMAGE_TYPES.items() if data.startswith(magic)), None)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    if not ext:
        return None
    UPLOADS.mkdir(parents=True, exist_ok=True)
    for old in UPLOADS.glob("*"):
        if old.stat().st_mtime < time.time() - UPLOAD_DAYS * 86400:
            old.unlink(missing_ok=True)
    path = UPLOADS / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.{ext}"
    path.write_bytes(data)
    return path


def valid_name(name):
    return (isinstance(name, str) and name.strip() != "" and len(name) <= MAX_NAME
            and not any(unicodedata.category(c) == "Cc" for c in name))


def version(page):
    """Changes when this server or the page changes. The phone reloads itself when it sees a new one."""
    return hashlib.sha1(SERVER_SOURCE + page).hexdigest()[:12]


# The file screen: folders and text files on the Mac, read and written as UTF-8.
def tty_processes(tty):
    """[pid, ppid, stat, name] for each process on a tty."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,tty=,stat=,comm="], capture_output=True, text=True, env={"LC_ALL": "C"}).stdout
    rows = [line.split(None, 4) for line in out.splitlines()]
    return [[r[0], r[1], r[3], os.path.basename(r[4])] for r in rows if len(r) == 5 and r[2] == tty], rows


def pane_folder(pane):
    """The folder of the pane's foreground process, or the home folder if it can't be read."""
    procs, _ = tty_processes(find_pane(pane)["tty"])
    front = [p[0] for p in procs if "+" in p[2]] or [p[0] for p in procs]
    for pid in reversed(front):
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", pid, "-Fn"], capture_output=True, text=True).stdout
        folder = next((line[1:] for line in out.splitlines() if line.startswith("n")), None)
        if folder and os.path.isdir(folder):
            return folder
    return str(Path.home())


def full_path(path, pane):
    """An absolute path. ~ is the home folder; a relative path, or none, starts at the pane's folder."""
    path = os.path.expanduser(path or "")
    if not os.path.isabs(path):
        path = os.path.join(pane_folder(pane) if pane else str(Path.home()), path)
    return os.path.normpath(path)


def list_folder(path):
    """Folders first, then the rest, each sorted by name. Hidden files are listed too."""
    with os.scandir(path) as it:
        entries = [{"name": e.name, "folder": e.is_dir()} for e in it]
    entries.sort(key=lambda e: (not e["folder"], e["name"].lower(), e["name"]))
    return {"path": path, "parent": os.path.dirname(path), "entries": entries}


def read_file(path):
    """The file's text and its mtime, or a one-line note instead of text for a big, binary or non-UTF-8 file.
    The mtime is a string: nanoseconds don't fit in a JavaScript number."""
    st = os.stat(path)
    out = {"path": path, "mtime": str(st.st_mtime_ns)}
    if st.st_size > MAX_FILE:
        return {**out, "note": f"too big to show: {st.st_size / 1024 / 1024:.1f} MB"}
    data = Path(path).read_bytes()
    if b"\0" in data[:8192]:
        return {**out, "note": "binary file, not shown"}
    try:
        return {**out, "text": data.decode("utf-8")}
    except UnicodeDecodeError as e:
        return {**out, "note": f"not UTF-8, not shown: {e}"}


def write_file(path, text, mtime):
    """Write the file, unless it changed on disk since the phone read it. Returns the new mtime, or None if it changed."""
    if str(os.stat(path).st_mtime_ns) != mtime:
        return None
    Path(path).write_bytes(text.encode("utf-8"))
    reload_in_nvim(path)
    return str(os.stat(path).st_mtime_ns)


# nvim 0.9+ opens a server socket by itself at $TMPDIR/nvim.$USER/<random>/nvim.<pid>.0. In a terminal, nvim is two
# processes: the screen one on the tty, and its child that holds the file and the socket.
def nvim_sockets(pids=None):
    found = glob.glob(os.path.join(os.environ.get("TMPDIR", "/tmp"), "nvim.*", "*", "nvim.*.0"))
    return [s for s in found if pids is None or s.split(".")[-2] in pids]


def nvim(socket_path, expr):
    """Ask a running nvim for the value of a Vim expression."""
    r = subprocess.run(["nvim", "--server", socket_path, "--remote-expr", expr], capture_output=True, text=True, timeout=3)
    return r.stdout if r.returncode == 0 else None


def nvim_file(pane):
    """The file open in the nvim that is the pane's foreground process, or None."""
    procs, rows = tty_processes(find_pane(pane)["tty"])
    front = {p[0] for p in procs if "+" in p[2] and p[3] == "nvim"}
    if not front:
        return None
    pids = front | {r[0] for r in rows if len(r) == 5 and r[1] in front}
    for s in nvim_sockets(pids):
        try:
            path = nvim(s, 'expand("%:p")')
        except (OSError, subprocess.TimeoutExpired):
            continue
        if path and os.path.isfile(path):
            return path
    return None


def reload_in_nvim(path):
    """After a save from the phone, every nvim with that file open reloads it (:checktime). It runs on a timer
    inside nvim, so a question nvim asks there (the buffer has its own changes too) can't hold up the save."""
    name = path.replace("'", "''")
    expr = f"timer_start(0, {{-> execute(map(filter(getbufinfo(), {{_, b -> b.name ==# '{name}'}}), {{_, b -> 'checktime ' .. b.bufnr}}))}})"
    for s in nvim_sockets():
        try:
            nvim(s, expr)
        except (OSError, subprocess.TimeoutExpired):
            pass


class Handler(BaseHTTPRequestHandler):
    def reply(self, status_code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, needs_origin):
        if DEV:
            return True
        login = self.headers.get("Tailscale-User-Login")
        if login != ALLOWED_LOGIN:
            self.log_message("forbidden: Tailscale-User-Login=%r", login)
            return False
        # Stops another web page open on the phone from typing into the terminal.
        if needs_origin and self.headers.get("Origin") != PUBLIC_ORIGIN:
            self.log_message("forbidden: Origin=%r", self.headers.get("Origin"))
            return False
        return True

    def cmux_failed(self, e):
        # The page goes back to the list when it reads "can't find pane".
        message = str(e) if e.code != "not_found" or "can't find" in str(e) else f"can't find pane: {e}"
        self.reply(404 if e.code == "not_found" else 503 if e.code == "unavailable" else 502, {"error": message})

    def do_GET(self):
        if not self.allowed(needs_origin=False):
            return self.reply(403, {"error": "forbidden"})
        try:
            m = STREAM_ROUTE.match(self.path)
            if m:
                return self.stream(find_pane(m[1])["id"])
            if self.path == "/":
                page = INDEX.read_bytes()
                return self.reply(200, page.replace(b"{{VERSION}}", version(page).encode()), "text/html; charset=utf-8")
            if self.path in STATIC_FILES:
                return self.reply(200, (STATIC / self.path.lstrip("/")).read_bytes(), STATIC_FILES[self.path])
            if self.path == "/api/panes":
                tree = windows()
                target = target_window(tree)
                return self.reply(200, {"panes": list_panes(target) if target else [], "sessions": sessions_for_phone(tree, target),
                                        "version": version(INDEX.read_bytes())})
            url = urlsplit(self.path)
            if url.path in ("/api/files", "/api/file"):
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                pane = query.get("pane") if re.fullmatch(ID, query.get("pane", "")) else None
                if url.path == "/api/file":
                    return self.reply(200, read_file(full_path(query.get("path"), pane)))
                # With no path, the pane's folder, and the file open in the pane's nvim if there is one.
                folder = list_folder(full_path(query.get("path"), pane))
                return self.reply(200, folder if query.get("path") else {**folder, "nvim_file": pane and nvim_file(pane)})
            self.reply(404, {"error": "not found"})
        except CmuxError as e:
            self.cmux_failed(e)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            self.reply(400, {"error": str(e)})
        except Exception:
            traceback.print_exc()
            self.reply(500, {"error": "server error"})

    def stream(self, pane):
        """The open pane's screen as server-sent events: a frame whenever the screen, its size or the scroll changes.
        The open stream is what keeps the pane sized to the phone. It ends when the phone goes or the pane closes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        seen, quiet, wrong_size = None, 0, 0
        while True:
            want = renew(pane)
            try:
                g = grid(pane)
                # A pane still at another size a second after the phone asked is asked again, every two seconds.
                wrong_size = wrong_size + 1 if want and g and [g.get("columns"), g.get("rows")] != want else 0
                if wrong_size % 25 == 12:
                    refit(pane)
            except CmuxError as e:
                self.wfile.write(f"event: gone\ndata: {json.dumps(str(e))}\n\n".encode())
                return self.wfile.flush()
            key = (g.get("render_epoch"), g.get("render_revision"), g.get("columns"), g.get("rows"), scrolled.get(pane, 0))
            if key != seen:
                text, offset = frame(g, scrolled.get(pane, 0))
                if offset != scrolled.get(pane, 0):
                    scrolled[pane] = offset
                body = {"frame": text, "cols": g.get("columns"), "rows": g.get("rows"), "scrolled": offset, "mouse": reads_mouse(g)}
                self.wfile.write(f"data: {json.dumps(body)}\n\n".encode())
                self.wfile.flush()
                seen, quiet = key, 0
            else:
                quiet += 1
                if quiet % 50 == 0:  # a comment line every few seconds, so a phone that left is noticed
                    self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
            time.sleep(STREAM_SECONDS)

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "").split(";")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if self.path == "/api/transcribe":
            if not self.allowed(needs_origin=True) or not content_type.startswith("audio/"):
                return self.reply(403, {"error": "forbidden"})
            if length > MAX_AUDIO:
                return self.reply(413, {"error": "recording too long"})
            try:
                return self.reply(200, {"text": transcribe(self.rfile.read(length))})
            except (WhisperError, OSError) as e:
                return self.reply(502, {"error": f"transcription failed: {e}"})
        if not self.allowed(needs_origin=True) or content_type != "application/json":
            return self.reply(403, {"error": "forbidden"})
        limit = MAX_IMAGES * 4 // 3 + MAX_BODY if self.path.endswith("/paste") else MAX_BODY
        if self.path == "/api/file":
            limit = 2 * MAX_FILE + MAX_BODY  # JSON escapes can make the text longer
        if length > limit:
            return self.reply(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                return self.reply(400, {"error": "bad json"})
            if self.path == "/api/file":
                path, text, mtime = body.get("path"), body.get("text"), body.get("mtime")
                if not all(isinstance(v, str) for v in (path, text, mtime)) or not os.path.isabs(path):
                    return self.reply(400, {"error": "bad save"})
                new_mtime = write_file(path, text, mtime)
                if new_mtime is None:
                    return self.reply(409, {"error": "Not saved: the file changed on disk after you opened it. Go back and open it again."})
                return self.reply(200, {"mtime": new_mtime})
            if self.path == "/api/windows":
                target = target_window(windows())
                if not target:
                    return self.reply(404, {"error": "cmux has no window open. Tap [edit], then [+ session]."})
                return self.reply(200, {"pane": add_window(target)})
            if self.path == "/api/sessions":
                session = new_session()
                TARGET_FILE.write_text(session)
                return self.reply(200, {"session": session})
            m = SESSION_ROUTE.match(self.path)
            if m:
                return self.session_action(m[1], m[2])
            m = WINDOW_ROUTE.match(self.path)
            if m:
                workspace, action = m[1], m[2]
                if action == "panes":
                    return self.reply(200, {"pane": add_pane(workspace)})
                ws = find_workspace(workspace)
                if action == "close":
                    for pane in ws.get("panes", []):
                        for s in pane.get("surfaces", []):
                            release(s["id"])
                    rpc("workspace.close", {"workspace_id": ws["id"], "force": True})
                    return self.reply(200, {"ok": True})
                if not valid_name(body.get("name")):
                    return self.reply(400, {"error": "bad name"})
                rpc("workspace.rename", {"workspace_id": ws["id"], "title": body["name"]})
                return self.reply(200, {"ok": True})
            m = PANE_ROUTE.match(self.path)
            if not m:
                return self.reply(404, {"error": "not found"})
            s, action = find_pane(m[1]), m[2]
            pane = s["id"]
            if action in ("open", "fit"):
                # The pane takes the phone's size; the terminal stream then keeps it there.
                cols, rows = body.get("cols"), body.get("rows")
                if not all(isinstance(v, int) and not isinstance(v, bool) for v in (cols, rows)):
                    return self.reply(400, {"error": "cols and rows must be whole numbers"})
                if action == "open":
                    to_bottom(pane)
                fit(pane, cols, rows)
                return self.reply(200, {"ok": True})
            elif action == "back":
                release(pane)
            elif action == "input":
                # What the phone's keyboard typed, as the bytes a terminal would send.
                data = body.get("data")
                if not isinstance(data, str) or not data:
                    return self.reply(400, {"error": "bad input"})
                to_bottom(pane)
                rpc("terminal.input", {"terminal_id": pane, "text": data})
            elif action == "scroll":
                count, col, row = body.get("count"), body.get("col"), body.get("row")
                if not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1000 for v in (count, col, row)):
                    return self.reply(400, {"error": "bad scroll"})
                scroll(pane, bool(body.get("up")), min(count, 50), col, row)
            elif action == "paste":
                # Dictated text or photos go into the pane's input; the phone's Enter key submits it.
                text, images = body.get("text", ""), body.get("images") or []
                if not isinstance(text, str) or not isinstance(images, list) or not all(isinstance(i, str) for i in images):
                    return self.reply(400, {"error": "bad paste"})
                if not text.strip() and not images:
                    return self.reply(400, {"error": "empty text"})
                paths = []
                for encoded in images:
                    try:
                        path = save_upload(base64.b64decode(encoded, validate=True))
                    except ValueError:
                        path = None
                    if not path:
                        return self.reply(400, {"error": "not an image"})
                    paths.append(str(path))
                # Claude Code turns a pasted image path into an attachment, but only at the end of the text.
                text = " ".join([text.strip(), *paths]).strip()
                to_bottom(pane)
                # cmux's paste path brackets the text, so multi-line text arrives as one paste and nothing is submitted.
                rpc("terminal.paste", {"surface_id": pane, "text": text, "submit_key": "none"})
            elif action == "key":
                key = KEYS.get(body.get("key"))
                if not key:
                    return self.reply(400, {"error": "key not allowed"})
                to_bottom(pane)
                if key.isdigit():
                    rpc("surface.send_text", {"surface_id": pane, "text": key})
                else:
                    rpc("surface.send_key", {"surface_id": pane, "key": key})
            elif action == "close":
                close_pane(s)
            elif action == "rename":
                if not valid_name(body.get("name")):
                    return self.reply(400, {"error": "bad name"})
                rpc("tab.action", {"surface_id": pane, "action": "rename", "title": body["name"]})
            self.reply(200, {"ok": True})
        except CmuxError as e:
            self.cmux_failed(e)
        except ValueError:
            self.reply(400, {"error": "bad json"})
        except OSError as e:
            self.reply(400, {"error": str(e)})
        except Exception:
            traceback.print_exc()
            self.reply(500, {"error": "server error"})

    def session_action(self, session, action):
        """Show another cmux window's workspaces on the phone, or close a cmux window and all in it."""
        window = next((w for w in windows() if w["id"].lower() == session.lower()), None)
        if not window:
            return self.reply(404, {"error": f"can't find session {session}"})
        if action == "switch":
            TARGET_FILE.write_text(window["id"])
        else:
            for s in surfaces(window):
                release(s["id"])
            rpc("window.close", {"window_id": window["id"]})
        self.reply(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def keep_awake():
    """Plugged in, the Mac does not sleep while this server runs (-s works on AC only; the display may still sleep).
    caffeinate exits with the server. It is not needed to serve, so its exit does not stop the server."""
    subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])


def start_whisper():
    """whisper-server keeps the speech model in memory so dictation takes a second, not a reload; --convert runs ffmpeg on the phone's AAC."""
    proc = subprocess.Popen(["whisper-server", "-m", str(WHISPER_MODEL), "--host", WHISPER_HOST, "--port", str(WHISPER_PORT),
                             "-l", "auto", "--convert"], cwd=STATE)  # --convert writes its temp file to the cwd
    threading.Thread(target=lambda: (proc.wait(), os._exit(1)), daemon=True).start()


if __name__ == "__main__":
    if not DEV and not (PUBLIC_ORIGIN and ALLOWED_LOGIN):
        sys.exit("CMUX_PHONE_ORIGIN and CMUX_PHONE_LOGIN must be set; run ./install.sh")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Panes a previous run left sized to the phone go back to the Mac before anything else.
    try:
        for pane, policy in json.loads(FITTED_FILE.read_text()).items():
            restore(pane, policy)
    except (OSError, ValueError):
        pass
    FITTED_FILE.write_text("{}")
    threading.Thread(target=housekeeping, daemon=True).start()
    if not DEV:
        start_whisper()
        keep_awake()
    print(f"cmux-phone on http://{HOST}:{PORT} for {ALLOWED_LOGIN or 'anyone on this Mac (--dev)'} at {PUBLIC_ORIGIN or 'localhost'}", flush=True)
    Server((HOST, PORT), Handler).serve_forever()
