"""MetaGPT-native Business Analyst & Architect service integrated with Gateway and TaskSpec v1.1."""

from __future__ import annotations

import difflib
import json
import re
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc.contracts import (
    AcceptanceCriterion,
    RepoContext,
    TaskSpec,
    TaskType,
    TestRunnerInfo,
    canonical_json_bytes,
    sha256_hex,
    validate_task_spec_against_repo,
)
from ai_sdlc_saas.db.models import ArtifactVersion, Task, TaskRun
from ai_sdlc_saas.errors import ConflictError, NotFoundError, ValidationError
from ai_sdlc_saas.gateway.service import invoke_role_model
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.orchestrator.graph import OrchestratorContext, SdlcGraphState, _save_json_artifact
from ai_sdlc_saas.orchestrator.state_machine import invalidate_run_approvals
from ai_sdlc_saas.repos.service import load_snapshot_repo_context

_CONTENT_RE = re.compile(r"\[CONTENT\](.*?)\[/CONTENT\]", re.DOTALL | re.IGNORECASE)
_PATH_TOKEN_RE = re.compile(r"\b([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:py|ts|tsx|js|jsx|md))\b")

_VAGUE_PATTERNS = (
    "improve the app",
    "make it better",
    "fix stuff",
    "do whatever",
    "etc.",
    "something like",
    "tbd",
)


