"""Where a setting comes from, and what happens when the file is missing or broken."""

import json

import pytest

from typesafe_computer_use import session
from typesafe_computer_use import settings as S
from typesafe_computer_use.cli import apply_provider_flags
from typesafe_computer_use.settings import Endpoint, Settings, SettingsError
from typesafe_computer_use.writer import Configured


@pytest.fixture(autouse=True)
def no_keys(clean_env):
    """Start every test from a shell with no provider keys, whatever the developer has exported."""
    for spec in [*S.TEXT_PROVIDERS.values(), *S.DECISION_PROVIDERS.values()]:
        clean_env.delenv(spec.key_env, raising=False)
    for name in (
        "CLICKER_WRITER_MODEL",
        "CLICKER_ANSWER_MODEL",
        "CLICKER_BROWSER",
        "CLICKER_EMAIL",
        "CLICKER_CLASSIFIER_BASE_URL",
    ):
        clean_env.delenv(name, raising=False)
    return clean_env


def test_settings_survive_a_round_trip_through_the_file(tmp_path):
    path = tmp_path / "settings.json"
    original = Settings(
        decisions=Endpoint(provider="vercel", model="anthropic/claude-sonnet-4.5"),
        writer=Endpoint(provider="openai", model="gpt-4.1", api_key="secret"),
        email="me@example.com",
    )

    original.save(path)

    assert oct(path.stat().st_mode)[-3:] == "600"  # it can hold API keys
    loaded = S.load(path)
    assert loaded.decisions == original.decisions
    assert loaded.writer.api_key == "secret"
    assert loaded.email == "me@example.com"


def test_a_broken_settings_file_falls_back_to_the_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json")

    assert S.load(path).decisions.provider == "typesafe"


def test_a_settings_file_from_another_version_keeps_what_it_can(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"writer": {"provider": "groq", "invented_field": 1}, "decisions": ["not", "a", "section"]}))

    loaded = S.load(path)

    assert loaded.writer.provider == "groq"
    assert loaded.decisions.provider == "typesafe" and loaded.answer.provider == "anthropic"  # the rest keep their defaults


