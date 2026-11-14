"""Private tenant-scoped Object Storage abstraction (local filesystem + S3-compatible contract)."""

from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int
    content_type: str


def validate_tenant_object_key(tenant_id: str, key: str) -> str:
    """Ensure `key` strictly resides under `tenants/{tenant_id}/` without path traversal."""
    cleaned = (key or "").replace("\\", "/").strip()
    if not cleaned or "\x00" in cleaned or cleaned.startswith("/"):
        raise ValueError(f"Invalid object key: {key!r}")
    norm = posixpath.normpath(cleaned)
    if norm in ("", ".", "..") or norm.startswith("../"):
        raise ValueError(f"Path traversal in object key: {key!r}")
    expected_prefix = f"tenants/{tenant_id}/"
    if not norm.startswith(expected_prefix):
        raise PermissionError(f"Cross-tenant object key access denied for tenant {tenant_id!r}: {norm!r}")
    return norm


def build_artifact_key(tenant_id: str, project_id: str, task_id: str, filename: str) -> str:
    safe_name = posixpath.basename(filename.replace("\\", "/").strip())
    if not safe_name or safe_name in (".", ".."):
        raise ValueError(f"Invalid artifact filename: {filename!r}")
    return f"tenants/{tenant_id}/projects/{project_id}/tasks/{task_id}/{safe_name}"


class ObjectStore(ABC):
    """Abstract private object storage backend."""

    @abstractmethod
    def put_bytes(self, tenant_id: str, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredObject:
        """Write bytes under a tenant-verified key."""

    @abstractmethod
    def get_bytes(self, tenant_id: str, key: str) -> bytes:
        """Read bytes only if `key` belongs to `tenant_id` and exists."""

    def get_json(self, tenant_id: str, key: str) -> dict[str, Any]:
        """Read JSON object payload from a tenant-scoped object key."""
        parsed = json.loads(self.get_bytes(tenant_id, key).decode("utf-8"))
        return dict(parsed) if isinstance(parsed, dict) else {"data": parsed}

    @abstractmethod
    def delete_object(self, tenant_id: str, key: str) -> bool:
        """Delete one object belonging to `tenant_id`."""

    @abstractmethod
    def delete_tenant_prefix(self, tenant_id: str) -> int:
        """Hard-delete all stored objects belonging to `tenant_id`."""

    @abstractmethod
    def list_keys(self, tenant_id: str) -> list[str]:
        """List all keys belonging to `tenant_id`."""


class LocalObjectStore(ObjectStore):
    """Local private disk object store rooted at `base_dir` (disposable cache / dev store)."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, tenant_id: str, key: str) -> Path:
        norm = validate_tenant_object_key(tenant_id, key)
        target = (self.base_dir / norm).resolve()
        if not target.is_relative_to(self.base_dir):
            raise PermissionError("Object path escaped root directory.")
        return target

    def put_bytes(
        self, tenant_id: str, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        target = self._resolve_path(tenant_id, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        return StoredObject(key=key, sha256=digest, size_bytes=len(data), content_type=content_type)

    def get_bytes(self, tenant_id: str, key: str) -> bytes:
        target = self._resolve_path(tenant_id, key)
        if not target.is_file():
            raise FileNotFoundError(f"Object not found: {key}")
        return target.read_bytes()

    def delete_object(self, tenant_id: str, key: str) -> bool:
        target = self._resolve_path(tenant_id, key)
        if target.is_file():
            target.unlink()
            return True
        return False

    def delete_tenant_prefix(self, tenant_id: str) -> int:
        tenant_root = (self.base_dir / "tenants" / tenant_id).resolve()
        if not tenant_root.is_relative_to(self.base_dir):
            raise PermissionError("Invalid tenant prefix.")
        if not tenant_root.exists():
            return 0
        count = sum(1 for p in tenant_root.rglob("*") if p.is_file())
        shutil.rmtree(tenant_root)
        return count

    def list_keys(self, tenant_id: str) -> list[str]:
        tenant_root = (self.base_dir / "tenants" / tenant_id).resolve()
        if not tenant_root.exists():
            return []
        return sorted(p.relative_to(self.base_dir).as_posix() for p in tenant_root.rglob("*") if p.is_file())
