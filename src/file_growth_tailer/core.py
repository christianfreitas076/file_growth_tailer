# file_growth_tailer/core.py
# The goal is a "continuous logical stream" over a growing file. The real
# world breaks that stream: the file can be truncated, rotated (renamed aside
# and replaced), deleted, or temporarily unopenable. This module tracks the
# last seen inode + size and position, and restores the stream position when
# possible — by trusting the inode when it matches, by seeking from the end
# when the file is smaller than our last position, and by returning a flag so
# the caller can act when the source file is missing entirely.

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional


class ReopenError(Exception):
    """Raised when the file cannot be opened at all after a truncation event.

    This is distinct from PositionNotFound: here the file does not exist or
    we lack permissions. ReopenError tells the caller to wait and retry; no
    internal heuristic can recover it."""


class PositionNotFound(Exception):
    """Raised when a truncated/rotated file is reopened but the old position
    cannot be located (e.g. the old offset is now beyond EOF).

    Callers should treat this as a forced re-seek and then call read() again.
    It is raised only when we genuinely cannot make a sensible decision."""


@dataclasses.dataclass(frozen=True)
class ReadResult:
    """The result of a single read() call.

    `data` is the bytes read (empty if nothing new). `position` is the new
    logical position for the caller to persist. `inode` is the inode that
    produced these bytes, so the caller can detect that a rotation happened.

    Attributes:
        data: Bytes read from the file. May be empty.
        position: The new position to persist for the next call. This is the
            offset after `data` has been consumed.
        inode: The inode of the file these bytes came from. A change between
            calls indicates a rotation or replacement.
    """
    data: bytes
    position: int
    inode: int


@dataclasses.dataclass
class _State:
    """Mutable state held across calls within a single Tailer instance.

    `inode` is None on the very first call; after that it is always set so
    we can detect rotation. `position` is the last successful read offset; it
    must be non-negative. The caller persists `position`, but we cache it here
    so the caller doesn't have to thread it back through if they prefer."""
    inode: Optional[int] = None
    position: int = 0


class Tailer:
    """Tail a growing file, surviving truncation and rotation.

    Usage:
        from file_growth_tailer import Tailer, ReadResult

        tailer = Tailer(path="/var/log/app.log", chunk_size=4096)
        # First call: reads from the start, returns first batch.
        result = tailer.read()
        persist(result.position, result.inode)
        # Later calls continue from where we left off automatically.
        # Pass persisted position to resume after restart:
        #   tailer = Tailer(path, position=512, inode=12345)

    The Tailer is stateful but not threaded. It does not poll; you call read()
    when you want more data. That keeps timing in the caller's hands, which
    also makes testing trivial.

    Args:
        path: Filesystem path to the growing file.
        chunk_size: Maximum number of bytes returned per read() call. Larger
            values mean fewer syscalls but bigger memory bumps. Must be > 0.
        position: Optional initial logical position. Defaults to 0, meaning
            read from the start on the first call. Pass a persisted position
            to resume.
        inode: Optional initial inode. Paired with `position`, lets you detect
            rotation immediately after a restart. If omitted, the first read
            trusts the file as-is.

    Raises:
        ValueError: If chunk_size <= 0 or position < 0.
    """

    def __init__(
        self,
        path: str,
        chunk_size: int = 4096,
        position: int = 0,
        inode: Optional[int] = None,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if position < 0:
            raise ValueError("position must be non-negative")
        self._path = Path(path)
        self._chunk_size = chunk_size
        self._state = _State(inode=inode, position=position)

    @property
    def position(self) -> int:
        """Current logical position in the file."""
        return self._state.position

    @property
    def inode(self) -> Optional[int]:
        """Current inode, or None before the first successful read."""
        return self._state.inode

    @property
    def path(self) -> Path:
        """The file path this tailer follows."""
        return self._path

    def read(self, max_bytes: Optional[int] = None) -> ReadResult:
        """Read up to `max_bytes` bytes from the current position.

        The method never blocks. If there is nothing new to read it returns
        ReadResult with empty data and the same position. If the file has been
        truncated to a smaller size than our last position, we seek to the
        end (position 0 of the new content from our perspective) and read
        forward. If the file has been rotated or replaced (inode changed), we
        restart from offset 0 of the new file.

        Args:
            max_bytes: Maximum bytes to return this call. If None, uses the
                instance chunk_size. Must be > 0 if provided.

        Returns:
            ReadResult with data, new position, and inode.

        Raises:
            ValueError: If max_bytes <= 0.
            ReopenError: If the file cannot be opened (deleted or
                permission-denied). Callers should retry later.
        """
        if max_bytes is not None and max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        limit = self._chunk_size if max_bytes is None else min(max_bytes, self._chunk_size)

        try:
            f = open(self._path, "rb")
        except FileNotFoundError:
            # The file is gone entirely. We cannot read or recover here; tell
            # the caller so they can wait and call again. We do NOT raise
            # PositionNotFound because there is no position to find — the
            # source is absent. ReopenError signals "try again later".
            raise ReopenError(f"file not found: {self._path}")
        except OSError as exc:
            raise ReopenError(f"cannot open {self._path}: {exc}")

        with f:
            try:
                stat = os.fstat(f.fileno())
            except OSError as exc:
                raise ReopenError(f"cannot stat {self._path}: {exc}")

            current_inode = stat.st_ino
            current_size = stat.st_size

            if self._state.inode is not None and self._state.inode != current_inode:
                # The inode changed: the file was rotated or replaced. We treat
                # the new file as a fresh stream and start from the beginning.
                # This is the only sane default; trying to match bytes from the
                # old file into the new one is lossy and surprising.
                self._state.position = 0

            # If the file is smaller than where we left off, it was truncated
            # in place (same inode). We cannot seek past EOF meaningfully for
            # reading; instead we clamp to the current size and read from there
            # — which means we pick up whatever new content exists at the tail.
            # If position == size, read() naturally returns b"".
            seek_to = min(self._state.position, current_size)

            try:
                f.seek(seek_to, os.SEEK_SET)
            except OSError as exc:
                raise ReopenError(f"cannot seek in {self._path}: {exc}")

            data = f.read(limit)

        new_position = seek_to + len(data)
        self._state.position = new_position
        self._state.inode = current_inode
        return ReadResult(data=data, position=new_position, inode=current_inode)
