"""Commercial SaaS subscription plans and seed helpers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ai_sdlc_saas.db.models import Plan


@dataclass(frozen=True)
class PlanSpec:
    id: str
    name: str
    monthly_price_cents: int
    included_credits_cents: int
    max_seats: int
    max_repos: int
    max_concurrent_tasks: int
    allowed_model_tiers: tuple[str, ...]


DEFAULT_PLANS: tuple[PlanSpec, ...] = (
    PlanSpec(
        id="trial",
        name="Free Trial",
        monthly_price_cents=0,
        included_credits_cents=1_000,  # $10.00 trial credit
        max_seats=3,
        max_repos=2,
        max_concurrent_tasks=2,
        allowed_model_tiers=("standard", "fast"),
    ),
    PlanSpec(
        id="pro",
        name="Pro",
        monthly_price_cents=4_900,  # $49/mo
        included_credits_cents=5_000,  # $50.00 included credits
        max_seats=10,
        max_repos=10,
        max_concurrent_tasks=5,
        allowed_model_tiers=("standard", "fast", "frontier"),
    ),
    PlanSpec(
        id="team",
        name="Team",
        monthly_price_cents=19_900,  # $199/mo
        included_credits_cents=25_000,  # $250.00 included credits
        max_seats=50,
        max_repos=50,
        max_concurrent_tasks=15,
        allowed_model_tiers=("standard", "fast", "frontier", "byok"),
    ),
    PlanSpec(
        id="enterprise",
        name="Enterprise",
        monthly_price_cents=99_900,  # $999/mo
        included_credits_cents=150_000,  # $1,500.00 included credits
        max_seats=500,
        max_repos=500,
        max_concurrent_tasks=50,
        allowed_model_tiers=("standard", "fast", "frontier", "byok"),
    ),
)


def seed_default_plans(session: Session) -> None:
    """Ensure built-in commercial plans exist idempotently."""
    for spec in DEFAULT_PLANS:
        existing = session.get(Plan, spec.id)
        if existing is None:
            session.add(
                Plan(
                    id=spec.id,
                    name=spec.name,
                    monthly_price_cents=spec.monthly_price_cents,
                    included_credits_cents=spec.included_credits_cents,
                    max_seats=spec.max_seats,
                    max_repos=spec.max_repos,
                    max_concurrent_tasks=spec.max_concurrent_tasks,
                    allowed_model_tiers_json=list(spec.allowed_model_tiers),
                )
            )
    session.flush()
