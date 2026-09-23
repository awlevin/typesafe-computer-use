"""Turning the settings into the services one run needs.

The command line and the window differ in how a goal is given and how a run is watched, not in
what a run is. Both come through here, so a provider that works in one works in the other.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from typesafe_sdk import TypeSafeClient

from .actions import Context
from .apps import resolve_app
from .chat_classifier import ChatClassifier
from .openai_writer import OpenAIWriter
from .platform_adapter import desktop
from .settings import ANTHROPIC, DECISION_PROVIDERS, TEXT_PROVIDERS, TYPESAFE, Endpoint, ProviderSpec, Settings, SettingsError
from .writer import Configured, Writer, client_for, make_writer, provider

CODESTRAL = "codestral"  # Mistral lists it with eyes; it has none


@dataclass(frozen=True)
class Services:
    """What a run talks to, and anything the user should be told about it before it starts.

    Only the classifier is required: without it there is no next action. A missing writer costs
    type_text, writer-proposed URLs and the answer, and the run says so and carries on.
    """

    classifier: Callable[[], object]  # opens the classifier for one run, as a context manager
    classifier_label: str
    writer: Writer | None
    answerer: Writer | None  # reads the screen the classifier stopped on; None answers with the writer
    notes: tuple[str, ...] = ()

    def describe(self) -> list[str]:
        return [
            f"classifier: {self.classifier_label}",
            f"writer: {provider(self.writer) if self.writer else 'disabled'}",
            *([f"answer: {provider(self.answerer)}"] if self.answerer else []),
            *self.notes,
        ]


def build(settings: Settings) -> Services:
    """Open every service the settings name. Raises SettingsError when the classifier cannot be used."""
    classifier, label = make_classifier(settings.decisions, settings.keys)
    notes: list[str] = []
    if settings.writer_from_environment():
        writer, answerer = make_writer(), None  # the endpoint the environment names answers too, as it always has
    else:
        writer = make_text_writer(settings.writer, settings.writer_model(), settings.keys)
        answerer = make_text_writer(settings.answer, settings.answer_model(), settings.keys)
    if writer is None:
        spec = settings.writer_spec()
        notes.append(
            f"writer disabled: no key for {spec.label} (set {spec.key_env}, or enter it in the app); "
            "type_text, writer-proposed URLs and the final answer need one"
        )
    elif answerer is None and isinstance(writer, Configured):
        notes.append(f"the answer has no key of its own, so {writer.label} answers too")
    reader = answerer or writer
    if isinstance(reader, Configured) and not reader.vision:
        notes.append(f"{reader.label} does not read images: the answer gets the screen's text only, and icons stay nameless")
    return Services(classifier=classifier, classifier_label=label, writer=writer, answerer=answerer, notes=tuple(notes))


def make_classifier(endpoint: Endpoint, keys: Mapping[str, str] | None = None) -> tuple[Callable[[], object], str]:
    """The classifier the settings name, as a factory the runner opens once per run, and its label."""
    spec = endpoint.spec(DECISION_PROVIDERS)
    key = endpoint.resolved_key(DECISION_PROVIDERS, keys)
    if spec.needs_key and not key:
        raise SettingsError(f"{spec.key_env} is not set (export it, put it in .env, or enter it in the app)")
    model = endpoint.resolved_model(DECISION_PROVIDERS)
    base_url = endpoint.resolved_base_url(DECISION_PROVIDERS)
    label = f"{spec.label} / {model}" if model else spec.label
    if spec.kind == TYPESAFE:
        return partial(TypeSafeClient, api_key=key, base_url=base_url, model=model or None), label
    if not model:
        raise SettingsError(f"{spec.label} needs a model name to classify with")
    if not base_url:
        raise SettingsError(f"{spec.label} needs a base URL")
    client = OpenAIWriter(base_url, key or "not-needed")
    return partial(ChatClassifier, client, model, label), label


def make_text_writer(endpoint: Endpoint, model: str, keys: Mapping[str, str] | None = None) -> Configured | None:
    """The writer one endpoint names, or None when it needs a key and has none."""
    spec = endpoint.spec(TEXT_PROVIDERS)
    key = endpoint.resolved_key(TEXT_PROVIDERS, keys)
    if spec.needs_key and not key and spec.kind != ANTHROPIC:  # Anthropic may still find a token of its own
        return None
    if not model:
        raise SettingsError(f"{spec.label} needs a model name")
    base_url = endpoint.resolved_base_url(TEXT_PROVIDERS)
    if spec.kind == ANTHROPIC and not base_url and key == os.environ.get(spec.key_env, "").strip():
        key = None  # the environment's own key: let the SDK read it with the base URL it was exported beside
    try:
        client = client_for(spec.kind, base_url, key)
    except ValueError as e:
        raise SettingsError(f"{spec.label}: {e}") from e
    if client is None:
        return None
    vision = spec.vision and not model.startswith(CODESTRAL)
    return Configured(client, model, vision, f"{spec.label} / {model}")


def context_factory(settings: Settings, goal: str, services: Services, ask: Callable[[str], str] | None = None):
    """The callable `runner.run` builds the action Context with, once the classifier is open.

    The apps are read once, here, rather than every step: installing one mid-run is not a thing.
    The browser name is resolved against them, so "Chrome" in a settings file is Google Chrome, and
    a name nothing matches is left for the platform to refuse.
    """
    apps = tuple(desktop.installed_apps())
    browser = resolve_app(settings.resolved_browser(), apps)

    def factory(typesafe, history: list[str]) -> Context:
        return Context(
            goal=goal,
            browser=browser,
            email=settings.resolved_email(),
            typesafe=typesafe,
            writer=services.writer,
            answerer=services.answerer,
            history=history,
            ask=ask,
            apps=apps,
        )

    return factory


def list_models(endpoint: Endpoint, catalog: Mapping[str, ProviderSpec], keys: Mapping[str, str] | None = None) -> list[str]:
    """Every model an endpoint says it serves, asked of the endpoint itself, for a menu.

    Raises SettingsError when it cannot be asked. jev is listed by the TypeSafe SDK, not a /models route.
    """
    spec = endpoint.spec(catalog)
    key = endpoint.resolved_key(catalog, keys)
    if spec.needs_key and not key:
        raise SettingsError(f"no key for {spec.label}: set {spec.key_env}")
    base_url = endpoint.resolved_base_url(catalog)
    try:
        if spec.kind == TYPESAFE:
            with TypeSafeClient(api_key=key, base_url=base_url) as client:
                return sorted(model.name for model in client.models.list().models)
        if spec.kind == ANTHROPIC:
            client = client_for(ANTHROPIC, base_url, key)
            if client is None:
                raise SettingsError(f"no key for {spec.label}: set {spec.key_env}")
            return sorted(model.id for model in client.models.list(limit=100))
        import openai

        return sorted(model.id for model in openai.OpenAI(base_url=base_url, api_key=key or "not-needed").models.list())
    except SettingsError:
        raise
    except Exception as e:
        raise SettingsError(f"{spec.label} would not list its models: {e}") from e


__all__ = ["Services", "SettingsError", "build", "context_factory", "list_models", "make_classifier", "make_text_writer"]
