"""Which service answers which question, and the settings file that remembers the choice.

Three requests leave this machine during a run: the classifier's (TypeSafe's jev by default), the
writer's (free text for a field or a URL), and the answer's (what to tell the user when the
classifier stops). Each is pointed at a provider here. Every provider that speaks OpenAI's Chat
Completions API (OpenAI, Vercel's AI Gateway, OpenRouter, xAI, Groq, Mistral, a local Ollama) is a
row in the catalog below, reached through `openai_writer.OpenAIWriter`, never code of its own.

Precedence, highest first: the environment, then the settings file, then the catalog default. A
key exported in the shell or written in `.env` is never replaced by one typed into the app, and
CLICKER_WRITER_BASE_URL or CLICKER_WRITER_API, when set, point the writer and the answer at that
endpoint exactly as README describes, whatever the file says.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from .config import DEFAULT_ANSWER_MODEL, DEFAULT_BROWSER, DEFAULT_WRITER_MODEL

SETTINGS_PATH = Path.home() / ".config" / "typesafe-computer-use" / "settings.json"

# How a provider is spoken to.
OPENAI = "openai"  # Chat Completions, whoever serves it: openai_writer.OpenAIWriter
ANTHROPIC = "anthropic"  # the Messages API: the Anthropic SDK itself
TYPESAFE = "typesafe"  # jev, through the TypeSafe SDK; classifier only

# Setting either of these hands the writer to the environment, as it always was: see writer.make_writer.
WRITER_ENDPOINT_ENV = ("CLICKER_WRITER_BASE_URL", "CLICKER_WRITER_API")


class SettingsError(ValueError):
    """A service the settings name cannot be used as they stand: a missing key, a missing model."""


@dataclass(frozen=True)
class ProviderSpec:
    """One service the app can be pointed at."""

    key: str
    label: str
    kind: str
    key_env: str  # the environment variable that holds its API key
    default_model: str
    base_url: str | None = None  # None: the SDK's own default, which only Anthropic and TypeSafe have
    base_url_env: str = ""  # a variable that overrides it, for an endpoint with no fixed home
    vision: bool = True  # whether its models read the screenshot sent with the answer
    needs_key: bool = True
    answer_model: str = ""  # a stronger reader for the answer, where the provider has one; else default_model


# The writer and the answer. Anthropic comes first because it is what the environment alone has
# always configured; a fresh install with nothing but ANTHROPIC_API_KEY behaves as it always did.
TEXT_PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic",
        kind=ANTHROPIC,
        key_env="ANTHROPIC_API_KEY",
        default_model=DEFAULT_WRITER_MODEL,
        answer_model=DEFAULT_ANSWER_MODEL,
    ),
    "vercel": ProviderSpec(
        key="vercel",
        label="Vercel AI Gateway",
        kind=OPENAI,
        key_env="AI_GATEWAY_API_KEY",
        default_model="anthropic/claude-sonnet-4.5",
        base_url="https://ai-gateway.vercel.sh/v1",
    ),
    "openai": ProviderSpec(
        key="openai",
        label="OpenAI",
        kind=OPENAI,
        key_env="OPENAI_API_KEY",
        default_model="gpt-4.1",
        base_url="https://api.openai.com/v1",
    ),
    "mistral": ProviderSpec(
        key="mistral",
        label="Mistral",
        kind=OPENAI,
        key_env="MISTRAL_API_KEY",
        default_model="codestral-latest",
        base_url="https://api.mistral.ai/v1",
        vision=False,
    ),
    "openrouter": ProviderSpec(
        key="openrouter",
        label="OpenRouter",
        kind=OPENAI,
        key_env="OPENROUTER_API_KEY",
        default_model="openai/gpt-4.1",
        base_url="https://openrouter.ai/api/v1",
    ),
    "xai": ProviderSpec(
        key="xai", label="xAI (Grok)", kind=OPENAI, key_env="XAI_API_KEY", default_model="grok-4", base_url="https://api.x.ai/v1"
    ),
    "groq": ProviderSpec(
        key="groq",
        label="Groq",
        kind=OPENAI,
        key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        vision=False,
    ),
    "ollama": ProviderSpec(
        key="ollama",
        label="Ollama (local)",
        kind=OPENAI,
        key_env="OLLAMA_API_KEY",
        default_model="llama3.2",
        base_url="http://localhost:11434/v1",
        vision=False,
        needs_key=False,
    ),
    "custom": ProviderSpec(
        key="custom",
        label="Custom OpenAI-compatible",
        kind=OPENAI,
        key_env="CLICKER_WRITER_API_KEY",
        default_model="",
        needs_key=False,
    ),
}

# The classifier. jev is the model the loop is built around, so TypeSafe is the default; any chat
# model behind an OpenAI-compatible endpoint can be asked the same questions instead (see
# chat_classifier.py), slower and dearer, with its own opinion of its confidence for a probability.
DECISION_PROVIDERS: dict[str, ProviderSpec] = {
    "typesafe": ProviderSpec(
        key="typesafe", label="TypeSafe (jev)", kind=TYPESAFE, key_env="TYPESAFE_API_KEY", default_model="", vision=False
    ),
    "vercel": replace(TEXT_PROVIDERS["vercel"], vision=False),
    "openai": replace(TEXT_PROVIDERS["openai"], default_model="gpt-4.1-mini", vision=False),
    "openrouter": replace(TEXT_PROVIDERS["openrouter"], default_model="openai/gpt-4.1-mini", vision=False),
    "ollama": TEXT_PROVIDERS["ollama"],
    "custom": ProviderSpec(
        key="custom",
        label="Custom OpenAI-compatible",
        kind=OPENAI,
        key_env="CLICKER_CLASSIFIER_API_KEY",
        default_model="",
        base_url_env="CLICKER_CLASSIFIER_BASE_URL",
        vision=False,
        needs_key=False,
    ),
}


@dataclass
class Endpoint:
    """One configured service: which provider, which model, and any overrides for it.

    `model`, `base_url` and `api_key` empty mean "whatever the catalog says", so switching provider
    never carries the last provider's model name across.
    """

    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str = ""

    def spec(self, catalog: Mapping[str, ProviderSpec]) -> ProviderSpec:
        return catalog.get(self.provider) or next(iter(catalog.values()))

    def resolved_model(self, catalog: Mapping[str, ProviderSpec], model_env: str | None = None, answer: bool = False) -> str:
        spec = self.spec(catalog)
        default = (spec.answer_model or spec.default_model) if answer else spec.default_model
        return _env(model_env) or self.model.strip() or default

    def resolved_base_url(self, catalog: Mapping[str, ProviderSpec]) -> str | None:
        """Where requests go: the environment first, then this endpoint, then the catalog."""
        spec = self.spec(catalog)
        return _env(spec.base_url_env) or self.base_url.strip() or spec.base_url

    def resolved_key(self, catalog: Mapping[str, ProviderSpec], keys: Mapping[str, str] | None = None) -> str | None:
        """The environment first, then the keys saved in the app, then this endpoint's own."""
        spec = self.spec(catalog)
        stored = (keys or {}).get(spec.key_env, "")
        return _env(spec.key_env) or stored.strip() or self.api_key.strip() or None


