"""Authenticated, deduplicated inbound webhooks for GitHub App and Stripe."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_db, get_runtime
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.billing.stripe_adapter import WebhookSignatureError, process_stripe_webhook
from ai_sdlc_saas.db.engine import set_session_tenant, system_routing_scope
from ai_sdlc_saas.db.models import GitHubInstallation
from ai_sdlc_saas.delivery.publisher import handle_pr_ci_feedback
from ai_sdlc_saas.errors import AuthError, NotFoundError, ValidationError
from ai_sdlc_saas.repos.github_app import process_github_webhook

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


def _extract_github_ci_feedback(
    event_type: str,
    payload: dict[str, Any],
) -> tuple[int | None, str | None, str, str]:
    repo_full = str((payload.get("repository") or {}).get("full_name") or "").strip() or None
    pr_number: int | None = None
    ci_status = "passed"
    summary = ""

    if event_type == "check_suite":
        suite = payload.get("check_suite") or {}
        prs = suite.get("pull_requests") or []
        if prs and isinstance(prs[0], dict) and prs[0].get("number") is not None:
            pr_number = int(prs[0]["number"])
        conclusion = str(suite.get("conclusion") or suite.get("status") or "").lower()
        ci_status = "passed" if conclusion in ("success", "neutral", "skipped") else "failed"
        summary = f"GitHub check_suite conclusion={conclusion or 'unknown'}"
    elif event_type == "check_run":
        cr = payload.get("check_run") or {}
        prs = cr.get("pull_requests") or []
        if prs and isinstance(prs[0], dict) and prs[0].get("number") is not None:
            pr_number = int(prs[0]["number"])
        conclusion = str(cr.get("conclusion") or cr.get("status") or "").lower()
        ci_status = "passed" if conclusion in ("success", "neutral", "skipped") else "failed"
        summary = f"GitHub check_run {cr.get('name', 'ci')} conclusion={conclusion or 'unknown'}"
    elif event_type == "pull_request_review":
        pr_obj = payload.get("pull_request") or {}
        rev = payload.get("review") or {}
        if pr_obj.get("number") is not None:
            pr_number = int(pr_obj["number"])
        state = str(rev.get("state") or "").lower()
        ci_status = "failed" if state == "changes_requested" else "passed"
        summary = f"PR review {state}: {str(rev.get('body') or '')[:300]}"
    return pr_number, repo_full, ci_status, summary


@router.post("/github")
async def api_github_webhook(
    request: Request,
    x_github_event: str = Header(default="ping"),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    raw_bytes = await request.body()
    try:
        payload = json.loads(raw_bytes.decode("utf-8") or "{}")
    except Exception as exc:
        raise ValidationError("Invalid GitHub webhook JSON payload.") from exc

    delivery_id = x_github_delivery.strip() or f"gh_{secrets.token_hex(8)}"
    res = process_github_webhook(
        db,
        delivery_id=delivery_id,
        event_type=x_github_event,
        signature_header=x_hub_signature_256,
        payload_bytes=raw_bytes,
        payload=payload,
        webhook_secret=runtime.settings.github_webhook_secret,
    )
    tenant_id = res.get("tenant_id")
    if not tenant_id:
        ext_inst = str((payload.get("installation") or {}).get("id") or "").strip()
        if ext_inst:
            with system_routing_scope(db):
                inst = db.scalar(
                    select(GitHubInstallation).where(GitHubInstallation.external_installation_id == ext_inst)
                )
            if inst is not None:
                tenant_id = inst.tenant_id

    if (
        res.get("status") == "processed"
        and tenant_id
        and x_github_event in ("check_suite", "check_run", "pull_request_review")
    ):
        set_session_tenant(db, str(tenant_id))
        pr_number, repo_full, ci_status, summary = _extract_github_ci_feedback(x_github_event, payload)
        if pr_number is not None:
            try:
                ci_res = handle_pr_ci_feedback(
                    db,
                    tenant_id=str(tenant_id),
                    external_pr_number=pr_number,
                    repo_full_name=repo_full,
                    ci_status=ci_status,
                    summary=summary,
                )
                res["ci_feedback"] = ci_res
            except NotFoundError:
                res["ci_feedback"] = {"status": "ignored_untracked_pr", "pr_number": pr_number}
    return res


@router.post("/stripe")
async def api_stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    raw_bytes = await request.body()
    try:
        json.loads(raw_bytes.decode("utf-8") or "{}")
    except Exception as exc:
        raise ValidationError("Invalid Stripe webhook JSON payload.") from exc

    try:
        return process_stripe_webhook(
            db,
            raw_body=raw_bytes,
            signature_header=stripe_signature,
            webhook_secret=runtime.settings.stripe_webhook_secret,
        )
    except WebhookSignatureError as exc:
        raise AuthError(str(exc)) from exc
