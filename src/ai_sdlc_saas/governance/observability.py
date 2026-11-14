"""Prometheus metrics instrumentation and runtime health counters."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.engine import system_routing_scope
from ai_sdlc_saas.db.models import Job, KillSwitch, SandboxResource

METRICS_REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS_TOTAL = Counter(
    "ai_sdlc_http_requests_total",
    "Total HTTP requests served by route template and status code.",
    ["method", "route", "status"],
    registry=METRICS_REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "ai_sdlc_http_request_duration_seconds",
    "HTTP request latency distribution in seconds.",
    ["method", "route"],
    registry=METRICS_REGISTRY,
)

WORKER_JOBS_TOTAL = Counter(
    "ai_sdlc_worker_jobs_total",
    "Total worker jobs executed by job type and outcome.",
    ["job_type", "outcome"],
    registry=METRICS_REGISTRY,
)

LLM_INVOCATIONS_TOTAL = Counter(
    "ai_sdlc_llm_invocations_total",
    "Total LLM role invocations by role, provider, and outcome.",
    ["role", "provider", "outcome"],
    registry=METRICS_REGISTRY,
)

LLM_COST_CENTS_TOTAL = Counter(
    "ai_sdlc_llm_cost_cents_total",
    "Accumulated upstream model spend in cents.",
    ["role", "provider"],
    registry=METRICS_REGISTRY,
)

CLEANUP_SWEEPS_TOTAL = Counter(
    "ai_sdlc_cleanup_sweeps_total",
    "Total sandbox cleanup operations by outcome.",
    ["outcome"],
    registry=METRICS_REGISTRY,
)

ACTIVE_SANDBOXES_GAUGE = Gauge(
    "ai_sdlc_active_sandboxes",
    "Current count of non-deleted sandbox resources.",
    registry=METRICS_REGISTRY,
)

QUEUED_JOBS_GAUGE = Gauge(
    "ai_sdlc_queued_jobs",
    "Current queued or leased background jobs.",
    registry=METRICS_REGISTRY,
)

DEAD_LETTER_JOBS_GAUGE = Gauge(
    "ai_sdlc_dead_letter_jobs",
    "Current failed or dead-letter jobs.",
    registry=METRICS_REGISTRY,
)

ACTIVE_KILL_SWITCHES_GAUGE = Gauge(
    "ai_sdlc_active_kill_switches",
    "Number of currently active emergency kill switches.",
    registry=METRICS_REGISTRY,
)


def record_http_metric(*, method: str, route: str, status_code: int, duration_seconds: float) -> None:
    """Record API request counter and latency histogram."""
    HTTP_REQUESTS_TOTAL.labels(method=method.upper(), route=route, status=str(status_code)).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method.upper(), route=route).observe(max(0.0, duration_seconds))


def record_worker_job_metric(*, job_type: str, outcome: str) -> None:
    """Record worker job execution outcome."""
    WORKER_JOBS_TOTAL.labels(job_type=job_type, outcome=outcome).inc()


def record_llm_metric(*, role: str, provider: str, outcome: str, cost_cents: int = 0) -> None:
    """Record model gateway invocation and spend."""
    LLM_INVOCATIONS_TOTAL.labels(role=role, provider=provider, outcome=outcome).inc()
    if cost_cents > 0:
        LLM_COST_CENTS_TOTAL.labels(role=role, provider=provider).inc(cost_cents)


def record_cleanup_metric(*, outcome: str, count: int = 1) -> None:
    """Record sandbox janitor cleanup outcome."""
    if count > 0:
        CLEANUP_SWEEPS_TOTAL.labels(outcome=outcome).inc(count)


def refresh_db_gauges(session: Session) -> dict[str, int]:
    """Query live operational queue and sandbox counts without tenant leakage."""
    with system_routing_scope(session):
        active_sandboxes = int(
            session.scalar(
                select(func.count())
                .select_from(SandboxResource)
                .where(SandboxResource.status.in_(["active", "cleanup_failed"]))
            )
            or 0
        )
        queued_jobs = int(
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status.in_(["queued", "leased", "running"]))
            )
            or 0
        )
        dead_jobs = int(
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status.in_(["failed", "dead_letter"]))
            )
            or 0
        )
    active_switches = int(
        session.scalar(
            select(func.count())
            .select_from(KillSwitch)
            .where(KillSwitch.is_active.is_(True))
        )
        or 0
    )
    ACTIVE_SANDBOXES_GAUGE.set(active_sandboxes)
    QUEUED_JOBS_GAUGE.set(queued_jobs)
    DEAD_LETTER_JOBS_GAUGE.set(dead_jobs)
    ACTIVE_KILL_SWITCHES_GAUGE.set(active_switches)
    return {
        "active_sandboxes": active_sandboxes,
        "queued_jobs": queued_jobs,
        "dead_letter_jobs": dead_jobs,
        "active_kill_switches": active_switches,
    }


def render_metrics_text(session: Session | None = None) -> tuple[bytes, str]:
    """Render Prometheus text exposition payload and media type."""
    if session is not None:
        refresh_db_gauges(session)
    return generate_latest(METRICS_REGISTRY), CONTENT_TYPE_LATEST
