from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tkn_genai_chat_note.config import (
    CONFIG_SCHEMA_VERSION,
    AppConfig,
    config_example_text,
    initialize_user_config,
    load_app_config,
    resolve_app_config,
)
from tkn_genai_chat_note.runtime import PipelineError


def write_yaml(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": CONFIG_SCHEMA_VERSION, **value}
    schema_version = document.pop("schema_version")
    schema_text = json.dumps(schema_version) if isinstance(schema_version, str) else str(schema_version)
    path.write_text(
        f"schema_version: {schema_text}\n{yaml.safe_dump(document, sort_keys=False)}",
        encoding="utf-8",
    )


def test_packaged_example_config_uses_portable_home_paths() -> None:
    value = yaml.safe_load(config_example_text())

    for key in ("cache_root",):
        assert "\\" not in value[key]
        assert value[key].startswith("~/")
    assert "installed_at" not in value
    assert "chat" not in value
    source = value["sources"]["my-windows-note-pc"]
    assert source["source_root"] == "~/.codex" and source["enabled"]
    assert source["include_archived"]
    for kind in ("raw", "data", "state"):
        assert source[kind + "_root"] == f"C:/path/to/codex/{kind}"
    assert not value["sources"]["my-wsl-ubuntu"]["enabled"]
    assert value["report_path"].startswith("~/")
    assert value["usage_report"]["price_scenarios"] == {}
    assert not {"codex_home", "source_id", "include_archived"}.intersection(value)
    assert "scopes" not in value
    assert value["schema_version"] == CONFIG_SCHEMA_VERSION
    assert config_example_text().splitlines()[0] == f'schema_version: "{CONFIG_SCHEMA_VERSION}"'
    assert value["generation"] == {"genai_profile": "codex-default"}
    assert value["genai_profiles"]["codex-default"]["provider"] == "codex"
    assert all("bridge_profile" not in entry for entry in value["genai_profiles"].values())
    assert "summary_prompt" not in value


def test_config_init_is_idempotent_and_protects_edited_config(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"

    created = initialize_user_config(target)
    unchanged = initialize_user_config(target)
    target.write_text("schema_version: 1\nmodel: edited\n", encoding="utf-8")

    with pytest.raises(PipelineError, match="different content"):
        initialize_user_config(target)

    replaced = initialize_user_config(target, force=True)

    assert created["status"] == "created"
    assert unchanged["status"] == "unchanged"
    assert replaced["status"] == "replaced"
    assert target.read_text(encoding="utf-8") == config_example_text()
    backup = Path(str(replaced["backupPath"]))
    assert backup.read_text(encoding="utf-8") == "schema_version: 1\nmodel: edited\n"


def test_precedence_and_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_yaml(
        home / ".tkn/genai_chat_note/config.yaml",
        {
            "runtime_minutes": 10,
            "genai_profiles": {"codex-default": {"model": "global"}},
        },
    )
    write_yaml(
        cwd / ".tkn/config.yaml",
        {
            "runtime_minutes": 20,
            "genai_profiles": {"codex-default": {"model": "local"}},
        },
    )
    write_yaml(
        explicit,
        {"runtime_minutes": 25, "sources": {"windows": {"state_root": "state"}}},
    )

    config = load_app_config(
        explicit_path=explicit,
        cwd=cwd,
        overrides={"runtime_minutes": 40},
    )

    assert config.runtime_minutes == 40
    assert config.model == "local"
    assert config.state_root == (explicit.parent / "state").absolute()
    assert config.registry_path == config.source_data_root / "project-registry.jsonl"
    assert config.projects_data_root == config.source_data_root / "projects"
    assert config.projects_state_root == config.source_state_root / "projects"


def test_config_resolution_reports_the_winning_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_yaml(
        home / ".tkn/genai_chat_note/config.yaml",
        {"genai_profiles": {"codex-default": {"model": "global"}}},
    )
    write_yaml(
        cwd / ".tkn/config.yaml",
        {
            "genai_profiles": {"codex-default": {"model": "project"}},
            "runtime_minutes": 10,
        },
    )
    write_yaml(explicit, {"runtime_minutes": 20})

    resolution = resolve_app_config(
        explicit_path=explicit,
        cwd=cwd,
        overrides={"runtime_minutes": 30},
    )

    assert resolution.config.model == "project"
    assert resolution.config.runtime_minutes == 30
    assert "schema_version" not in resolution.sources
    assert resolution.sources["genai_profiles.codex-default.model"].startswith("project:")
    assert resolution.sources["runtime_minutes"] == "CLI option"
    assert [layer["kind"] for layer in resolution.layers] == [
        "built-in",
        "global",
        "project",
        "explicit",
    ]
    assert all(layer["effectiveSchemaVersion"] == CONFIG_SCHEMA_VERSION for layer in resolution.layers)
    assert all("migration" not in layer for layer in resolution.layers)


@pytest.mark.parametrize("value", [None, "custom.md"])
def test_retired_summary_prompt_is_an_unknown_key(tmp_path: Path, value: object) -> None:
    write_yaml(tmp_path / ".tkn/config.yaml", {"summary_prompt": value})

    with pytest.raises(PipelineError, match="unknown configuration key: summary_prompt"):
        load_app_config(cwd=tmp_path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"unknown_setting": True})
    with pytest.raises(PipelineError, match="unknown configuration key"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_config_file_requires_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("runtime_minutes: 10\n", encoding="utf-8")

    with pytest.raises(PipelineError, match="schema_version is required"):
        load_app_config(explicit_path=path, cwd=tmp_path)


@pytest.mark.parametrize(
    ("schema_version", "message"),
    [
        ("3", "expected a quoted MAJOR.MINOR.PATCH"),
        ('"2"', "expected a quoted MAJOR.MINOR.PATCH"),
        ('"2.0"', "expected a quoted MAJOR.MINOR.PATCH"),
        ('"2.0.0-rc1"', "expected a quoted MAJOR.MINOR.PATCH"),
        ('"1.9.0"', "unsupported older configuration schema_version"),
        ('"9.0.0"', "does not convert older files"),
        ('"10.0.0"', "does not convert older files"),
        ('"11.1.0"', "does not convert older files"),
        ('"12.2.0"', "unsupported newer configuration schema_version"),
        ('"13.0.0"', "unsupported newer configuration schema_version"),
    ],
)
def test_unsupported_schema_versions_are_rejected(
    tmp_path: Path,
    schema_version: str,
    message: str,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"schema_version: {schema_version}\n", encoding="utf-8")

    with pytest.raises(PipelineError, match=message):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_same_major_minor_newer_patch_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"schema_version": "12.1.7", "runtime_minutes": 10})

    resolution = resolve_app_config(explicit_path=path, cwd=tmp_path)

    assert resolution.config.schema_version == CONFIG_SCHEMA_VERSION
    explicit = resolution.layers[-1]
    assert explicit["schemaVersion"] == "12.1.7"
    assert explicit["effectiveSchemaVersion"] == CONFIG_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("schema_version", "settings"),
    [
        ("1.0.0", {"provider": "codex", "model": "gpt-5.6-sol"}),
        ("2.2.1", {"raw_root": "old-raw"}),
        ("3.0.0", {"chat": {"providers": {}}}),
        ("4.1.0", {"codex_home": "old"}),
        ("5.0.0", {"chat": {"providers": {"codex": {}}}}),
        ("6.0.0", {"chat": {"providers": {"codex": {"sources": {}}}}}),
        ("7.2.0", {"reviews": {}}),
        ("8.4.0", {"runtime_minutes": 10}),
        ("9.0.0", {"generation": {"profiles": {"codex": {"bridge_profile": "codex-default"}}}}),
    ],
)
def test_older_schema_files_stop_without_conversion(
    tmp_path: Path, schema_version: str, settings: dict[str, object]
) -> None:
    path = tmp_path / "old.yaml"
    write_yaml(path, {"schema_version": schema_version, **settings})
    before = path.read_bytes()
    with pytest.raises(PipelineError, match="does not convert older files"):
        load_app_config(explicit_path=path, cwd=tmp_path)
    assert path.read_bytes() == before


