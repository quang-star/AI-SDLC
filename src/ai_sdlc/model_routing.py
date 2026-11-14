"""Select named model routes for the AI SDLC spikes and adapters.

Bare model IDs always use 9Router. Explicit LangChain provider prefixes remain
available for callers that intentionally select a direct provider.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

DEFAULT_ROUTER_BASE_URL: Final[str] = "http://localhost:20128/v1"


@dataclass(frozen=True)
class ModelTarget:
    label: str
    model: str


@dataclass(frozen=True)
class RouterConnectionConfig:
    """Validated 9Router OpenAI-compatible gateway configuration."""

    base_url: str
    api_key: str
    model_id: str
    role: str

    def __repr__(self) -> str:
        return (
            f"RouterConnectionConfig(base_url={self.base_url!r}, "
            f"api_key='***', model_id={self.model_id!r}, role={self.role!r})"
        )


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


def resolve_role_router_config(
    *,
    role: str = "ba",
    model: str | None = None,
    env: Mapping[str, str] | None = None,
) -> RouterConnectionConfig:
    """Resolve validated 9Router connection details and bare router model ID for a named role."""
    environ = os.environ if env is None else env
    if model is not None:
        target = select_model_targets(model=model, models=None, roles=None, env=environ)[0]
    elif "NINE_ROUTER_ROLE_MODELS" in environ:
        target = select_model_targets(model=None, models=None, roles=(role,), env=environ)[0]
    elif "NINE_ROUTER_MODEL" in environ or "DEEPAGENTS_MODEL" in environ:
        target = select_model_targets(model=None, models=None, roles=None, env=environ)[0]
    else:
        raise ValueError(
            f"Set NINE_ROUTER_ROLE_MODELS (with `{role}`), NINE_ROUTER_MODEL, or pass --model."
        )

    if not target.model.startswith("9router:"):
        raise ValueError(
            f"Role {role!r} resolved to {target.model!r}, expected a 9Router route (`9router:<model-id>`)."
        )
    bare_model_id = target.model.split(":", 1)[1].strip()
    if not bare_model_id:
        raise ValueError("9Router requires a non-empty model ID.")

    base_url = (
        environ.get("NINE_ROUTER_BASE_URL")
        or environ.get("OPENAI_BASE_URL")
        or DEFAULT_ROUTER_BASE_URL
    ).strip().rstrip("/")
    try:
        parsed = urlparse(base_url)
        valid_url = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError(
            "NINE_ROUTER_BASE_URL / OPENAI_BASE_URL must be an http(s) URL without credentials, query or fragment."
        )

    api_key = (environ.get("NINE_ROUTER_API_KEY") or environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "9Router requires NINE_ROUTER_API_KEY or OPENAI_API_KEY (router key, not upstream provider key)."
        )

    return RouterConnectionConfig(
        base_url=base_url,
        api_key=api_key,
        model_id=bare_model_id,
        role=role,
    )
