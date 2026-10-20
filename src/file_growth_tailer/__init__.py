# file_growth_tailer/__init__.py
# This module re-exports the public API so callers can import directly from the
# package root. Keeping the implementation in core.py separates the public
# surface from internal logic, making it trivial to evolve without breaking
# imports.
from .core import (
    Tailer,
    ReadResult,
    ReopenError,
    PositionNotFound,
)

__all__ = [
    "Tailer",
    "ReadResult",
    "ReopenError",
    "PositionNotFound",
]

__version__ = "1.0.0"