def test_a_fresh_install_starts_on_whichever_provider_has_a_key(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw")

    assert S.default_settings().writer.provider == "vercel"


def test_with_no_keys_at_all_the_writer_is_anthropic_as_it_always_was():
    assert S.default_settings().writer.provider == "anthropic"


def test_anthropic_keeps_a_cheaper_writer_and_a_stronger_reader():
    settings = Settings()
    assert settings.writer_model() == "claude-haiku-4-5" and settings.answer_model() == "claude-sonnet-5"


def test_the_model_variables_still_win(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_MODEL", "mistral-large-latest")
    settings = Settings(writer=Endpoint(provider="mistral", model="codestral-latest"))

    assert settings.writer_model() == "mistral-large-latest"


def test_a_model_the_user_typed_beats_the_catalog_default():
    assert Settings(writer=Endpoint(provider="openai", model="gpt-4.1-mini")).writer_model() == "gpt-4.1-mini"
    assert Settings(writer=Endpoint(provider="openai")).writer_model() == "gpt-4.1"


def test_the_browser_and_email_read_the_environment_first(monkeypatch):
    settings = Settings(browser="Safari", email="file@example.com")
    assert settings.resolved_browser() == "Safari"

    monkeypatch.setenv("CLICKER_BROWSER", "Arc")
    monkeypatch.setenv("CLICKER_EMAIL", "shell@example.com")
    assert settings.resolved_browser() == "Arc" and settings.resolved_email() == "shell@example.com"


def test_an_unknown_provider_name_does_not_crash_the_app():
    assert Settings(writer=Endpoint(provider="a-provider-that-was-removed")).writer_spec().key in S.TEXT_PROVIDERS


def test_keys_survive_the_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    Settings(keys={"GROQ_API_KEY": "gsk-test"}).save(path)

    assert S.load(path).keys == {"GROQ_API_KEY": "gsk-test"}


def test_every_key_the_app_can_hold_names_the_providers_that_read_it():
    slots = dict(S.key_slots())

    assert slots["TYPESAFE_API_KEY"] == "TypeSafe (jev)"
    assert slots["AI_GATEWAY_API_KEY"] == "Vercel AI Gateway"
    assert set(slots) == {spec.key_env for spec in [*S.TEXT_PROVIDERS.values(), *S.DECISION_PROVIDERS.values()]}
    assert slots["CLICKER_WRITER_API_KEY"] == "Custom OpenAI-compatible"  # the writer's own, as README names it


def test_the_settings_file_can_be_pointed_somewhere_else(tmp_path, monkeypatch):
    """A path baked in as a default argument cannot be redirected, and a test would overwrite the real file."""
    elsewhere = tmp_path / "elsewhere.json"
    monkeypatch.setattr(S, "SETTINGS_PATH", elsewhere)

    Settings(browser="Safari").save()

    assert elsewhere.exists() and S.load().browser == "Safari"


def test_the_environment_still_beats_a_key_entered_in_the_app(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-the-shell")
    settings = Settings(writer=Endpoint(provider="openai"), keys={"OPENAI_API_KEY": "typed-in-the-app"})

    assert settings.key(S.TEXT_PROVIDERS["openai"]) == "from-the-shell"
    assert settings.key_source(S.TEXT_PROVIDERS["openai"]) == "environment"


# --- turning settings into services ---------------------------------------------------------


def test_a_run_without_a_classifier_key_refuses_before_it_starts():
    with pytest.raises(SettingsError, match="TYPESAFE_API_KEY"):
        session.build(Settings())


@pytest.fixture
def jev(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")


def test_a_missing_writer_is_a_note_and_not_a_failure(jev, monkeypatch):
    monkeypatch.setattr(session, "client_for", lambda api, base_url, key: None)  # no Anthropic token anywhere either

    services = session.build(Settings())

    assert services.writer is None and services.answerer is None
    assert "writer disabled" in services.notes[0] and "ANTHROPIC_API_KEY" in services.notes[0]


def test_each_writer_carries_its_own_model_and_eyes(jev, monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    settings = Settings(writer=Endpoint(provider="mistral"), answer=Endpoint(provider="openai", model="gpt-4.1"))

    services = session.build(settings)

    assert isinstance(services.writer, Configured) and services.writer.model == "codestral-latest"
    assert not services.writer.vision  # codestral reads no images
    assert services.answerer.model == "gpt-4.1" and services.answerer.vision
    assert str(services.writer.base_url).startswith("https://api.mistral.ai/v1")


def test_the_answer_falls_back_to_the_writer_when_it_has_no_key_of_its_own(jev, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    settings = Settings(writer=Endpoint(provider="openai"), answer=Endpoint(provider="groq"))

    services = session.build(settings)

    assert services.answerer is None and services.writer.label == "OpenAI / gpt-4.1"
    assert "answers too" in services.notes[0]


def test_a_text_only_answer_model_says_so_up_front(jev, monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")

    services = session.build(S.default_settings())

    assert any("does not read images" in note for note in services.notes)


def test_a_key_entered_in_the_app_is_used_when_the_environment_has_none(jev):
    settings = Settings(writer=Endpoint(provider="openai"), keys={"OPENAI_API_KEY": "typed-in-the-app"})

    assert session.build(settings).writer.client._client.api_key == "typed-in-the-app"
    assert settings.key_source(settings.writer_spec()) == "app"


def test_one_key_serves_every_endpoint_that_names_it(jev):
    settings = Settings(writer=Endpoint(provider="groq"), answer=Endpoint(provider="groq"), keys={"GROQ_API_KEY": "one-key"})

    services = session.build(settings)

    assert services.writer.client._client.api_key == "one-key" and services.answerer.client._client.api_key == "one-key"


def test_an_anthropic_key_typed_into_the_app_goes_to_anthropic_and_nowhere_else(jev, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1/proxy-for-another-key")
    settings = Settings(keys={"ANTHROPIC_API_KEY": "typed"})

    writer = session.build(settings).writer

    assert writer.client.base_url.host == "api.anthropic.com" and writer.client.api_key == "typed"


def test_the_environments_own_anthropic_pair_travels_together(jev, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1/proxy")

    writer = session.build(Settings()).writer

    assert writer.client.api_key == "from-shell" and writer.client.base_url.host == "127.0.0.1"
    assert writer.custom  # not Anthropic's own API, so the schema goes in the prompt too


def test_the_writer_variables_in_the_environment_win_over_the_file(jev, monkeypatch, endpoint):
    monkeypatch.setenv("CLICKER_WRITER_API", "openai")
    monkeypatch.setenv("CLICKER_WRITER_BASE_URL", endpoint.url + "/v1")
    settings = Settings(writer=Endpoint(provider="mistral"), keys={"MISTRAL_API_KEY": "m"})

    services = session.build(settings)

    assert not isinstance(services.writer, Configured) and services.answerer is None  # upstream's make_writer, unchanged
    assert str(services.writer.base_url).startswith(endpoint.url)


def test_a_chat_model_classifier_needs_a_model_and_an_endpoint(monkeypatch):
    with pytest.raises(SettingsError, match="model name"):
        session.make_classifier(Endpoint(provider="custom"))
    monkeypatch.setenv("CLICKER_CLASSIFIER_BASE_URL", "http://127.0.0.1:1/v1")
    _, label = session.make_classifier(Endpoint(provider="custom", model="qwen3"))
    assert label == "Custom OpenAI-compatible / qwen3"


def test_every_classifier_in_the_catalog_can_be_built_or_explains_itself(monkeypatch):
    """No entry in the menus may fail in a way the app cannot put into a sentence. A provider with no
    default model is the one legitimate refusal: "custom" exists so a URL and a model can be typed in."""
    for key, spec in S.DECISION_PROVIDERS.items():
        monkeypatch.setenv(spec.key_env, "stub")
        endpoint = Endpoint(provider=key, base_url="" if spec.kind == S.TYPESAFE else "http://127.0.0.1:1/v1")
        try:
            factory, label = session.make_classifier(endpoint)
            assert callable(factory) and label
        except SettingsError as e:
            assert not spec.default_model and "model name" in str(e)


def test_a_flag_points_one_run_elsewhere_and_starts_the_provider_from_its_own_defaults():
    from argparse import Namespace

    settings = Settings(writer=Endpoint(provider="openai", model="gpt-4.1-mini", base_url="http://old"))
    flags = Namespace(classifier=None, classifier_model="jev-2", writer="groq", writer_model=None, answer=None, answer_model=None)

    apply_provider_flags(settings, flags)

    assert settings.writer == Endpoint(provider="groq") and settings.decisions.model == "jev-2"


def test_icons_are_named_only_when_asked_for_and_only_by_a_model_that_reads_images(jev, monkeypatch):
    from typesafe_computer_use.platform_adapter import desktop

    monkeypatch.setattr(desktop, "installed_apps", lambda: ["Safari"])
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("MISTRAL_API_KEY", "m")

    def labeller(**choices):
        settings = Settings(**choices)
        return session.context_factory(settings, "g", session.build(settings))(None, []).labeller

    assert labeller(writer=Endpoint(provider="openai"), answer=Endpoint(provider="openai")) is None  # off by default
    assert labeller(writer=Endpoint(provider="openai"), answer=Endpoint(provider="openai"), name_icons=True) is not None
    assert labeller(writer=Endpoint(provider="mistral"), answer=Endpoint(provider="mistral"), name_icons=True) is None


def test_the_clipboard_is_not_shared_unless_the_user_says_so(tmp_path):
    assert Settings().share_clipboard is False
    path = tmp_path / "settings.json"
    Settings(share_clipboard=True, name_icons=True).save(path)
    loaded = S.load(path)
    assert loaded.share_clipboard and loaded.name_icons
