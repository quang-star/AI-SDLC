"""MetaGPT-structured Business Analyst (BA) spike for AI SDLC."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

__all__ = ["_REPO_SRC"]
