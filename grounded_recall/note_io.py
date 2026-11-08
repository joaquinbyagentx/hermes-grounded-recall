"""Descriptor-relative note reads; no symlinks below a configured root.

Configured roots are trusted administrator input (a root itself may be an alias).
Pin each resolved root once, then open every descendant component with O_NOFOLLOW.
A path check followed by ordinary open() would reintroduce a symlink-swap race.
Platforms without the required primitives fail closed rather than follow links.
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence


class NoteReader:
    def __init__(self, roots: Sequence[Path]):
        self._roots = []
        try:
            self._dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            self._file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        except AttributeError:
            return
        if os.open not in os.supports_dir_fd:
            return
        for root in roots:
            fd = None
            try:
                # Resolve only trusted roots, never paths discovered in the corpus.
                canonical = root.resolve(strict=True)
                fd = os.open(canonical.anchor, self._dir_flags)
                for part in canonical.parts[1:]:
                    child = os.open(part, self._dir_flags, dir_fd=fd)
                    os.close(fd)
                    fd = child
                self._roots.append((root, fd))
                fd = None
            except (OSError, RuntimeError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        for _root, fd in self._roots:
            os.close(fd)
        self._roots.clear()

    @contextmanager
    def open(self, path: Path):
        for root, root_fd in self._roots:
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                continue
            if not parts or any(p in (".", "..") for p in parts):
                continue
            parent = os.dup(root_fd)
            fd = None
            try:
                for part in parts[:-1]:
                    child = os.open(part, self._dir_flags, dir_fd=parent)
                    os.close(parent)
                    parent = child
                fd = os.open(parts[-1], self._file_flags, dir_fd=parent)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("not a regular note")
                stream = os.fdopen(fd, encoding="utf-8", errors="replace")
                fd = None  # stream now owns the descriptor, even if the caller raises
                with stream:
                    yield stream
                return
            finally:
                if fd is not None:
                    os.close(fd)
                os.close(parent)
        raise OSError("note is outside the configured roots or safe reads are unavailable")