@dataclass
class Settings:
    decisions: Endpoint = field(default_factory=lambda: Endpoint(provider="typesafe"))
    writer: Endpoint = field(default_factory=lambda: Endpoint(provider="anthropic"))
    answer: Endpoint = field(default_factory=lambda: Endpoint(provider="anthropic"))
    browser: str = ""
    email: str = ""
    # Off unless the user turns it on. The clipboard holds whatever was last copied, a password from
    # a manager included, and nothing about a copied string says it is one: the credential guard sees
    # fields, never the clipboard. On, the classifier reads it every step, which is how text is
    # carried from one app to another.
    share_clipboard: bool = False
    # API keys typed into the app, by environment variable name. One key serves every endpoint that
    # names it, so a key entered once is a key the writer, the answer and the classifier all have.
    keys: dict[str, str] = field(default_factory=dict)

    # --- what the rest of the app asks for -------------------------------------------------

    def key(self, spec: ProviderSpec) -> str | None:
        """The key this provider would use, from wherever it comes."""
        return Endpoint(provider=spec.key).resolved_key(_catalog_for(spec), self.keys)

    def key_source(self, spec: ProviderSpec) -> str:
        """Where that key comes from: "environment", "app", or "" when there is none."""
        if _env(spec.key_env):
            return "environment"
        return "app" if self.keys.get(spec.key_env, "").strip() else ""

    def decisions_spec(self) -> ProviderSpec:
        return self.decisions.spec(DECISION_PROVIDERS)

    def writer_spec(self) -> ProviderSpec:
        return self.writer.spec(TEXT_PROVIDERS)

    def answer_spec(self) -> ProviderSpec:
        return self.answer.spec(TEXT_PROVIDERS)

    def writer_model(self) -> str:
        return self.writer.resolved_model(TEXT_PROVIDERS, "CLICKER_WRITER_MODEL")

    def answer_model(self) -> str:
        return self.answer.resolved_model(TEXT_PROVIDERS, "CLICKER_ANSWER_MODEL", answer=True)

    def writer_from_environment(self) -> bool:
        """Whether the environment names the writer's endpoint itself, which then wins over the file."""
        return any(_env(name) for name in WRITER_ENDPOINT_ENV)

    def resolved_browser(self) -> str:
        return _env("CLICKER_BROWSER") or self.browser.strip() or DEFAULT_BROWSER

    def resolved_email(self) -> str | None:
        return _env("CLICKER_EMAIL") or self.email.strip() or None

    # --- persistence -----------------------------------------------------------------------

    def save(self, path: Path | None = None) -> None:
        """Write the file, readable only by this user: it can hold API keys typed into the app.

        The path is looked up when this is called, never baked in as a default argument, which is
        evaluated once at import: a test that redirected SETTINGS_PATH would otherwise still write
        over the real file.
        """
        path = path or SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)