def test_source_id_must_be_safe_for_storage(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"sources": {"../outside": {}}})

    with pytest.raises(PipelineError, match="source_id must use only"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_schema_version_cannot_be_overridden_as_a_setting(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="cannot be a CLI override"):
        load_app_config(cwd=tmp_path, overrides={"schema_version": "2.0.7"})


def test_active_provider_requires_matching_provider_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = tmp_path / "config.yaml"
    write_yaml(path, {"generation": {"genai_profile": "ollama"}})

    with pytest.raises(PipelineError, match="must name an entry under genai_profiles"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_cli_can_select_an_already_configured_provider_without_repeating_its_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(
        path,
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "genai_profiles": {
                "claude-sonnet": {
                    "provider": "claude-code",
                    "model": "claude-sonnet-4-6",
                    "reasoning_effort": "medium",
                }
            },
        },
    )

    resolution = resolve_app_config(
        explicit_path=path,
        cwd=tmp_path,
        overrides={"provider": "claude-code"},
    )

    assert resolution.config.provider == "claude-code"
    assert resolution.config.model == "claude-sonnet-4-6"
    assert resolution.config.reasoning_effort == "medium"
    assert resolution.config.generation.genai_profile == "claude-sonnet"  # Declared wins over claude-default.
    assert resolution.sources["generation.genai_profile"] == "CLI option"
    assert resolution.sources["genai_profiles.claude-sonnet.model"].startswith("explicit:")


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("codex", "gpt-5.6-sol"),
        ("claude-code", "claude-sonnet-4-6"),
        ("github-copilot", "claude-sonnet-4.6"),
        ("ollama", "qwen3.5:9b"),
    ],
)
def test_inference_providers_resolve_without_changing_codex_source(
    tmp_path: Path,
    provider: str,
    model: str,
) -> None:
    config = load_app_config(
        cwd=tmp_path,
        overrides={"provider": provider, "model": model},
    )

    assert config.provider == provider
    assert config.model == model
    assert config.sessions_root == config.codex_home / "sessions"
    assert config.inference_config().provider == provider


