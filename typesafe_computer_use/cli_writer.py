"""The writer's requests, sent to a coding agent's command line instead of an API: Claude Code,
Codex, or OpenCode, each signed in already. No key goes in `.env`; the agent's own login pays."""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

CLI_APIS = ("claude-code", "codex", "opencode")
TIMEOUT_SECONDS = 180.0  # an agent CLI starts cold on every call: seconds, not milliseconds
# (writing, answering) per agent, unless CLICKER_WRITER_MODEL or CLICKER_ANSWER_MODEL names one.
# Writing (text to type, a URL) is short work for a small, fast model. Answering reads a screenshot,
# so it needs a model that sees images. None leaves the agent's own configured model.
DEFAULT_MODELS = {
    "claude-code": ("haiku", "sonnet"),
    "codex": ("gpt-6-luna", None),
    "opencode": ("opencode/nemotron-3.5-lightning-free", "opencode/mimo-v2.6-flash-free"),
}
# Codex reasons at whatever effort its config sets; writing needs little.
CODEX_WRITING_EFFORT = "low"


class CliWriterError(Exception):
    """The agent CLI could not be started, failed, or answered with nothing."""


class CliWriter:
    """An agent CLI behind the one call the writer makes, `messages.create`.

    writer.py builds each request as an Anthropic Messages call and reads text blocks back, so this
    turns one into a single headless run of the agent, with no tools and no saved session, and
    hands its final message back as one text block. The JSON schema is passed where the CLI takes
    one (Claude Code, Codex) and is in the prompt either way.
    """

    def __init__(self, api: str, writing_model: str | None = None, answering_model: str | None = None):
        if api not in CLI_APIS:
            raise ValueError(f"unknown agent CLI {api!r}")
        self.api = api
        default_writing, default_answering = DEFAULT_MODELS[api]
        self.writing_model = writing_model or os.environ.get("CLICKER_WRITER_MODEL") or default_writing
        self.answering_model = answering_model or os.environ.get("CLICKER_ANSWER_MODEL") or default_answering
        self.base_url = None  # no endpoint: provider() names the CLI instead
        self.messages = SimpleNamespace(create=self._create)

    def describe(self) -> str:
        models = f"{self.writing_model or 'its default'} (writing), {self.answering_model or 'its default'} (answering)"
        return f"{self.api} CLI  models: {models}"

    def _create(
        self, *, model: str, max_tokens: int, system: str, messages: list[dict], output_config=None, thinking=None, reasoning=None
    ):
        from .config import answer_model  # the writer asks for answer_model() when it reads the screen

        chosen = self.answering_model if model == answer_model() else self.writing_model
        schema = output_config["format"]["schema"] if output_config else None
        text, images = _flatten(messages)
        with tempfile.TemporaryDirectory(prefix="clicker-writer-") as tmp:
            paths = [_save_image(Path(tmp), i, data) for i, data in enumerate(images)]
            run = getattr(self, f"_run_{self.api.replace('-', '_')}")
            reply = run(chosen, system, text, images, paths, schema, Path(tmp))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)], usage=None)

    def _run_claude_code(self, model, system, text, images, paths, schema, tmp: Path) -> str:
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}} for b in images]
        content.append({"type": "text", "text": text})
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
        command = [
            "claude", "-p", "--tools", "", "--no-session-persistence", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--system-prompt", system,
            *(["--model", model] if model else []), *(["--json-schema", json.dumps(schema)] if schema else []),
        ]  # fmt: skip
        out = _run(command, line + "\n", tmp)
        result = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        if result.get("is_error") or result.get("subtype") != "success":
            raise CliWriterError(f"claude failed: {str(result.get('result') or out)[-300:]}")
        structured = result.get("structured_output")
        return json.dumps(structured) if structured is not None else str(result.get("result") or "")

    def _run_codex(self, model, system, text, images, paths, schema, tmp: Path) -> str:
        last = tmp / "last-message.txt"
        command = ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", str(tmp), "-o", str(last)]
        if model:
            command += ["-m", model]
        if model and model == self.writing_model and model != self.answering_model:
            command += ["-c", f'model_reasoning_effort="{CODEX_WRITING_EFFORT}"']
        for path in paths:
            command += ["-i", str(path)]
        if schema:
            schema_file = tmp / "schema.json"
            schema_file.write_text(json.dumps(schema), encoding="utf-8")
            command += ["--output-schema", str(schema_file)]
        _run([*command, "-"], f"{system}\n\n{text}", tmp)
        if not last.is_file():
            raise CliWriterError("codex wrote no final message")
        return last.read_text(encoding="utf-8")

    def _run_opencode(self, model, system, text, images, paths, schema, tmp: Path) -> str:
        args = ["opencode", "run", "--format", "json", *(["-m", model] if model else [])]
        for path in paths:
            args += ["-f", _wsl_path(path) if sys.platform == "win32" else str(path)]
        if sys.platform == "win32" and not shutil.which("opencode"):
            # OpenCode often lives in WSL only: its login shell has it on PATH.
            args = ["wsl.exe", "-e", "bash", "-lc", shlex.join(args)]
        out = _run(args, f"{system}\n\n{text}", tmp)
        parts = []
        for raw in out.splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") == "text":
                parts.append(event.get("part", {}).get("text", ""))
        if not parts:
            raise CliWriterError(f"opencode returned no text: {out[-300:]}")
        return parts[-1]


def _flatten(messages: list[dict]) -> tuple[str, list[str]]:
    """The text of the request, and its images as base64 PNG, in order."""
    texts: list[str] = []
    images: list[str] = []
    for message in messages:
        content = message["content"]
        for block in [{"type": "text", "text": content}] if isinstance(content, str) else content:
            if block.get("type") == "text":
                texts.append(block["text"])
            elif block.get("type") == "image":
                images.append(block["source"]["data"])
    return "\n\n".join(texts), images


def _save_image(folder: Path, index: int, data: str) -> Path:
    path = folder / f"screen-{index}.png"
    path.write_bytes(base64.b64decode(data))
    return path


def _wsl_path(path: Path) -> str:
    """C:\\Users\\x\\a.png as WSL sees it, /mnt/c/Users/x/a.png."""
    p = PureWindowsPath(path)
    return "/mnt/" + p.drive.rstrip(":").lower() + "/" + "/".join(p.parts[1:])


def _run(command: list[str], stdin: str, cwd: Path) -> str:
    # On Windows the CLIs are often .cmd/.ps1 shims, which only a shell resolves.
    exe = shutil.which(command[0]) or command[0]
    try:
        done = subprocess.run(
            [exe, *command[1:]], input=stdin, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=cwd, timeout=TIMEOUT_SECONDS,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CliWriterError(f"{command[0]} did not run: {e}") from e
    if done.returncode != 0:
        raise CliWriterError(f"{command[0]} exited {done.returncode}: {(done.stderr or done.stdout)[-300:]}")
    return done.stdout
