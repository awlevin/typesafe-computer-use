"""Minimal Chrome DevTools Protocol client.

This is the *only* module in the browser backend that talks to the browser.
Everything above it deals in plain dicts, so the backend can be swapped the way
`macos.py` is swapped for a Linux port in the original.

Why CDP instead of pixels: the DOM already knows the text, the role, the label
and the click point. Reading it is an exact answer in single-digit milliseconds.
OCR is a lossy guess that costs hundreds of milliseconds and needs Screen
Recording permission.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import websocket

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

# A fresh profile per instance. A shared directory leaves a SingletonLock
# behind after the first Chrome is killed, and the next launch then refuses to
# start — which is exactly how a benchmark run silently produces no numbers.
DEFAULT_PROFILE = ""


class CDPError(RuntimeError):
    pass


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return found
    raise CDPError("no Chrome/Chromium binary found")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def local_debugger_url(url: str, port: int) -> str:
    """A page's debugger URL, only when it points back at this Chrome's own loopback port.

    The port was free when it was picked, but another process could take it before Chrome
    does and answer /json/list itself. The session then connects nowhere but here.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != port:
        raise CDPError(f"debugger URL is not this Chrome's loopback port {port}: {url!r}")
    return url


def _get_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


class Chrome:
    """A dedicated Chrome instance. Never touches the user's own profile."""

    def __init__(self, *, port: int | None = None, headed: bool = False, profile: str | None = None):
        self.port = port or free_port()
        self.headed = headed
        self.profile = profile or tempfile.mkdtemp(prefix="tscu-chrome-")
        self._ephemeral = profile is None
        self.proc: subprocess.Popen | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, *, window: tuple[int, int] = (1440, 900), timeout: float = 25.0) -> Chrome:
        args = [
            find_chrome(),
            # The debugging socket listens on the loopback address (Chrome's default;
            # --remote-debugging-address is never passed), and accepts a websocket from
            # one origin, which `attach` sends. Chrome 111+ refuses every other origin,
            # so a web page cannot attach to this browser.
            f"--remote-debugging-port={self.port}",
            f"--remote-allow-origins={self.origin}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-extensions",
            "--metrics-recording-only",
            "about:blank",
        ]
        if not self.headed:
            args.insert(1, "--headless=new")
            args.insert(2, f"--window-size={window[0]},{window[1]}")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _get_json(f"http://127.0.0.1:{self.port}/json/version", timeout=1.0)
                return self
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                time.sleep(0.1)
        self.close()  # `with` never reaches __exit__ when __enter__ raises
        raise CDPError(f"Chrome did not expose CDP on port {self.port} within {timeout}s")

    def page_target(self, timeout: float = 10.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for target in _get_json(f"http://127.0.0.1:{self.port}/json/list"):
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return local_debugger_url(str(target["webSocketDebuggerUrl"]), self.port)
            time.sleep(0.1)
        raise CDPError("no page target available")

    def __enter__(self) -> Chrome:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def attach(self, **session_kwargs: Any) -> Session:
        session = Session(self.page_target(), origin=self.origin, **session_kwargs)
        session.call("Page.enable")
        session.call("Runtime.enable")
        return session

    def close(self) -> None:
        """Stop Chrome, and remove the profile when this instance made it."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        if self._ephemeral:
            shutil.rmtree(self.profile, ignore_errors=True)


class Session:
    """One flat CDP session over websocket. Sync, because the loop is sync."""

    def __init__(self, ws_url: str, *, origin: str | None = None, timeout: float = 30.0, max_size: int | None = 64 * 1024 * 1024):
        self.ws_url = ws_url
        self.timeout = timeout
        self._id = 0
        self._ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size, origin=origin)
        self.calls = 0

    # -- plumbing ----------------------------------------------------------
    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = self._ws.recv()
            if not raw:
                raise CDPError("websocket closed")
            data = json.loads(raw)
            if data.get("id") != msg_id:
                continue  # an event, not our reply
            self.calls += 1
            if "error" in data:
                raise CDPError(f"{method}: {data['error']}")
            return dict(data.get("result") or {})

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        if "exceptionDetails" in result:
            raise CDPError(f"JS error: {result['exceptionDetails'].get('text')}")
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
