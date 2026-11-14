"""GitHub App ownership binding, HMAC webhook handling, and permissive httpx REST client."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.engine import set_session_tenant, system_routing_scope
from ai_sdlc_saas.db.models import GitHubInstallation, Repository, WebhookDelivery
from ai_sdlc_saas.errors import AuthError, ConflictError, NotFoundError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.security.crypto import (
    EnvelopeCipher,
    hash_token,
    verify_github_hmac_sha256,
)


class GitHubRestClient:
    """Minimal GitHub REST API v3 client using httpx (permissive MIT/Apache stack)."""

    def __init__(self, *, api_base: str = "https://api.github.com", token: str) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def create_pull_request(
        self,
        *,
        owner_repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{self.api_base}/repos/{owner_repo}/pulls",
                headers=self._headers(),
                json={"title": title, "body": body, "head": head, "base": base},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    def post_issue_comment(self, *, owner_repo: str, issue_number: int, body: str) -> dict[str, Any]:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{self.api_base}/repos/{owner_repo}/issues/{issue_number}/comments",
                headers=self._headers(),
                json={"body": body},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data


def bind_github_installation(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    external_installation_id: str,
    account_login: str,
    ownership_proof: str,
    access_token: str | None = None,
    cipher: EnvelopeCipher | None = None,
    permissions: dict[str, Any] | None = None,
) -> GitHubInstallation:
    """Bind a GitHub App installation to a verified tenant using an ownership proof digest."""
    if not ownership_proof.strip():
        raise AuthError("Ownership proof token is required to bind a GitHub App installation.")

    with system_routing_scope(session):
        existing_other = session.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.external_installation_id == external_installation_id
            )
        )
        if existing_other is not None and existing_other.tenant_id != tenant_id and existing_other.status == "active":
            raise ConflictError(
                f"GitHub App installation {external_installation_id!r} is already bound to another tenant."
            )
    set_session_tenant(session, tenant_id)

    encrypted = cipher.encrypt(access_token) if (access_token and cipher) else None
    existing = session.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.tenant_id == tenant_id,
            GitHubInstallation.external_installation_id == external_installation_id,
        )
    )
    if existing is not None:
        existing.account_login = account_login
        existing.ownership_proof_digest = hash_token(ownership_proof)
        if encrypted is not None:
            existing.encrypted_token = encrypted
        existing.status = "active"
        existing.revoked_at = None
        if permissions is not None:
            existing.permissions_json = permissions
        inst = existing
    else:
        inst = GitHubInstallation(
            id=f"ins_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            external_installation_id=external_installation_id,
            account_login=account_login,
            ownership_proof_digest=hash_token(ownership_proof),
            encrypted_token=encrypted,
            status="active",
            permissions_json=permissions or {"contents": "write", "pull_requests": "write"},
        )
        session.add(inst)

    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="github.installation_bound",
        target_type="installation",
        target_id=inst.id,
        details={"external_installation_id": external_installation_id, "account_login": account_login},
    )
    session.flush()
    return inst


def revoke_github_installation(
    session: Session,
    *,
    external_installation_id: str,
    status: str = "uninstalled",
    actor_user_id: str | None = None,
) -> GitHubInstallation:
    """Revoke a GitHub App installation and immediately disconnect all linked tenant repos."""
    prev_tenant = session.info.get("tenant_id")
    try:
        with system_routing_scope(session):
            inst = session.scalar(
                select(GitHubInstallation).where(
                    GitHubInstallation.external_installation_id == external_installation_id
                )
            )
        if inst is None:
            raise NotFoundError(f"Installation {external_installation_id!r} not found.")

        set_session_tenant(session, inst.tenant_id)
        inst.status = status
        inst.encrypted_token = None
        inst.revoked_at = datetime.now(UTC)

        session.execute(
            update(Repository)
            .where(Repository.tenant_id == inst.tenant_id, Repository.installation_id == inst.id)
            .values(status="disconnected")
        )
        record_audit_event(
            session,
            tenant_id=inst.tenant_id,
            actor_user_id=actor_user_id,
            action=f"github.installation_{status}",
            target_type="installation",
            target_id=inst.id,
            details={"external_installation_id": external_installation_id},
        )
        session.flush()
        return inst
    finally:
        set_session_tenant(session, prev_tenant)


def process_github_webhook(
    session: Session,
    *,
    delivery_id: str,
    event_type: str,
    signature_header: str | None,
    payload_bytes: bytes,
    payload: dict[str, Any],
    webhook_secret: str,
) -> dict[str, Any]:
    """Verify HMAC signature, deduplicate delivery ID, and process lifecycle webhooks."""
    if not verify_github_hmac_sha256(payload_bytes, signature_header, webhook_secret):
        raise AuthError("Invalid GitHub webhook signature.")

    existing = session.get(WebhookDelivery, delivery_id)
    if existing is not None:
        return {"status": "duplicate_ignored", "delivery_id": delivery_id}

    session.add(WebhookDelivery(delivery_id=delivery_id, source="github", event_type=event_type))
    action = str(payload.get("action", ""))

    if event_type == "installation" and action in ("deleted", "suspend"):
        ext_id = str(payload.get("installation", {}).get("id", ""))
        if ext_id:
            try:
                inst = revoke_github_installation(
                    session,
                    external_installation_id=ext_id,
                    status="uninstalled" if action == "deleted" else "suspended",
                )
                return {"status": "processed", "event": f"installation.{action}", "tenant_id": inst.tenant_id}
            except NotFoundError:
                return {"status": "ignored_unknown_installation", "delivery_id": delivery_id}

    if event_type == "repository" and action == "renamed":
        repo_data = payload.get("repository", {})
        new_full_name = str(repo_data.get("full_name", ""))
        ext_inst_id = str(payload.get("installation", {}).get("id", ""))
        old_name = str(payload.get("changes", {}).get("repository", {}).get("name", {}).get("from", ""))
        if ext_inst_id and new_full_name:
            with system_routing_scope(session):
                matched_inst = session.scalar(
                    select(GitHubInstallation).where(GitHubInstallation.external_installation_id == ext_inst_id)
                )
            if matched_inst is not None:
                prev_tenant = session.info.get("tenant_id")
                set_session_tenant(session, matched_inst.tenant_id)
                try:
                    repos = list(
                        session.scalars(
                            select(Repository).where(
                                Repository.tenant_id == matched_inst.tenant_id,
                                Repository.installation_id == matched_inst.id,
                            )
                        )
                    )
                    for r in repos:
                        if old_name and r.full_name.endswith(f"/{old_name}"):
                            r.full_name = new_full_name
                            break
                finally:
                    set_session_tenant(session, prev_tenant)

    session.flush()
    return {"status": "processed", "event": f"{event_type}.{action}" if action else event_type, "delivery_id": delivery_id}
