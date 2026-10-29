# file_growth_tailer

Follows a growing file from the last known position, handling truncation, rotation, and reappearance so the caller receives a continuous logical stream.

## Usage

```python
from file_growth_tailer import Tailer, ReadResult, ReopenError

tailer = Tailer(path="/var/log/app.log", chunk_size=4096)
try:
    result = tailer.read()
    # persist result.position and result.inode somewhere durable
    process(result.data)
except ReopenError:
    # file is gone or unreadable; try again on the next loop tick
    pass
```

To resume after a restart, pass the persisted position and inode:

```python
tailer = Tailer(path="/var/log/app.log", position=last_pos, inode=last_inode)
result = tailer.read()
```

## Why

The standard library gives you `seek` and `read`, but nothing stitches them across the three failure modes of a long-lived log file: the file grows, gets truncated in place, or is rotated out and replaced. Naive tailers lose data on truncation or double-read on rotation. This library exists to make one decision and stick to it: when the inode changes, the stream restarts at offset zero; when the same inode shrinks, the position is clamped to the new size. The caller persists `position` and `inode` between calls, so restarts survive a crash.

The trade-off: we do not try to match bytes across a rotation. If your logs are rotated mid-write, bytes written to the old inode after the rotation are lost to this tailer. That is intentional — recovering them requires filesystem-specific heuristics that are not portable and not worth the complexity for a single-file tailer.

## The awkward edge

`ReopenError` is raised when the file is missing or unreadable. It is not raised on truncation (handled silently by clamping) or rotation (handled silently by resetting). The caller must catch `ReopenError` and retry; there is no internal retry loop, because the appropriate backoff is application-specific.

## Exports

- `Tailer` — the tailer class. Constructor: `Tailer(path, chunk_size=4096, position=0, inode=None)`.
- `ReadResult` — frozen dataclass returned by `read()`: `data: bytes`, `position: int`, `inode: int`.
- `ReopenError` — raised when the file cannot be opened or statted.
- `PositionNotFound` — reserved for future use; currently not raised by `read()`, but exported so callers can catch it defensively if later versions raise it.

## Performance

The window keeps a bounded buffer, so `push` is constant time and memory does not
grow with the length of the stream. `peak` and `trough` are linear in the window
size, which is the trade that keeps `push` cheap.

## Limitations

Values are coerced to floats, so very large integers lose precision. If you need
exact integer aggregates over a window, this is the wrong tool.

