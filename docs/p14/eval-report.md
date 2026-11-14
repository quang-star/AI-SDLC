# P14 System Evaluation, Load & Fault Drill Report

## 1. Benchmark Suite (`evals/suite.json` via `scripts/run_evals.py`)
- **Total Cases:** `13` (`dev`: `7`, `holdout`: `6`)
- **Passed:** `13 / 13` (`overall_accuracy = 1.0`)
- **Median Case Latency:** `0.066 ms`
- **Split Separation:** Holdout cases (`eval-holdout-*`) are isolated from development cases (`eval-dev-*`) and exercise BA ambiguity detection, contradictory requirements, hallucinated file paths, Python + JS/TS grounded specs, and adversarial AST verification (constant equality / no-op tautology detection vs genuine verification passes).

### Detailed Results (`scripts/run_evals.py`)
| Case ID | Split | Suite | Expected | Observed | Latency (ms) | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `eval-dev-ambig-01` | `dev` | `ba_clarify` | `true` | `true` (`1` Q) | `0.174` | `PASS` |
| `eval-dev-contradict-02` | `dev` | `ba_clarify` | `true` | `true` (`1` Q) | `0.066` | `PASS` |
| `eval-dev-hallucinated-path-03` | `dev` | `ba_clarify` | `true` | `true` (`1` Q) | `0.046` | `PASS` |
| `eval-dev-grounded-py-04` | `dev` | `ba_clarify` | `false` | `false` (`0` Q) | `0.042` | `PASS` |
| `eval-dev-grounded-ts-05` | `dev` | `ba_clarify` | `false` | `false` (`0` Q) | `0.038` | `PASS` |
| `eval-dev-verifier-tautology-06` | `dev` | `verifier_adversarial` | `FAIL` | `FAIL` (`trivial_or_noop_test`) | `7.292` | `PASS` |
| `eval-dev-verifier-real-pass-07` | `dev` | `verifier_adversarial` | `PASS` | `PASS` | `13.289` | `PASS` |
| `eval-holdout-ambig-01` | `holdout` | `ba_clarify` | `true` | `true` (`1` Q) | `0.118` | `PASS` |
| `eval-holdout-contradict-02` | `holdout` | `ba_clarify` | `true` | `true` (`1` Q) | `0.064` | `PASS` |
| `eval-holdout-hallucinated-path-03` | `holdout` | `ba_clarify` | `true` | `true` (`1` Q) | `0.056` | `PASS` |
| `eval-holdout-grounded-multi-04` | `holdout` | `ba_clarify` | `false` | `false` (`0` Q) | `0.058` | `PASS` |
| `eval-holdout-verifier-tautology-05` | `holdout` | `verifier_adversarial` | `FAIL` | `FAIL` (`trivial_or_noop_test`) | `5.773` | `PASS` |
| `eval-holdout-verifier-real-pass-06` | `holdout` | `verifier_adversarial` | `PASS` | `PASS` | `7.864` | `PASS` |

## 2. Concurrent Multi-Tenant Load Test (`scripts/load_test.py`)
- **Configuration:** `10` concurrent tenant workers, burst registrations + `210` authenticated metadata/task API requests against live Uvicorn + SQLite WAL (`Depends(get_db, scope="function")`, `PRAGMA busy_timeout=30000`).
- **Observed Metrics:**
  - `concurrent_tenants`: `10`
  - `register_burst_measured`: `10` (`register_burst_p95_ms`: `1624.85 ms`)
  - `api_requests_measured`: `210`
  - `total_elapsed_s`: `4.319 s` (`throughput_rps`: `50.93 req/s`)
  - `api_p50_ms`: `99.76 ms`
  - `api_p95_ms`: `196.29 ms` (Roadmap §9 target `< 500 ms` — **PASS**)
  - `api_max_ms`: `660.3 ms`
  - `cross_tenant_leaks`: `0`
  - `errors`: `[]`

## 3. Fault, Live Docker Compose, Postgres RLS & Recovery Drill Summary
- **Full Pytest Suite (`python -m pytest -v --tb=short`):** `48 passed` (`0` failed, `0` skipped), including live PostgreSQL 16 + Redis suite and Playwright browser E2E.
- **Live PostgreSQL 16 `NOBYPASSRLS` & Redis Verification (`tests/test_postgres_rls_live.py`):** Verified `ai_sdlc_app` (`rolsuper=false`, `rolbypassrls=false`), `FORCE ROW LEVEL SECURITY` + `{table}_tenant_isolation` policies on all `38` tenant-scoped tables, raw SQL negative/positive cross-tenant isolation, end-to-end FastAPI/worker lifecycle under RLS, and distributed `SlidingWindowRateLimiter` across instances via live Redis (`redis://127.0.0.1:16379/0`).
- **Live Docker Compose Stack (`deploy/docker-compose.yml`):** Verified `postgres` (healthy), `redis` (healthy), `migrate` (`alembic upgrade head` exited `0`), `api` (healthy on `:8000`), `worker`, and `web` (`:8080` Nginx proxy) with live HTTP smoke checks (`8000/healthz -> 200`, `8000/readyz -> 200`, `8080/ -> 200`, `POST 8080/api/v1/auth/register -> 201`).
- **Live PostgreSQL Backup & Restore Drill (`scripts/backup_restore_drill.py --postgres`):** Verified `pg_dump -Fc` -> SHA-256 checksum (`8a22d03ec68c3c4aab3fcf21b74cbcab8ba9a69cc66a0b8527f99531697a6b5e`, `dump_bytes = 173084`) -> fresh DB `pg_restore` -> exact table row count parity (`tenants=1`, `audit_events=1`, `usage_ledger=1`, etc.) and `FORCE ROW LEVEL SECURITY` preservation on `projects` & `tasks`. SQLite + ObjectStore drill (`run_backup_restore_drill`) also remains green (`integrity_check = "ok"`).
- **Orchestrator & Billing Fault Suites (`tests/test_orchestrator_faults.py`, `tests/test_billing_ledger.py`, `tests/test_delivery_pr_release.py`):** Verified lease reclaim after worker crash, dead-letter bounds, janitor orphan worktree/preview cleanup, atomic quota reservation concurrency races, idempotent Stripe webhook deduplication/out-of-order rejection, and GitHub webhook remote drift / PR deduplication.
- **Playwright Full-Stack UI E2E (`tests/e2e/test_browser_e2e.py`):** Verified multi-tenant browser registration, repository scaffolding, task execution with live SSE frames, Spec/Plan/Delivery human gates, private preview launch, Developer RBAC `403` denial on release promotion, and Tenant Beta cross-tenant `403/404` isolation against the live React+Vite bundle.

## 4. Scope Boundary & Cloud Staging Follow-Ups
- Local evaluations and pipeline integration tests exercised deterministic/mock LLM gateway adapters (`DeterministicMockAdapter`) and local worktree git fixtures rather than paid live LLM keys.
- Live 100-task cloud staging A/B workflow cost comparison (`Dev-only` vs `QA+Dev` vs `full MetaGPT+ChatDev+Dev`), cloud KMS KEK provisioning, multi-replica API horizontal scaling soak, and live GitHub App / Stripe / 9Router staging credentials remain scheduled prior to public GA.
