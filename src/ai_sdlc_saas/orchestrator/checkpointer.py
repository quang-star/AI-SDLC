"""Tenant-scoped SQLAlchemy LangGraph checkpointer backed by `graph_checkpoints`."""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    SerializerProtocol,
    get_checkpoint_id,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_sdlc_saas.db.engine import set_session_tenant, system_routing_scope
from ai_sdlc_saas.db.models import GraphCheckpoint


def _pack_typed(serde: SerializerProtocol, obj: Any) -> bytes:
    typ, raw = serde.dumps_typed(obj)
    payload = {"t": typ, "b": base64.b64encode(raw).decode("ascii")}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _unpack_typed(serde: SerializerProtocol, blob: bytes) -> Any:
    payload = json.loads(blob.decode("utf-8"))
    typ = str(payload["t"])
    raw = base64.b64decode(payload["b"])
    return serde.loads_typed((typ, raw))


def _resolve_ids(config: RunnableConfig) -> tuple[str, str, str]:
    conf = config.get("configurable") or {}
    tenant_id = str(conf.get("tenant_id") or "").strip()
    thread_id = str(conf.get("thread_id") or "").strip()
    checkpoint_ns = str(conf.get("checkpoint_ns") or "")
    if not tenant_id:
        raise ValueError("LangGraph configurable['tenant_id'] is required for tenant-isolated checkpoints.")
    if not thread_id:
        raise ValueError("LangGraph configurable['thread_id'] is required.")
    return tenant_id, thread_id, checkpoint_ns


