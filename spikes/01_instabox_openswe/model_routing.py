"""Select named model routes for the spike and future SDLC adapters.

Re-exports the shared implementation from `ai_sdlc.model_routing` so Spike 01
can be invoked standalone without duplicating route-selection logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from ai_sdlc.model_routing import (  # noqa: E402
    DEFAULT_ROUTER_BASE_URL,
    ModelTarget,
    RouterConnectionConfig,
    normalize_model,
    resolve_role_router_config,
    select_model_targets,
)

__all__ = [
    "DEFAULT_ROUTER_BASE_URL",
    "ModelTarget",
    "RouterConnectionConfig",
    "normalize_model",
    "resolve_role_router_config",
    "select_model_targets",
]
