"""Tamper-evident tenant-scoped audit logging with automatic secret redaction."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from ai_sdlc_saas.db.models import AuditEvent
from ai_sdlc_saas.security.redaction import redact_payload


def record_audit_event(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str | None,
    action: str,
    target_type: str,
    target_id: str,
    details: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Persist an audit log entry with sensitive fields automatically redacted."""
    clean_details = redact_payload(dict(details or {}))
    evt = AuditEvent(
        id=f"aud_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details_json=clean_details,
    )
    session.add(evt)
    session.flush()
    return evt
