"""Backup & restore drill verifying SQLite hot-backup, ObjectStore parity, and SHA-256 checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.engine import build_engine, init_db_schema
from ai_sdlc_saas.db.models import (
    ArtifactVersion,
    AuditEvent,
    Project,
    PullRequestRecord,
    Repository,
    Task,
    TaskRun,
    Tenant,
    UsageLedgerEntry,
    User,
)
from ai_sdlc_saas.storage.object_store import LocalObjectStore, build_artifact_key


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _collect_object_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            out[rel] = _sha256_file(p)
    return out


def _sqlite_table_counts(sqlite_path: Path) -> dict[str, int]:
    engine = create_engine(f"sqlite:///{sqlite_path}", future=True)
    try:
        tables = (
            Tenant,
            Project,
            Repository,
            Task,
            TaskRun,
            ArtifactVersion,
            PullRequestRecord,
            AuditEvent,
            UsageLedgerEntry,
        )
        counts: dict[str, int] = {}
        with Session(engine) as session:
            for model in tables:
                counts[model.__tablename__] = int(
                    session.scalar(select(func.count()).select_from(model)) or 0
                )
        return counts
    finally:
        engine.dispose()


def run_backup_restore_drill(
    *,
    source_sqlite_path: Path,
    source_object_store_dir: Path,
    work_dir: Path,
) -> dict[str, Any]:
    """Execute full backup -> manifest checksum -> restore -> integrity/count verification."""
    if not source_sqlite_path.exists():
        raise FileNotFoundError(f"Source SQLite database not found: {source_sqlite_path}")

    backup_dir = work_dir / "backup"
    restore_dir = work_dir / "restored"
    backup_dir.mkdir(parents=True, exist_ok=True)
    restore_dir.mkdir(parents=True, exist_ok=True)

    backup_db = backup_dir / "db.sqlite"
    backup_objects = backup_dir / "objects"

    # 1. Online SQLite backup + object snapshot
    src_conn = sqlite3.connect(str(source_sqlite_path))
    dst_conn = sqlite3.connect(str(backup_db))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    if source_object_store_dir.exists():
        shutil.copytree(source_object_store_dir, backup_objects, dirs_exist_ok=True)
    else:
        backup_objects.mkdir(parents=True, exist_ok=True)

    src_hashes = _collect_object_hashes(source_object_store_dir)
    bak_hashes = _collect_object_hashes(backup_objects)
    if src_hashes != bak_hashes:
        raise RuntimeError("ObjectStore backup checksum mismatch vs source.")

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "db_sha256": _sha256_file(backup_db),
        "object_hashes": bak_hashes,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # 2. Restore into isolated target directory
    restored_db = restore_dir / "db.sqlite"
    restored_objects = restore_dir / "objects"
    shutil.copy2(backup_db, restored_db)
    shutil.copytree(backup_objects, restored_objects, dirs_exist_ok=True)

    # 3. Verify integrity, checksum parity, and table row parity
    if _sha256_file(restored_db) != manifest["db_sha256"]:
        raise RuntimeError("Restored SQLite SHA-256 mismatch.")

    chk_conn = sqlite3.connect(str(restored_db))
    try:
        integrity = chk_conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Restored SQLite integrity_check failed: {integrity}")
    finally:
        chk_conn.close()

    restored_hashes = _collect_object_hashes(restored_objects)
    if restored_hashes != manifest["object_hashes"]:
        raise RuntimeError("Restored ObjectStore checksum mismatch.")

    src_counts = _sqlite_table_counts(source_sqlite_path)
    restored_counts = _sqlite_table_counts(restored_db)
    if src_counts != restored_counts:
        raise RuntimeError(f"Table row count mismatch: src={src_counts} restored={restored_counts}")

    return {
        "ok": True,
        "db_sha256": manifest["db_sha256"],
        "object_files_verified": len(restored_hashes),
        "table_counts": restored_counts,
        "integrity_check": "ok",
    }


_CORE_TABLES = (
    Tenant,
    Project,
    Repository,
    Task,
    TaskRun,
    ArtifactVersion,
    PullRequestRecord,
    AuditEvent,
    UsageLedgerEntry,
)


def _pg_table_counts(database_url: str) -> dict[str, int]:
    engine = create_engine(database_url, future=True)
    try:
        counts: dict[str, int] = {}
        with Session(engine) as session:
            for model in _CORE_TABLES:
                counts[model.__tablename__] = int(
                    session.scalar(select(func.count()).select_from(model)) or 0
                )
        return counts
    finally:
        engine.dispose()


def _replace_db_name(database_url: str, new_db_name: str) -> str:
    parsed = urlparse(database_url)
    return urlunparse(parsed._replace(path=f"/{new_db_name}"))


def _resolve_postgres_container(explicit: str | None) -> str:
    if explicit:
        return explicit
    root = Path(__file__).resolve().parents[1]
    compose_file = root / "deploy" / "docker-compose.yml"
    proc = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "ps", "-q", "postgres"],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""
    if not container_id:
        raise RuntimeError("No running postgres container found via docker compose; pass --postgres-container.")
    return container_id


def run_postgres_backup_restore_drill(
    *,
    database_url: str = "postgresql+psycopg://ai_sdlc_owner:ai_sdlc_owner_dev_PLACEHOLDER@127.0.0.1:5432/ai_sdlc",
    work_dir: Path,
    postgres_container: str | None = None,
    source_object_store_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute live pg_dump -> SHA-256 manifest -> fresh DB pg_restore -> row count & RLS parity check."""
    container = _resolve_postgres_container(postgres_container)
    parsed = urlparse(database_url)
    pg_user = parsed.username or "ai_sdlc_owner"
    src_db = parsed.path.lstrip("/") or "ai_sdlc"
    restore_db = f"ai_sdlc_restore_drill_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"

    backup_dir = work_dir / "pg_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dump_file = backup_dir / f"{src_db}.dump"

    dump_proc = subprocess.run(
        ["docker", "exec", container, "pg_dump", "-U", pg_user, "-Fc", src_db],
        capture_output=True,
        check=True,
    )
    dump_file.write_bytes(dump_proc.stdout)
    dump_sha256 = _sha256_file(dump_file)

    object_hashes: dict[str, str] = {}
    if source_object_store_dir and source_object_store_dir.exists():
        backup_objects = backup_dir / "objects"
        restored_objects = work_dir / "pg_restored_objects"
        shutil.copytree(source_object_store_dir, backup_objects)
        shutil.copytree(backup_objects, restored_objects)
        object_hashes = _collect_object_hashes(backup_objects)
        if _collect_object_hashes(restored_objects) != object_hashes:
            raise RuntimeError("Restored ObjectStore checksum mismatch.")

    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            pg_user,
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f'DROP DATABASE IF EXISTS "{restore_db}";',
            "-c",
            f'CREATE DATABASE "{restore_db}" OWNER "{pg_user}";',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        subprocess.run(
            ["docker", "exec", "-i", container, "pg_restore", "-U", pg_user, "-d", restore_db, "--exit-on-error"],
            input=dump_proc.stdout,
            capture_output=True,
            check=True,
        )
        src_counts = _pg_table_counts(database_url)
        restore_url = _replace_db_name(database_url, restore_db)
        restored_counts = _pg_table_counts(restore_url)
        if src_counts != restored_counts:
            raise RuntimeError(f"PostgreSQL row count mismatch: src={src_counts} restored={restored_counts}")

        rls_engine = create_engine(restore_url, future=True)
        try:
            with Session(rls_engine) as session:
                rows = session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN ('projects', 'tasks') ORDER BY relname"
                    )
                ).all()
                rls_state = {r[0]: {"rls": bool(r[1]), "force_rls": bool(r[2])} for r in rows}
        finally:
            rls_engine.dispose()

        if not all(v["rls"] and v["force_rls"] for v in rls_state.values()) or len(rls_state) != 2:
            raise RuntimeError(f"Restored PostgreSQL schema missing FORCE ROW LEVEL SECURITY: {rls_state}")

        return {
            "ok": True,
            "mode": "postgres",
            "container": container,
            "source_db": src_db,
            "restore_db": restore_db,
            "dump_bytes": len(dump_proc.stdout),
            "db_sha256": dump_sha256,
            "object_files_verified": len(object_hashes),
            "table_counts": restored_counts,
            "rls_verified_in_restore": rls_state,
        }
    finally:
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                pg_user,
                "-d",
                "postgres",
                "-c",
                f'DROP DATABASE IF EXISTS "{restore_db}" WITH (FORCE);',
            ],
            capture_output=True,
            text=True,
            check=False,
        )