def test_remote_ollama_endpoint_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="loopback"):
        load_app_config(
            cwd=tmp_path,
            overrides={
                "provider": "ollama",
                "model": "qwen3.5:9b",
                "ollama_base_url": "https://ollama.example.com",
            },
        )


def test_source_layers_resolve_paths_and_preserve_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tkn_genai_chat_note.config import config_document, write_config

    home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    global_path = home / ".tkn/genai_chat_note/config.yaml"
    write_yaml(global_path, {"sources": {"pc": {"source_root": "logs"}}})
    project = tmp_path / "work/.tkn/config.yaml"
    write_yaml(
        project,
        {
            "sources": {"pc": {"include_archived": False}},
            "genai_profiles": {"claude-default": {"model": "example", "cli": {"executable": "claude"}}},
        },
    )
    explicit = tmp_path / "explicit/config.yaml"
    write_yaml(explicit, {"sources": {"pc": {"data_root": "data"}}})
    resolution = resolve_app_config(
        explicit_path=explicit, cwd=tmp_path / "work", overrides={"sources": {"pc": {"source_root": "cli-codex"}}}
    )
    config = resolution.config
    assert config.source_root == tmp_path / "work/cli-codex"
    assert config.source_id == "pc" and not config.include_archived
    assert config.data_root == explicit.parent / "data"
    assert config.genai_profiles["claude-default"].model == "example"
    assert resolution.sources["sources.pc.source_root"] == "CLI option"
    assert resolution.sources["sources.pc.raw_root"] == "built-in defaults"
    assert resolution.sources["sources.pc.data_root"].startswith("explicit:")
    saved = tmp_path / "saved.yaml"
    write_config(config, saved)
    document = yaml.safe_load(saved.read_text(encoding="utf-8"))
    assert document == config_document(config)
    assert "chat" not in document
    assert config_document(load_app_config(explicit_path=saved, cwd=tmp_path)) == document


