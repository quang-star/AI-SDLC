"""Select named model routes for the spike and future SDLC adapters.

Bare model IDs always use 9Router. Explicit LangChain provider prefixes remain
available for callers that intentionally select a direct provider.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTarget:
    label: str
    model: str


def normalize_model(model: str) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Model ID must be a non-empty string.")
    model = model.strip()
    if any(char.isspace() for char in model):
        raise ValueError("Model ID cannot contain whitespace.")
    if ":" not in model:
        return f"9router:{model}"
    provider, model_id = model.split(":", 1)
    if not provider or not model_id:
        raise ValueError("A model ID must follow `9router:<model-id>` or `provider:<model-id>`.")
    return f"{provider.lower()}:{model_id}"


def select_model_targets(
    *,
    model: str | None,
    models: Sequence[str] | None,
    roles: Sequence[str] | None,
    env: Mapping[str, str],
) -> tuple[ModelTarget, ...]:
    """CLI selection wins over environment; role maps preserve declared order."""
    if sum(value is not None for value in (model, models, roles)) > 1:
        raise ValueError("Choose only one of --model, --models, or --roles.")
    if model is not None:
        return (ModelTarget("default", normalize_model(model)),)
    if models is not None:
        if not models:
            raise ValueError("--models requires at least one model ID.")
        return tuple(ModelTarget(f"model_{i + 1}", normalize_model(value)) for i, value in enumerate(models))

    # An explicit role selection ignores the single-model environment default.
    single = env.get("NINE_ROUTER_MODEL", env.get("DEEPAGENTS_MODEL"))
    if roles is None and single is not None:
        return (ModelTarget("default", normalize_model(single)),)

    raw_roles = env.get("NINE_ROUTER_ROLE_MODELS")
    if raw_roles is None:
        raise ValueError("Set NINE_ROUTER_MODEL, NINE_ROUTER_ROLE_MODELS, or pass --model / --models.")
    try:
        role_map = json.loads(raw_roles)
    except (ValueError, TypeError):
        raise ValueError("NINE_ROUTER_ROLE_MODELS must be a JSON object of role -> model ID.") from None
    if not isinstance(role_map, dict) or not role_map:
        raise ValueError("NINE_ROUTER_ROLE_MODELS must be a non-empty JSON object.")
    targets: dict[str, ModelTarget] = {}
    for role, value in role_map.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role):
            raise ValueError("Role names must be lowercase identifiers (max 32 characters).")
        targets[role] = ModelTarget(role, normalize_model(value))
    selected = tuple(roles) if roles is not None else tuple(targets)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Select at least one role, without duplicates.")
    if any(role not in targets for role in selected):
        raise ValueError("Requested role is missing from NINE_ROUTER_ROLE_MODELS.")
    return tuple(targets[role] for role in selected)
