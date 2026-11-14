"""Database engine, session management, PostgreSQL RLS context, and tenant query enforcement."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker, with_loader_criteria

from ai_sdlc_saas.db.models import TENANT_SCOPED_MODELS, Base, TenantOwnedMixin


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine with SQLite FK enforcement and Postgres pool reset hooks."""
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30.0
        if ":memory:" not in database_url:
            db_path = database_url.removeprefix("sqlite:///")
            if db_path and db_path != ":memory:":
                Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, echo=echo, future=True, pool_pre_ping=True, connect_args=connect_args)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
            mode_row = cursor.execute("PRAGMA journal_mode").fetchone()
            if not mode_row or str(mode_row[0]).lower() != "wal":
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    else:

        @event.listens_for(engine, "checkin")
        def _reset_pg_tenant_on_checkin(dbapi_connection: Any, _connection_record: Any) -> None:
            # Ensure pooled connections never retain app.tenant_id or app.system_scope across checkouts,
            # and never leave psycopg in INTRANS before SQLAlchemy's next pool_pre_ping.
            if dbapi_connection is None:
                return
            cursor = None
            try:
                if not getattr(dbapi_connection, "autocommit", False):
                    dbapi_connection.rollback()
                cursor = dbapi_connection.cursor()
                cursor.execute("RESET app.tenant_id")
                cursor.execute("RESET app.system_scope")
                if not getattr(dbapi_connection, "autocommit", False):
                    dbapi_connection.commit()
            except Exception:
                try:
                    if not getattr(dbapi_connection, "autocommit", False):
                        dbapi_connection.rollback()
                except Exception:
                    pass
            finally:
                if cursor is not None:
                    cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a Session factory enforcing automatic ORM tenant criteria when `session.info['tenant_id']` is bound."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)

    @event.listens_for(factory, "do_orm_execute")
    def _enforce_tenant_criteria(execute_state: ORMExecuteState) -> None:
        bound_tenant = execute_state.session.info.get("tenant_id")
        if not bound_tenant or execute_state.is_column_load or execute_state.is_relationship_load:
            return
        if execute_state.is_select:
            execute_state.statement = execute_state.statement.options(
                with_loader_criteria(
                    TenantOwnedMixin,
                    lambda cls: cls.tenant_id == bound_tenant,
                    include_aliases=True,
                )
            )

    return factory


def init_db_schema(engine: Engine) -> None:
    """Create all schema tables and seed default commercial plans if empty."""
    if not (engine.dialect.name == "postgresql" and inspect(engine).has_table("tenants")):
        Base.metadata.create_all(bind=engine)
    # Seed default plans idempotently
    from ai_sdlc_saas.billing.plans import seed_default_plans

    factory = create_session_factory(engine)
    with factory() as session:
        seed_default_plans(session)
        session.commit()


def set_session_tenant(session: Session, tenant_id: str | None) -> None:
    """Bind `tenant_id` on SQLAlchemy session info and PostgreSQL transaction local GUC (`app.tenant_id`)."""
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    if tenant_id is None:
        session.info.pop("tenant_id", None)
        session.info.pop("system_scope", None)
        if dialect == "postgresql":
            session.execute(text("RESET app.tenant_id"))
            session.execute(text("RESET app.system_scope"))
        return

    session.info["tenant_id"] = tenant_id
    session.info.pop("system_scope", None)
    if dialect == "postgresql":
        session.execute(text("RESET app.system_scope"))
        session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})


@contextmanager
def system_routing_scope(session: Session) -> Iterator[None]:
    """Transaction-local control-plane routing scope for pre-tenant lookup, worker queue, and janitor ops."""
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    prev_tenant = session.info.get("tenant_id")
    session.info.pop("tenant_id", None)
    session.info["system_scope"] = "control_plane"
    if dialect == "postgresql":
        session.execute(text("SELECT set_config('app.system_scope', 'control_plane', true)"))
    try:
        yield
    finally:
        session.info.pop("system_scope", None)
        if dialect == "postgresql":
            try:
                session.execute(text("RESET app.system_scope"))
            except Exception:
                pass
        if prev_tenant is not None:
            try:
                set_session_tenant(session, prev_tenant)
            except Exception:
                pass


@contextmanager
def tenant_transaction(factory: sessionmaker[Session], tenant_id: str) -> Iterator[Session]:
    """Open a transaction strictly scoped to `tenant_id` and reset context cleanly on exit."""
    session = factory()
    try:
        set_session_tenant(session, tenant_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        try:
            set_session_tenant(session, None)
        except Exception:
            pass
        session.close()


_SYSTEM_ROUTING_RLS_TABLES: frozenset[str] = frozenset(
    {
        "memberships",
        "invitations",
        "installations",
        "repositories",
        "jobs",
        "tasks",
        "sandbox_resources",
        "cleanup_jobs",
        "preview_environments",
        "graph_checkpoints",
        "audit_events",
    }
)


def postgres_rls_statements() -> list[str]:
    """Return DDL statements enabling FORCE ROW LEVEL SECURITY across all tenant-scoped tables."""
    stmts: list[str] = []
    for model in TENANT_SCOPED_MODELS:
        table = model.__tablename__
        stmts.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        stmts.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        stmts.append(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};")
        if table in _SYSTEM_ROUTING_RLS_TABLES:
            predicate = (
                "tenant_id = current_setting('app.tenant_id', true) "
                "OR current_setting('app.system_scope', true) = 'control_plane'"
            )
        else:
            predicate = "tenant_id = current_setting('app.tenant_id', true)"
        stmts.append(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"USING ({predicate}) "
            f"WITH CHECK ({predicate});"
        )
    return stmts
