"""Model Gateway services: BYOK credential vault, tenant routes, policy freeze, fallback, and metering."""

from __future__ import annotations

import math
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc.contracts import canonical_json_bytes, sha256_hex
from ai_sdlc_saas.db.models import (
    CredentialRef,
    KillSwitch,
    ModelPolicy,
    ModelRoute,
    RequestUsage,
)
from ai_sdlc_saas.errors import (
    BudgetExceededError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServiceUnavailableError,
    ValidationError,
)
from ai_sdlc_saas.gateway.adapters import (
    DeterministicMockAdapter,
    GatewayResponse,
    ModelAdapter,
    OpenAICompatibleAdapter,
    compress_messages,
)
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.security.crypto import EnvelopeCipher, mask_secret
from ai_sdlc_saas.security.ssrf import validate_external_http_url

VALID_COMPRESSION_MODES = ("none", "rtk", "headroom")


class RouteHealthTracker:
    """In-memory per-route concurrency guard, RPM window, and failure cooldown tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, int] = defaultdict(int)
        self._rpm: dict[str, deque[float]] = defaultdict(deque)
        self._cooldown_until: dict[str, float] = {}

    def is_in_cooldown(self, route_id: str) -> bool:
        with self._lock:
            return self._cooldown_until.get(route_id, 0.0) > time.monotonic()

    def mark_failure(self, route_id: str, cooldown_seconds: float = 15.0) -> None:
        with self._lock:
            self._cooldown_until[route_id] = time.monotonic() + cooldown_seconds

    def acquire(self, route: ModelRoute) -> None:
        now = time.monotonic()
        with self._lock:
            if self._cooldown_until.get(route.id, 0.0) > now:
                raise ServiceUnavailableError(f"Route {route.name!r} is in temporary failure cooldown.")
            bucket = self._rpm[route.id]
            while bucket and bucket[0] < now - 60.0:
                bucket.popleft()
            if len(bucket) >= max(1, route.rate_limit_rpm):
                raise RateLimitError(f"Route {route.name!r} exceeded per-minute limit ({route.rate_limit_rpm}).")
            if self._active[route.id] >= max(1, route.max_concurrency):
                raise RateLimitError(f"Route {route.name!r} exceeded max concurrency ({route.max_concurrency}).")
            bucket.append(now)
            self._active[route.id] += 1

    def release(self, route_id: str) -> None:
        with self._lock:
            if self._active[route_id] > 0:
                self._active[route_id] -= 1


_DEFAULT_TRACKER = RouteHealthTracker()


def check_kill_switches(
    session: Session,
    *,
    tenant_id: str,
    provider: str | None = None,
    workflow: str | None = None,
) -> None:
    switches = list(session.scalars(select(KillSwitch).where(KillSwitch.is_active.is_(True))))
    for sw in switches:
        if sw.scope == "global":
            raise ServiceUnavailableError(f"Platform execution paused by global kill switch: {sw.reason}")
        if sw.scope == "tenant" and sw.target_key == tenant_id:
            raise ForbiddenError(f"Tenant execution paused by kill switch: {sw.reason}")
        if provider and sw.scope == "provider" and sw.target_key == provider:
            raise ServiceUnavailableError(f"Provider {provider!r} paused by kill switch: {sw.reason}")
        if workflow and sw.scope == "workflow" and sw.target_key == workflow:
            raise ServiceUnavailableError(f"Workflow {workflow!r} paused by kill switch: {sw.reason}")


def create_credential(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    name: str,
    provider_type: str,
    secret_value: str,
    cipher: EnvelopeCipher,
) -> CredentialRef:
    if not secret_value.strip():
        raise ValidationError("Credential secret cannot be empty.")
    if provider_type not in ("9router", "openai_compatible", "github_pat"):
        raise ValidationError(f"Unsupported provider_type {provider_type!r}.")

    cred = CredentialRef(
        id=f"crd_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        name=name.strip()[:120],
        provider_type=provider_type,
        encrypted_secret=cipher.encrypt(secret_value.strip()),
        masked_preview=mask_secret(secret_value.strip()),
        key_version=cipher.key_version,
    )
    session.add(cred)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="credential.created",
        target_type="credential",
        target_id=cred.id,
        details={"name": cred.name, "provider_type": provider_type, "masked_preview": cred.masked_preview},
    )
    session.flush()
    return cred


def list_credentials(session: Session, *, tenant_id: str) -> list[CredentialRef]:
    return list(
        session.scalars(
            select(CredentialRef)
            .where(CredentialRef.tenant_id == tenant_id)
            .order_by(CredentialRef.created_at.desc())
        )
    )


def revoke_credential(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    credential_id: str,
) -> CredentialRef:
    cred = session.scalar(
        select(CredentialRef).where(CredentialRef.id == credential_id, CredentialRef.tenant_id == tenant_id)
    )
    if cred is None:
        raise NotFoundError("Credential not found in active organization.")
    cred.revoked_at = datetime.now(UTC)
    cred.encrypted_secret = "REVOKED"
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="credential.revoked",
        target_type="credential",
        target_id=cred.id,
    )
    session.flush()
    return cred


def create_model_route(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    name: str,
    provider: str,
    base_url: str,
    model_id: str,
    credential_ref_id: str | None = None,
    allow_local_gateway: bool = False,
    rate_limit_rpm: int = 60,
    max_concurrency: int = 4,
    input_cost_microcents_per_1k: int = 150,
    output_cost_microcents_per_1k: int = 600,
) -> ModelRoute:
    if provider not in ("9router", "openai_compatible", "mock"):
        raise ValidationError(f"Unsupported model route provider {provider!r}.")
    if provider != "mock":
        validated_url = validate_external_http_url(base_url, allow_local=allow_local_gateway)
    else:
        validated_url = base_url.strip() or "mock://local"

    if credential_ref_id is not None:
        cred = session.scalar(
            select(CredentialRef).where(
                CredentialRef.id == credential_ref_id,
                CredentialRef.tenant_id == tenant_id,
                CredentialRef.revoked_at.is_(None),
            )
        )
        if cred is None:
            raise ForbiddenError("Credential not found, revoked, or belongs to another organization.")

    route = ModelRoute(
        id=f"rte_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        name=name.strip()[:120],
        provider=provider,
        base_url=validated_url,
        model_id=model_id.strip()[:150],
        credential_ref_id=credential_ref_id,
        rate_limit_rpm=max(1, rate_limit_rpm),
        max_concurrency=max(1, max_concurrency),
        input_cost_microcents_per_1k=max(1, input_cost_microcents_per_1k),
        output_cost_microcents_per_1k=max(1, output_cost_microcents_per_1k),
    )
    session.add(route)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="model_route.created",
        target_type="model_route",
        target_id=route.id,
        details={"provider": provider, "model_id": model_id},
    )
    session.flush()
    return route


def list_model_routes(session: Session, *, tenant_id: str) -> list[ModelRoute]:
    return list(
        session.scalars(
            select(ModelRoute).where(ModelRoute.tenant_id == tenant_id).order_by(ModelRoute.created_at.desc())
        )
    )


def build_adapter_for_route(
    session: Session,
    *,
    tenant_id: str,
    route: ModelRoute,
    cipher: EnvelopeCipher,
    allow_local_gateway: bool,
    override_adapters: dict[str, ModelAdapter] | None = None,
    role: str | None = None,
) -> ModelAdapter:
    if override_adapters:
        for key in (route.id, role or "", route.provider, "default"):
            if key and key in override_adapters:
                return override_adapters[key]
    if route.provider == "mock":
        return DeterministicMockAdapter()

    api_key = ""
    if route.credential_ref_id:
        cred = session.scalar(
            select(CredentialRef).where(
                CredentialRef.id == route.credential_ref_id,
                CredentialRef.tenant_id == tenant_id,
            )
        )
        if cred is None or cred.revoked_at is not None:
            raise ForbiddenError("Bound route credential is missing or revoked.")
        api_key = cipher.decrypt(cred.encrypted_secret)

    return OpenAICompatibleAdapter(
        base_url=route.base_url,
        api_key=api_key,
        allow_local=allow_local_gateway,
    )


def probe_model_route(
    session: Session,
    *,
    tenant_id: str,
    route_id: str,
    cipher: EnvelopeCipher,
    allow_local_gateway: bool,
    override_adapters: dict[str, ModelAdapter] | None = None,
) -> ModelRoute:
    route = session.scalar(
        select(ModelRoute).where(ModelRoute.id == route_id, ModelRoute.tenant_id == tenant_id)
    )
    if route is None:
        raise NotFoundError("Model route not found in active organization.")
    adapter = build_adapter_for_route(
        session,
        tenant_id=tenant_id,
        route=route,
        cipher=cipher,
        allow_local_gateway=allow_local_gateway,
        override_adapters=override_adapters,
    )
    caps = adapter.probe(model_id=route.model_id)
    route.capabilities_json = caps
    route.last_probed_at = datetime.now(UTC)
    session.flush()
    return route


def upsert_model_policy(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    project_id: str | None,
    role_route_map: dict[str, str],
    compression_mode: str = "none",
    max_context_tokens: int = 32_000,
    max_output_tokens: int = 8_192,
    per_task_budget_cents: int = 500,
) -> ModelPolicy:
    if compression_mode not in VALID_COMPRESSION_MODES:
        raise ValidationError(f"Unsupported compression_mode: {compression_mode!r}")
    if per_task_budget_cents <= 0:
        raise ValidationError("per_task_budget_cents must be > 0.")

    # Verify every route_id referenced belongs to the same tenant.
    route_ids = {rid for rid in role_route_map.values() if rid}
    if route_ids:
        valid_routes = set(
            session.scalars(
                select(ModelRoute.id).where(ModelRoute.tenant_id == tenant_id, ModelRoute.id.in_(route_ids))
            )
        )
        missing = route_ids - valid_routes
        if missing:
            raise ForbiddenError(f"Model route(s) not found in tenant: {', '.join(sorted(missing))}")

    digest_payload = {
        "project_id": project_id,
        "role_route_map": dict(sorted(role_route_map.items())),
        "compression_mode": compression_mode,
        "max_context_tokens": max_context_tokens,
        "max_output_tokens": max_output_tokens,
        "per_task_budget_cents": per_task_budget_cents,
    }
    digest = sha256_hex(canonical_json_bytes(digest_payload))

    policy = ModelPolicy(
        id=f"pol_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        project_id=project_id,
        role_route_map_json=dict(role_route_map),
        compression_mode=compression_mode,
        max_context_tokens=max_context_tokens,
        max_output_tokens=max_output_tokens,
        per_task_budget_cents=per_task_budget_cents,
        policy_digest=digest,
    )
    session.add(policy)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="model_policy.saved",
        target_type="model_policy",
        target_id=policy.id,
        details={"project_id": project_id, "compression_mode": compression_mode, "digest": digest},
    )
    session.flush()
    return policy


def resolve_active_policy(session: Session, *, tenant_id: str, project_id: str | None) -> ModelPolicy | None:
    if project_id:
        proj_pol = session.scalar(
            select(ModelPolicy)
            .where(ModelPolicy.tenant_id == tenant_id, ModelPolicy.project_id == project_id)
            .order_by(ModelPolicy.created_at.desc())
        )
        if proj_pol is not None:
            return proj_pol
    return session.scalar(
        select(ModelPolicy)
        .where(ModelPolicy.tenant_id == tenant_id, ModelPolicy.project_id.is_(None))
        .order_by(ModelPolicy.created_at.desc())
    )


def freeze_policy_snapshot(session: Session, *, tenant_id: str, project_id: str | None) -> dict[str, Any]:
    """Capture immutable run-level ModelPolicy snapshot so mid-run policy edits do not shift routes."""
    policy = resolve_active_policy(session, tenant_id=tenant_id, project_id=project_id)
    if policy is None:
        return {
            "policy_id": "default-builtin",
            "policy_digest": sha256_hex(b"default-builtin"),
            "role_route_map": {},
            "compression_mode": "none",
            "max_context_tokens": 32_000,
            "max_output_tokens": 8_192,
            "per_task_budget_cents": 500,
        }
    return {
        "policy_id": policy.id,
        "policy_digest": policy.policy_digest,
        "role_route_map": dict(policy.role_route_map_json),
        "compression_mode": policy.compression_mode,
        "max_context_tokens": policy.max_context_tokens,
        "max_output_tokens": policy.max_output_tokens,
        "per_task_budget_cents": policy.per_task_budget_cents,
    }


def compute_usage_cost_cents(route: ModelRoute, resp: GatewayResponse) -> tuple[int, int]:
    """Compute (upstream_cost_microcents, customer_cost_cents) ensuring usage is never zero/free."""
    in_cost = (resp.input_tokens / 1000.0) * route.input_cost_microcents_per_1k
    out_cost = (resp.output_tokens / 1000.0) * route.output_cost_microcents_per_1k
    upstream_micro = max(1, math.ceil(in_cost + out_cost))
    # Commercial markup 1.25x, minimum 1 cent per executed LLM call so missing/tiny calls are billed safely.
    customer_cents = max(1, math.ceil((upstream_micro * 1.25) / 100.0))
    return upstream_micro, customer_cents


def invoke_role_model(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    attempt_id: str,
    role: str,
    messages: list[dict[str, str]],
    cipher: EnvelopeCipher,
    allow_local_gateway: bool = False,
    policy_snapshot: dict[str, Any] | None = None,
    json_mode: bool = False,
    temperature: float = 0.0,
    tracker: RouteHealthTracker | None = None,
    override_adapters: dict[str, ModelAdapter] | None = None,
) -> dict[str, Any]:
    """Execute one LLM call via frozen role routing + fallback + per-attempt RequestUsage metering."""
    snap = policy_snapshot or freeze_policy_snapshot(session, tenant_id=tenant_id, project_id=None)
    per_task_cap = int(snap.get("per_task_budget_cents") or 500)

    spent_so_far = sum(
        int(u.customer_cost_cents)
        for u in session.scalars(
            select(RequestUsage).where(RequestUsage.tenant_id == tenant_id, RequestUsage.task_id == task_id)
        )
    )
    if spent_so_far >= per_task_cap:
        raise BudgetExceededError(
            f"Task {task_id} exceeded per_task_budget_cents ({spent_so_far} >= {per_task_cap})."
        )

    role_map: dict[str, str] = dict(snap.get("role_route_map") or {})
    candidate_ids: list[str] = []
    for key in (role, f"{role}_fallback", "default", "default_fallback"):
        rid = role_map.get(key)
        if rid and rid not in candidate_ids:
            candidate_ids.append(rid)

    routes: list[ModelRoute] = []
    for rid in candidate_ids:
        r = session.scalar(
            select(ModelRoute).where(
                ModelRoute.id == rid,
                ModelRoute.tenant_id == tenant_id,
                ModelRoute.is_enabled.is_(True),
            )
        )
        if r is not None:
            routes.append(r)

    if not routes:
        routes = list(
            session.scalars(
                select(ModelRoute)
                .where(ModelRoute.tenant_id == tenant_id, ModelRoute.is_enabled.is_(True))
                .order_by(ModelRoute.created_at.asc())
            )
        )

    # Built-in fallback deterministic mock route if caller is running hermetic local route without external endpoints.
    use_builtin_mock = False
    if not routes:
        use_builtin_mock = True

    prepared_messages = compress_messages(
        messages,
        mode=str(snap.get("compression_mode") or "none"),
        max_context_tokens=int(snap.get("max_context_tokens") or 32_000),
    )
    max_out = int(snap.get("max_output_tokens") or 8_192)
    health = tracker or _DEFAULT_TRACKER

    if use_builtin_mock:
        check_kill_switches(session, tenant_id=tenant_id, provider="mock")
        mock_route = ModelRoute(
            id="rte_builtin_mock",
            tenant_id=tenant_id,
            name="builtin-mock",
            provider="mock",
            base_url="mock://local",
            model_id="mock-sdlc-v1",
            input_cost_microcents_per_1k=100,
            output_cost_microcents_per_1k=300,
        )
        mock_adapter: ModelAdapter = DeterministicMockAdapter()
        if override_adapters:
            for key in (role, mock_route.id, "mock", "default"):
                if key and key in override_adapters:
                    mock_adapter = override_adapters[key]
                    break
        resp = mock_adapter.chat_completion(
            messages=prepared_messages,
            model_id=mock_route.model_id,
            temperature=temperature,
            max_tokens=max_out,
            json_mode=json_mode,
        )
        selected_route = mock_route
    else:
        last_err: Exception | None = None
        selected_route_opt: ModelRoute | None = None
        resp_opt: GatewayResponse | None = None
        for route in routes:
            check_kill_switches(session, tenant_id=tenant_id, provider=route.provider)
            try:
                health.acquire(route)
            except (ServiceUnavailableError, RateLimitError) as exc:
                last_err = exc
                continue
            try:
                adapter = build_adapter_for_route(
                    session,
                    tenant_id=tenant_id,
                    route=route,
                    cipher=cipher,
                    allow_local_gateway=allow_local_gateway,
                    override_adapters=override_adapters,
                    role=role,
                )
                resp_opt = adapter.chat_completion(
                    messages=prepared_messages,
                    model_id=route.model_id,
                    temperature=temperature,
                    max_tokens=max_out,
                    json_mode=json_mode,
                )
                selected_route_opt = route
                break
            except Exception as exc:
                health.mark_failure(route.id)
                last_err = exc
                continue
            finally:
                health.release(route.id)

        if resp_opt is None or selected_route_opt is None:
            raise ServiceUnavailableError(
                f"All configured model routes failed for role {role!r}: {last_err}"
            )
        resp = resp_opt
        selected_route = selected_route_opt

    upstream_micro, customer_cents = compute_usage_cost_cents(selected_route, resp)
    usage_row = RequestUsage(
        id=f"usg_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id,
        role=role,
        route_id=selected_route.id,
        actual_model_id=resp.actual_model_id,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cached_tokens=resp.cached_tokens,
        usage_status=resp.usage_status,
        upstream_cost_microcents=upstream_micro,
        customer_cost_cents=customer_cents,
    )
    session.add(usage_row)
    session.flush()
    return {
        "response": resp,
        "route_id": selected_route.id,
        "actual_model_id": resp.actual_model_id,
        "usage": usage_row,
    }
