# Runbook: Upstream LLM Provider or GitHub API Outage

## Detection & Triage
1. Monitor `ai_sdlc_llm_invocations_total` (by status/provider) and `ai_sdlc_http_requests_total` at `GET /api/v1/metrics`.
2. Verify upstream connectivity using route probes (`POST /api/v1/models/routes/{route_id}/probe`) and inspect `RequestUsage.usage_status` (`FAILED` / `UNKNOWN_COST`).

## Immediate Mitigation
1. **Activate Provider Kill Switch:**
   - Engage provider isolation immediately:
     `POST /api/v1/ops/kill-switches` with `scope="provider"`, `target_key="<provider_name>"`, `is_active=true`, `reason="upstream outage"`.
2. **Reroute or Fail Over:**
   - Switch active `ModelPolicy.role_route_map` entries to healthy secondary routes or verified `9router` / `openai_compatible` endpoints.
   - If GitHub PR publishing is degraded, `OutboxEvent` and retryable `Job` records retain idempotency without double-posting or losing completed patches.
3. **Prevent Excess Budget Reservations:**
   - Failed/timed-out runs settle `QuotaReservation` cleanly in `cleanup_and_settle` (`consumed` vs `released`), never silently swallowing customer credits.

## Verification Before Closing
- Run `POST /api/v1/models/routes/{route_id}/probe` until healthy capability JSON returns cleanly.
- Clear provider kill switch (`is_active=false`) and spot-check a queued test task end-to-end.
