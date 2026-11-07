"""Non-destructive storage initialization and OS-released pipeline locks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .archive_reader import validate_archive_marker
from .config import AppConfig, global_config_path
from .initialization import (
    ROOT_KINDS,
    ROOT_OWNERSHIP_MARKER,
    inspect_reset_target_ownership,
    validate_reset_targets,
)
from .locks import exclusive_lock
from .raw_capture import RawCaptureError, archive_managed
from .runtime import PipelineError, atomic_write_json, now_iso

STORAGE_VERSION = 5


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot read pipeline state: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"pipeline state must be an object: {path}")
    return value


def validate_source_layout(config: AppConfig) -> None:
    """Check all configured outputs and enabled inputs before any source writes."""
    output_roots = [
        ("codex", source_id, kind, path.resolve())
        for source_id in config.sources
        for kind, path in config.source_storage_paths(source_id).items()
    ]
    input_roots = [
        ("codex", source_id, source.source_root.expanduser().resolve())
        for source_id, source in config.sources.items()
        if source.enabled
    ]
    for index, (provider, source_id, root) in enumerate(input_roots):
        for other_provider, other_id, other in input_roots[index + 1 :]:
            if root == other or root.is_relative_to(other) or other.is_relative_to(root):
                raise PipelineError(
                    f"chat source roots overlap: {provider}/{source_id} and "
                    f"{other_provider}/{other_id}; register each input directory once"
                )
    for index, (provider, source_id, kind, root) in enumerate(output_roots):
        for other_provider, other_id, other_kind, other in output_roots[index + 1 :]:
            if root == other or root.is_relative_to(other) or other.is_relative_to(root):
                raise PipelineError(
                    f"configured source roots overlap: {provider}/{source_id}/{kind} and "
                    f"{other_provider}/{other_id}/{other_kind}"
                )
        for _provider, _source_id, source_root in input_roots:
            if root == source_root or root.is_relative_to(source_root) or source_root.is_relative_to(root):
                raise PipelineError("configured output root overlaps a chat source root")


def validate_storage(config: AppConfig, config_path: Path | None = None) -> tuple[Path, ...]:
    config.require_supported_chat_sources()
    roots = validate_reset_targets(config, config_path or global_config_path())
    for path in (config.data_root, config.state_root, config.raw_root, config.source_cache_root):
        if path.is_symlink():
            raise PipelineError(f"pipeline root must not be a symbolic link: {path}")
    archive = archive_managed(config.raw_root)
    if archive:
        # The archiver owns raw_root; its marker replaces this pipeline's Raw ownership marker.
        try:
            validate_archive_marker(config.raw_root.expanduser().absolute(), config.source_id)
        except RawCaptureError as exc:
            raise PipelineError(str(exc)) from exc
    for ownership in inspect_reset_target_ownership(roots):
        if archive and ownership["kind"] == "raw":
            continue
        if ownership["status"] not in {"missing", "empty", "owned"}:
            raise PipelineError(f"refusing unowned or invalid pipeline root: {ownership['path']}; choose fresh roots")
    for kind, namespace in config.source_storage_paths().items():
        for directory in (namespace,):
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                raise PipelineError(f"{kind} source namespace must not be a link: {directory}")
            if directory.exists() and not directory.is_dir():
                raise PipelineError(f"{kind} source namespace must be a directory: {directory}")
    identity = {"sourceProvider": config.source_provider, "sourceId": config.source_id}
    for root in roots:
        marker = read_json(root / ROOT_OWNERSHIP_MARKER)
        if marker and any(marker.get(key) != value for key, value in identity.items()):
            raise PipelineError(f"storage root belongs to a different source identity or names none: {root}")
    descriptor = read_json(config.data_root / "store.json")
    if descriptor and (
        descriptor.get("schemaVersion") != "1.0.0"
        or descriptor.get("storageVersion") != STORAGE_VERSION
        or descriptor.get("rawRefPrefix") != f"raw:/{config.source_provider}/{config.source_id}/"
        or any(descriptor.get(key) != value for key, value in identity.items())
    ):
        raise PipelineError("store descriptor has an unsupported version or different source identity")
    validate_source_layout(config)
    metadata = read_json(config.state_root / "pipeline.json")
    if metadata.get("storageVersion") == STORAGE_VERSION and not descriptor:
        raise PipelineError("source store descriptor is missing; restore store.json before processing")
    return roots


@contextmanager
def _root_lock(root: Path) -> Iterator[None]:
    with exclusive_lock(root / ".pipeline.lock", f"another pipeline run holds the storage lock: {root}"):
        yield


@contextmanager
def pipeline_storage(
    config: AppConfig,
    *,
    initialize: bool,
    dry_run: bool,
    config_path: Path | None = None,
) -> Iterator[None]:
    roots = validate_storage(config, config_path)
    metadata_path = config.source_state_root / "pipeline.json"
    metadata = read_json(metadata_path)
    if metadata and metadata.get("storageVersion") != STORAGE_VERSION:
        raise PipelineError("unsupported pipeline storage version; choose fresh configured roots")
    if not metadata and not initialize:
        raise PipelineError("pipeline is not initialized; run `tkn-genai-chat-note clone` first")
    if metadata and (
        metadata.get("sourceId") != config.source_id or metadata.get("sourceProvider") != config.source_provider
    ):
        raise PipelineError("source identity differs from this namespace")
    if dry_run:
        yield
        return
    # An archive-managed raw_root is read only: no directory, marker, or lock is written there.
    # Confirmed archive manifests and their versions never change, so reading needs no lock.
    archive = archive_managed(config.raw_root)
    owned = [(kind, root) for kind, root in zip(ROOT_KINDS, roots, strict=True) if not (archive and kind == "raw")]
    for kind, root in owned:
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ROOT_OWNERSHIP_MARKER
        if not marker.exists():
            atomic_write_json(
                marker,
                {
                    "schemaVersion": 1,
                    "applicationId": "tkn-genai-chat-note-pipeline",
                    "rootKind": kind,
                    "sourceProvider": config.source_provider,
                    "sourceId": config.source_id,
                },
            )
    with ExitStack() as stack:
        for root in sorted(root for _kind, root in owned):
            stack.enter_context(_root_lock(root))
        validate_storage(config, config_path)
        descriptor = config.data_root / "store.json"
        if not descriptor.exists():
            atomic_write_json(
                descriptor,
                {
                    "schemaVersion": "1.0.0",
                    "storageVersion": STORAGE_VERSION,
                    "sourceProvider": config.source_provider,
                    "sourceId": config.source_id,
                    "rawRefPrefix": f"raw:/{config.source_provider}/{config.source_id}/",
                    "dataRefAliases": {},
                    "rawRefAliases": [],
                },
            )
        # Check again under the lock; clone never resets an existing store.
        metadata = read_json(metadata_path)
        if not metadata:
            atomic_write_json(
                metadata_path,
                {
                    "storageVersion": STORAGE_VERSION,
                    "sourceId": config.source_id,
                    "sourceProvider": config.source_provider,
                    "createdAt": now_iso(),
                },
            )
        elif (
            metadata.get("storageVersion") != STORAGE_VERSION
            or metadata.get("sourceId") != config.source_id
            or metadata.get("sourceProvider") != config.source_provider
        ):
            raise PipelineError("pipeline storage changed while acquiring its lock")
        yield
