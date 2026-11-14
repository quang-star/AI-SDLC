"""Initial AI SDLC SaaS schema with composite tenant constraints and PostgreSQL RLS."""

from __future__ import annotations

from alembic import op
from sqlalchemy import text
from sqlalchemy.orm import Session

from ai_sdlc_saas.billing.plans import seed_default_plans
from ai_sdlc_saas.db.engine import postgres_rls_statements
from ai_sdlc_saas.db.models import Base

revision = "0001_initial_saas_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        for stmt in postgres_rls_statements():
            op.execute(text(stmt))
        op.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ai_sdlc_app;"))
        op.execute(text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ai_sdlc_app;"))
    session = Session(bind=bind)
    seed_default_plans(session)
    session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