def extract_content_block(raw_text: str) -> dict[str, Any] | None:
    """Extract JSON object from MetaGPT `[CONTENT]...[/CONTENT]` delimiters if present."""
    match = _CONTENT_RE.search(raw_text or "")
    candidate = match.group(1).strip() if match else (raw_text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 2:
            candidate = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    if not candidate.startswith("{"):
        return None
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def analyze_requirement_gaps(
    requirement_text: str,
    repo_context: RepoContext,
    *,
    all_tracked_files: set[str] | None = None,
) -> list[str]:
    """Detect ambiguity, contradiction, or hallucinated repository path references."""
    questions: list[str] = []
    lowered = requirement_text.lower().strip()
    words = [w for w in re.split(r"\s+", lowered) if w]

    if len(words) < 5 or any(p in lowered for p in _VAGUE_PATTERNS):
        questions.append(
            "What specific input, output, or observable behavior should change and which file/function owns it?"
        )

    has_contradiction = (
        ("always" in lowered and "never" in lowered)
        or ("enable auth" in lowered and "skip auth" in lowered)
        or ("disable" in lowered and "must allow" in lowered)
    )
    if has_contradiction:
        questions.append(
            "The requirement contains potentially conflicting rules ('always/enable' vs 'never/skip'); which behavior takes precedence?"
        )

    tracked = set(repo_context.tracked_files)
    if all_tracked_files:
        tracked |= all_tracked_files
    mentioned_paths = _PATH_TOKEN_RE.findall(requirement_text)
    if mentioned_paths:
        unknown = [p for p in mentioned_paths if p not in tracked and "create" not in lowered and "new file" not in lowered]
        if unknown:
            questions.append(
                f"Referenced file(s) {', '.join(unknown)} do not exist in the repository snapshot; should they be created or mapped to existing tracked files?"
            )
    return questions


def _pick_default_source_targets(repo_context: RepoContext) -> tuple[str, ...]:
    source_files = [
        f.path
        for f in repo_context.file_contexts
        if f.role == "source"
    ]
    if source_files:
        return tuple(source_files[:3])
    docs_files = [f.path for f in repo_context.file_contexts if f.role == "docs"]
    if docs_files:
        return tuple(docs_files[:2])
    other = [
        p
        for p in repo_context.tracked_files
        if p not in repo_context.existing_test_files and p not in repo_context.protected_config_paths
    ]
    return tuple(other[:2])


def _propose_new_test_path(repo_context: RepoContext, slug_hint: str) -> str:
    safe_slug = re.sub(r"[^a-z0-9_]+", "_", slug_hint.lower()).strip("_") or "task"
    safe_slug = safe_slug[:28]
    existing = set(repo_context.tracked_files)
    ext = ".test.ts" if repo_context.language in ("typescript",) else (".test.js" if repo_context.language == "javascript" else ".py")
    prefix = f"tests/test_{safe_slug}" if ext == ".py" else f"tests/{safe_slug}"
    candidate = f"{prefix}{ext}"
    idx = 2
    while candidate in existing:
        candidate = f"{prefix}_{idx}{ext}"
        idx += 1
    return candidate


def _map_contract_task_type(task_type: str) -> TaskType:
    if task_type in ("bugfix", "feature", "refactor", "chore"):
        return task_type  # type: ignore[return-value]
    if task_type == "tests":
        return "feature"
    return "chore"


def synthesize_grounded_task_spec(
    *,
    repo_context: RepoContext,
    task: Task,
    model_id: str,
    clarification_answers: dict[str, str] | None = None,
    llm_draft: dict[str, Any] | None = None,
    spec_version: int = 1,
    spec_id: str | None = None,
) -> TaskSpec:
    """Build and RepoContext-validate a `TaskSpec` from LLM draft or deterministic grounding."""
    req = task.requirement_text
    if clarification_answers:
        qa_lines = "\n".join(f"- Q: {k}\n  A: {v}" for k, v in sorted(clarification_answers.items()))
        req = f"{task.requirement_text}\n\nClarifications:\n{qa_lines}"

    contract_tt = _map_contract_task_type(task.task_type)
    tdd_exempt = task.task_type == "docs" or repo_context.language == "docs"
    default_targets = _pick_default_source_targets(repo_context)
    default_new_test = () if tdd_exempt else (_propose_new_test_path(repo_context, task.title),)

    if llm_draft:
        tracked_set = set(repo_context.tracked_files)
        raw_targets = [
            p for p in llm_draft.get("target_files", [])
            if p in tracked_set and p not in repo_context.existing_test_files and p not in repo_context.protected_config_paths
        ]
        targets = tuple(raw_targets) or default_targets
        raw_new_tests = tuple(str(p) for p in llm_draft.get("expected_new_test_files", []) if p)
        new_tests = raw_new_tests or default_new_test
        raw_acs = llm_draft.get("acceptance_criteria") or []
        acs: list[AcceptanceCriterion] = []
        for idx, item in enumerate(raw_acs, 1):
            if not isinstance(item, dict):
                continue
            ac_targets = tuple(p for p in (item.get("target_files") or ()) if p in tracked_set) or targets[:1]
            acs.append(
                AcceptanceCriterion(
                    ac_id=str(item.get("ac_id") or f"AC-{idx}"),
                    description=str(item.get("description") or task.title),
                    expected_behavior=str(item.get("expected_behavior") or "Behavior matches specification."),
                    verification_method="static_check" if tdd_exempt else str(item.get("verification_method") or "unit_test"),  # type: ignore[arg-type]
                    target_files=ac_targets,
                )
            )
        if not acs:
            acs = [
                AcceptanceCriterion(
                    ac_id="AC-1",
                    description=task.title,
                    expected_behavior=req[:300],
                    verification_method="static_check" if tdd_exempt else "unit_test",
                    target_files=targets[:1],
                )
            ]
        spec = TaskSpec.create(
            repo_context=repo_context,
            user_requirement=req,
            task_type=contract_tt,
            title=str(llm_draft.get("title") or task.title),
            summary=str(llm_draft.get("summary") or task.title),
            problem_statement=str(llm_draft.get("problem_statement") or req),
            scope_in=tuple(llm_draft.get("scope_in") or list(targets)),
            scope_out=tuple(llm_draft.get("scope_out") or ["Protected configs and existing test files remain unchanged"]),
            target_files=targets,
            expected_new_test_files=new_tests,
            acceptance_criteria=tuple(acs),
            constraints=tuple(llm_draft.get("constraints") or ["Respect Repository Policy and existing public contracts"]),
            tdd_exempt=tdd_exempt,
            author_agent="metagpt:ProductManager",
            model_id=model_id,
            spec_id=spec_id,
            spec_version=spec_version,
        )
    else:
        spec = TaskSpec.create(
            repo_context=repo_context,
            user_requirement=req,
            task_type=contract_tt,
            title=task.title,
            summary=f"{task.title} on {', '.join(default_targets) or repo_context.repo_name}",
            problem_statement=req,
            scope_in=default_targets or (repo_context.tracked_files[:1] or ("README.md",)),
            scope_out=("Do not edit existing test files or protected configuration files.",),
            target_files=default_targets,
            expected_new_test_files=default_new_test,
            acceptance_criteria=(
                AcceptanceCriterion(
                    ac_id="AC-1",
                    description=task.title,
                    expected_behavior=req[:300],
                    verification_method="static_check" if tdd_exempt else "unit_test",
                    target_files=default_targets[:1],
                ),
            ),
            constraints=("Verify changes against clean baseline and repository tests.",),
            tdd_exempt=tdd_exempt,
            author_agent="metagpt:ProductManager",
            model_id=model_id,
            spec_id=spec_id,
            spec_version=spec_version,
        )

    errors = validate_task_spec_against_repo(spec, repo_context)
    if errors:
        raise ValidationError(
            "Generated TaskSpec failed repository grounding validation.",
            details={"validation_errors": errors},
        )
    return spec


def metagpt_ba_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
) -> dict[str, Any]:
    """Run MetaGPT BA stage via Gateway `role='ba'` to produce grounded TaskSpec + clarifications."""
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    primary_repo_id = state["repo_ids"][0] if state.get("repo_ids") else ""
    if primary_repo_id:
        _snap, raw_ctx = load_snapshot_repo_context(
            session,
            tenant_id=state["tenant_id"],
            repo_id=primary_repo_id,
            object_store=ctx.object_store,
        )
        repo_context = RepoContext.from_dict(raw_ctx)
    else:
        # Fallback synthetic docs RepoContext when no code repo bound (pure docs task)
        repo_context = RepoContext(
            context_id="ctx-docs",
            captured_at=task.created_at.isoformat(),
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
            test_runner=TestRunnerInfo(framework="none", command_hint=""),
        )

    all_tracked: set[str] = set(repo_context.tracked_files)
    for rid in list(state.get("repo_ids") or [])[1:]:
        try:
            _s, extra_raw = load_snapshot_repo_context(
                session,
                tenant_id=state["tenant_id"],
                repo_id=rid,
                object_store=ctx.object_store,
            )
            all_tracked.update(extra_raw.get("tracked_files") or ())
        except Exception:
            pass

    questions = (
        []
        if state.get("clarification_answers")
        else analyze_requirement_gaps(
            task.requirement_text,
            repo_context,
            all_tracked_files=all_tracked,
        )
    )

    attempt_id = f"att_ba_{secrets.token_hex(4)}"
    gw_out = invoke_role_model(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt_id,
        role="ba",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a MetaGPT ProductManager. Respond with JSON inside [CONTENT]...[/CONTENT] "
                    "grounding target_files strictly in RepoContext.tracked_files."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\nRequirement: {task.requirement_text}\n"
                    f"Tracked files: {', '.join(repo_context.tracked_files[:30])}"
                ),
            },
        ],
        cipher=ctx.cipher,
        allow_local_gateway=ctx.settings.allow_local_gateway,
        policy_snapshot=run.frozen_policy_json or None,
        override_adapters=ctx.override_adapters,
    )
    parsed_draft = extract_content_block(gw_out["response"].content)
    if parsed_draft and isinstance(parsed_draft.get("clarification_questions"), list):
        for q in parsed_draft["clarification_questions"]:
            q_str = str(q).strip()
            if q_str and q_str not in questions:
                questions.append(q_str)

    spec_version = 2 if state.get("clarification_answers") else 1
    spec = synthesize_grounded_task_spec(
        repo_context=repo_context,
        task=task,
        model_id=gw_out["actual_model_id"],
        clarification_answers=state.get("clarification_answers"),
        llm_draft=parsed_draft,
        spec_version=spec_version,
        spec_id=run.spec_id or None,
    )
    art, _ = _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=task.id,
        run_id=run.id,
        artifact_type="TaskSpec",
        filename="task_spec.json",
        payload=spec.to_dict(),
        version=spec.spec_version,
        metadata={"model_id": gw_out["actual_model_id"], "clarification_questions": questions},
    )
    run.spec_id = art.id
    run.spec_version = spec.spec_version
    run.spec_sha256 = spec.spec_sha256

    return {
        "spec_sha256": spec.spec_sha256,
        "spec_version": spec.spec_version,
        "clarification_questions": questions,
    }


