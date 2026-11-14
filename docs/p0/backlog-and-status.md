# AI SDLC SaaS — Implementation Backlog & Phase Status Tracker

- **Governance Rule:** Status values strictly follow Roadmap Section 8: `NOT_STARTED`, `IN_PROGRESS`, `BLOCKED`, `READY_FOR_ACCEPTANCE`, `DONE`. A phase is never marked `DONE` merely because files exist; each phase requires executable test/verification evidence.

## Phase Status Overview

| Phase | Name | Owner | Dependencies | Status | Verification Evidence |
| --- | --- | --- | --- | --- | --- |
| **P0** | Product Requirements, Support Matrix & SaaS Design | `platform-arch` | Spike 01/02 evidence | `DONE` | `docs/p0/{prd,support-matrix,architecture-decisions,threat-model-and-data-policy,third-party-notices,ux-flows,backlog-and-status}.md`, `instabox-sdk==0.5.2` inspection, `src/ai_sdlc` audit |
| **P1** | Product Codebase, Versioned Contracts & API/UI Skeleton | `platform-core` | P0 | `DONE` | `pyproject.toml`, `src/ai_sdlc`, `apps/web`, `docs/api/openapi.json`, `tests/test_contracts*.py`, `mypy src`, `ruff check` |
| **P2** | Auth, Organizations, RBAC & Tenant Data Isolation | `platform-sec` | P1 | `DONE` | `tests/test_auth_rbac_tenancy.py`, `tests/test_postgres_rls_live.py` (live PG 16 38-table `FORCE ROW LEVEL SECURITY` + `NOBYPASSRLS` `ai_sdlc_app` + `system_routing_scope`) |
| **P3** | Repository Connections, Project Profiles & RepoContext | `repo-engine` | P2 | `DONE` | `tests/test_repos_profiles.py` (GitHub App webhooks/tokens, Python + JS/TS templates & context detection) |
| **P4** | Production Model Gateway, Role Routing & Budget Controls | `gateway-team` | P2, P3 | `DONE` | `tests/test_gateway_budget.py` (encrypted BYOK `CredentialRef`, probes, policy freeze, atomic reservation) |
| **P5** | Durable Job Engine, LangGraph Orchestration & Resource Lifecycle | `workflow-team` | P1–P4 | `DONE` | `tests/test_orchestrator_faults.py` (`StateGraph`, lease reclaim, dead-letter, janitor sweep, SSE cursor frames) |
| **P6** | MetaGPT Native Adapter: BA, Architect & Spec Approval | `agent-ba` | P3–P5 | `DONE` | `tests/test_metagpt_chatdev.py`, `scripts/run_evals.py`, `evals/` |
| **P7** | ChatDev Native Adapter: Spec Critique, TestPlan & Review | `agent-qa` | P6 | `DONE` | `tests/test_metagpt_chatdev.py`, `workflows/*.yaml` (read-only allowlist & tenant memory) |
| **P8** | Full Coding Pipeline: Plan, Subtasks, Test-First & Fix Loop | `agent-dev` | P5–P7 | `DONE` | `tests/test_coding_pipeline.py` (chronological Red-before-edit, Plan gate, bounded repair) |
| **P9** | Independent Verification & Product Policy Engine | `verifier-team` | P8 | `DONE` | `tests/test_verification_adversarial.py`, `scripts/run_evals.py` (fresh-clone check, tautology/patch-header guards) |
| **P10** | Git Delivery, PR Feedback, Preview & Release Workflow | `delivery-team` | P3, P8–P9 | `DONE` | `tests/test_delivery_pr_release.py` (PR idempotency, drift guard, CI webhooks, preview tokens, release approvals) |
| **P11** | Full Web App & Customer Onboarding (E2E Browser Verified) | `frontend-team` | P1–P10 | `DONE` | `apps/web/` (`npm run build`), `tests/e2e/test_browser_e2e.py` (Playwright owner/dev/Beta flows + SSE + RBAC) |
| **P12** | Subscriptions, Quotas, Immutable Usage Ledger & Billing | `billing-team` | P4, P5 | `DONE` | `tests/test_billing_ledger.py` (integer-cents ledger, quota races, Stripe webhook reconciliation, invoices) |
| **P13** | Production Deployment, Security, Observability & Support | `ops-sec` | P1–P12 | `DONE` | `tests/test_governance_ops.py`, `tests/test_postgres_rls_live.py` (Redis rate limit & live `deploy/docker-compose.yml`), `scripts/backup_restore_drill.py --postgres`, `docs/runbooks/` |
| **P14** | System Evaluation, Fault Drills & Release Candidate | `qa-release` | P0–P13 | `READY_FOR_ACCEPTANCE` | `48/48` pytest pass, `tests/test_evals_and_drills.py`, `scripts/{run_evals,load_test,backup_restore_drill,check_secrets}.py`, `docs/p14/{eval-report,security-review,release-checklist}.md` *(live PG16/Redis/Docker Compose verified; cloud KMS & live-provider 100-task staging benchmark remain external before public GA)* |

## Task Backlog

