"""An exclusive, non-blocking lock on a file, released when the process ends."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .runtime import PipelineError


@contextmanager
def exclusive_lock(path: Path, busy: str, *, create_parent: bool = False) -> Iterator[None]:
    """Hold `path` locked; raise PipelineError(`busy`) when another run holds it."""
    if path.is_symlink():
        raise PipelineError(f"lock must not be a symbolic link: {path}")
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PipelineError(busy) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