def metagpt_architect_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
) -> dict[str, Any]:
    """Run MetaGPT Architect role to emit `ArchitectDesign` artifact bound to current spec digest."""
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    gw_out = invoke_role_model(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        run_id=run.id,
        attempt_id=f"att_arch_{secrets.token_hex(4)}",
        role="architect",
        messages=[
            {"role": "system", "content": "You are a MetaGPT Architect producing concise module & interface designs."},
            {"role": "user", "content": f"Design modules for {task.title} (spec={run.spec_sha256})."},
        ],
        cipher=ctx.cipher,
        allow_local_gateway=ctx.settings.allow_local_gateway,
        policy_snapshot=run.frozen_policy_json or None,
        override_adapters=ctx.override_adapters,
    )
    parsed = extract_content_block(gw_out["response"].content) or {}
    design_payload = {
        "schema_version": "1.1",
        "spec_sha256": run.spec_sha256,
        "spec_version": run.spec_version,
        "model_id": gw_out["actual_model_id"],
        "repo_ids": list(state.get("repo_ids") or []),
        "modules": parsed.get("modules") or ["primary_target_module"],
        "api_contracts": parsed.get("api_contracts") or [],
        "sequence_notes": parsed.get("sequence_notes") or f"Ordered changes per repo baseline for {task.title}.",
    }
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=task.id,
        run_id=run.id,
        artifact_type="ArchitectDesign",
        filename="architect_design.json",
        payload=design_payload,
        version=int(run.spec_version or 1),
    )
    return {}


