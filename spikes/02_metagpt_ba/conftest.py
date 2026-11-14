"""Pytest configuration ensuring Spike 02 and shared `src` packages resolve."""

from __future__ import annotations

import sys
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_SRC = _SPIKE_DIR.parents[1] / "src"

for path in (_SPIKE_DIR, _REPO_SRC):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
