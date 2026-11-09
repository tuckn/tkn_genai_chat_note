"""Strict layered YAML configuration for the standalone pipeline."""

from __future__ import annotations

import os
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from tkn_genai_bridge import TokenPricing
from tkn_genai_bridge.models import PROVIDER_NAMES

from .config_validation import validate_config_layer
from .genai_profiles import GenAIProfileConfig, default_genai_profiles
from .process_settings import ChatSourceSettings, DigestSettings, ProcessSettings, VaultSettings
from .report_settings import GenAIPriceScenario, UsageReportSettings
from .review_settings import ReviewSettings
from .runtime import (
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_RUNTIME_MINUTES,
    DEFAULT_SOURCE_ID,
    InferenceConfig,
    PipelineError,
    atomic_write_text,
)

CONFIG_SCHEMA_VERSION: Literal["12.0.0"] = "12.0.0"
_CONFIG_SCHEMA_VERSION_PARTS = (12, 0, 0)
_CONFIG_SCHEMA_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
APP_DIRECTORY_NAME = "genai_chat_note"
CONFIG_EXAMPLE_RESOURCE = "resources/config.example.yaml"
# Command-line options that adjust the selected GenAI profile instead of a configuration key.
GENERATION_OPTION_KEYS = frozenset(
    {
        "provider",
        "model",
        "reasoning_effort",
        "codex_executable",
        "claude_executable",
        "copilot_executable",
        "ollama_base_url",
    }
)
# Command-line selectors of the review generation connection.
TRANSPORT_OPTIONS = {
    "codex_executable": "codex",
    "claude_executable": "claude-code",
    "copilot_executable": "github-copilot",
}


@dataclass(frozen=True)
class ConfigResolution:
    """Resolved configuration together with its inspectable provenance."""

    config: AppConfig
    sources: dict[str, str]
    layers: tuple[dict[str, Any], ...]
    effective_schema_version: str


def default_app_root() -> Path:
    return Path.home() / ".tkn" / APP_DIRECTORY_NAME


