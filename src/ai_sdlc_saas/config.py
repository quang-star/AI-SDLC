"""Environment-separated configuration for AI SDLC SaaS control plane and workers."""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DEV_JWT = "dev-only-jwt-secret-key-do-not-use-in-production-32bytes"
_DEFAULT_DEV_GH_WEBHOOK = "dev-only-github-webhook-secret"
_DEFAULT_DEV_STRIPE_WEBHOOK = "dev-only-stripe-webhook-secret"


def _derive_dev_fernet_key(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


class Settings(BaseSettings):
    """Application settings with strict dev/staging/production secret checks."""

    model_config = SettingsConfigDict(env_prefix="AI_SDLC_", env_file=".env", extra="ignore")

    env: Literal["development", "dev", "local", "staging", "production", "test"] = "development"
    database_url: str = "sqlite:///./.local_dev/ai_sdlc.db"
    object_store_dir: Path = Path("./.local_dev/object_store")
    worktree_root: Path = Path("./.local_dev/worktrees")
    jwt_secret: str = _DEFAULT_DEV_JWT
    jwt_ttl_minutes: int = 60 * 12
    encryption_key: str = ""
    github_webhook_secret: str = _DEFAULT_DEV_GH_WEBHOOK
    stripe_webhook_secret: str = _DEFAULT_DEV_STRIPE_WEBHOOK
    allow_local_gateway: bool = True
    allow_local_git_repos: bool = True
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    rate_limit_rpm: int = 300
    redis_url: str = ""
    data_region_default: Literal["us-east", "eu-central", "ap-southeast"] = "us-east"
    nine_router_base_url: str = Field(default_factory=lambda: os.environ.get("NINE_ROUTER_BASE_URL", "http://127.0.0.1:20128/v1"))
    nine_router_api_key: str = Field(default_factory=lambda: os.environ.get("NINE_ROUTER_API_KEY", ""))

    def validate_security_posture(self) -> None:
        """Fail closed if staging/production uses default or missing cryptographic secrets."""
        if self.env in ("staging", "production"):
            if self.jwt_secret == _DEFAULT_DEV_JWT or len(self.jwt_secret) < 32:
                raise RuntimeError("Staging/production requires a strong AI_SDLC_JWT_SECRET (>=32 chars).")
            if not self.encryption_key.strip():
                raise RuntimeError("Staging/production requires AI_SDLC_ENCRYPTION_KEY (Fernet key(s)).")
            if self.github_webhook_secret == _DEFAULT_DEV_GH_WEBHOOK:
                raise RuntimeError("Staging/production requires AI_SDLC_GITHUB_WEBHOOK_SECRET.")
            if self.stripe_webhook_secret == _DEFAULT_DEV_STRIPE_WEBHOOK:
                raise RuntimeError("Staging/production requires AI_SDLC_STRIPE_WEBHOOK_SECRET.")
            if self.database_url.startswith("sqlite"):
                raise RuntimeError("Staging/production cannot run on SQLite; configure PostgreSQL DATABASE_URL.")

    def resolved_fernet_keys(self) -> list[str]:
        """Return ordered Fernet keys (first encrypts, all can decrypt during rotation)."""
        raw = self.encryption_key.strip()
        if raw:
            keys = [k.strip() for k in raw.split(",") if k.strip()]
            for key in keys:
                Fernet(key.encode("ascii"))
            return keys
        if self.env in ("staging", "production"):
            raise RuntimeError("Missing AI_SDLC_ENCRYPTION_KEY in staging/production.")
        return [_derive_dev_fernet_key(f"dev-envelope:{self.jwt_secret}")]

    def parsed_cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=4)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_security_posture()
    return settings
