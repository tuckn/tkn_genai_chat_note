"""Connections are defined in this application's own genai_profiles, never in a shared Bridge file."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tkn_genai_bridge import Runtime

from tkn_genai_chat_note.api_inference import ApiClient
from tkn_genai_chat_note.cli import build_parser, main
from tkn_genai_chat_note.config import AppConfig, config_document, load_app_config, resolve_app_config
from tkn_genai_chat_note.genai_profiles import GenAIProfileConfig
from tkn_genai_chat_note.runtime import PipelineError

ENDPOINT = "https://example.openai.azure.com/openai/v1"
PRICE = {"currency": "JPY", "pricing_date": "2026-01-01", "input_per_million": 30, "output_per_million": 180}


def profiles():
    return {
        "azure-high": {"provider": "azure-openai", "model": "notes", "reasoning_effort": "high",
                       "azure": {"endpoint": ENDPOINT}, "pricing": {"notes": PRICE},
                       "limits": {"max_calls": 5, "max_cost_jpy": 50}},
        "azure-low": {"provider": "azure-openai", "model": "other", "reasoning_effort": "low",
                      "azure": {"endpoint": "https://second.openai.azure.com/openai/v1"}},
        "local-gemma": {"provider": "ollama", "model": "local", "ollama": {"base_url": "http://localhost:11500"}},
        "codex-high": {"provider": "codex", "model": "remote", "reasoning_effort": "high",
                       "cli": {"executable": "custom-codex"}},
    }


def application(tmp_path, *, selected="azure-high", entries=None, **extra):
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": "12.0.0",
        "genai_profiles": profiles() if entries is None else entries,
        "generation": {"genai_profile": selected},
        **extra,
    }), encoding="utf-8")
    return path


def fingerprint(cfg):
    from tkn_genai_chat_note.review_generation import ReviewGenerator
    return ReviewGenerator(cfg.enabled_source_configs()[0], "test").signature


def unreadable_shared_bridge_config():
    path = Path.home() / ".tkn/genai_bridge/config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invalid: [yaml", encoding="utf-8")
    return path


def test_defaults_use_cli_logins_without_reading_shared_bridge_config():
    unreadable_shared_bridge_config()
    cfg = load_app_config()
    assert cfg.generation.genai_profile == "codex-default"
    assert {"codex-default", "claude-default", "copilot-default", "antigravity-default"} <= set(cfg.genai_profiles)
    assert cfg.provider == "codex" and cfg.model == "provider-default"
    options = cfg.inference_config().inference_options
    assert options["genai_profile"] == "codex-default" and options["bridge"]["provider"] == "codex"
    assert "limits" not in options


def test_profile_switch_and_overrides_only_change_the_selected_entry(tmp_path):
    path = application(tmp_path)
    original = path.read_bytes()
    high = load_app_config(explicit_path=path, cwd=tmp_path)
    result = resolve_app_config(explicit_path=path, cwd=tmp_path,
                                overrides={"profile": "azure-low", "model": "replacement"})
    low = result.config
    assert high.provider == low.provider == "azure-openai"
    assert low.model == "replacement" and low.reasoning_effort == "low"
    assert low.active_provider_config.endpoint.startswith("https://second.")
    assert low.genai_profiles["azure-high"].model == "notes"
    assert result.sources["generation.genai_profile"] == "CLI option"
    assert result.sources["genai_profiles.azure-low.model"] == "CLI option"
    assert path.read_bytes() == original
    local = load_app_config(explicit_path=path, cwd=tmp_path, overrides={"profile": "local-gemma"})
    assert local.provider == "ollama" and local.ollama_base_url == "http://localhost:11500"
    assert local.active_provider_config.limits is not None  # API providers get the default budget.
    codex = load_app_config(explicit_path=path, cwd=tmp_path,
                            overrides={"profile": "codex-high", "codex_executable": "other-codex"})
    assert codex.codex_executable == "other-codex"
    assert codex.inference_config().inference_options["bridge"]["cli"]["executable"] == "other-codex"
    with pytest.raises(PipelineError, match="requires an active ollama"):
        load_app_config(explicit_path=path, cwd=tmp_path, overrides={"ollama_base_url": "http://localhost:1"})


def test_compatibility_provider_selector(tmp_path):
    path = application(tmp_path)
    with pytest.raises(PipelineError, match="multiple genai_profiles.*--profile"):
        load_app_config(explicit_path=path, cwd=tmp_path, overrides={"provider": "azure-openai"})
    codex = load_app_config(explicit_path=path, cwd=tmp_path, overrides={"provider": "codex"})
    assert codex.generation.genai_profile == "codex-high"  # The declared entry wins over codex-default.
    claude = load_app_config(explicit_path=path, cwd=tmp_path, overrides={"provider": "claude-code"})
    assert claude.generation.genai_profile == "claude-default"
    with pytest.raises(PipelineError, match="unknown GenAI profile"):
        load_app_config(explicit_path=path, cwd=tmp_path, overrides={"profile": "missing"})
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--profile", "azure-high", "--provider", "azure-openai", "config", "list"])


@pytest.mark.parametrize("profile", [
    {"provider": "unknown"},
    {"provider": "codex", "unknown": True},
    {"provider": "azure-openai", "model": "notes"},
    {"provider": "codex", "limits": {"max_calls": 2}},
    {"provider": "codex", "model_digest": "sha256:x"},
    {"provider": "ollama", "model": "local", "ollama": {"base_url": "http://remote.example"}},
])
def test_invalid_profiles_fail_offline(tmp_path, profile):
    path = application(tmp_path, selected="chosen", entries={"chosen": profile})
    with pytest.raises(PipelineError):
        load_app_config(explicit_path=path, cwd=tmp_path)
    with pytest.raises(ValidationError):
        GenAIProfileConfig.model_validate(profile)


def test_generation_must_name_a_defined_profile(tmp_path):
    with pytest.raises(PipelineError, match="generation.genai_profile"):
        load_app_config(explicit_path=application(tmp_path, selected="missing"), cwd=tmp_path)


def test_layers_merge_by_name_and_provider_change_replaces_the_connection(tmp_path):
    project = tmp_path / ".tkn/config.yaml"
    project.parent.mkdir()
    project.write_text(yaml.safe_dump({"schema_version": "12.0.0", "genai_profiles": profiles(),
                                       "generation": {"genai_profile": "azure-high"}}), encoding="utf-8")
    override = {"schema_version": "12.0.0", "genai_profiles": {"azure-high": {"reasoning_effort": "medium"}}}
    path = tmp_path / "override.yaml"
    path.write_text(yaml.safe_dump(override), encoding="utf-8")
    cfg = load_app_config(explicit_path=path, cwd=tmp_path)
    assert cfg.reasoning_effort == "medium" and cfg.model == "notes"
    assert cfg.active_provider_config.endpoint == ENDPOINT
    assert cfg.active_provider_config.limits.max_calls == 5
    override["genai_profiles"]["azure-high"] = {"provider": "codex", "model": "remote"}
    path.write_text(yaml.safe_dump(override), encoding="utf-8")
    cfg = load_app_config(explicit_path=path, cwd=tmp_path)
    assert cfg.provider == "codex" and cfg.codex_executable == "codex"
    assert cfg.active_provider_config.endpoint is None and not cfg.active_provider_config.bridge.pricing


def test_executable_paths_resolve_from_their_config_file(tmp_path):
    entries = {"local": {"provider": "codex", "cli": {"executable": "bin/codex.cmd"}},
               "bare": {"provider": "claude-code", "cli": {"executable": "claude"}}}
    path = application(tmp_path, selected="local", entries=entries)
    cfg = load_app_config(explicit_path=path, cwd=tmp_path / "elsewhere")
    assert Path(cfg.codex_executable) == (tmp_path / "bin/codex.cmd").absolute()
    assert cfg.genai_profiles["bare"].executable == "claude"


def test_identity_ignores_profile_names_and_prices_but_not_models(tmp_path):
    path = application(tmp_path)
    first = load_app_config(explicit_path=path, cwd=tmp_path)
    renamed = deepcopy(profiles())
    renamed["same"] = renamed.pop("azure-high")
    renamed["same"]["pricing"]["notes"]["input_per_million"] = 90
    second = load_app_config(explicit_path=application(tmp_path, selected="same", entries=renamed), cwd=tmp_path)
    assert fingerprint(second) == fingerprint(first)
    model = load_app_config(explicit_path=path, cwd=tmp_path, overrides={"model": "two"})
    assert fingerprint(model) != fingerprint(first)


def test_api_client_keeps_local_boundary_and_application_ceilings(tmp_path):
    entries = {"local": {"provider": "ollama", "model": "local", "local_only": True, "max_output_tokens": 2048,
                         "ollama": {"think": False, "context_tokens": 32768}}}
    cfg = load_app_config(explicit_path=application(tmp_path, selected="local", entries=entries), cwd=tmp_path)
    client = ApiClient(cfg.inference_config())
    assert client.profile.local_only and client.profile.ollama.think is False
    assert client.profile.max_output_tokens == 2048
    assert client.limits.input_tokens + client.limits.output_tokens <= 32768
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    assert "answer" in client.body("question", schema)["messages"][0]["content"]
    client.close()
    assert client.runtime._closed


def test_config_list_exposes_the_resolved_connection(tmp_path, capsys):
    path = application(tmp_path)
    assert main(["--config", str(path), "config", "list", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    details = output["generationResolved"]
    assert details["profile"] == "azure-high" and details["model"] == "notes"
    assert details["inferenceOptions"]["genai_profile"] == "azure-high"
    assert details["inferenceOptions"]["limits"]["max_calls"] == 5
    document = output["config"]["genai_profiles"]["azure-high"]
    assert document["provider"] == "azure-openai" and document["limits"]["max_cost_jpy"] == 50
    assert output["sources"]["genai_profiles.azure-high.model"].startswith("explicit:")


def test_config_document_round_trips(tmp_path):
    cfg = load_app_config(explicit_path=application(tmp_path), cwd=tmp_path)
    document = config_document(cfg)
    rebuilt = AppConfig.model_validate({key: value for key, value in document.items()})
    assert rebuilt.genai_profiles == cfg.genai_profiles


def test_schema9_bridge_references_are_rejected_without_conversion(tmp_path):
    path = tmp_path / "old.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "9.0.0", "generation": {
        "active_profile": "notes", "profiles": {"notes": {"bridge_profile": "shared-notes"}}}}), encoding="utf-8")
    with pytest.raises(PipelineError, match="does not convert older files"):
        load_app_config(explicit_path=path, cwd=tmp_path)


@pytest.mark.parametrize("variant", ["codex", "antigravity"])
def test_cli_runtime_receives_profile_settings(tmp_path, monkeypatch, variant):
    from tkn_genai_bridge import Usage
    from tkn_genai_bridge.providers.base import ProviderResponse

    from tkn_genai_chat_note import inference

    provider = {"codex": "codex", "antigravity": "antigravity"}[variant]
    entries = {"chosen": {"provider": provider, "model": "test-model", "reasoning_effort": "medium",
                          "cli": {"executable": "custom-cli"}}}
    cfg = load_app_config(explicit_path=application(tmp_path, selected="chosen", entries=entries), cwd=tmp_path)
    observed = []

    class Backend:
        def generate(self, actual, request):
            assert actual.provider == provider and actual.cli.executable == "custom-cli"
            assert actual.reasoning_effort == "medium"
            return ProviderResponse({"ok": True}, usage=Usage(input_tokens=10, output_tokens=2))

    monkeypatch.setattr(inference, "Runtime", lambda profile, **kw: Runtime(profile, backend=Backend(), **kw))
    assert inference.invoke_structured(cfg.inference_config(), "test", {"type": "object"}, cwd=tmp_path,
                                       timeout=10, usage_observer=observed.append) == {"ok": True}
    assert observed[-1]["bridgeProfile"] == "chosen"
    assert observed[-1]["inputTokens"] == 10