def key_slots() -> list[tuple[str, str]]:
    """Every API key the app can hold: the variable name, and the providers that read it."""
    providers: dict[str, list[str]] = {}
    for spec in [*TEXT_PROVIDERS.values(), *DECISION_PROVIDERS.values()]:
        labels = providers.setdefault(spec.key_env, [])
        if spec.label not in labels:
            labels.append(spec.label)
    return [(key_env, ", ".join(labels)) for key_env, labels in providers.items()]


def _catalog_for(spec: ProviderSpec) -> Mapping[str, ProviderSpec]:
    return TEXT_PROVIDERS if TEXT_PROVIDERS.get(spec.key) is spec else DECISION_PROVIDERS


def _env(name: str | None) -> str:
    return (os.environ.get(name) or "").strip() if name else ""


def load(path: Path | None = None) -> Settings:
    """The saved settings, or the defaults. A file that has gone bad is ignored, never fatal."""
    try:
        raw = json.loads((path or SETTINGS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_settings()
    return from_dict(raw) if isinstance(raw, dict) else default_settings()


def from_dict(raw: dict) -> Settings:
    """Settings from saved JSON, keeping the defaults for anything missing or malformed."""
    settings = default_settings()
    if isinstance(raw.get("keys"), dict):
        settings.keys = {str(name): value for name, value in raw["keys"].items() if isinstance(value, str)}
    for name, kind in _SECTIONS.items():
        section = raw.get(name)
        if isinstance(section, dict):
            known = {k: v for k, v in section.items() if k in {f.name for f in fields(kind)}}
            setattr(settings, name, replace(getattr(settings, name), **known))
    for name in ("browser", "email"):
        if isinstance(raw.get(name), str):
            setattr(settings, name, raw[name])
    if isinstance(raw.get("share_clipboard"), bool):
        settings.share_clipboard = raw["share_clipboard"]
    return settings


_SECTIONS: dict[str, type] = {"decisions": Endpoint, "writer": Endpoint, "answer": Endpoint}


def default_settings() -> Settings:
    """The defaults, with the writer and the answer on the first text provider with a key in the environment."""
    settings = Settings()
    for key, spec in TEXT_PROVIDERS.items():
        if spec.needs_key and _env(spec.key_env):
            settings.writer = Endpoint(provider=key)
            settings.answer = Endpoint(provider=key)
            break
    return settings