| Task ID | Phase | Description | Dependencies | Acceptance Criteria | Rollback Strategy | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `T-P0-01` | P0 | Audit `src/ai_sdlc/` & `instabox-sdk==0.5.2`, write PRD, Support Matrix, ADRs, Threat Model, Notices, UX flows | Spike 01/02 | All P0 checklist items covered with explicit owner, limits, trust boundaries | Docs-only | `DONE` |
| `T-P1-01` | P1 | Package `src/ai_sdlc` & `src/ai_sdlc_saas` via `pyproject.toml`, configure ruff/mypy/secret-scan | `T-P0-01` | Editable install/imports work; ruff/mypy/secret scan clean | Revert `pyproject.toml` | `DONE` |
| `T-P1-02` | P1 | Extend contracts (`ExecutionPlan`, `SubtaskSpec`, `SpecChangeRequest`, `VerificationReport`, `ReviewReport`, v1.0->v1.1 migration) | `T-P1-01` | Determinism & migration tests pass; Spike 01/02 tests stay green | Keep v1.0 contract layer intact | `DONE` |
| `T-P1-03` | P1 | FastAPI OpenAPI skeleton + React/TS/Vite shell + generated API client | `T-P1-02` | `docs/api/openapi.json` exported; `npm run build` succeeds | Revert `apps/web` | `DONE` |
| `T-P2-01` | P2 | Auth, Sessions, Orgs, Invitations, RBAC matrix, PostgreSQL migrations w/ composite FKs & RLS | `T-P1-02` | Cross-tenant tests & pool reset tests pass | Alembic downgrade | `DONE` |
| `T-P3-01` | P3 | GitHub App webhooks/tokens, multi-repo projects, Python & JS/TS `RepoContext` & templates | `T-P2-01` | Onboards Python + TS fixture repos, blocks revoked installation & unsupported profile | Feature flag repo provider | `DONE` |
| `T-P4-01` | P4 | Production Model Gateway, encrypted BYOK, capability probes, policy freeze, atomic budget reservation | `T-P2-01`, `T-P3-01` | Concurrent overspend & tenant isolation tests pass | Fallback to deterministic mock / disabled route | `DONE` |
| `T-P5-01` | P5 | LangGraph `StateGraph` + tenant checkpointer, outbox/jobs/leases, janitor, SSE cursor stream | `T-P1`–`T-P4` | Fault-injection crash recovery tests pass without double PR/charge/orphan | Pause worker dispatch | `DONE` |
| `T-P6-01` | P6 | MetaGPT native BA+Architect service, ambiguity -> `WAITING_INPUT`, spec versioning/diff & evals | `T-P3`–`T-P5` | Unapproved spec cannot dispatch Dev; BA eval suite passes | Keep v1 spec gate | `DONE` |
| `T-P7-01` | P7 | ChatDev 2.0 YAML workflow service (Critic/QA/Reviewer), read-only allowlist, tenant memory | `T-P6-01` | Uncovered AC blocked; ChatDev cannot run shell or read other tenant memory | Disable memory feature flag | `DONE` |
| `T-P8-01` | P8 | Full coding pipeline: Plan gate, multi-repo subtasks, chronological test-first, bounded repair, partial patch | `T-P5`–`T-P7` | Multi-file & multi-repo tasks pass; Red runs chronologically before code edit | Toggle `LOCAL_WORKTREE` vs `INSTABOX` runner | `DONE` |
| `T-P9-01` | P9 | Multi-stack independent verification & product policy engine + adversarial bypass suite | `T-P8-01` | All bypass vectors rejected; 100% PASS reports have clean re-apply + AC traceability | Reject unverified runs | `DONE` |
| `T-P10-01` | P10 | Trusted publisher, remote head drift, PR/CI webhook loop, multi-repo ChangeSet, private preview, release gate | `T-P3`, `T-P8`–`T-P9` | Create/update/reopen PR, drift, duplicate webhook & revoke tests pass | Disable auto-publish | `DONE` |
| `T-P11-01` | P11 | Complete Web App (Onboarding, Workbench, Diff/Traceability, Billing, Governance, Support) + Playwright E2E | `T-P2`–`T-P10` | Playwright E2E happy + negative (RBAC, stale approval, cross-tenant, SSE) pass | Static rollback bundle | `DONE` |
| `T-P12-01` | P12 | Plans, immutable integer-cents usage ledger, Stripe simulator/webhook reconciliation, quota race suite | `T-P4`, `T-P5` | Concurrency quota race, failed payment, out-of-order/duplicate webhook & refund tests pass | Disable paid upgrades | `DONE` |
| `T-P13-01` | P13 | Production Docker/KMS/SSRF/rate-limit/observability/runbooks/export-delete/backup-restore/support console | `T-P1`–`T-P12` | Export, hard delete URL invalidation, SSRF, kill-switch, live Docker Compose (`postgres`/`redis`/`api`/`worker`/`web`) & `pg_dump`/`pg_restore` drill pass | Runbook rollback procedure | `DONE` |
| `T-P14-01` | P14 | Evaluation benchmarks (dev/holdout), multi-tenant load test, fault drills, security review & GA checklist | `T-P0`–`T-P13` | `48/48` pytest pass, evals (`13/13`), load test (`p95 = 196.29 ms`), live PG16 RLS + Redis + `pg_dump`/`pg_restore` drills pass; GA checklist records remaining cloud KMS & live-key staging gates | Hold GA rollout | `READY_FOR_ACCEPTANCE` |