def _bootstrap_synthetic_source(base_dir: Path) -> tuple[Path, Path]:
    db_path = base_dir / "source.db"
    obj_dir = base_dir / "source_objects"
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{db_path}",
        object_store_dir=str(obj_dir),
        worktree_root=str(base_dir / "worktrees"),
        jwt_secret="drill-secret-key-min-32-chars-ok!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    )
    engine = build_engine(settings.database_url)
    init_db_schema(engine)
    store = LocalObjectStore(obj_dir)
    with Session(engine) as session:
        t = Tenant(id="tnt_drill", slug="drill-org", name="Drill Org")
        u = User(id="usr_drill", email="drill@example.com", password_hash="x", full_name="Drill")
        session.add_all([t, u])
        session.flush()
        p = Project(id="prj_drill", tenant_id=t.id, slug="drill-proj", name="Drill Proj")
        session.add(p)
        session.commit()
    key = build_artifact_key("tnt_drill", "prj_drill", "tsk_drill", "spec.json")
    store.put_bytes(
        "tnt_drill",
        key,
        json.dumps({"drill": True, "v": 1}).encode("utf-8"),
        content_type="application/json",
    )
    engine.dispose()
    return db_path, obj_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI SDLC SaaS backup and restore drill.")
    parser.add_argument("--db-path", type=Path, default=None, help="Existing SQLite DB path to drill.")
    parser.add_argument("--object-dir", type=Path, default=None, help="Existing ObjectStore dir to drill.")
    parser.add_argument("--postgres", action="store_true", help="Run live PostgreSQL pg_dump/pg_restore drill.")
    parser.add_argument("--postgres-container", type=str, default=None, help="Docker Postgres container ID or name.")
    parser.add_argument(
        "--database-url",
        type=str,
        default="postgresql+psycopg://ai_sdlc_owner:ai_sdlc_owner_dev_PLACEHOLDER@127.0.0.1:5432/ai_sdlc",
        help="PostgreSQL owner connection URL for --postgres mode.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        if args.postgres:
            report = run_postgres_backup_restore_drill(
                database_url=args.database_url,
                work_dir=tmp_path / "pg_drill",
                postgres_container=args.postgres_container,
                source_object_store_dir=args.object_dir,
            )
        else:
            if args.db_path and args.object_dir:
                src_db, src_obj = args.db_path, args.object_dir
            else:
                src_db, src_obj = _bootstrap_synthetic_source(tmp_path / "seed")
            report = run_backup_restore_drill(
                source_sqlite_path=src_db,
                source_object_store_dir=src_obj,
                work_dir=tmp_path / "drill",
            )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