def default_user_cache_root() -> Path:
    configured = os.getenv("XDG_CACHE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return base / APP_DIRECTORY_NAME


class GenerationConfig(BaseModel):
    """Select the genai_profiles entry used by calendar review generation."""

    model_config = ConfigDict(extra="forbid")

    genai_profile: str = Field(default="codex-default", min_length=1)


def validate_source_id(value: str) -> str:
    """Preserve exact identity while requiring a portable path component."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(
            "source_id must use only ASCII letters, digits, dot, underscore, or hyphen; "
            "start with a letter or digit; lowercase kebab-case is recommended"
        )
    if value.endswith(".") or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", value.split(".")[0]):
        raise ValueError("source_id must not end with a dot or use a Windows reserved device name")
    return value


SourceId = Annotated[str, AfterValidator(validate_source_id)]


class CodexSourceConfig(BaseModel):
    """One local Codex input and its owned output roots; the map key is its ID."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    source_root: Path = Field(default_factory=lambda: Path.home() / ".codex")
    include_archived: bool = True
    raw_root: Path | None = None
    data_root: Path | None = None
    state_root: Path | None = None

    reviews: ReviewSettings = Field(default_factory=ReviewSettings)

    @field_validator("source_root", "raw_root", "data_root", "state_root", mode="before")
    @classmethod
    def require_path(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("source paths must not be empty")
        return value


class AppConfig(BaseModel):
    """Codex acquisition sources and independent inference backend selection."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["12.0.0"] = CONFIG_SCHEMA_VERSION
    sources: dict[SourceId, CodexSourceConfig] = Field(default_factory=lambda: {DEFAULT_SOURCE_ID: CodexSourceConfig()})
    cache_root: Path = Field(default_factory=default_user_cache_root)
    report_path: Path = Field(default_factory=lambda: default_app_root() / "reports")
    usage_report: UsageReportSettings = Field(default_factory=UsageReportSettings)
    genai_profiles: dict[str, GenAIProfileConfig] = Field(default_factory=default_genai_profiles)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    runtime_minutes: int = Field(default=DEFAULT_RUNTIME_MINUTES, gt=0)
    model_timeout_seconds: int = Field(default=DEFAULT_MODEL_TIMEOUT_SECONDS, gt=0)
    # Note processes (role redesign): where conversations are read, the Vault and work folders.
    chat_sources: dict[SourceId, ChatSourceSettings] = Field(default_factory=dict)
    digests: DigestSettings = Field(default_factory=DigestSettings)
    utc_offset_minutes: int = Field(default=540, ge=-720, le=840)
    path_aliases: list[list[str]] = Field(default_factory=list)
    processes: ProcessSettings = Field(default_factory=ProcessSettings)
    vault: VaultSettings = Field(default_factory=VaultSettings)

    _selected_source: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def unique_source_ids(self) -> Self:
        if len({name.casefold() for name in self.sources}) != len(self.sources):
            raise ValueError("source_id keys must be unique ignoring case")
        return self

    @model_validator(mode="after")
    def unique_chat_sources(self) -> Self:
        seen: dict[tuple[str, str], str] = {}
        for name, settings in self.chat_sources.items():
            key = (settings.app, settings.source_id.casefold())
            if key in seen:
                raise ValueError(f"chat_sources {seen[key]} and {name} both name {settings.app} "
                                 f"source {settings.source_id}; evidence IDs and digests need one entry each")
            seen[key] = name
        return self

    @model_validator(mode="after")
    def path_alias_groups(self) -> Self:
        if any(len(group) < 2 or any(not item.strip() for item in group) for group in self.path_aliases):
            raise ValueError("each path_aliases entry must list two or more nonempty folder prefixes")
        return self

    @model_validator(mode="after")
    def resolve_genai_references(self) -> Self:
        if any(not name.strip() or name != name.strip() for name in self.genai_profiles):
            raise ValueError("genai_profiles names must be nonempty with no surrounding whitespace")
        if self.generation.genai_profile not in self.genai_profiles:
            raise ValueError(f"generation.genai_profile {self.generation.genai_profile!r} "
                             "must name an entry under genai_profiles")
        for name, scenario in self.usage_report.price_scenarios.items():
            if not isinstance(scenario, GenAIPriceScenario):
                continue
            profile = self.genai_profiles.get(scenario.genai_profile)
            if profile is None:
                raise ValueError(f"usage_report.price_scenarios.{name}.genai_profile "
                                 f"{scenario.genai_profile!r} is not defined under genai_profiles")
            if scenario.model not in profile.bridge.pricing:
                raise ValueError(f"genai_profiles.{scenario.genai_profile} has no pricing for {scenario.model!r} "
                                 f"(usage_report.price_scenarios.{name})")
        return self

    def price_scenario_pricing(self) -> dict[str, TokenPricing]:
        """Reference prices by scenario name; named profiles supply their confirmed rates."""
        return {
            name: (self.genai_profiles[scenario.genai_profile].bridge.pricing[scenario.model]
                   if isinstance(scenario, GenAIPriceScenario) else scenario)
            for name, scenario in self.usage_report.price_scenarios.items()
        }

    def for_source(self, source_id: str) -> Self:
        if source_id not in self.sources:
            raise PipelineError(f"unknown source_id: {source_id}")
        selected = self.model_copy()
        selected._selected_source = source_id
        return selected

    def enabled_source_configs(self, source_id: str | None = None) -> list[Self]:
        if self._selected_source is not None:
            if source_id is not None and source_id != self._selected_source:
                raise PipelineError(f"unknown source_id: {source_id}")
            source_id = self._selected_source
        if source_id is not None and source_id not in self.sources:
            raise PipelineError(f"unknown source_id: {source_id}")
        enabled = [
            self.for_source(name)
            for name, source in self.sources.items()
            if source.enabled and (source_id is None or name == source_id)
        ]
        if not enabled:
            raise PipelineError("no enabled Codex source selected; set sources.<source_id>.enabled to true")
        return enabled

    @property
    def source_identity(self) -> tuple[str, str]:
        # Published storage/evidence identities retain the acquisition application.
        return ("codex", self.source_id)

    @property
    def source_settings(self) -> CodexSourceConfig:
        return self.sources[self.source_id]

    @property
    def source_root(self) -> Path:
        return self.source_settings.source_root

    @property
    def codex_home(self) -> Path:
        """Internal adapter convenience; the public configuration uses source_root."""
        return self.source_root

    @property
    def source_id(self) -> str:
        if self._selected_source is not None:
            return self._selected_source
        names = [name for name, source in self.sources.items() if source.enabled]
        if len(names) != 1:
            raise PipelineError("select exactly one enabled source with --source <source_id>")
        return names[0]

    @property
    def source_provider(self) -> str:
        return "codex"

    def source_storage_paths(self, source_id: str | None = None) -> dict[str, Path]:
        source_id = source_id if source_id is not None else self.source_id
        settings = self.sources[source_id]
        paths = {
            kind: (getattr(settings, kind + "_root") or default_app_root() / kind / "codex" / source_id)
            .expanduser()
            .absolute()
            for kind in ("raw", "data", "state")
        }
        paths["cache"] = self.cache_root.expanduser().absolute() / "codex" / source_id
        return paths

    @property
    def raw_root(self) -> Path:
        return self.source_storage_paths()["raw"]

    @property
    def data_root(self) -> Path:
        return self.source_storage_paths()["data"]

    @property
    def state_root(self) -> Path:
        return self.source_storage_paths()["state"]

    @property
    def source_raw_root(self) -> Path:
        return self.raw_root

    @property
    def source_data_root(self) -> Path:
        return self.data_root

    @property
    def source_state_root(self) -> Path:
        return self.state_root

    @property
    def source_cache_root(self) -> Path:
        return self.source_storage_paths()["cache"]

    @property
    def include_archived(self) -> bool:
        return self.source_settings.include_archived

    def require_supported_chat_sources(self) -> None:
        self.enabled_source_configs()

    @property
    def sessions_root(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def app_state_path(self) -> Path:
        return self.codex_home / ".codex-global-state.json"

    @property
    def registry_path(self) -> Path:
        return self.source_data_root / "project-registry.jsonl"

    @property
    def projects_data_root(self) -> Path:
        return self.source_data_root / "projects"

    @property
    def projects_state_root(self) -> Path:
        return self.source_state_root / "projects"

    @property
    def reports_root(self) -> Path:
        return self.source_state_root / "reports"

    @property
    def provider(self) -> str:
        return self.active_provider_config.provider

    @property
    def active_provider_config(self) -> GenAIProfileConfig:
        return self.genai_profiles[self.generation.genai_profile]

    @property
    def model(self) -> str:
        return self.active_provider_config.model

    @property
    def reasoning_effort(self) -> str:
        return self.active_provider_config.reasoning_effort

    def _provider_executable(self, provider: str, default: str) -> str:
        settings = self.active_provider_config if self.provider == provider else None
        return settings.executable if settings is not None and settings.executable is not None else default

    @property
    def codex_executable(self) -> str:
        return self._provider_executable("codex", "codex")

    @property
    def claude_executable(self) -> str:
        return self._provider_executable("claude-code", "claude")

    @property
    def copilot_executable(self) -> str:
        return self._provider_executable("github-copilot", "copilot")

    @property
    def ollama_base_url(self) -> str:
        settings = self.active_provider_config if self.provider == "ollama" else None
        if settings is not None and settings.endpoint is not None:
            return settings.endpoint
        return "http://127.0.0.1:11434"

    @property
    def reviews(self) -> ReviewSettings:
        return self.source_settings.reviews

    @property
    def review_data_root(self) -> Path:
        return self.data_root / "reviews"

    @property
    def review_state_root(self) -> Path:
        return self.state_root / "reviews"

    def inference_config(self) -> InferenceConfig:
        return InferenceConfig(
            provider=self.provider,
            generation_profile=self.generation.genai_profile,
            codex_bin=self.codex_executable,
            claude_bin=self.claude_executable,
            copilot_bin=self.copilot_executable,
            ollama_base_url=self.ollama_base_url,
            inference_options=self.active_provider_config.inference_options(self.generation.genai_profile),
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            runtime_minutes=self.runtime_minutes,
            model_timeout_seconds=self.model_timeout_seconds,
        )


def global_config_path() -> Path:
    return default_app_root() / "config.yaml"


def project_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ".tkn" / "config.yaml"


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]  # PyYAML has no bundled type stubs.
    """Reject duplicate YAML keys instead of silently discarding a source."""


def _unique_mapping(loader: _UniqueKeyLoader, node: Any) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise PipelineError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _read_layer(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise PipelineError(f"cannot read config: {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PipelineError(f"config must contain a YAML mapping: {path}")
    return {str(key): item for key, item in value.items()}


def _leaf_paths(value: Any, prefix: str = "") -> tuple[str, ...]:
    if isinstance(value, dict) and value:
        return tuple(
            child
            for key, item in value.items()
            for child in _leaf_paths(item, f"{prefix}.{key}" if prefix else str(key))
        )
    return (prefix,)


def _mark_sources(sources: dict[str, str], value: dict[str, Any], label: str) -> None:
    for path in _leaf_paths(value):
        if path:
            sources[path] = label


def _deep_merge(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        current = target.get(key)
        if key == "genai_profiles" and isinstance(value, dict) and isinstance(current, dict):
            # Profiles merge by name; a different provider replaces the whole connection.
            for name, settings in value.items():
                previous = current.get(name)
                if (isinstance(settings, dict) and isinstance(previous, dict)
                        and settings.get("provider", previous.get("provider")) == previous.get("provider")):
                    _deep_merge(previous, settings)
                else:
                    current[name] = deepcopy(settings)
        elif key == "sources" and value == {}:
            target[key] = {}
        elif isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            target[key] = value


def _default_config_document() -> dict[str, Any]:
    document = AppConfig().model_dump(mode="python", by_alias=True)
    # Defer source defaults until validation: an explicit map must not acquire a
    # hidden built-in source. Later layers merge declared sources by stable ID.
    document.pop("sources")
    return document


def _inspect_config_schema(value: dict[str, Any], path: Path) -> dict[str, Any]:
    """Accept only this application's schema; older files are not converted."""
    if "schema_version" not in value:
        raise PipelineError(
            f'configuration schema_version is required: {path}; set schema_version: "{CONFIG_SCHEMA_VERSION}"'
        )
    raw_version = value["schema_version"]
    if not isinstance(raw_version, str) or not _CONFIG_SCHEMA_VERSION_PATTERN.fullmatch(raw_version):
        raise PipelineError(
            f"invalid configuration schema_version {raw_version!r}: {path}; "
            f'expected a quoted MAJOR.MINOR.PATCH value such as "{CONFIG_SCHEMA_VERSION}"'
        )
    current_major, current_minor, _current_patch = _CONFIG_SCHEMA_VERSION_PARTS
    major, minor, _patch = (int(part) for part in raw_version.split("."))
    if major > current_major or (major == current_major and minor > current_minor):
        raise PipelineError(
            f"unsupported newer configuration schema_version {raw_version!r}: {path}; "
            f"this application supports schema {CONFIG_SCHEMA_VERSION}; upgrade tkn-genai-chat-note"
        )
    if major != current_major:
        raise PipelineError(
            f"unsupported older configuration schema_version {raw_version!r}: {path}; this application "
            f"reads schema {CONFIG_SCHEMA_VERSION} only and does not convert older files; write the file "
            "for this schema (`config init` creates an example)"
        )
    return {
        "schemaVersion": raw_version,
        "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
    }


def _config_properties(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("schema_version", None)
    return result


def _resolve_paths(value: dict[str, Any], base: Path) -> dict[str, Any]:
    result = deepcopy(value)

    def resolve(raw: Any) -> Path:
        expanded_text = os.path.expandvars(str(raw))
        if expanded_text == "~":
            expanded = Path.home()
        elif expanded_text.startswith(("~/", "~\\")):
            expanded = Path.home() / expanded_text[2:]
        else:
            expanded = Path(expanded_text).expanduser()
        return expanded if expanded.is_absolute() else (base / expanded).absolute()

    for key in ("raw_root", "data_root", "state_root", "cache_root", "report_path"):
        if result.get(key) is not None:
            result[key] = resolve(result[key])
    for section, keys in (("processes", ("data_root", "state_root")), ("vault", ("root",))):
        settings = result.get(section)
        if isinstance(settings, dict):
            for key in keys:
                if isinstance(settings.get(key), str) and settings[key].strip():
                    settings[key] = resolve(settings[key])
    chat_sources = result.get("chat_sources")
    if isinstance(chat_sources, dict):
        for settings in chat_sources.values():
            if isinstance(settings, dict):
                for key in ("root", "archive"):
                    if isinstance(settings.get(key), str) and settings[key].strip():
                        settings[key] = resolve(settings[key])
    profiles = result.get("genai_profiles")
    if isinstance(profiles, dict):
        for settings in profiles.values():
            cli = settings.get("cli") if isinstance(settings, dict) else None
            executable = cli.get("executable") if isinstance(cli, dict) else None
            # A bare command name stays a PATH lookup; a path is relative to its config file.
            if isinstance(cli, dict) and isinstance(executable, str) and (
                    Path(executable).is_absolute() or executable.startswith("~")
                    or "/" in executable or "\\" in executable):
                cli["executable"] = str(resolve(executable))
    source_map = result.get("sources")
    if isinstance(source_map, dict):
        if len({str(key).casefold() for key in source_map}) != len(source_map):
            raise PipelineError("source_id keys must be unique ignoring case")
        for settings in source_map.values():
            if isinstance(settings, dict):
                for key in ("source_root", "raw_root", "data_root", "state_root"):
                    if settings.get(key) is not None:
                        if isinstance(settings[key], str) and not settings[key].strip():
                            raise PipelineError("source paths must not be empty")
                        settings[key] = resolve(settings[key])
    return result


def _apply_runtime_overrides(
    merged: dict[str, Any],
    overrides: dict[str, Any],
    *,
    working: Path,
    sources: dict[str, str],
) -> None:
    if "schema_version" in overrides:
        raise PipelineError("schema_version is configuration-source metadata and cannot be a CLI override")
    resolved = _resolve_paths(overrides, working)
    options = {key: resolved.pop(key) for key in tuple(resolved)
               if key in GENERATION_OPTION_KEYS or key == "profile"}
    _deep_merge(merged, resolved)
    _mark_sources(sources, resolved, "CLI option")
    if not options:
        return
    profiles = merged.get("genai_profiles")
    generation = merged.get("generation")
    if not isinstance(profiles, dict) or not isinstance(generation, dict):
        raise PipelineError("genai_profiles and generation must be YAML mappings")
    requested = options.get("profile")
    provider = options.get("provider")
    if requested is not None and provider is not None:
        raise PipelineError("use either --profile or --provider, not both")
    name = str(requested or generation.get("genai_profile", "codex-default"))
    if provider is not None:
        if provider not in PROVIDER_NAMES:
            raise PipelineError(f"unsupported inference provider: {provider}")
        matches = [key for key, item in profiles.items()
                   if isinstance(item, dict) and item.get("provider", "codex") == provider]
        declared = [key for key in matches if any(
            path.startswith(f"genai_profiles.{key}.") and label != "built-in defaults"
            for path, label in sources.items())]
        # A connection the user configured wins over a built-in default of the same provider.
        matches = declared or matches
        if len(matches) > 1:
            raise PipelineError(f"multiple genai_profiles use {provider!r}: {', '.join(matches)}; select --profile")
        if matches:
            name = matches[0]
        else:
            if provider == "azure-openai":
                raise PipelineError("define an azure-openai connection under genai_profiles first")
            name = str(provider)
            if name in profiles:
                raise PipelineError(f"GenAI profile name {name!r} already belongs to another provider; use --profile")
            profiles[name] = {"provider": provider}
            _mark_sources(sources, {"genai_profiles": {name: profiles[name]}}, "CLI option")
    if name not in profiles:
        raise PipelineError(f"unknown GenAI profile: {name}; define genai_profiles.{name}")
    settings = profiles[name]
    if not isinstance(settings, dict):
        raise PipelineError(f"genai_profiles.{name} must be a YAML mapping")
    if requested is not None or provider is not None:
        generation["genai_profile"] = name
        sources["generation.genai_profile"] = "CLI option"
    for field, value in (("model", options.get("model")), ("reasoning_effort", options.get("reasoning_effort"))):
        if value is not None:
            settings[field] = value
            sources[f"genai_profiles.{name}.{field}"] = "CLI option"
    kind = settings.get("provider", "codex")
    for option, required in TRANSPORT_OPTIONS.items():
        if option in options:
            if kind != required:
                raise PipelineError(f"--{option.replace('_', '-')} requires an active {required} GenAI profile")
            cli = settings.get("cli")
            settings["cli"] = {**(cli if isinstance(cli, dict) else {}), "executable": options[option]}
            sources[f"genai_profiles.{name}.cli.executable"] = "CLI option"
    if "ollama_base_url" in options:
        if kind != "ollama":
            raise PipelineError("--ollama-base-url requires an active ollama GenAI profile")
        ollama = settings.get("ollama")
        settings["ollama"] = {**(ollama if isinstance(ollama, dict) else {}), "base_url": options["ollama_base_url"]}
        sources[f"genai_profiles.{name}.ollama.base_url"] = "CLI option"


def resolve_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> ConfigResolution:
    """Load defaults, global, project, explicit, then CLI overrides."""

    working = (cwd or Path.cwd()).absolute()
    merged = _default_config_document()
    sources: dict[str, str] = {}
    _mark_sources(sources, _config_properties(merged), "built-in defaults")
    layer_specs = [
        ("global", global_config_path()),
        ("project", project_config_path(working)),
        (
            "explicit",
            explicit_path.expanduser().absolute() if explicit_path else None,
        ),
    ]
    layer_report: list[dict[str, Any]] = [
        {
            "kind": "built-in",
            "path": None,
            "exists": True,
            "schemaVersion": CONFIG_SCHEMA_VERSION,
            "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
        }
    ]
    for kind, path in layer_specs:
        if path is None:
            continue
        report: dict[str, Any] = {
            "kind": kind,
            "path": str(path),
            "exists": path.is_file(),
            "schemaVersion": None,
            "effectiveSchemaVersion": None,
        }
        layer_report.append(report)
        if kind == "explicit" and not report["exists"]:
            raise PipelineError(f"explicit configuration file not found: {path}")
        if report["exists"]:
            raw_layer = _read_layer(path)
            report.update(_inspect_config_schema(raw_layer, path))
            layer = _config_properties(raw_layer)
            try:
                validate_config_layer(AppConfig, layer)
            except ValueError as exc:
                raise PipelineError(f"invalid configuration layer {path}: {exc}") from exc
            resolved_layer = _resolve_paths(layer, path.parent)
            _deep_merge(merged, resolved_layer)
            _mark_sources(sources, resolved_layer, f"{kind}: {path}")
    _apply_runtime_overrides(
        merged,
        overrides or {},
        working=working,
        sources=sources,
    )
    try:
        config = AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    resolved = config.model_copy(
        update={
            "cache_root": config.cache_root.expanduser().absolute(),
        }
    )
    effective_leaves = _leaf_paths(_config_properties(resolved.model_dump(mode="python", by_alias=True)))
    sources = {key: sources.get(key, "built-in defaults") for key in effective_leaves}
    return ConfigResolution(
        config=resolved,
        sources=sources,
        layers=tuple(layer_report),
        effective_schema_version=CONFIG_SCHEMA_VERSION,
    )


def load_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load and return the effective application configuration."""

    return resolve_app_config(
        explicit_path=explicit_path,
        cwd=cwd,
        overrides=overrides,
    ).config


def config_example_text() -> str:
    """Read the application-owned example distributed in the package."""

    try:
        return files(__package__ or "").joinpath(CONFIG_EXAMPLE_RESOURCE).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise PipelineError(f"packaged config example is unavailable: {exc}") from exc


def initialize_user_config(
    path: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create the user config safely from the packaged example."""

    target = (path or global_config_path()).expanduser().absolute()
    example = config_example_text()
    expected = example.encode("utf-8")
    backup_path: Path | None = None

    if target.exists() and not target.is_file():
        raise PipelineError(f"config target is not a file: {target}")
    if target.is_file():
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise PipelineError(f"cannot read config: {target}: {exc}") from exc
        if current == expected:
            return {
                "status": "unchanged",
                "configPath": str(target),
                "backupPath": None,
            }
        if not force:
            raise PipelineError(
                f"config already exists with different content: {target}; "
                "review it or use `config init --force` to back it up and replace it"
            )
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
        backup_path = target.with_name(f"{target.name}.{stamp}.bak")
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            raise PipelineError(f"cannot back up config: {target}: {exc}") from exc

    try:
        atomic_write_text(target, example)
    except OSError as exc:
        raise PipelineError(f"cannot write config: {target}: {exc}") from exc
    return {
        "status": "replaced" if backup_path else "created",
        "configPath": str(target),
        "backupPath": str(backup_path) if backup_path else None,
    }


def validate_config_file_document(value: Any, path: Path) -> AppConfig:
    """Validate one configuration file's content as if it were the only user layer."""
    if not isinstance(value, dict):
        raise PipelineError(f"config must contain a YAML mapping: {path}")
    value = {str(key): item for key, item in value.items()}
    _inspect_config_schema(value, path)
    layer = _config_properties(value)
    try:
        validate_config_layer(AppConfig, layer)
    except ValueError as exc:
        raise PipelineError(f"invalid configuration layer {path}: {exc}") from exc
    merged = _default_config_document()
    _deep_merge(merged, _resolve_paths(layer, path.parent))
    try:
        return AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc


def config_document(config: AppConfig) -> dict[str, Any]:
    document = config.model_dump(mode="json", by_alias=True)
    for source_id, settings in document["sources"].items():
        for kind, path in config.source_storage_paths(source_id).items():
            if kind != "cache":
                settings[kind + "_root"] = str(path)
    return document


def write_config(config: AppConfig, target: Path) -> None:
    document = config_document(config)
    schema_version = document.pop("schema_version")
    text = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
    )
    atomic_write_text(target, f'schema_version: "{schema_version}"\n{text}')
