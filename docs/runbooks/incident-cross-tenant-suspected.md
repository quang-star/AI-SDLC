# Runbook: Suspected Cross-Tenant Isolation Breach

## Severity: P0 Security Incident

## Immediate Containment (< 5 minutes)
1. **Freeze Execution Globally or per Tenant:**
   - Activate `POST /api/v1/ops/kill-switches` (`scope="global"`, `target_key="all"`, `is_active=true`) if systemic; otherwise lock affected `scope="tenant"`, `target_key="<tenant_id>"`.
2. **Revoke Active Operator Grants & Suspect Sessions:**
   - Revoke any active `SupportAccessGrant` records via `DELETE /api/v1/support/grants/{grant_id}` or directly expire `expires_at` / set `revoked_at`.
   - Revoke affected `UserSession` records and invalidate active preview URLs (`PreviewEnvironment.revoked_at = now()`).
3. **Preserve Forensic Evidence:**
   - Snapshot database before cleanup; export immutable `AuditEvent`, `OutboxEvent`, `TaskEvent`, and API request logs (`request_id`) filtered across suspect time windows.

## Root-Cause Verification Checklist
- Verify PostgreSQL role is non-superuser `NOBYPASSRLS` (`ai_sdlc_app` in `deploy/postgres/init-rls-role.sql`), NOT owner role.
- Verify `set_session_tenant` connection-pool `reset_session_tenant` listener executes on every `checkin` (`src/ai_sdlc_saas/db/engine.py`).
- Inspect `LocalObjectStore` key resolution (`resolve_path` path traversal block and tenant prefix boundary), `verify_preview_access`, and SSE token validation (`src/ai_sdlc_saas/api/deps.py`).
- Run `pytest tests/test_auth_rbac_tenancy.py tests/test_governance_ops.py` against Postgres + SQLite.

## Communication & Recovery
- Only resume traffic after patch verification, automated regression coverage, and customer security notification if customer data exposure is confirmed.
