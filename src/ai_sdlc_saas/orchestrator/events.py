"""Sequential per-task event journal and SSE formatting with secret redaction."""

from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.models import TaskEvent
from ai_sdlc_saas.security.redaction import redact_payload


def emit_task_event(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    event_type: str,
    stage: str,
    payload: dict[str, Any] | None = None,
) -> TaskEvent:
    """Persist an ordered, secret-redacted `TaskEvent` row for live UI and SSE replay."""
    ev = TaskEvent(
        event_id=f"evt_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        event_type=event_type,
        stage=stage,
        payload_json=redact_payload(payload or {}),
    )
    session.add(ev)
    session.flush()
    return ev


def list_task_events_since(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    after_seq: int = 0,
    limit: int = 200,
) -> list[TaskEvent]:
    """Return ordered task events strictly with `seq > after_seq` belonging to `tenant_id`."""
    stmt = (
        select(TaskEvent)
        .where(
            TaskEvent.tenant_id == tenant_id,
            TaskEvent.task_id == task_id,
            TaskEvent.seq > after_seq,
        )
        .order_by(TaskEvent.seq.asc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def format_sse_frame(ev: TaskEvent) -> str:
    data = {
        "seq": ev.seq,
        "event_id": ev.event_id,
        "task_id": ev.task_id,
        "run_id": ev.run_id,
        "event_type": ev.event_type,
        "stage": ev.stage,
        "payload": ev.payload_json,
        "created_at": ev.created_at.isoformat(),
    }
    return f"id: {ev.seq}\nevent: {ev.event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
