"""Shared fixtures, a guard that keeps every test off the real machine, and import-only
stand-ins for the platform-only modules.

The suite is pure logic and should run on any OS. The platform adapters
(`typesafe_computer_use.macos`, `typesafe_computer_use.windows`) import their
platform's packages at module scope, but the tests only ever import them -- they
call nothing but the pure rules -- so a stand-in that exists and raises on any
real use is enough to run the whole suite anywhere. Where a real module is
installed, nothing is registered for it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


def _absent(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def _stub(name: str) -> types.ModuleType:
    """A module that can be imported and nothing else: every unset attribute raises."""
    module = types.ModuleType(name)

    def _getattr(attr: str) -> object:
        raise RuntimeError(f"{name}.{attr} is unavailable on this OS; tests must not call the platform adapter")

    module.__getattr__ = _getattr
    sys.modules[name] = module
    return module


REAL_ACCESSIBILITY = not _absent("ApplicationServices")

if _absent("Quartz"):
    _stub("Quartz").kCGHIDEventTap = 0
if not REAL_ACCESSIBILITY:
    _stub("ApplicationServices")
if _absent("ocrmac"):

    class _OCR:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("ocrmac is unavailable off macOS")

    _package = _stub("ocrmac")
    _package.__path__ = []
    _package.ocrmac = _stub("ocrmac.ocrmac")
    _package.ocrmac.OCR = _OCR
for _windows_module in ("psutil", "uiautomation", "win32api", "win32con", "win32gui", "win32process", "winocr"):
    if _absent(_windows_module):
        _stub(_windows_module)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from typesafe_computer_use import macos  # noqa: E402
from typesafe_computer_use.models import Item, Screen  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_machine(monkeypatch):
    """No test reaches the computer it runs on, whoever wrote it.

    The suite runs on the developer's own Mac, often while they use it. Every call that would
    move the pointer, press a key, run AppleScript (which opens apps and URLs), capture the
    screen, open a file, or act on another app's accessibility element refuses here, so a test
    that forgot to patch one fails instead of taking over the machine. A test that needs one
    patches it itself, after this. The pointer reads as mid-screen, never the abort corner.
    """

    def refuse(what: str):
        def call(*args, **kwargs):
            raise RuntimeError(f"a test reached the real machine through {what}; patch it in the test")

        return call

    for name in ("_post", "osascript", "screenshot", "open_path"):
        monkeypatch.setattr(macos, name, refuse(f"macos.{name}"))
    monkeypatch.setattr(macos, "mouse_location", lambda: (500.0, 500.0))
    if REAL_ACCESSIBILITY:
        for name in ("AXUIElementPerformAction", "AXUIElementSetAttributeValue"):
            monkeypatch.setattr(macos.AS, name, refuse(f"ApplicationServices.{name}"))


@pytest.fixture
def screen() -> Screen:
    return Screen(image=Image.new("RGB", (2000, 1200)), scale=2.0, app="Google Chrome", field=None, url=None)


def item(index: int, text: str, x1=100, y1=100, x2=400, y2=130, conf=1.0) -> Item:
    return Item(index, text, conf, x1, y1, x2, y2)


@pytest.fixture
def make_item():
    return item


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLICKER_TEST_KEY", raising=False)
    return tmp_path


WRITER_ENV = (
    "CLICKER_WRITER_API",
    "CLICKER_WRITER_BASE_URL",
    "CLICKER_WRITER_API_KEY",
    "CLICKER_WRITER_VISION",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
)


@pytest.fixture
def clean_env(monkeypatch):
    """No writer configuration from the shell running the tests."""
    for name in WRITER_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def endpoint():
    """A writer endpoint on localhost, as a proxy or a local model would serve one.

    It answers the Anthropic Messages API or OpenAI's Chat Completions API by path, records each
    request with its headers, and replies with `state["reply"]`. `state["reject"]` may return an
    error message for a request body, which then gets a 400, the way an endpoint refuses a parameter.
    """
    seen: list[dict] = []
    state: dict = {"reply": '{"ok": true, "url": "https://example.com", "reason": ""}', "reject": lambda body: None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            refusal = state["reject"](body)
            if refusal:
                status, out = 400, {"type": "error", "error": {"type": "invalid_request_error", "message": refusal}}
            elif self.path.endswith("/chat/completions"):
                status, out = (
                    200,
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "created": 0,
                        "model": body["model"],
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": state["reply"]}, "finish_reason": "stop"}
                        ],
                    },
                )
            else:
                status, out = (
                    200,
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": body["model"],
                        "content": [{"type": "text", "text": state["reply"]}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", seen=seen, state=state)
    server.shutdown()
    server.server_close()
