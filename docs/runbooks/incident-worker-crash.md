# Runbook: Worker Crash, Stale Leases, or Orphan Worktrees

## Detection & Triage
1. Inspect Prometheus metrics at `GET /api/v1/metrics` (`ai_sdlc_worker_jobs_total`, `ai_sdlc_cleanup_sweeps_total`, active task/run gauges).
2. Check stalled jobs in `jobs` (`status='RUNNING'`, `lease_expires_at < now()`), dead-letter jobs (`attempts >= max_attempts`), and tasks with `cleanup_status != 'clean'`.
3. For an affected tenant (with an active consented `SupportAccessGrant`), inspect `GET /api/v1/ops/tenants/{tenant_id}/inspect`.

## Immediate Mitigation
1. **Pause new dispatches if cascading crashes occur:**
   - Enable a global or tenant kill switch via `POST /api/v1/ops/kill-switches` (`scope="global"`, `target_key="all"`, `is_active=true`, `reason="worker crash storm"`).
2. **Recover expired leases & drain safe jobs:**
   - Restart unhealthy worker replicas (`docker compose restart worker`).
   - Worker reclaim logic (`drain_ready_jobs` / `reclaim_stale_leases`) automatically returns expired-lease `RUNNING` jobs to `QUEUED` (up to `max_attempts`) and respects idempotency keys (`Job.idempotency_key`, `QuotaReservation`, `PullRequestRecord`).
3. **Clean orphan worktrees & stale previews:**
   - Trigger `POST /api/v1/workers/janitor-sweep` (or wait for periodic worker sweep) to execute `janitor_sweep` and verify `cleanup_status == "clean"`.

## Verification Before Closing
- Confirm `ai_sdlc_worker_jobs_total{status="succeeded"}` increases cleanly without duplicate `PullRequestRecord` or double ledger debits (`UsageLedgerEntry.idempotency_key`).
- Lift the kill switch (`is_active=false`) and record root cause in the incident timeline.
