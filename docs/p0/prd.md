# P0 — Product Requirements Document (PRD): AI SDLC SaaS

- **Document ID:** `DOC-P0-PRD-001`
- **Status:** `APPROVED_FOR_BUILD`
- **Owner:** Platform Product & Engineering (`platform-core`)
- **Last Updated:** 2026-11-13
- **Target Product Scope:** Multi-tenant SaaS for automated software specification, test planning, sandboxed implementation, independent verification, code review, and PR/release delivery.

## 1. Personas & Roles

| Persona | Default RBAC Role | Primary Responsibilities | Explicit Restrictions |
| --- | --- | --- | --- |
| Workspace Founder / Executive | `Owner` | Create/delete org, transfer ownership, configure billing/payment, manage admins, data export/delete, release approval | Cannot bypass immutable verification evidence gates (`PASS` requires executed test evidence) |
| Engineering Manager / Tech Lead | `Admin` | Manage members/invitations, connect GitHub installations, configure repo policies/protected paths, configure model routes & budgets, approve specs/plans/delivery/release | Cannot delete organization or transfer ownership |
| Software Engineer | `Developer` | Create tasks, inspect repo context, answer BA clarifications, edit draft specs/test plans, cancel/retry/resume own tasks, triage minor findings | Cannot approve final delivery/merge/release on high-risk tasks when dual-control is enabled; cannot configure billing or view raw BYOK keys |
| QA / Staff Reviewer | `Reviewer` | Review/approve/reject `TaskSpec`, `TestPlan`, `ExecutionPlan`, `ReviewReport`, and `DeliveryApproval`; confirm manual acceptance criteria | Cannot manage billing, invite members, or extract raw credentials |
| Product Stakeholder / Auditor | `Viewer` | Read projects, tasks, timelines, verification reports, PR links, and usage summaries | Read-only; cannot mutate any state, create tasks, or approve |
| Finance / Billing Ops | `Admin` / `Owner` (explicit `billing:*` perms) | Manage plans, checkout, invoices, refunds, soft/hard quota alerts | Billing permissions decoupled from `Developer` coding permissions |
| SaaS Platform SRE / Support | `Operator` (control-plane role, outside tenant membership) | Investigate stuck jobs/cleanup failures, reconcile ledger discrepancies, issue manual kill switches | Requires time-bounded `support_access` grant with mandatory reason and audit trail; zero silent access to tenant source/BYOK |

## 2. Supported Task Types & Fast-Track Policy

| Task Type | Description | Default Pipeline Path | Fast-Track Eligibility |
| --- | --- | --- | --- |
| `bugfix` | Localized defect fix with regression test requirement | Full 16-step pipeline (`Red` before `Green` mandatory unless explicitly `tdd_exempt`) | No (`tdd_exempt=false` default) |
| `feature` | Multi-file feature addition or API/UI extension | Full 16-step pipeline with Architect design + subtasks | No |
| `refactor` | Structural code cleanup preserving observable contracts | Full pipeline + baseline no-regression gate | Low-risk single-repo refactor may skip multi-round BA debate if policy allows; still requires Spec + Verification |
| `docs` | Documentation-only or non-runtime comment updates | Lightweight spec + `tdd_exempt=true` with explicit doc/build check evidence | Yes (`docs_only` profile) |
| `chore` | Dependency/CI/tooling maintenance | Requires `allow_config_changes=true` + mandatory `Plan` & `Delivery` human approval | Never auto-skips approval (`allow_config_changes` forces `NEEDS_HUMAN_REVIEW`) |
| `project_scaffold` | Bootstrap new repository from versioned starter template | Template clone + license check + baseline suite check before admitting tasks | Uses versioned template spec |

> **Anti-bypass rule:** High-risk tasks (touching auth, billing, migrations, CI/CD, protected configs, or >1 repo) cannot self-downgrade to fast-track regardless of LLM classification.

## 3. Core Use Cases & Stop/Budget/Permission Matrix

