"""Tenant-scoped Server-Sent Events stream with Last-Event-ID cursor replay."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_db, get_runtime, require_permission
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import Membership, Task, Tenant
from ai_sdlc_saas.errors import NotFoundError
from ai_sdlc_saas.orchestrator.events import format_sse_frame, list_task_events_since

router = APIRouter(prefix="/api/v1/tasks/{task_id}", tags=["events"])


@router.get("/events/log")
def api_list_task_events(
    task_id: str,
    after_seq: int = Query(default=0, ge=0),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant.id))
    if task is None:
        raise NotFoundError("Task not found.")
    events = list_task_events_since(db, tenant_id=tenant.id, task_id=task.id, after_seq=after_seq)
    return [
        {
            "seq": ev.seq,
            "event_id": ev.event_id,
            "event_type": ev.event_type,
            "stage": ev.stage,
            "payload": ev.payload_json,
            "created_at": ev.created_at.isoformat(),
        }
        for ev in events
    ]


@router.get("/events")
async def api_stream_task_events(
    task_id: str,
    after_seq: int = Query(default=0, ge=0),
    follow: bool = Query(default=False),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> StreamingResponse:
    _ctx, tenant, _mem = perm
    task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant.id))
    if task is None:
        raise NotFoundError("Task not found.")

    cursor = after_seq
    if last_event_id and last_event_id.strip().isdigit():
        cursor = max(cursor, int(last_event_id.strip()))

    tenant_id = tenant.id

    async def _event_stream() -> AsyncIterator[str]:
        nonlocal cursor
        polls = 0
        while True:
            frames: list[str] = []
            terminal = False
            missing_or_deleting = False
            with runtime.session_factory() as s:
                set_session_tenant(s, tenant_id)
                t_check = s.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
                if t_check is None or t_check.status == "DELETING":
                    missing_or_deleting = True
                else:
                    events = list_task_events_since(s, tenant_id=tenant_id, task_id=task_id, after_seq=cursor)
                    for ev in events:
                        cursor = max(cursor, ev.seq)
                        frames.append(format_sse_frame(ev))
                    terminal = t_check.status in ("DELIVERED", "FAILED", "CANCELLED", "DEAD_LETTER")
            if missing_or_deleting:
                break
            for frame in frames:
                yield frame
            if not follow or terminal or polls >= 20:
                break
            polls += 1
            await asyncio.sleep(0.25)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")
