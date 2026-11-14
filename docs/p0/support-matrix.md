# P0 — Official Support Matrix & Operational Limits

- **Document ID:** `DOC-P0-SUPPORT-MATRIX-001`
- **Owner:** Runtime & Verification Team
- **Effective Baseline:** GA Release Scope

## 1. Official Language & Framework Support Matrix

Repositories outside this matrix are **rejected explicitly at onboarding / task intake** (`422 UNSUPPORTED_PROFILE`) before reserving or spending LLM budget.

| Profile ID | Language / Runtime | Supported Package Managers | Test Runner | Lint / Type / Build Gates | Ephemeral Service Support |
| --- | --- | --- | --- | --- | --- |
| `python-pytest` | Python 3.11 – 3.13 | `pip`, `uv`, `poetry` (via lockfile/pyproject) | `pytest --junitxml=...` (exit code `0` required for Green; `1` required for Red) | `ruff check`, `mypy` (optional per project policy), `python -m build` (optional) | PostgreSQL / Redis recipe hooks via isolated test network |
| `node-ts-vitest` | Node.js 20 – 22 + TypeScript 5+ | `npm` (`package-lock.json`), `pnpm` | `vitest run --reporter=junit` / `npm test` | `eslint`, `tsc --noEmit`, `npm run build` | Ephemeral test container recipe hooks |
| `node-js-jest` | Node.js 20 – 22 + JavaScript/TS | `npm` (`package-lock.json`) | `jest --ci` / `npm test` | `eslint`, `npm run build` | Ephemeral test container recipe hooks |
| `docs-only` | Markdown / reStructuredText / OpenAPI docs | N/A | Link/schema/markdown validation (`tdd_exempt=true` permitted) | Secret scan + forbidden path check + optional docs build | None |

### Explicitly Unsupported at GA (Rejected Pre-Dispatch)
- Compiled monorepos requiring Go, Rust, C++, Java/Maven/Gradle, .NET, mobile iOS/Android Xcode/Gradle builds, or GPU/CUDA kernels.
- Private registries requiring interactive SSO/MFA during sandbox `install`.
- Repositories containing unignored secrets (`.env`, `.pem`, private keys) or symlink escapes in tracked tree.

## 2. Repository Size, Context, and Execution Limits

| Limit Parameter | Default GA Limit | Enterprise Configurable Cap | Failure Behavior |
| --- | --- | --- | --- |
| Max tracked files per repo | 2,500 files | 10,000 files | Rejected at RepoContext capture (`REPO_TOO_LARGE`) |
| Max repository checkout archive | 50 MiB | 200 MiB | Rejected before sandbox provisioning |
| Max single inspectable file size | 1 MiB | 2 MiB | File omitted from excerpt summary with metadata notice |
| Max BA/QA prompt excerpt budget | 32,000 chars | 64,000 chars | Deterministic priority ordering (`source` -> `test` -> `config` -> `docs`) |
| Single command `exec` timeout | 120s default (hard max 600s, InstaBox SDK cap) | 600s hard ceiling | Returns `timed_out=True`; never auto-retried on ambiguous write |
| Max coding agent steps per attempt | 60 steps (max 80) | 120 steps | Stops safely, exports partial patch, surfaces `NEEDS_HUMAN_REVIEW` / `FAIL` |
| Max bounded repair loops | 3 attempts | 5 attempts | Stops with classified reason (`TEST_FAILURE`, `ENV_FAILURE`, `SPEC_INVALID`) |
| Max task wall time | 30 minutes (`1800s`) | 60 minutes (`3600s`) | Safe cancel + partial patch export + janitor cleanup |
| Ephemeral preview TTL | 4 hours | 24 hours | Auto-destroyed by janitor; hard quota max 2 active previews/tenant on Pro |

## 3. Multi-Repository Policy

1. **Decomposition:** A single `Task` within a multi-repo `Project` creates one parent `TaskSpec` and separate per-repo `SubtaskSpec` records (`repo_id`, `base_commit_sha`, `branch_name`, `dependency_order`).
2. **Isolation:** Each repository gets its own clean baseline capture, sandbox worktree/VM, independent verification run, `VerificationReport`, and `PullRequest`.
3. **Non-Atomic Delivery Guarantee:** The product explicitly documents and enforces that **multi-repo PR merges are NOT atomic across GitHub repositories**. Each repo PR tracks its own merge/CI state inside a `ChangeSet`; partial merge failures surface explicit rollback instructions.

## 4. Preview / Staging vs Merge / Production Release Policy

- **Preview/Staging:** Automatically deployable only to tenant-isolated ephemeral preview environments with randomized access tokens, zero production credentials, and enforced TTL.
- **Merge / Release / Production Handoff:** Requires explicit `delivery:approve` / `release:approve` permission from an authorized `Admin` or `Owner`, verified `VerificationReport.verdict == "PASS"`, zero unresolved `blocker` review findings, green required CI checks, and matching `patch_sha256` against the remote base branch head.
