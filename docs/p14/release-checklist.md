# P14 Release Candidate & GA Readiness Checklist

## 1. Release Candidate Evidence Matrix (Roadmap §9 Alignment)
| Area | Release Gate | Status on Local Host | Evidence / Artifact |
| --- | --- | --- | --- |
| **Product Flows** | Critical onboarding, project/repo, task/gates, preview, release, billing, governance journeys via Web UI | `PASS` (local live server) | `apps/web/`, `tests/e2e/test_browser_e2e.py` |
| **Native Agents & Contracts** | Contracts v1.0->v1.1 + native MetaGPT BA/Architect + ChatDev 2.0 YAML QA/Reviewer + Spike regression | `PASS` | `tests/test_contracts*.py`, `tests/test_metagpt_chatdev.py`, `spikes/01_instabox_openswe`, `spikes/02_metagpt_ba` |
| **Tenant Isolation** | Mandatory `tenant_id` scoping + ORM criteria + PG 16 `FORCE ROW LEVEL SECURITY` (`38` tables) + non-superuser `NOBYPASSRLS` role (`ai_sdlc_app`) + zero leak under load | `PASS` (Live PG 16 + SQLite/ORM) | `deploy/postgres/init-rls-role.sql`, `tests/test_auth_rbac_tenancy.py`, `tests/test_postgres_rls_live.py`, `scripts/load_test.py` (`0` leaks) |
| **Authorization & Approval Integrity** | Role permission matrix, SHA-256 digest binding (`stale approval` blocked), agent cannot self-approve | `PASS` | `tests/test_auth_rbac_tenancy.py`, `tests/test_delivery_pr_release.py`, `tests/e2e/test_browser_e2e.py` |
| **Quality & Adversarial Verification** | BA clarify/grounded evals (`dev` + `holdout`), adversarial patch/tautology/fresh-clone verifier | `PASS` (`13/13` evals, `0.066 ms` median) | `scripts/run_evals.py`, `tests/test_verification_adversarial.py`, `docs/p14/eval-report.md` |
| **Reliability & Idempotency** | Lease reclaim on worker crash, outbox/janitor cleanup, zero double PR / double ledger debit, Redis rate limiting | `PASS` (Live Redis + Compose) | `tests/test_orchestrator_faults.py`, `tests/test_billing_ledger.py`, `tests/test_delivery_pr_release.py`, `tests/test_postgres_rls_live.py` |
| **Performance** | `10` concurrent tenants, metadata API `p95 < 500 ms` | `PASS` (`p95 = 196.29 ms`, `210` API reqs + `10` reg) | `scripts/load_test.py`, `tests/test_evals_and_drills.py` |
| **Security & Data Lifecycle** | Secret hygiene scan, encrypted `CredentialRef`, SSRF guard, GDPR export exclusion, hard delete + token/URL invalidation | `PASS` (local/Compose); `STAGING_PENDING` (cloud KMS) | `scripts/check_secrets.py`, `tests/test_governance_ops.py`, `tests/test_postgres_rls_live.py`, `docs/p14/security-review.md` |
| **Backup, Recovery & Runbooks** | Live PostgreSQL `pg_dump`/`pg_restore` + SQLite/ObjectStore drill + incident playbooks | `PASS` (Live PG 16 + SQLite) | `scripts/backup_restore_drill.py --postgres` (`173084` B, `force_rls: true`), `docs/runbooks/` |
| **Static Quality & Packaging** | `mypy src` (`77` files), `ruff check`, Vite production bundle build, live Docker Compose (`postgres`, `redis`, `migrate`, `api`, `worker`, `web`) | `PASS` (`48/48` pytest) | `pyproject.toml`, `deploy/docker-compose.yml`, `apps/web/dist/` |

## 2. Deployment Architecture & Rollback Triggers
- **Artifacts Verified Live:** `deploy/docker-compose.yml`, `deploy/Dockerfile.api`, `deploy/Dockerfile.worker`, `deploy/Dockerfile.web`, `deploy/postgres/init-rls-role.sql` (`postgres` healthy `:5432`, `redis` healthy `:16379->6379`, `migrate` exited `0`, `api` healthy `:8000`, `worker` up, `web` `:8080`, smoke-tested `healthz`, `readyz`, `/`, and `POST /api/v1/auth/register`).
- **Console Entrypoints:** `ai-sdlc-api` (`ai_sdlc_saas.api.app:run_cli`), `ai-sdlc-worker` (`ai_sdlc_saas.orchestrator.worker:run_cli`).
- **On-Call Owners & Runbooks:**
  - Worker crash / stale leases / cleanup backlog: `docs/runbooks/incident-worker-crash.md` (`platform-runtime`)
  - Upstream LLM gateway / 9Router outage & kill switch: `docs/runbooks/incident-provider-outage.md` (`gateway-oncall`)
  - Suspected cross-tenant access / emergency session & token containment: `docs/runbooks/incident-cross-tenant-suspected.md` (`security-oncall`)
  - Backup restore & KEK rotation: `docs/runbooks/backup-restore-and-key-rotation.md` (`sre-oncall`)
- **Mandatory Rollback Triggers:**
  1. Any confirmed cross-tenant row, artifact, preview, or cache leak (`S0`: enable global kill switch immediately).
  2. API 5xx rate `> 2%` or metadata `p95 > 500 ms` sustained over 5 minutes after release.
  3. Double billing ledger settlement, runaway non-cleaning worktrees/previews, or failed Alembic migration.

## 3. Cloud Staging & External Credential Gates Remaining Before Public GA
`deploy/docker-compose.yml`, PostgreSQL 16 `FORCE ROW LEVEL SECURITY` (`ai_sdlc_app` `NOBYPASSRLS`), Redis distributed rate limiting, and live `pg_dump`/`pg_restore` have all passed locally. Only cloud-external credentials and live-provider staging items remain before public GA:
1. Provision cloud KMS-backed KEK (`AI_SDLC_ENCRYPTION_KEY`), horizontal `>1` API replica + WAF edge limits soak, and live GitHub App PEM / Stripe / 9Router staging credentials.
2. Execute paid 100-task staging benchmark (`Dev-only` vs `QA+Dev` vs `full workflow`) and invite pilot cohort with finalized regional legal/Terms/DPA documentation before flipping public GA.