| Use Case ID | Journey | Inputs | Outputs | Required Permissions | Budget & Quota Behavior | Mandatory Stop / Pause States |
| --- | --- | --- | --- | --- | --- | --- |
| `UC-01` | Register, verify account, create org, invite team | Email, password, org name, invitee email + role | Verified `User`, `Tenant`, `Owner` `Membership`, `Session`, `AuditEvent` | Public registration; `members:manage` for invites | Free trial credit grant recorded in `usage_ledger` | Email verification required before repo/task actions; expired/revoked session blocks API (`401`) |
| `UC-02` | Connect GitHub installation or Git repo & validate profile | Installation ID / repo URL, stack profile (`python-pytest`, `node-ts-vitest`, etc.), branch | Bound `Installation`, `Repository`, `RepoProfile`, baseline `EnvironmentCheckReport` | `repo:connect`, `project:manage` | Zero LLM cost; rejects unsupported stack/oversized repo pre-dispatch | `UNSUPPORTED_PROFILE`, `REPO_LIMIT_EXCEEDED`, `INSTALLATION_REVOKED` |
| `UC-03` | Configure model routes, BYOK keys, and budget policy | Role map (`ba/qa/dev/reviewer`), encrypted BYOK key, monthly cap, per-task cap | `ModelPolicy`, `CredentialRef` (masked), capability probe results (`chat`, `tools`, `structured`) | `model:configure` | Probe uses minimal check or mocked transport; no unvalidated model saved as active | `CAPABILITY_CHECK_FAILED`, `INVALID_CREDENTIAL` |
| `UC-04` | Submit task & run BA Spec + QA Critique/TestPlan | Natural-language prompt, project ID, target repo(s), task type, max budget cents | `Task`, `Run`, `RepoContext` (pinned SHA), `TaskSpec` v1, `SpecCritique`, `TestPlan` | `task:create` | Atomic quota reservation (`quota_reservations`) before LLM dispatch | `WAITING_INPUT` (ambiguous requirement), `WAITING_BUDGET` (insufficient quota), `BASELINE_STALE` |
| `UC-05` | Review, manually edit, approve, or reject Spec & TestPlan | Edited spec JSON / approval decision + comment + `spec_sha256` | Immutable `ApprovalRecord` or `TaskSpec` v(N+1) (clears prior approvals) | `spec:approve` | Zero LLM cost on human decision; edits invalidate old digest (`409 STALE_APPROVAL` if stale) | `WAITING_APPROVAL` (worker lease released while waiting) |
| `UC-06` | Generate ExecutionPlan & approve Plan + Cost Reservation | Approved `TaskSpec` + `TestPlan` | `ExecutionPlan` with per-repo `SubtaskSpec`s, sensitive path list, estimated cost | `plan:approve` | Reserves execution + sandbox budget before VM/worktree creation | `WAITING_APPROVAL`, `WAITING_BUDGET` |
| `UC-07` | Test-first authoring, clean baseline Red, coding & bounded repair | Pinned `spec_sha256`, `test_plan_id`, `plan_sha256`, `repo_commit_sha` | Test patch artifact, Red report (`exit_code=1`), Candidate patch artifact | Worker identity (`system:worker` scoped to `run_id`, cannot approve/publish) | Tracks tokens + sandbox seconds per attempt; halts if wall-time/cost/attempt cap reached | `WAITING_BUDGET`, `SPEC_CHANGE_REQUESTED`, `CANCELLED` |
| `UC-08` | Independent Verification & Code Review | Candidate `patch.diff`, `pre_agent_snapshot` / clean base commit | `VerificationReport` (exit codes, JUnit, static gates, AC traceability) + `ReviewReport` | Control-plane Verifier + ChatDev Reviewer (read-only tools) | Reviewer model cost metered separately from Dev | `FAIL`, `NEEDS_HUMAN_REVIEW`, `BLOCKED_ENVIRONMENT`, `INSUFFICIENT_EVIDENCE` |
| `UC-09` | Delivery approval & trusted PR / CI / Preview / Release | Verified `patch_sha256`, `delivery:approve`, optional preview/merge/release action | Draft/ready PR(s) per repo, `ChangeSet`, `PreviewEnvironment`, `ReleaseNotes` + rollback plan | `delivery:approve`, `release:approve` (separate from `Developer`) | Preview runtime metered against tenant quota & auto-expired by janitor | `BASELINE_STALE` (remote head drifted), `CI_FAILED`, `INSTALLATION_REVOKED` |
| `UC-10` | Cancel, pause, resume, retry, or triage errors | User action (`cancel`, `resume`, `retry_stage`) + live membership re-check | Safe worker stop, partial patch export, sandbox cleanup ledger update | `task:cancel` / `task:create` (membership re-verified on resume!) | Releases unused reserved credits; settles confirmed usage | `CANCELLED`, `cleanup_status=FAILED` (queued for janitor) |
| `UC-11` | Subscription, billing, invoices, and webhook reconciliation | Plan selection, Stripe checkout/webhook, refund/downgrade | `Subscription`, `Invoice`, immutable `usage_ledger` entries | `billing:manage` | Concurrency-safe atomic ledger updates; duplicate/out-of-order webhooks idempotent | `PAST_DUE`, `SUSPENDED` (grace period expiration blocks new dispatches) |
| `UC-12` | Audit inspection, tenant data export, and hard delete | Date/actor filters, export request, confirmed delete org request | Signed JSON/ZIP export bundle; crypto-shredded keys & deleted objects/checkpoints | `audit:read`, `data:export`, `data:delete` (`Owner`) | Free of LLM charges; background retention job tracks completion | Revoked access immediately blocks old artifact/SSE URLs (`401`/`404`) |
