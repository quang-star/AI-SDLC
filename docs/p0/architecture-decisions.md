# P0 — Architecture Decision Records (ADRs) & Core Code Audit

- **Document ID:** `DOC-P0-ADR-001`
- **Owner:** Architecture Lead (`platform-arch`)
- **Date:** 2026-11-13

## 1. Audit of Existing `src/ai_sdlc/` Foundation

Audited files in `src/ai_sdlc/` (verified against `tests/test_contracts.py` — 6/6 tests passing, plus 148 Spike 01 tests and 7 Spike 02 tests):

| Module | Verified Capabilities | Identified Gaps for SaaS GA | Action in P1+ |
| --- | --- | --- | --- |
| `src/ai_sdlc/contracts.py` | Frozen dataclasses (`RepoContext`, `TaskSpec`, `TestPlan`, `ExecutionReport`, `ApprovalRecord`), canonical SHA-256 digests, tamper detection, AC traceability | `SCHEMA_VERSION="1.0"` lacks `tenant_id`, `project_id`, `task_id`, `run_id`, `attempt_id`; missing `ExecutionPlan`, `SubtaskSpec`, `SpecChangeRequest`, `VerificationReport`, `ReviewReport`, and schema migration path | Preserve `1.0` compatibility 100%; introduce `1.1` provenance extensions + new contracts + `migrate_contract_payload()` |
| `src/ai_sdlc/repo_context.py` | Safe file walking, Python AST symbol extraction, `pyproject.toml` pytest detection, git HEAD anchor & dirty check | Python-only symbol/runner extraction; no explicit size/file count caps; no JS/TS `package.json`/`tsconfig.json` detection | Extend safely for TS/JS + multi-profile detection + deterministic size caps & cache keys |
| `src/ai_sdlc/repo_policy.py` | Traversal rejection, symlink/escape blocking, `.env`/key/secret filtering, recursive `conftest.py` protection | Python-centric test/protected path rules | Extend protected paths & test globs for JS/TS (`vitest.config.*`, `jest.config.*`, `tsconfig*.json`, `package-lock.json`, `*.test.ts`, etc.) while keeping existing Python rules invariant |
| `src/ai_sdlc/model_routing.py` | Role-based target selection (`9router:<model>`), URL validation without embedded creds | Reads process env only (`NINE_ROUTER_*`); no tenant policy, capability flags, encrypted BYOK refs, or rate/budget limits | Keep CLI helper intact for spikes; add `ai_sdlc_saas.gateway` production broker & tenant policy |

## 2. Key Architecture Decisions

### ADR-001: API, Core Contracts & Frontend Split
- **Decision:** Python `FastAPI` REST + SSE control plane (`src/ai_sdlc_saas/api`), preserving `src/ai_sdlc/` frozen dataclasses and adding Pydantic v2 validation schemas at API boundaries; `React 19 + TypeScript + Vite` frontend (`apps/web/`) consuming generated OpenAPI client types.
- **Rationale:** Zero rewrite risk for verified digest/validation logic while enforcing strict request/response schema validation and clean static-deployable UI.

### ADR-002: PostgreSQL Multi-Tenant Data Model, Composite FKs & RLS
- **Decision:** SQLAlchemy 2.0 + Alembic migrations targeting PostgreSQL 16+ as production store, with:
  1. Every tenant-owned table storing `tenant_id` NOT NULL.
  2. Composite primary/unique keys `(tenant_id, id)` and composite foreign keys `(tenant_id, project_id)` preventing cross-tenant linkage at DB level.
  3. PostgreSQL Row-Level Security (`ENABLE` + `FORCE ROW LEVEL SECURITY`) keyed on `current_setting('app.tenant_id', true)`.
  4. Runtime DB role `ai_sdlc_app` explicitly created `NOSUPERUSER NOBYPASSRLS` (migrations run under separate owner role).
  5. Connection pool checkout/transaction discipline: `SET LOCAL app.tenant_id = ...` inside transaction, explicit `RESET app.tenant_id` on session close/checkin.
  6. SQLite support strictly for single-developer local/unit test convenience behind the same repository & tenant-context enforcement layer (SQLite does not support Postgres RLS, so Postgres RLS DDL + pool tests are maintained in `alembic/` and `deploy/postgres/`).

### ADR-003: Durable Workflow Engine (LangGraph + Outbox + Worker Leases)
- **Decision:** Self-hosted `LangGraph` `StateGraph` using a custom tenant-scoped `SqlAlchemyCheckpointSaver` (`langgraph-checkpoint`), paired with a PostgreSQL transactional `outbox_events` + `jobs` table and lease-based separate worker process (`ai_sdlc_saas.orchestrator.worker`).
- **Rationale:** LangGraph checkpoints handle graph resume & `interrupt()` waits (`WAITING_INPUT`, `WAITING_APPROVAL`), while domain DB tables remain the sole source of truth for RBAC, approvals, billing, and resource ownership. Long-running agent/verification jobs never execute inside FastAPI HTTP handlers.

