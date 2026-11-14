# P0 — Threat Model, Trust Boundaries & Data Governance Policy

- **Document ID:** `DOC-P0-SECURITY-DATA-001`
- **Owner:** Security & Compliance Lead (`platform-sec`)
- **Date:** 2026-11-13

## 1. Trust Boundaries & Untrusted Input Rules

| Boundary | Trusted Side | Untrusted Side | Enforcement Controls |
| --- | --- | --- | --- |
| Client / Browser <-> API | FastAPI control plane | Browser payload, headers, `tenant_id` params | Session token / JWT verification + active DB `Membership` lookup; client `tenant_id` validated against membership; RBAC matrix per endpoint |
| Control Plane <-> GitHub & Stripe Webhooks | Webhook endpoints | External HTTP POST payloads | Mandatory HMAC-SHA256 signature verification (`X-Hub-Signature-256`, `Stripe-Signature`), constant-time compare, `delivery_id` / `event_id` deduplication, actor permission check |
| Control Plane <-> LLM Providers | Gateway Broker | LLM text/JSON responses | Structured Pydantic validation, semantic RepoContext grounding checks, output treated as untrusted data (XSS-escaped in UI, never `eval`'d or shell-interpolated) |
| Control Plane <-> Sandbox VM / Worktree | Verifier & Orchestrator | Source code, tests, `README`, `.git/config`, `conftest.py`, agent commands | Symlink/secret block before upload; sandbox has zero control-plane/GitHub/BYOK credentials; SSRF validator blocks metadata/private IPs; Verifier re-applies `patch.diff` on separate clean checkout |
| Agent <-> Approval & Publish Gates | Human Reviewer / Owner | Coding/BA/QA agents | Agents cannot write `approvals`, `memberships`, `subscriptions`, or invoke `/publish` or `/release`; every approval binds `tenant_id + project_id + run_id + base_commit_sha + artifact_sha256 + policy_version` |

## 2. Adversarial Threat Mitigations (Verified in P9 Suite)

1. **Test Tampering / Assertion Deletion:** Immutable `existing_test_files` locked at `base_commit_sha`; Red test must genuinely fail (`exit_code == 1`) on clean baseline and pass (`exit_code == 0`) on clean Green checkout.
2. **Test Discovery / Config Manipulation:** `pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`, `vitest.config.*`, `jest.config.*`, `.github/**`, and nested `conftest.py` at any depth are protected paths; unauthorized edits cause immediate `REJECT`.
3. **Fake JUnit / Report Spoofing:** Verifier runs test commands directly outside agent control, validates non-empty suites, rejects collection/interruption exit codes (`!= 0` on Green), parses XML safely without external entity resolution.
4. **Symlink / Gitlink Escapes:** Git modes `120000` (symlink) and `160000` (submodule/gitlink) trigger immediate `REJECT`; local file collector rejects symlinks before reading bytes.
5. **Out-of-Git Dependency Tampering:** Clean Green environment restores `pre_agent_snapshot` / fresh base checkout and applies ONLY `patch.diff`, discarding any pip/npm mutations done in the coding sandbox.
6. **Prompt Injection inside Repo Files:** `RepoContext` treats file contents as delimited untrusted context; BA/QA schema validators and static Hard Gates enforce constraints regardless of prompt text inside source/README comments.

## 3. Data Regions, Retention, Export & Deletion Policy

- **Supported Data Regions (Config-Selectable per Tenant at Creation):** `us-east` (default), `eu-central`, `ap-southeast`. Control-plane DB, ObjectStore bucket, and backup snapshots remain pinned to tenant's selected region; regional InstaBox/gateway routing is gated by region configuration.
- **Retention Defaults (Configurable per Plan):**
  - Ephemeral sandboxes & snapshots: deleted immediately on run close (hard TTL <= 4h; janitor sweeps every 5 min).
  - Task artifacts (`RepoContext`, `TaskSpec`, `TestPlan`, `VerificationReport`, logs): 90 days (Pro) / 365 days (Team/Enterprise).
  - Audit logs (`audit_events`) & financial ledger (`usage_ledger`): 7 years for accounting/compliance (anonymized `user_email` on user deletion while preserving financial/audit integrity).
- **Customer Data Export:** Owner/Admin triggers `POST /api/v1/governance/exports`; produces tenant-scoped ZIP/JSON archive of projects, tasks, specs, test plans, verification reports, PR metadata, and audit events (zero raw BYOK keys or platform secrets).
- **Customer Data Deletion / Right to Erasure:** Owner triggers `POST /api/v1/governance/delete-tenant-data` (with confirmation token). Immediately revokes all tenant sessions & GitHub installation bindings, deletes/crypto-shreds BYOK keys, purges object storage keys, checkpoints, tenant memory, and task/project rows, records deletion certificate, and ensures all old artifact/SSE URLs return `401`/`404`.
- **Support Access Policy:** Platform operators cannot view tenant data without an explicit `SupportAccessGrant` specifying `tenant_id`, `operator_id`, `reason`, `ticket_id`, and `expires_at` (max 4 hours), logged to immutable `audit_events` visible to the tenant Owner.