def diff_spec_versions(old_dict: dict[str, Any], new_dict: dict[str, Any]) -> str:
    old_lines = json.dumps(old_dict, indent=2, sort_keys=True).splitlines()
    new_lines = json.dumps(new_dict, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(old_lines, new_lines, fromfile="spec_prev.json", tofile="spec_new.json", lineterm="")
    )


def edit_task_spec(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    task_id: str,
    updates: dict[str, Any],
    object_store: Any,
) -> dict[str, Any]:
    """Create next `TaskSpec` version, compute diff, and invalidate stale human approvals."""
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None or not task.active_run_id:
        raise NotFoundError("Task not found.")
    if task.status in ("DELIVERED", "CANCELLED"):
        raise ConflictError(f"Cannot edit specification when task is {task.status!r}.")
    run = session.get(TaskRun, task.active_run_id)
    assert run is not None

    prev_art = session.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.tenant_id == tenant_id,
            ArtifactVersion.task_id == task_id,
            ArtifactVersion.artifact_type == "TaskSpec",
        )
        .order_by(ArtifactVersion.version.desc())
    )
    if prev_art is None:
        raise NotFoundError("No initial TaskSpec exists yet for this task.")

    prev_dict: dict[str, Any] = json.loads(object_store.get_bytes(tenant_id, prev_art.object_key).decode("utf-8"))
    prev_spec = TaskSpec.from_dict(prev_dict)

    # Reconstruct RepoContext from repo snapshot
    if task.repo_ids_json:
        _snap, raw_ctx = load_snapshot_repo_context(
            session, tenant_id=tenant_id, repo_id=task.repo_ids_json[0], object_store=object_store
        )
        repo_ctx = RepoContext.from_dict(raw_ctx)
    else:
        repo_ctx = RepoContext(
            context_id=prev_spec.context_id,
            captured_at=prev_spec.created_at,
            repo_name=prev_spec.repo_name,
            repo_source=prev_spec.repo_name,
            repo_commit_sha=prev_spec.repo_commit_sha,
            commit_source="tree_snapshot",
            repo_tree_sha256=prev_spec.repo_tree_sha256,
            is_dirty=False,
            language="docs",
            tracked_files=prev_spec.target_files or ("README.md",),
            existing_test_files=(),
            protected_config_paths=(),
            protected_config_hashes={},
            test_runner=TestRunnerInfo(framework="none", command_hint=""),
        )

    new_ver = prev_spec.spec_version + 1
    new_acs = (
        tuple(AcceptanceCriterion.from_dict(x) for x in updates["acceptance_criteria"])
        if "acceptance_criteria" in updates
        else prev_spec.acceptance_criteria
    )
    updated_spec = TaskSpec.create(
        repo_context=repo_ctx,
        user_requirement=str(updates.get("user_requirement") or prev_spec.user_requirement),
        task_type=prev_spec.task_type,
        title=str(updates.get("title") or prev_spec.title),
        summary=str(updates.get("summary") or prev_spec.summary),
        problem_statement=str(updates.get("problem_statement") or prev_spec.problem_statement),
        scope_in=tuple(updates.get("scope_in") or prev_spec.scope_in),
        scope_out=tuple(updates.get("scope_out") or prev_spec.scope_out),
        target_files=tuple(updates.get("target_files") or prev_spec.target_files),
        expected_new_files=tuple(updates.get("expected_new_files") or prev_spec.expected_new_files),
        expected_new_test_files=tuple(updates.get("expected_new_test_files") or prev_spec.expected_new_test_files),
        acceptance_criteria=new_acs,
        constraints=tuple(updates.get("constraints") or prev_spec.constraints),
        tdd_exempt=bool(updates.get("tdd_exempt", prev_spec.tdd_exempt)),
        allow_config_changes=bool(updates.get("allow_config_changes", prev_spec.allow_config_changes)),
        author_agent="user:spec-editor",
        model_id=prev_spec.model_id,
        spec_id=prev_spec.spec_id,
        spec_version=new_ver,
    )
    val_errors = validate_task_spec_against_repo(updated_spec, repo_ctx)
    if val_errors:
        raise ValidationError("Edited TaskSpec failed validation.", details={"validation_errors": val_errors})

    new_dict = updated_spec.to_dict()
    diff_text = diff_spec_versions(prev_dict, new_dict)

    art = ArtifactVersion(
        id=f"art_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=task.id,
        run_id=run.id,
        artifact_type="TaskSpec",
        version=new_ver,
        schema_version="1.1",
        sha256=updated_spec.spec_sha256,
        object_key=build_spec_key(tenant_id, task.project_id, task.id, new_ver),
        metadata_json={"edited_by": actor_user_id, "previous_sha256": prev_spec.spec_sha256, "diff": diff_text},
    )
    object_store.put_bytes(
        tenant_id, art.object_key, canonical_json_bytes(new_dict), content_type="application/json"
    )
    session.add(art)
    run.spec_id = art.id
    run.spec_version = new_ver
    run.spec_sha256 = updated_spec.spec_sha256

    invalidated = invalidate_run_approvals(
        session,
        tenant_id=tenant_id,
        task_id=task.id,
        reason=f"TaskSpec edited to v{new_ver} ({updated_spec.spec_sha256[:12]})",
        gate_types=("spec", "plan", "delivery", "release"),
    )
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="spec.edited",
        target_type="task",
        target_id=task.id,
        details={"spec_version": new_ver, "spec_sha256": updated_spec.spec_sha256, "invalidated_approvals": invalidated},
    )
    session.flush()
    return {
        "artifact_id": art.id,
        "spec_version": new_ver,
        "spec_sha256": updated_spec.spec_sha256,
        "invalidated_approvals": invalidated,
        "diff": diff_text,
        "spec": new_dict,
    }


def build_spec_key(tenant_id: str, project_id: str, task_id: str, version: int) -> str:
    return f"tenants/{tenant_id}/projects/{project_id}/tasks/{task_id}/v{version}_task_spec.json"
