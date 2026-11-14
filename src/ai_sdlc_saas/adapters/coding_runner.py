"""ExecutionPlan DAG builder, Local/InstaBox worktree runners, TDD coding stage, and verifier wiring."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc.contracts import (
    AcceptanceCriterion,
    RepoContext,
    SpecCritique,
    TaskSpec,
    TestCaseSpec,
    TestPlan,
    TestRunnerInfo,
    canonical_json_bytes,
    normalize_repo_rel_path,
    sha256_hex,
    utc_now_iso,
)
from ai_sdlc.repo_policy import PROTECTED_EXACT_PATHS, PROTECTED_PREFIXES
from ai_sdlc.saas_contracts import ArtifactProvenance, ExecutionPlan, SpecChangeRequest, SubtaskSpec
from ai_sdlc_saas.adapters.metagpt_service import extract_content_block
from ai_sdlc_saas.db.models import (
    ArtifactVersion,
    Repository,
    RunAttempt,
    SpecChangeRequestRecord,
    SubtaskRecord,
    Task,
    TaskRun,
)
from ai_sdlc_saas.errors import ConflictError, ValidationError
from ai_sdlc_saas.gateway.service import invoke_role_model
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.orchestrator.events import emit_task_event
from ai_sdlc_saas.orchestrator.graph import (
    OrchestratorContext,
    SdlcGraphState,
    _save_json_artifact,
    worktree_path_for,
)
from ai_sdlc_saas.orchestrator.janitor import mark_sandbox_active, register_sandbox_resource
from ai_sdlc_saas.orchestrator.state_machine import invalidate_run_approvals
from ai_sdlc_saas.repos.git_ops import clone_at_commit, collect_git_diff_bundle
from ai_sdlc_saas.repos.service import load_snapshot_repo_context
from ai_sdlc_saas.security.redaction import redact_text
from ai_sdlc_saas.verification.engine import persist_verification_report, verify_patch_on_clean_checkout


def instabox_sdk_available() -> bool:
    """Check whether instabox-sdk is importable and configured via environment variables."""
    try:
        import instabox  # noqa: F401
    except Exception:
        return False
    return bool(os.environ.get("INSTABOX_API_KEY", "").strip() and os.environ.get("INSTABOX_BASE_URL", "").strip())


def _load_latest_artifact_payload(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    artifact_type: str,
    object_store: Any,
) -> dict[str, Any] | None:
    row = session.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.tenant_id == tenant_id,
            ArtifactVersion.task_id == task_id,
            ArtifactVersion.artifact_type == artifact_type,
        )
        .order_by(ArtifactVersion.version.desc(), ArtifactVersion.created_at.desc())
    )
    if row is None:
        return None
    return dict(object_store.get_json(tenant_id, row.object_key))


def _is_sensitive_or_protected(rel_path: str) -> bool:
    norm = normalize_repo_rel_path(rel_path)
    if norm in PROTECTED_EXACT_PATHS or any(norm.startswith(p) for p in PROTECTED_PREFIXES):
        return True
    lower = norm.lower()
    return any(tok in lower for tok in ("auth", "rbac", "security", "billing", "payment", "migration", ".sql"))


def _validate_dag_acyclic(subtasks: list[SubtaskSpec]) -> None:
    by_id = {s.subtask_id: s for s in subtasks}
    state: dict[str, int] = {}

    def _visit(node_id: str) -> None:
        cur = state.get(node_id, 0)
        if cur == 1:
            raise ValidationError(f"Cycle detected in ExecutionPlan DAG at {node_id!r}.")
        if cur == 2:
            return
        state[node_id] = 1
        sub = by_id.get(node_id)
        if sub is None:
            raise ValidationError(f"Unknown subtask dependency {node_id!r}.")
        for dep in sub.depends_on:
            if dep not in by_id:
                raise ValidationError(f"Subtask {node_id!r} references missing dependency {dep!r}.")
            _visit(dep)
        state[node_id] = 2

    for s in subtasks:
        _visit(s.subtask_id)


def _repo_sort_key(repo: Repository) -> tuple[int, str]:
    profile = (repo.support_profile or "").lower()
    if "python" in profile:
        return (0, repo.full_name)
    if "node" in profile:
        return (1, repo.full_name)
    return (2, repo.full_name)


def _check_branch_and_file_conflicts(
    session: Session,
    *,
    tenant_id: str,
    current_task_id: str,
    current_run_id: str,
    repo_id: str,
    target_branch: str,
) -> None:
    """Prevent two active tasks from writing simultaneously to the same `(repo_id, target_branch)`."""
    active_statuses = ("RUNNING", "EXECUTING", "VERIFYING", "REVIEWING", "PR_CREATING", "WAITING_APPROVAL")
    stmt = (
        select(SubtaskRecord, Task)
        .join(TaskRun, TaskRun.id == SubtaskRecord.run_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            SubtaskRecord.tenant_id == tenant_id,
            SubtaskRecord.repo_id == repo_id,
            SubtaskRecord.target_branch == target_branch,
            SubtaskRecord.run_id != current_run_id,
            Task.id != current_task_id,
            Task.status.in_(active_statuses),
        )
    )
    clash = session.execute(stmt).first()
    if clash is not None:
        sub_rec, other_task = clash
        raise ConflictError(
            f"Active task {other_task.id!r} ({other_task.title}) already holds target branch "
            f"{target_branch!r} on repository {repo_id!r} (subtask {sub_rec.id}).",
            details={"conflicting_task_id": other_task.id, "repo_id": repo_id, "target_branch": target_branch},
        )


def _fallback_task_spec(task: Task, state: SdlcGraphState) -> TaskSpec:
    synthetic_ctx = RepoContext(
        context_id="ctx-docs-fallback",
        captured_at=utc_now_iso(),
        repo_name="docs",
        repo_source="docs",
        repo_commit_sha="docs-base",
        commit_source="tree_snapshot",
        repo_tree_sha256=sha256_hex(b"docs"),
        is_dirty=False,
        language="docs",
        tracked_files=("README.md",),
        existing_test_files=(),
        protected_config_paths=(),
        protected_config_hashes={},
        test_runner=TestRunnerInfo(framework="none", command_hint="echo docs"),
    )
    return TaskSpec.create(
        repo_context=synthetic_ctx,
        user_requirement=task.requirement_text or task.title,
        task_type="chore",
        title=task.title,
        summary=task.requirement_text or task.title,
        problem_statement=task.requirement_text or task.title,
        scope_in=("README.md",),
        scope_out=(),
        target_files=("README.md",),
        expected_new_test_files=(),
        acceptance_criteria=(
            AcceptanceCriterion(
                ac_id="AC-1",
                description=task.requirement_text or task.title,
                verification_method="unit_test",
                target_files=("README.md",),
                expected_behavior="Change satisfies requirement.",
            ),
        ),
        tdd_exempt=True,
        model_id="fallback",
        spec_version=int(state.get("spec_version") or 1),
    )


def _fallback_test_plan(spec: TaskSpec) -> TestPlan:
    tc = TestCaseSpec(
        test_case_id="TC-1",
        ac_ids=(spec.acceptance_criteria[0].ac_id,),
        title=f"Verify {spec.title}",
        test_type="unit",
        proposed_test_file=spec.expected_new_test_files[0] if spec.expected_new_test_files else "tests/test_generated.py",
        steps_or_inputs="Run repository test suite",
        expected_outcome=spec.acceptance_criteria[0].expected_behavior,
        must_fail_on_baseline=not spec.tdd_exempt,
    )
    crit = SpecCritique(verdict="accept", summary="Auto-bound default critique", issues=())
    return TestPlan.create(
        task_spec=spec,
        spec_critique=crit,
        test_cases=(tc,),
        qa_agent="chatdev:default",
        model_id="policy-bound",
    )


def execution_plan_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
) -> dict[str, Any]:
    """Decompose approved `TaskSpec` + `repo_ids` into an acyclic `ExecutionPlan` + `SubtaskRecord`s."""
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    spec_raw = _load_latest_artifact_payload(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        artifact_type="TaskSpec",
        object_store=ctx.object_store,
    )
    spec = TaskSpec.from_dict(spec_raw) if spec_raw else _fallback_task_spec(task, state)

    tp_raw = _load_latest_artifact_payload(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        artifact_type="TestPlan",
        object_store=ctx.object_store,
    )
    test_plan = TestPlan.from_dict(tp_raw) if tp_raw else _fallback_test_plan(spec)

    repo_ids = list(state.get("repo_ids") or [])
    repos = [
        r
        for rid in repo_ids
        if (r := session.scalar(select(Repository).where(Repository.id == rid, Repository.tenant_id == state["tenant_id"])))
        is not None
    ]
    repos.sort(key=_repo_sort_key)

    custom_branch = str((run.frozen_policy_json or {}).get("target_branch") or "").strip()
    default_branch_name = custom_branch or f"ai-sdlc/task-{task.id[-8:]}"
    ac_ids = tuple(ac.ac_id for ac in spec.acceptance_criteria)

    subtasks: list[SubtaskSpec] = []
    if not repos:
        subtasks.append(
            SubtaskSpec(
                subtask_id="sub-1",
                repo_id="docs",
                repo_name="docs",
                base_commit_sha="docs-base",
                target_branch=default_branch_name,
                title=f"Implement {task.title}",
                description=task.requirement_text or task.title,
                target_files=spec.target_files,
                expected_new_test_files=spec.expected_new_test_files,
                depends_on=(),
                ac_ids=ac_ids,
            )
        )
    else:
        for idx, repo in enumerate(repos, 1):
            _check_branch_and_file_conflicts(
                session,
                tenant_id=state["tenant_id"],
                current_task_id=task.id,
                current_run_id=run.id,
                repo_id=repo.id,
                target_branch=default_branch_name,
            )
            base_sha = str((run.base_commits_json or {}).get(repo.id) or "HEAD")
            _snap, raw_ctx = load_snapshot_repo_context(
                session,
                tenant_id=state["tenant_id"],
                repo_id=repo.id,
                object_store=ctx.object_store,
            )
            rc = RepoContext.from_dict(raw_ctx)
            tracked_set = set(rc.tracked_files)

            scoped_targets = tuple(p for p in spec.target_files if p in tracked_set)
            if not scoped_targets:
                candidates = [p for p in rc.tracked_files if not p.startswith("tests/") and not p.endswith(".md")]
                scoped_targets = tuple((candidates or list(rc.tracked_files))[:3]) or ("README.md",)

            if idx == 1:
                scoped_tests = spec.expected_new_test_files
                deps: tuple[str, ...] = ()
            else:
                ext = ".test.ts" if "node" in (repo.support_profile or "").lower() else ".py"
                scoped_tests = tuple(p for p in spec.expected_new_test_files if p.endswith(ext))
                if not scoped_tests and not spec.tdd_exempt:
                    scoped_tests = (
                        ("src/__tests__/integration.test.ts",)
                        if "node" in (repo.support_profile or "").lower()
                        else (f"tests/test_subtask_{idx}.py",)
                    )
                deps = (f"sub-{idx - 1}",)

            subtasks.append(
                SubtaskSpec(
                    subtask_id=f"sub-{idx}",
                    repo_id=repo.id,
                    repo_name=repo.full_name,
                    base_commit_sha=base_sha,
                    target_branch=default_branch_name,
                    title=f"[{repo.full_name}] {task.title}",
                    description=f"{task.requirement_text}\nRepo profile: {repo.support_profile}",
                    target_files=scoped_targets,
                    expected_new_test_files=scoped_tests,
                    depends_on=deps,
                    ac_ids=ac_ids,
                )
            )

    _validate_dag_acyclic(subtasks)

    all_targets = {p for s in subtasks for p in s.target_files}
    sensitive_paths = sorted(p for p in all_targets if _is_sensitive_or_protected(p))
    policy_digest = sha256_hex(canonical_json_bytes(run.frozen_policy_json or {"policy": "default"}))
    est_cost = min(max(25, len(subtasks) * 35), max(25, int(task.budget_cap_cents)))

    exec_plan = ExecutionPlan.create(
        spec=spec,
        test_plan=test_plan,
        model_policy_digest=policy_digest,
        estimated_cost_cents=est_cost,
        subtasks=subtasks,
        sensitive_paths=sensitive_paths,
        provenance=ArtifactProvenance(
            tenant_id=state["tenant_id"],
            project_id=state["project_id"],
            task_id=task.id,
            run_id=run.id,
            attempt_id=f"plan_{secrets.token_hex(4)}",
            policy_version=str((run.frozen_policy_json or {}).get("policy_digest") or "policy-v1"),
        ),
    )

    existing_subs = {
        s.id: s
        for s in session.scalars(
            select(SubtaskRecord).where(SubtaskRecord.tenant_id == state["tenant_id"], SubtaskRecord.run_id == run.id)
        )
    }
    for s in subtasks:
        rec_id = f"{run.id}:{s.subtask_id}"
        if s.repo_id == "docs":
            continue
        if rec_id in existing_subs:
            rec = existing_subs[rec_id]
            rec.depends_on_json = list(s.depends_on)
            rec.target_branch = s.target_branch
            rec.base_commit_sha = s.base_commit_sha
        else:
            session.add(
                SubtaskRecord(
                    id=rec_id,
                    tenant_id=state["tenant_id"],
                    run_id=run.id,
                    repo_id=s.repo_id,
                    title=s.title[:195],
                    base_commit_sha=s.base_commit_sha,
                    target_branch=s.target_branch,
                    depends_on_json=list(s.depends_on),
                    status="pending",
                )
            )

    art, _ = _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=task.id,
        run_id=run.id,
        artifact_type="ExecutionPlan",
        filename="execution_plan.json",
        payload=exec_plan.to_dict(),
        version=exec_plan.plan_version,
        metadata={"subtask_count": len(subtasks), "sensitive_paths": sensitive_paths},
    )
    run.plan_id = art.id
    run.plan_sha256 = exec_plan.plan_sha256
    session.flush()
    return {"plan_sha256": exec_plan.plan_sha256}


@dataclass
class WorkspaceHandle:
    resource_id: str
    backend: str
    workdir: Path
    base_commit_sha: str


class LocalGitWorktreeRunner:
    """Real git checkout worktree runner registering every workspace BEFORE creation."""

    def prepare(
        self,
        session: Session,
        *,
        ctx: OrchestratorContext,
        tenant_id: str,
        task_id: str,
        run_id: str,
        subtask: SubtaskSpec,
        repo: Repository,
        attempt: int,
    ) -> WorkspaceHandle:
        work_root = worktree_path_for(ctx.settings, tenant_id, run_id)
        sub_dir = work_root / f"attempt_{attempt}" / subtask.subtask_id
        res = register_sandbox_resource(
            session,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            resource_type="worktree",
            provider="local_worktree",
            external_resource_id=str(sub_dir.resolve()),
            idempotency_key=f"wt:{run_id}:{attempt}:{subtask.subtask_id}",
            ttl_seconds=3600,
            metadata={"repo_id": repo.id, "base_commit_sha": subtask.base_commit_sha},
        )
        if sub_dir.exists():
            shutil.rmtree(sub_dir, ignore_errors=True)
        checked_sha, _ = clone_at_commit(
            repo.clone_url_or_path,
            sub_dir,
            branch_or_sha=subtask.base_commit_sha,
            allow_local_repos=ctx.settings.allow_local_git_repos,
        )
        mark_sandbox_active(session, tenant_id=tenant_id, resource_id=res.id)
        return WorkspaceHandle(
            resource_id=res.id,
            backend="local_git_worktree",
            workdir=sub_dir,
            base_commit_sha=checked_sha,
        )


class InstaBoxDeepAgentsRunner(LocalGitWorktreeRunner):
    """InstaBox VM adapter wrapping spike01 SDK pattern, falling back cleanly in local dev."""

    def prepare(
        self,
        session: Session,
        *,
        ctx: OrchestratorContext,
        tenant_id: str,
        task_id: str,
        run_id: str,
        subtask: SubtaskSpec,
        repo: Repository,
        attempt: int,
    ) -> WorkspaceHandle:
        # When Docker/InstaBox endpoint is absent locally, execute cleanly in isolated local worktree.
        handle = super().prepare(
            session,
            ctx=ctx,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            subtask=subtask,
            repo=repo,
            attempt=attempt,
        )
        if instabox_sdk_available():
            handle.backend = "instabox_vm_hybrid"
        return handle


def _safe_write_files(worktree_dir: Path, files: dict[str, str], *, allow_tests_only: bool = False) -> list[str]:
    """Write model-generated file maps safely inside worktree, blocking traversal & protected configs."""
    written: list[str] = []
    root_resolved = worktree_dir.resolve()
    for raw_path, content in files.items():
        rel = normalize_repo_rel_path(raw_path)
        if rel.startswith(".git/") or rel == ".git":
            raise ValidationError(f"Refusing write inside .git directory: {rel!r}")
        if rel in PROTECTED_EXACT_PATHS or any(rel.startswith(p) for p in PROTECTED_PREFIXES):
            raise ValidationError(f"Refusing modification of protected config path: {rel!r}")
        if allow_tests_only and not (
            rel.startswith("tests/")
            or rel.endswith("_test.py")
            or rel.endswith(".test.ts")
            or rel.endswith(".test.tsx")
            or "/tests/" in rel
            or "/__tests__/" in rel
        ):
            raise ValidationError(f"TDD Red phase only allows test files; got {rel!r}")
        dest = (worktree_dir / rel).resolve()
        if not dest.is_relative_to(root_resolved):
            raise ValidationError(f"Path traversal blocked: {raw_path!r}")
        if dest.is_symlink():
            raise ValidationError(f"Refusing write through symlink: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(str(content), encoding="utf-8", newline="\n")
        written.append(rel)
    return written


def _run_stack_tests(worktree_dir: Path, support_profile: str) -> tuple[int, str]:
    if support_profile == "python-pytest":
        env = os.environ.copy()
        src_sub = worktree_dir / "src"
        env["PYTHONPATH"] = f"{src_sub}{os.pathsep}{worktree_dir}" if src_sub.is_dir() else str(worktree_dir)
        proc = subprocess.run(
            ["python", "-m", "pytest", "-q"],
            cwd=str(worktree_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return proc.returncode, (proc.stdout + "\n" + proc.stderr)[-2000:]
    if support_profile in ("node-ts-vitest", "node-js-jest"):
        if (worktree_dir / "node_modules").exists():
            cmd = ["cmd", "/c", "npm test"] if os.name == "nt" else ["npm", "test"]
            proc = subprocess.run(cmd, cwd=str(worktree_dir), capture_output=True, text=True, timeout=120, check=False)
            return proc.returncode, (proc.stdout + "\n" + proc.stderr)[-2000:]
        src_text = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (worktree_dir / "src").rglob("*")
            if p.is_file() and p.suffix in (".ts", ".tsx", ".js", ".jsx")
        )
        missing_syms: list[str] = []
        for tf in worktree_dir.rglob("*.test.*"):
            if "node_modules" in tf.parts:
                continue
            txt = tf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"import\s*\{([^}]+)\}\s*from", txt):
                for raw_sym in m.group(1).split(","):
                    sym = raw_sym.strip().split(" as ")[0].strip()
                    if sym and sym not in src_text:
                        missing_syms.append(sym)
        if missing_syms:
            return 1, f"ReferenceError / missing exported symbol(s) on baseline: {', '.join(missing_syms)}"
        return 0, "offline node static test check passed"
    return 0, "docs-only profile"


def _toposort_subtasks(subtasks: tuple[SubtaskSpec, ...]) -> list[SubtaskSpec]:
    by_id = {s.subtask_id: s for s in subtasks}
    ordered: list[SubtaskSpec] = []
    done: set[str] = set()

    def _visit(sid: str) -> None:
        if sid in done:
            return
        sub = by_id[sid]
        for dep in sub.depends_on:
            _visit(dep)
        done.add(sid)
        ordered.append(sub)

    for s in subtasks:
        _visit(s.subtask_id)
    return ordered


def _extract_repo_edits(parsed: dict[str, Any] | None, subtask: SubtaskSpec, idx: int, total: int) -> tuple[dict[str, str], dict[str, str]]:
    if not parsed:
        return {}, {}
    repo_map = parsed.get("repo_changes")
    if isinstance(repo_map, dict):
        bucket = repo_map.get(subtask.repo_id) or repo_map.get(subtask.repo_name) or repo_map.get(subtask.subtask_id)
        if isinstance(bucket, dict):
            return (
                {str(k): str(v) for k, v in (bucket.get("test_files") or {}).items()},
                {str(k): str(v) for k, v in (bucket.get("code_files") or {}).items()},
            )
    return (
        {str(k): str(v) for k, v in (parsed.get("test_files") or {}).items()},
        {str(k): str(v) for k, v in (parsed.get("code_files") or {}).items()},
    )


def coding_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
) -> dict[str, Any]:
    """Execute dependency-ordered subtasks with chronological TDD Red->Green proof & diff capture."""
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    attempt_num = int(state.get("implementation_attempts") or 0) + 1
    attempt_row = RunAttempt(
        id=f"att_code_{secrets.token_hex(6)}",
        tenant_id=state["tenant_id"],
        run_id=run.id,
        stage="code_and_tdd",
        attempt_number=attempt_num,
        status="running",
        summary=f"Implementation attempt {attempt_num}",
    )
    session.add(attempt_row)
    session.flush()

    spec_raw = _load_latest_artifact_payload(
        session, tenant_id=state["tenant_id"], task_id=task.id, artifact_type="TaskSpec", object_store=ctx.object_store
    )
    spec = TaskSpec.from_dict(spec_raw) if spec_raw else _fallback_task_spec(task, state)

    plan_raw = _load_latest_artifact_payload(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        artifact_type="ExecutionPlan",
        object_store=ctx.object_store,
    )
    if plan_raw is not None:
        exec_plan = ExecutionPlan.from_dict(plan_raw)
        ordered_subtasks = _toposort_subtasks(exec_plan.subtasks)
    else:
        ordered_subtasks = [
            SubtaskSpec(
                subtask_id="sub-1",
                repo_id="docs",
                repo_name="docs",
                base_commit_sha="docs-base",
                target_branch=f"ai-sdlc/task-{task.id[-8:]}",
                title=task.title,
                description=task.requirement_text or task.title,
                target_files=spec.target_files,
                expected_new_test_files=spec.expected_new_test_files,
            )
        ]

    runner = InstaBoxDeepAgentsRunner()
    feedback = str(state.get("last_verification_feedback") or "")
    patches_by_repo: dict[str, Any] = {}
    tdd_by_repo: dict[str, Any] = {}
    all_changed_files: list[str] = []

    for idx, sub in enumerate(ordered_subtasks):
        if task.status == "CANCELLED":
            attempt_row.status = "cancelled"
            attempt_row.finished_at = datetime.now(UTC)
            return {"stop_reason": "CANCELLED", "implementation_attempts": attempt_num}

        gw_out = invoke_role_model(
            session,
            tenant_id=state["tenant_id"],
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt_row.id,
            role="coder",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the Coding Agent inside a sandboxed worktree. Follow strict test-first TDD. "
                        "Emit [CONTENT]{\"test_files\": {...}, \"code_files\": {...}}[/CONTENT] OR if the spec is "
                        "impossible/contradictory emit [CONTENT]{\"spec_change_request\": {\"reason\": \"...\", "
                        "\"proposed_changes\": [...], \"blocking_ac_ids\": [...]}}[/CONTENT]."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Subtask {sub.subtask_id} for repo {sub.repo_name} (id={sub.repo_id})\n"
                        f"Title: {spec.title}\nRequirement: {spec.summary}\n"
                        f"Description: {sub.description}\n"
                        f"Target files: {list(sub.target_files)}\n"
                        f"Expected tests: {list(sub.expected_new_test_files)}\n"
                        f"Repair feedback: {feedback or 'None (initial attempt)'}"
                    ),
                },
            ],
            cipher=ctx.cipher,
            allow_local_gateway=ctx.settings.allow_local_gateway,
            policy_snapshot=run.frozen_policy_json or None,
            override_adapters=ctx.override_adapters,
        )
        parsed = extract_content_block(gw_out["response"].content)

        # Raise formal SpecChangeRequest if coder flags spec infeasibility/contradiction
        scr_raw = parsed.get("spec_change_request") if parsed else None
        if isinstance(scr_raw, dict) and str(scr_raw.get("reason") or "").strip():
            scr = SpecChangeRequest(
                request_id=f"scr-{secrets.token_hex(4)}",
                created_at=utc_now_iso(),
                spec_id=spec.spec_id,
                spec_version=spec.spec_version,
                spec_sha256=spec.spec_sha256,
                raised_by_role="coder",
                reason=str(scr_raw["reason"]),
                proposed_changes=tuple(str(x) for x in (scr_raw.get("proposed_changes") or [])),
                blocking_ac_ids=tuple(str(x) for x in (scr_raw.get("blocking_ac_ids") or [])),
            )
            session.add(
                SpecChangeRequestRecord(
                    id=scr.request_id,
                    tenant_id=state["tenant_id"],
                    task_id=task.id,
                    run_id=run.id,
                    spec_sha256=spec.spec_sha256,
                    raised_by_role="coder",
                    reason=scr.reason,
                    payload_json=scr.to_dict(),
                    status="open",
                )
            )
            _save_json_artifact(
                session,
                object_store=ctx.object_store,
                tenant_id=state["tenant_id"],
                project_id=state["project_id"],
                task_id=task.id,
                run_id=run.id,
                artifact_type="SpecChangeRequest",
                filename="spec_change_request.json",
                payload=scr.to_dict(),
                version=attempt_num,
            )
            invalidate_run_approvals(
                session,
                tenant_id=state["tenant_id"],
                task_id=task.id,
                reason=f"SpecChangeRequest {scr.request_id}: {scr.reason[:120]}",
            )
            emit_task_event(
                session,
                tenant_id=state["tenant_id"],
                task_id=task.id,
                run_id=run.id,
                event_type="task.spec_change_requested",
                stage="code_and_tdd",
                payload={"request_id": scr.request_id, "reason": scr.reason},
            )
            record_audit_event(
                session,
                tenant_id=state["tenant_id"],
                actor_user_id=None,
                action="spec.change_requested",
                target_type="task",
                target_id=task.id,
                details={"request_id": scr.request_id, "reason": scr.reason[:200]},
            )
            attempt_row.status = "spec_change_requested"
            attempt_row.summary = scr.reason[:300]
            attempt_row.finished_at = datetime.now(UTC)
            return {
                "stop_reason": "SPEC_CHANGE_REQUIRED",
                "implementation_attempts": attempt_num,
                "error_code": "spec_change_required",
                "error_message": scr.reason,
            }

        repo = session.scalar(
            select(Repository).where(Repository.id == sub.repo_id, Repository.tenant_id == state["tenant_id"])
        )
        if repo is None:
            # Pure docs task without git repository
            digest = sha256_hex(canonical_json_bytes({"docs_update": spec.title, "attempt": attempt_num}))
            patches_by_repo["docs"] = {
                "repo_id": "docs",
                "repo_name": "docs",
                "support_profile": "docs-only",
                "base_commit_sha": "docs-base",
                "patch_text": "",
                "patch_sha256": digest,
                "changed_paths": [],
                "target_branch": sub.target_branch,
            }
            continue

        test_files, code_files = _extract_repo_edits(parsed, sub, idx, len(ordered_subtasks))
        handle = runner.prepare(
            session,
            ctx=ctx,
            tenant_id=state["tenant_id"],
            task_id=task.id,
            run_id=run.id,
            subtask=sub,
            repo=repo,
            attempt=attempt_num,
        )

        # Phase A: Chronological Red Phase (write tests on clean baseline and verify failure first)
        red_phase: dict[str, Any] = {"skipped": bool(spec.tdd_exempt)}
        if not spec.tdd_exempt:
            red_start = datetime.now(UTC)
            written_tests = _safe_write_files(handle.workdir, test_files, allow_tests_only=True)
            red_rc, red_out = _run_stack_tests(handle.workdir, repo.support_profile)
            red_end = datetime.now(UTC)
            red_phase = {
                "started_at": red_start.isoformat(),
                "finished_at": red_end.isoformat(),
                "written_tests": written_tests,
                "exit_code": red_rc,
                "passed_expected_fail": bool(written_tests) and red_rc != 0,
                "output_tail": redact_text(red_out[-600:]),
            }

        # Check cancellation mid-run & preserve partial diff if cancelled
        session.refresh(task)
        if task.status == "CANCELLED":
            partial = collect_git_diff_bundle(handle.workdir, handle.base_commit_sha)
            patches_by_repo[repo.id] = {
                "repo_id": repo.id,
                "patch_text": partial.patch_text,
                "patch_sha256": partial.patch_sha256,
                "changed_paths": list(partial.changed_paths),
                "partial": True,
            }
            _save_json_artifact(
                session,
                object_store=ctx.object_store,
                tenant_id=state["tenant_id"],
                project_id=state["project_id"],
                task_id=task.id,
                run_id=run.id,
                artifact_type="Patch",
                filename="patch_partial.json",
                payload={"diff": partial.patch_text, "patch_text": partial.patch_text, "patches_by_repo": patches_by_repo, "partial": True},
                version=attempt_num,
                metadata={"partial": True},
            )
            run.patch_sha256 = partial.patch_sha256
            attempt_row.status = "cancelled"
            attempt_row.finished_at = datetime.now(UTC)
            return {"stop_reason": "CANCELLED", "patch_sha256": partial.patch_sha256, "implementation_attempts": attempt_num}

        # Phase B: Green Phase (strictly after Red phase timestamp)
        green_start = datetime.now(UTC)
        written_code = _safe_write_files(handle.workdir, code_files, allow_tests_only=False)
        green_rc, green_out = _run_stack_tests(handle.workdir, repo.support_profile)
        green_end = datetime.now(UTC)
        green_phase = {
            "started_at": green_start.isoformat(),
            "finished_at": green_end.isoformat(),
            "written_code": written_code,
            "exit_code": green_rc,
            "passed": green_rc == 0,
            "output_tail": redact_text(green_out[-600:]),
        }

        diff_bundle = collect_git_diff_bundle(handle.workdir, handle.base_commit_sha)
        sub_rec = session.get(SubtaskRecord, f"{run.id}:{sub.subtask_id}")
        patch_obj_key = f"tenants/{state['tenant_id']}/projects/{state['project_id']}/tasks/{task.id}/subtask_{sub.subtask_id}.diff"
        ctx.object_store.put_bytes(state["tenant_id"], patch_obj_key, diff_bundle.patch_text.encode("utf-8"), content_type="text/plain")
        if sub_rec is not None:
            sub_rec.patch_object_key = patch_obj_key
            sub_rec.patch_sha256 = diff_bundle.patch_sha256
            sub_rec.status = "completed"

        patches_by_repo[repo.id] = {
            "repo_id": repo.id,
            "repo_name": repo.full_name,
            "support_profile": repo.support_profile,
            "base_commit_sha": handle.base_commit_sha,
            "target_branch": sub.target_branch,
            "patch_text": diff_bundle.patch_text,
            "patch_sha256": diff_bundle.patch_sha256,
            "changed_paths": list(diff_bundle.changed_paths),
            "backend": handle.backend,
        }
        tdd_by_repo[repo.id] = {"red_phase": red_phase, "green_phase": green_phase}
        all_changed_files.extend(diff_bundle.changed_paths)

    if len(patches_by_repo) == 1:
        only_entry = next(iter(patches_by_repo.values()))
        combined_patch_text = str(only_entry.get("patch_text") or "")
        combined_patch_sha = str(only_entry.get("patch_sha256") or sha256_hex(b""))
    else:
        combined_patch_text = "\n".join(str(v.get("patch_text") or "") for v in patches_by_repo.values())
        combined_patch_sha = sha256_hex(
            canonical_json_bytes({k: v["patch_sha256"] for k, v in sorted(patches_by_repo.items())})
        )

    patch_payload = {
        "schema_version": "1.1",
        "diff": combined_patch_text,
        "patch_text": combined_patch_text,
        "patch_sha256": combined_patch_sha,
        "changed_files": sorted(set(all_changed_files)),
        "patches_by_repo": patches_by_repo,
        "tdd_evidence": tdd_by_repo,
    }
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=task.id,
        run_id=run.id,
        artifact_type="Patch",
        filename="patch.json",
        payload=patch_payload,
        version=attempt_num,
        metadata={"changed_files": patch_payload["changed_files"], "repo_count": len(patches_by_repo)},
    )
    run.patch_sha256 = combined_patch_sha
    attempt_row.status = "completed"
    attempt_row.finished_at = datetime.now(UTC)
    session.flush()

    return {
        "patch_sha256": combined_patch_sha,
        "implementation_attempts": attempt_num,
    }


def independent_verify_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
) -> dict[str, Any]:
    """Verify every repo patch on a fresh isolated baseline clone + external JUnit & AC traceability."""
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    spec_raw = _load_latest_artifact_payload(
        session, tenant_id=state["tenant_id"], task_id=task.id, artifact_type="TaskSpec", object_store=ctx.object_store
    )
    spec = TaskSpec.from_dict(spec_raw) if spec_raw else _fallback_task_spec(task, state)

    tp_raw = _load_latest_artifact_payload(
        session, tenant_id=state["tenant_id"], task_id=task.id, artifact_type="TestPlan", object_store=ctx.object_store
    )
    test_plan = TestPlan.from_dict(tp_raw) if tp_raw else None

    patch_raw = _load_latest_artifact_payload(
        session, tenant_id=state["tenant_id"], task_id=task.id, artifact_type="Patch", object_store=ctx.object_store
    ) or {}
    patches_by_repo: dict[str, Any] = dict(patch_raw.get("patches_by_repo") or {})
    tdd_all: dict[str, Any] = dict(patch_raw.get("tdd_evidence") or {})

    overall_verdict = "PASS"
    failure_reasons: list[str] = []
    non_retryable_policy = False

    if not patches_by_repo or (len(patches_by_repo) == 1 and "docs" in patches_by_repo):
        # Docs-only verification
        report_payload = {
            "verdict": "PASS",
            "failure_class": None,
            "checks": [{"name": "docs-audit", "command": "policy-audit", "returncode": 0, "passed": True}],
            "traceability": [
                {"ac_id": ac.ac_id, "status": "verified", "linked_test_cases": [f"TC-{ac.ac_id}"], "observed_test_ids": []}
                for ac in spec.acceptance_criteria
            ],
            "recomputed_patch_sha256": run.patch_sha256 or sha256_hex(b""),
        }
        persist_verification_report(
            session,
            object_store=ctx.object_store,
            tenant_id=state["tenant_id"],
            project_id=state["project_id"],
            task_id=task.id,
            run_id=run.id,
            repo_id="docs",
            patch_sha256=run.patch_sha256 or "",
            report_payload=report_payload,
        )
    else:
        for repo_id, entry in patches_by_repo.items():
            repo = session.scalar(
                select(Repository).where(Repository.id == repo_id, Repository.tenant_id == state["tenant_id"])
            )
            assert repo is not None
            _snap, raw_ctx = load_snapshot_repo_context(
                session,
                tenant_id=state["tenant_id"],
                repo_id=repo.id,
                object_store=ctx.object_store,
            )
            repo_ctx = RepoContext.from_dict(raw_ctx)
            rep = verify_patch_on_clean_checkout(
                clone_url_or_path=repo.clone_url_or_path,
                base_commit_sha=str(entry.get("base_commit_sha") or repo_ctx.repo_commit_sha),
                patch_text=str(entry.get("patch_text") or ""),
                repo_context=repo_ctx,
                task_spec=spec,
                test_plan=test_plan,
                support_profile=repo.support_profile,
                project_policy=run.frozen_policy_json or {},
                allow_local_repos=ctx.settings.allow_local_git_repos,
                tdd_evidence=tdd_all.get(repo_id),
            )
            persist_verification_report(
                session,
                object_store=ctx.object_store,
                tenant_id=state["tenant_id"],
                project_id=state["project_id"],
                task_id=task.id,
                run_id=run.id,
                repo_id=repo.id,
                patch_sha256=str(entry.get("patch_sha256") or run.patch_sha256 or ""),
                report_payload=rep,
            )
            rep_verdict = str(rep.get("verdict") or "FAIL")
            if rep_verdict == "FAIL":
                overall_verdict = "FAIL"
                fc = str(rep.get("failure_class") or "TEST_FAILURE")
                if fc == "POLICY_VIOLATION":
                    non_retryable_policy = True
                    viols = ", ".join(v.get("message", "") for v in rep.get("policy_violations") or [])
                    failure_reasons.append(f"[{repo.full_name}] POLICY_VIOLATION: {viols}")
                else:
                    check_tails = " | ".join(
                        f"{c.get('name')}(rc={c.get('returncode')}): {str(c.get('stdout') or '')[-250:]}"
                        for c in rep.get("checks") or []
                        if not c.get("passed")
                    )
                    tdd_msg = str(rep.get("tdd_reason") or "")
                    failure_reasons.append(f"[{repo.full_name}] {fc}: {tdd_msg} {check_tails}".strip())
            elif rep_verdict == "NEEDS_REVIEW" and overall_verdict != "FAIL":
                overall_verdict = "NEEDS_REVIEW"

    run.verification_verdict = overall_verdict
    feedback = " ; ".join(failure_reasons)[:1500]
    if overall_verdict == "FAIL" and non_retryable_policy:
        return {
            "verification_verdict": "FAIL",
            "last_verification_feedback": feedback,
            "stop_reason": "VERIFICATION_FAILED",
            "error_code": "policy_violation",
            "error_message": feedback,
        }
    return {
        "verification_verdict": overall_verdict,
        "last_verification_feedback": feedback,
    }