def test_source_home_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CHAT_SOURCE_TEST_ROOT", str(tmp_path / "environment"))
    for raw, expected in [
        ("~/logs", tmp_path / "logs"),
        ("$CHAT_SOURCE_TEST_ROOT/logs", tmp_path / "environment/logs"),
    ]:
        target = tmp_path / "config.yaml"
        write_yaml(target, {"sources": {"windows": {"source_root": raw}}})
        config = load_app_config(explicit_path=target, cwd=tmp_path)
        assert config.sources["windows"].source_root == expected


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"../outside": {}}, "source_id must use only"),
        ({"windows": {"provider": "claude-code"}}, "unknown configuration key"),
        ({"windows": {"source_root": "  "}}, "paths must not be empty"),
        ({"windows": {"source_root": None}}, "Input is not a valid path"),
    ],
)
def test_invalid_source_settings_are_rejected_before_merging(
    tmp_path: Path, settings: dict[str, object], message: str
) -> None:
    target = tmp_path / "config.yaml"
    write_yaml(target, {"sources": settings})
    with pytest.raises(PipelineError, match=message):
        load_app_config(explicit_path=target, cwd=tmp_path)


def test_unknown_chat_key_in_lower_layer_cannot_be_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    write_yaml(
        tmp_path / ".tkn/genai_chat_note/config.yaml",
        {"sources": {"windows": {"hom": "typo"}}},
    )
    with pytest.raises(PipelineError, match="unknown configuration key: sources.windows.hom"):
        load_app_config(
            cwd=tmp_path,
            overrides={"sources": {"windows": {"source_root": "correct"}}},
        )


def test_sources_have_fixed_codex_identity(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"sources": {"pc-main": {"source_root": "~/source"}}})
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    assert config.source_identity == ("codex", "pc-main")
    assert config.raw_root == Path.home() / ".tkn/genai_chat_note/raw/codex/pc-main"
    assert config.source_data_root == config.data_root


def test_source_roots_keep_declaring_layer_and_config_list_reports_final_paths(tmp_path: Path) -> None:
    from tkn_genai_chat_note.config import config_document

    work = tmp_path / "work"
    project = work / ".tkn/config.yaml"
    project.parent.mkdir(parents=True)
    write_yaml(project, {"sources": {"machine": {"data_root": "project-data"}}})
    explicit = tmp_path / "explicit.yaml"
    write_yaml(explicit, {"sources": {"machine": {"raw_root": "raw"}}})
    resolution = resolve_app_config(explicit_path=explicit, cwd=work)
    config = resolution.config
    assert config.data_root == (project.parent / "project-data").absolute()
    assert config.raw_root == (tmp_path / "raw").absolute()
    assert config.state_root == Path.home() / ".tkn/genai_chat_note/state/codex/machine"
    document = config_document(config)
    assert "raw_root" not in document and "state_root" not in document
    assert document["sources"]["machine"]["raw_root"] == str(config.raw_root)
    assert resolution.sources["sources.machine.data_root"].startswith("project:")


def test_earlier_application_directories_are_not_read(tmp_path: Path) -> None:
    """Only ~/.tkn/genai_chat_note is the user directory; earlier directories are neither read nor changed."""
    earlier = [Path.home() / ".tkn" / name / "config.yaml"
               for name in ("genai_chat_note_pipeline", "codex_chat_note_pipeline")]
    write_yaml(earlier[0], {"runtime_minutes": 17})
    write_yaml(earlier[1], {"schema_version": "9.0.0", "runtime_minutes": 18})
    before = [path.read_bytes() for path in earlier]

    assert load_app_config(cwd=tmp_path).runtime_minutes == AppConfig().runtime_minutes
    assert initialize_user_config()["status"] == "created"
    assert (Path.home() / ".tkn/genai_chat_note/config.yaml").read_text(encoding="utf-8") == config_example_text()
    assert [path.read_bytes() for path in earlier] == before


@pytest.mark.parametrize("key", ["chat", "providers", "codex"])
def test_old_acquisition_wrappers_are_rejected_in_schema_7(tmp_path: Path, key: str) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {key: {}})
    with pytest.raises(PipelineError, match="unknown configuration key"):
        load_app_config(explicit_path=path, cwd=tmp_path)