### ADR-004: Production Model Gateway & Tenant Credential Boundary
- **Decision:** Backend credential broker abstracts `9Router` (local dev or managed backend cluster, NEVER exposed to end users), `DirectOpenAICompatible` (with tenant BYOK encrypted via AES-GCM/Fernet envelope), and `DeterministicMock` (testing).
- **Rationale:** Inspection confirms `9Router` local uses a single router API key + SQLite config with no native multi-tenant RBAC. Therefore tenant isolation, route allowlists, capability verification (`chat`, `tool_calling`, `structured_output`), rate/concurrency limits, policy freezing, and usage reservation are enforced exclusively in our control-plane gateway service.

### ADR-005: InstaBox Multi-Tenancy Verification & Coding/Verification Sandbox Isolation
- **SDK Inspection Findings (`instabox-sdk==0.5.2`):**
  - Verified from `instabox._client` and `instabox._types`:
    - `Machines.create(...)` accepts `idempotency_key`, `metadata` (tags), `auto_suspend_after_seconds`, `archive_seconds`, `delete_after_seconds`, `fail_fast`, `org_id`, `snapshot_id`.
    - `Snapshots.create(...)` accepts `idempotency_key`, `root_only`.
    - `machines.exec(...)` enforces `1..600s` execution timeout.
    - `instabox-sdk==0.5.2` **does NOT expose per-VM region selection or custom network egress firewall rules** in SDK parameters.
- **Decision:**
  1. Always pass `idempotency_key`, `metadata={"tenant_id": ..., "run_id": ..., "attempt_id": ...}`, and hard `delete_after_seconds` safety TTL on every InstaBox VM creation, backed by `sandbox_resources` ledger + periodic janitor sweeper.
  2. Treat every sandbox VM as **untrusted**: zero platform keys, zero BYOK LLM keys, zero GitHub App tokens, and zero control-plane DB credentials inside sandbox env/filesystem. All LLM calls and Git pushes run on the trusted control plane.
  3. Because SDK v0.5.2 lacks per-call region/egress rules, strict data-residency or zero-egress enterprise tiers require dedicated InstaBox orgs/regional pools or customer-hosted runners, verified before admitting regulated workloads.
  4. Verification (Red & Green) runs strictly in **separate clean environments** from the coding VM (`pre_agent_snapshot` clones or clean local `git worktree` checkouts in dev/CI), re-applying extracted `patch.diff` and verifying tree SHA parity.

### ADR-006: Native MetaGPT & ChatDev Adapter Isolation
- **Decision:**
  - **MetaGPT (`apps/metagpt_service/` + `adapters/metagpt_service.py`):** Preserves Spike 02 finding (`metagpt==0.8.2` blocked on PyPI by missing `lancedb==0.4.0` and eager Windows-incompatible imports). Isolates real `ActionNode` + `[CONTENT]` + `ProductManager` + `Architect` roles in a dedicated HTTP/JSON service/module routing exclusively through the control-plane `ModelGateway`.
  - **ChatDev (`apps/chatdev_service/` + `adapters/chatdev_service.py`):** Pinned workflow YAML definitions (`workflows/spec_critique_testplan.yaml`, `workflows/code_review.yaml`) aligned to `OpenBMB/ChatDev` commit `464a681ba7bda869db97a50c5fa707c9a0fb57a0` (Apache-2.0), executed via an isolated Python workflow engine with strict read-only tool allowlist (zero shell, zero repo write credentials) and strictly tenant+project-scoped memory.

### ADR-007: Object Storage & Ephemeral Caching
- **Decision:** `ObjectStore` abstraction (`LocalEncryptedObjectStore` for local/CI, `S3CompatibleObjectStore` interface for production) enforcing key prefixes `tenants/{tenant_id}/projects/{project_id}/tasks/{task_id}/...` with path-traversal rejection and authenticated backend download proxy / short-lived signed URLs that immediately fail once tenant data is deleted or membership revoked.

### ADR-008: Commercial Billing & Immutable Usage Ledger
- **Decision:** Select Stripe-compatible payment adapter (`stripe_adapter.py`) with HMAC webhook verification (`Stripe-Signature`) and idempotent meter/event tracking. All currency values stored as exact integers (`cents` / `microcents`); `usage_ledger` is append-only (`grant`, `reserve`, `consume`, `release`, `adjust`, `refund`). Failed tasks charge confirmed upstream token + sandbox cost unless classified `PROVIDER_PLATFORM_FAULT` per published failure credit policy.
