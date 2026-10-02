"""The agent-CLI writer: the request it builds for each CLI and the text it reads back, with no CLI run."""

import json
from pathlib import Path, PureWindowsPath

import pytest

from typesafe_computer_use import cli_writer
from typesafe_computer_use.cli_writer import CliWriter, CliWriterError, _flatten, _wsl_path
from typesafe_computer_use.writer import make_writer, provider

SCHEMA = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}
PNG = "aGVsbG8="  # base64 of b"hello": the writer only passes the bytes along


def request(image: bool = False) -> dict:
    content = [{"type": "text", "text": '{"goal": "open eBay"}'}]
    if image:
        content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}})
    return {
        "model": "claude-haiku-4-5",
        "max_tokens": 200,
        "system": "Propose a URL.",
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        "thinking": {"type": "disabled"},
    }


@pytest.fixture
def ran(monkeypatch):
    """Each CLI call, recorded; the reply comes from `replies` in order."""
    log: list[dict] = []
    replies: list = []

    def fake(command, stdin, cwd):
        log.append({"command": command, "stdin": stdin, "files": {p.name: p.read_bytes() for p in Path(cwd).iterdir()}})
        reply = replies.pop(0)
        return reply(command, Path(cwd)) if callable(reply) else reply

    monkeypatch.setattr(cli_writer, "_run", fake)
    return log, replies


@pytest.mark.parametrize("api", ["claude-code", "codex", "opencode"])
def test_each_cli_is_a_writer_with_no_key(api, monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_API", api)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    writer = make_writer()
    assert isinstance(writer, CliWriter) and writer.api == api
    assert provider(writer).startswith(f"{api} CLI")


def test_claude_code_gets_the_image_inline_and_the_schema_as_a_flag(ran):
    log, replies = ran
    replies.append(json.dumps({"type": "result", "subtype": "success", "structured_output": {"url": "https://www.ebay.com/"}}))
    reply = CliWriter("claude-code").messages.create(**request(image=True))
    assert json.loads(reply.content[0].text) == {"url": "https://www.ebay.com/"}
    (call,) = log
    command = call["command"]
    assert command[:2] == ["claude", "-p"] and command[command.index("--tools") + 1] == ""
    assert command[command.index("--model") + 1] == "haiku"
    assert json.loads(command[command.index("--json-schema") + 1]) == SCHEMA
    sent = json.loads(call["stdin"])["message"]["content"]
    assert [b["type"] for b in sent] == ["image", "text"] and sent[0]["source"]["data"] == PNG


def test_claude_code_reports_a_failed_run(ran):
    _, replies = ran
    replies.append(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "no"}))
    with pytest.raises(CliWriterError):
        CliWriter("claude-code").messages.create(**request())


def test_codex_gets_files_for_the_image_and_schema_and_reads_its_last_message(ran):
    log, replies = ran

    def answer(command, cwd: Path) -> str:
        Path(command[command.index("-o") + 1]).write_text('{"url": "https://www.ebay.com/"}', encoding="utf-8")
        return ""

    replies.append(answer)
    reply = CliWriter("codex").messages.create(**request(image=True))
    assert json.loads(reply.content[0].text) == {"url": "https://www.ebay.com/"}
    (call,) = log
    command = call["command"]
    assert command[:2] == ["codex", "exec"] and "--ephemeral" in command and command[-1] == "-"
    assert command[command.index("-s") + 1] == "read-only" and command[command.index("-m") + 1] == "gpt-6-luna"
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="low"'
    assert call["files"]["screen-0.png"] == b"hello" and json.loads(call["files"]["schema.json"]) == SCHEMA
    assert call["stdin"].startswith("Propose a URL.") and '"goal": "open eBay"' in call["stdin"]


def test_opencode_reads_the_last_text_event(ran, monkeypatch):
    log, replies = ran
    monkeypatch.setattr(cli_writer.sys, "platform", "linux")
    events = [
        {"type": "step_start"},
        {"type": "text", "part": {"text": '{"url": "https://www.ebay.com/"}'}},
        {"type": "step_finish"},
    ]
    replies.append("\n".join(json.dumps(e) for e in events))
    reply = CliWriter("opencode").messages.create(**request())
    assert json.loads(reply.content[0].text) == {"url": "https://www.ebay.com/"}
    assert log[0]["command"][:4] == ["opencode", "run", "--format", "json"]


def test_opencode_with_no_text_is_an_error(ran, monkeypatch):
    _, replies = ran
    monkeypatch.setattr(cli_writer.sys, "platform", "linux")
    replies.append(json.dumps({"type": "step_finish"}))
    with pytest.raises(CliWriterError):
        CliWriter("opencode").messages.create(**request())


def test_the_answer_model_is_used_when_the_writer_reads_the_screen(ran, monkeypatch):
    log, replies = ran
    replies.append(json.dumps({"type": "result", "subtype": "success", "structured_output": {"url": "x"}}))
    CliWriter("claude-code").messages.create(**{**request(), "model": "claude-sonnet-5"})
    command = log[0]["command"]
    assert command[command.index("--model") + 1] == "sonnet"


def test_codex_answers_on_its_configured_model_and_effort(ran):
    log, replies = ran

    def answer(command, cwd: Path) -> str:
        Path(command[command.index("-o") + 1]).write_text("{}", encoding="utf-8")
        return ""

    replies.append(answer)
    CliWriter("codex").messages.create(**{**request(), "model": "claude-sonnet-5"})
    command = log[0]["command"]
    assert "-m" not in command and "-c" not in command


def test_opencode_writes_on_a_small_model_and_answers_on_one_that_sees(ran, monkeypatch):
    log, replies = ran
    monkeypatch.setattr(cli_writer.sys, "platform", "linux")
    text = json.dumps({"type": "text", "part": {"text": "{}"}})
    replies.extend([text, text])
    CliWriter("opencode").messages.create(**request())
    CliWriter("opencode").messages.create(**{**request(), "model": "claude-sonnet-5"})
    models = [call["command"][call["command"].index("-m") + 1] for call in log]
    assert models == ["opencode/nemotron-3.5-lightning-free", "opencode/mimo-v2.6-flash-free"]


def test_a_named_model_overrides_the_cli_default(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_MODEL", "gpt-5-mini")
    assert CliWriter("codex").writing_model == "gpt-5-mini"


def test_flatten_keeps_text_and_images_in_order():
    text, images = _flatten([{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}])
    assert (text, images) == ("a\n\nb", [])
    assert _flatten([{"role": "user", "content": "plain"}]) == ("plain", [])


def test_wsl_path_maps_a_windows_drive():
    assert _wsl_path(PureWindowsPath(r"C:\Users\x\AppData\Local\Temp\a.png")) == "/mnt/c/Users/x/AppData/Local/Temp/a.png"