class TenantSqlAlchemyCheckpointSaver(BaseCheckpointSaver[str]):
    """Persists LangGraph checkpoints in `graph_checkpoints` strictly partitioned by `tenant_id`."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__()
        self.session_factory = session_factory

    def _row_to_tuple(self, row: GraphCheckpoint) -> CheckpointTuple:
        checkpoint: Checkpoint = _unpack_typed(self.serde, row.checkpoint_blob)
        metadata: CheckpointMetadata = _unpack_typed(self.serde, row.metadata_blob)
        cfg: RunnableConfig = {
            "configurable": {
                "tenant_id": row.tenant_id,
                "thread_id": row.thread_id,
                "checkpoint_ns": row.checkpoint_ns,
                "checkpoint_id": row.checkpoint_id,
            }
        }
        parent_cfg: RunnableConfig | None = None
        if row.parent_checkpoint_id:
            parent_cfg = {
                "configurable": {
                    "tenant_id": row.tenant_id,
                    "thread_id": row.thread_id,
                    "checkpoint_ns": row.checkpoint_ns,
                    "checkpoint_id": row.parent_checkpoint_id,
                }
            }
        pending_writes: list[PendingWrite] = []
        for item in sorted(row.writes_json or [], key=lambda x: (str(x.get("task_id", "")), int(x.get("idx", 0)))):
            val = self.serde.loads_typed((str(item["t"]), base64.b64decode(item["b"])))
            pending_writes.append((str(item["task_id"]), str(item["channel"]), val))

        return CheckpointTuple(
            config=cfg,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_cfg,
            pending_writes=pending_writes,
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        tenant_id, thread_id, checkpoint_ns = _resolve_ids(config)
        checkpoint_id = get_checkpoint_id(config)
        with self.session_factory() as session:
            set_session_tenant(session, tenant_id)
            stmt = select(GraphCheckpoint).where(
                GraphCheckpoint.tenant_id == tenant_id,
                GraphCheckpoint.thread_id == thread_id,
                GraphCheckpoint.checkpoint_ns == checkpoint_ns,
            )
            if checkpoint_id:
                stmt = stmt.where(GraphCheckpoint.checkpoint_id == checkpoint_id)
            else:
                stmt = stmt.order_by(GraphCheckpoint.created_at.desc(), GraphCheckpoint.checkpoint_id.desc())
            row = session.scalar(stmt.limit(1))
            if row is None:
                return None
            return self._row_to_tuple(row)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            return
        tenant_id, thread_id, checkpoint_ns = _resolve_ids(config)
        before_id = get_checkpoint_id(before) if before else None
        with self.session_factory() as session:
            set_session_tenant(session, tenant_id)
            stmt = (
                select(GraphCheckpoint)
                .where(
                    GraphCheckpoint.tenant_id == tenant_id,
                    GraphCheckpoint.thread_id == thread_id,
                    GraphCheckpoint.checkpoint_ns == checkpoint_ns,
                )
                .order_by(GraphCheckpoint.created_at.desc(), GraphCheckpoint.checkpoint_id.desc())
            )
            rows = list(session.scalars(stmt))

        emitted = 0
        for row in rows:
            if before_id and row.checkpoint_id >= before_id:
                continue
            tup = self._row_to_tuple(row)
            if filter:
                meta = tup.metadata or {}
                if not all(meta.get(k) == v for k, v in filter.items()):
                    continue
            yield tup
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        tenant_id, thread_id, checkpoint_ns = _resolve_ids(config)
        parent_checkpoint_id = get_checkpoint_id(config)
        checkpoint_id = str(checkpoint["id"])
        cp_blob = _pack_typed(self.serde, checkpoint)
        md_blob = _pack_typed(self.serde, metadata)

        with self.session_factory() as session:
            set_session_tenant(session, tenant_id)
            existing = session.scalar(
                select(GraphCheckpoint).where(
                    GraphCheckpoint.tenant_id == tenant_id,
                    GraphCheckpoint.thread_id == thread_id,
                    GraphCheckpoint.checkpoint_ns == checkpoint_ns,
                    GraphCheckpoint.checkpoint_id == checkpoint_id,
                )
            )
            if existing is None:
                session.add(
                    GraphCheckpoint(
                        id=f"gcp_{secrets.token_hex(8)}",
                        tenant_id=tenant_id,
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        parent_checkpoint_id=parent_checkpoint_id,
                        checkpoint_blob=cp_blob,
                        metadata_blob=md_blob,
                        writes_json=[],
                    )
                )
            else:
                existing.checkpoint_blob = cp_blob
                existing.metadata_blob = md_blob
            session.commit()

        return {
            "configurable": {
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        tenant_id, thread_id, checkpoint_ns = _resolve_ids(config)
        checkpoint_id = get_checkpoint_id(config)
        if not checkpoint_id:
            return
        with self.session_factory() as session:
            set_session_tenant(session, tenant_id)
            row = session.scalar(
                select(GraphCheckpoint).where(
                    GraphCheckpoint.tenant_id == tenant_id,
                    GraphCheckpoint.thread_id == thread_id,
                    GraphCheckpoint.checkpoint_ns == checkpoint_ns,
                    GraphCheckpoint.checkpoint_id == checkpoint_id,
                )
            )
            if row is None:
                return
            current_writes = list(row.writes_json or [])
            seen = {(str(w.get("task_id")), int(w.get("idx", 0))) for w in current_writes}
            for pos, (channel, val) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, pos)
                key = (task_id, idx)
                if idx >= 0 and key in seen:
                    continue
                typ, raw = self.serde.dumps_typed(val)
                packed = {
                    "task_id": task_id,
                    "task_path": task_path,
                    "idx": idx,
                    "channel": channel,
                    "t": typ,
                    "b": base64.b64encode(raw).decode("ascii"),
                }
                # Replace special negative idx writes if already stored, or append
                current_writes = [
                    w for w in current_writes if not (str(w.get("task_id")) == task_id and int(w.get("idx", 0)) == idx)
                ]
                current_writes.append(packed)
                seen.add(key)
            row.writes_json = current_writes
            session.commit()

    def delete_thread(self, thread_id: str) -> None:
        with self.session_factory() as session:
            with system_routing_scope(session):
                rows = list(session.scalars(select(GraphCheckpoint).where(GraphCheckpoint.thread_id == thread_id)))
                for r in rows:
                    session.delete(r)
                session.commit()
