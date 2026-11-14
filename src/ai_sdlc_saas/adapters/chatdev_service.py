"""ChatDev 2.0 YAML workflow QA/critique, read-only diff review, and tenant+project memory."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc.contracts import (
    CritiqueIssue,
    SpecCritique,
    TaskSpec,
    TestCaseSpec,
    TestPlan,
    utc_now_iso,
)
from ai_sdlc_saas.adapters.metagpt_service import extract_content_block
from ai_sdlc_saas.db.models import (
    ArtifactVersion,
    ReviewFindingRecord,
    Task,
    TaskRun,
    TenantMemory,
    TestPlanRecord,
)
from ai_sdlc_saas.errors import ForbiddenError, NotFoundError, ValidationError
from ai_sdlc_saas.gateway.service import invoke_role_model
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.orchestrator.graph import OrchestratorContext, SdlcGraphState, _save_json_artifact
from ai_sdlc_saas.security.redaction import redact_text

_DEFAULT_WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / "workflows"
_FORBIDDEN_WRITE_TOOLS = frozenset({"shell", "bash", "exec", "write_file", "edit_file", "git_push", "delete_file"})
_READONLY_TOOLS = frozenset(
    {"read_repo_context", "read_task_spec", "read_patch_diff", "read_verification_report"}
)


def load_and_validate_workflow(yaml_path: Path) -> dict[str, Any]:
    """Load a ChatDev YAML workflow and enforce bounded rounds + strictly read-only tools."""
    if not yaml_path.is_file():
        raise NotFoundError(f"Workflow YAML not found: {yaml_path}")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError("Invalid workflow YAML structure.")

    max_rounds = int(data.get("max_rounds") or 1)
    if max_rounds < 1 or max_rounds > 5:
        raise ValidationError(f"Workflow max_rounds must be between 1 and 5, got {max_rounds}.")

    allowed = [str(t) for t in (data.get("allowed_tools") or [])]
    for tool in allowed:
        if tool in _FORBIDDEN_WRITE_TOOLS or tool not in _READONLY_TOOLS:
            raise ForbiddenError(
                f"ChatDev workflow {yaml_path.name!r} attempted disallowed/non-readonly tool {tool!r}."
            )
    return data


def _load_latest_artifact_json(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    artifact_type: str,
    object_store: Any,
) -> dict[str, Any] | None:
    art = session.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.tenant_id == tenant_id,
            ArtifactVersion.task_id == task_id,
            ArtifactVersion.artifact_type == artifact_type,
        )
        .order_by(ArtifactVersion.version.desc(), ArtifactVersion.created_at.desc())
    )
    if art is None:
        return None
    raw = object_store.get_bytes(tenant_id, art.object_key).decode("utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def chatdev_qa_critique_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
    *,
    workflow_path: Path | None = None,
) -> dict[str, Any]:
    """Run bounded read-only QA critique + TestPlan generation anchored to TaskSpec."""
    wf = load_and_validate_workflow(workflow_path or (_DEFAULT_WORKFLOWS_DIR / "spec_critique_testplan.yaml"))
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    spec_dict = _load_latest_artifact_json(
        session, tenant_id=state["tenant_id"], task_id=task.id, artifact_type="TaskSpec", object_store=ctx.object_store
    )
    if spec_dict is None:
        raise NotFoundError("TaskSpec artifact missing before QA critique.")
    spec = TaskSpec.from_dict(spec_dict)

    # Retrieve approved tenant+project lessons (isolated to tenant & project)
    memories = list(
        session.scalars(
            select(TenantMemory).where(
                TenantMemory.tenant_id == state["tenant_id"],
                TenantMemory.project_id == state["project_id"],
                TenantMemory.approved_by_user_id.is_not(None),
            )
        )
    )
    memory_hint = "\n".join(f"- {m.lesson_text}" for m in memories[:5])

    gw_out = invoke_role_model(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        run_id=run.id,
        attempt_id=f"att_qa_{secrets.token_hex(4)}",
        role="qa_critique",
        messages=[
            {
                "role": "system",
                "content": (
                    f"ChatDev YAML workflow {wf['name']} (max_rounds={wf['max_rounds']}, tools={wf['allowed_tools']}). "
                    "Critique spec ACs and emit JSON [CONTENT]...[/CONTENT] mapping every AC to TestCaseSpec."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Spec: {spec.title}\nACs: {len(spec.acceptance_criteria)}\n"
                    f"Approved project lessons:\n{memory_hint or '(none)'}"
                ),
            },
        ],
        cipher=ctx.cipher,
        allow_local_gateway=ctx.settings.allow_local_gateway,
        policy_snapshot=run.frozen_policy_json or None,
        override_adapters=ctx.override_adapters,
    )
    parsed = extract_content_block(gw_out["response"].content) or {}

    issues: list[CritiqueIssue] = []
    for raw_iss in parsed.get("issues") or []:
        if isinstance(raw_iss, dict) and raw_iss.get("message"):
            issues.append(CritiqueIssue.from_dict(raw_iss))

    # Deterministic ambiguity/coverage critique checks
    if any("always" in ac.description.lower() and "never" in ac.expected_behavior.lower() for ac in spec.acceptance_criteria):
        issues.append(
            CritiqueIssue(
                severity="blocker",
                ac_id=spec.acceptance_criteria[0].ac_id,
                message="Contradictory requirement behavior detected between description and expected_behavior.",
                suggestion="Clarify precedence before coding.",
            )
        )

    verdict_raw = str(parsed.get("verdict") or "").lower()
    if any(i.severity == "blocker" for i in issues):
        critique_verdict = "block"
    elif verdict_raw in ("accept", "revise", "block"):
        critique_verdict = verdict_raw
    else:
        critique_verdict = "accept"

    proposed_test = spec.expected_new_test_files[0] if spec.expected_new_test_files else "tests/test_task_generated.py"
    raw_tcs = parsed.get("test_cases") or []
    tcs: list[TestCaseSpec] = []
    for item in raw_tcs:
        if isinstance(item, dict) and item.get("test_case_id") and item.get("ac_ids"):
            tcs.append(TestCaseSpec.from_dict(item))

    covered_acs = {ac for tc in tcs for ac in tc.ac_ids}
    for idx, ac in enumerate(spec.acceptance_criteria, 1):
        if ac.ac_id not in covered_acs:
            tcs.append(
                TestCaseSpec(
                    test_case_id=f"TC-{idx}",
                    ac_ids=(ac.ac_id,),
                    title=f"Verify {ac.ac_id}: {ac.description[:80]}",
                    test_type="unit",
                    proposed_test_file=proposed_test,
                    steps_or_inputs=f"Exercise {', '.join(ac.target_files or spec.target_files) or 'target module'}",
                    expected_outcome=ac.expected_behavior,
                    must_fail_on_baseline=not spec.tdd_exempt,
                )
            )

    spec_critique = SpecCritique(
        verdict=critique_verdict,  # type: ignore[arg-type]
        summary=str(parsed.get("summary") or f"Critique of {spec.spec_id} v{spec.spec_version} ({len(tcs)} test cases)"),
        issues=tuple(issues),
    )
    test_plan = TestPlan.create(
        task_spec=spec,
        spec_critique=spec_critique,
        test_cases=tuple(tcs),
        qa_agent="chatdev:SpecCritic+QAStrategist",
        model_id=gw_out["actual_model_id"],
        qa_role="qa_critique",
    )

    plan_dict = test_plan.to_dict()
    record_verdict = "BLOCKED" if critique_verdict == "block" else "READY"
    session.add(
        TestPlanRecord(
            id=f"tpl_{secrets.token_hex(8)}",
            tenant_id=state["tenant_id"],
            task_id=task.id,
            run_id=run.id,
            spec_sha256=spec.spec_sha256,
            verdict=record_verdict,
            payload_json=plan_dict,
        )
    )
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=task.id,
        run_id=run.id,
        artifact_type="TestPlan",
        filename="test_plan.json",
        payload=plan_dict,
        version=spec.spec_version,
    )
    session.flush()
    return {"qa_verdict": record_verdict}


def _scan_diff_heuristics(patch_text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not patch_text:
        return findings
    current_file = "unknown"
    for idx, line in enumerate(patch_text.splitlines(), 1):
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if re.search(r"sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}", added):
            findings.append(
                {
                    "severity": "blocker",
                    "file_path": current_file,
                    "line_number": idx,
                    "title": "Potential hardcoded credential detected in diff",
                    "detail": redact_text(added.strip())[:240],
                }
            )
        if "pdb.set_trace()" in added or "breakpoint()" in added:
            findings.append(
                {
                    "severity": "major",
                    "file_path": current_file,
                    "line_number": idx,
                    "title": "Debug breakpoint left in source diff",
                    "detail": added.strip()[:200],
                }
            )
    return findings


def chatdev_code_review_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
    *,
    workflow_path: Path | None = None,
) -> dict[str, Any]:
    """Separate diff-based Code Review stage enforcing read-only workflow + findings."""
    wf = load_and_validate_workflow(workflow_path or (_DEFAULT_WORKFLOWS_DIR / "code_review.yaml"))
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    patch_dict = _load_latest_artifact_json(
        session, tenant_id=state["tenant_id"], task_id=task.id, artifact_type="Patch", object_store=ctx.object_store
    ) or {}
    patch_text = str(patch_dict.get("diff") or patch_dict.get("patch_text") or "")

    gw_out = invoke_role_model(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        run_id=run.id,
        attempt_id=f"att_rev_{secrets.token_hex(4)}",
        role="reviewer",
        messages=[
            {
                "role": "system",
                "content": (
                    f"ChatDev YAML workflow {wf['name']} (tools={wf['allowed_tools']}). "
                    "Review patch diff separately from author. Emit JSON findings if any."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\nVerification: {state.get('verification_verdict')}\n"
                    f"Diff excerpt:\n{redact_text(patch_text[:4000])}"
                ),
            },
        ],
        cipher=ctx.cipher,
        allow_local_gateway=ctx.settings.allow_local_gateway,
        policy_snapshot=run.frozen_policy_json or None,
        override_adapters=ctx.override_adapters,
    )
    parsed = extract_content_block(gw_out["response"].content) or {}

    findings_data = _scan_diff_heuristics(patch_text)
    for raw_f in parsed.get("findings") or []:
        if isinstance(raw_f, dict) and raw_f.get("title"):
            findings_data.append(
                {
                    "severity": str(raw_f.get("severity") or "minor"),
                    "file_path": str(raw_f.get("file_path") or "unknown"),
                    "line_number": raw_f.get("line_number"),
                    "title": str(raw_f["title"])[:190],
                    "detail": str(raw_f.get("detail") or raw_f["title"]),
                }
            )

    for f in findings_data:
        session.add(
            ReviewFindingRecord(
                id=f"fnd_{secrets.token_hex(8)}",
                tenant_id=state["tenant_id"],
                task_id=task.id,
                run_id=run.id,
                severity=f["severity"],
                file_path=f["file_path"],
                line_number=f.get("line_number"),
                title=f["title"],
                detail=f["detail"],
                status="open",
            )
        )

    has_blocker = any(f["severity"] == "blocker" for f in findings_data)
    verdict = "BLOCK" if has_blocker else str(parsed.get("verdict") or "APPROVE")
    if verdict not in ("APPROVE", "REQUEST_CHANGES", "BLOCK"):
        verdict = "APPROVE"

    report_payload = {
        "schema_version": "1.1",
        "task_id": task.id,
        "run_id": run.id,
        "patch_sha256": run.patch_sha256 or "",
        "verdict": verdict,
        "model_id": gw_out["actual_model_id"],
        "summary": str(parsed.get("summary") or f"Diff review completed with {len(findings_data)} finding(s)."),
        "findings": findings_data,
        "reviewed_at": utc_now_iso(),
    }
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=task.id,
        run_id=run.id,
        artifact_type="ReviewReport",
        filename="review_report.json",
        payload=report_payload,
    )
    session.flush()
    return {"review_verdict": verdict}


# ---------------------------------------------------------------------------
# Tenant + Project Scoped Lesson Memory (Never Shared Across Tenants/Projects)
# ---------------------------------------------------------------------------


def create_tenant_memory(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    source_run_id: str,
    lesson_text: str,
    approved_by_user_id: str | None = None,
) -> TenantMemory:
    cleaned = redact_text(lesson_text.strip())
    if len(cleaned) < 5:
        raise ValidationError("Lesson text must be at least 5 characters.")
    mem = TenantMemory(
        id=f"meml_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        project_id=project_id,
        lesson_text=cleaned[:2000],
        source_run_id=source_run_id,
        approved_by_user_id=approved_by_user_id,
    )
    session.add(mem)
    session.flush()
    return mem


def list_tenant_memories(session: Session, *, tenant_id: str, project_id: str) -> list[TenantMemory]:
    return list(
        session.scalars(
            select(TenantMemory)
            .where(TenantMemory.tenant_id == tenant_id, TenantMemory.project_id == project_id)
            .order_by(TenantMemory.created_at.desc())
        )
    )


def approve_tenant_memory(
    session: Session, *, tenant_id: str, project_id: str, memory_id: str, actor_user_id: str
) -> TenantMemory:
    mem = session.scalar(
        select(TenantMemory).where(
            TenantMemory.id == memory_id,
            TenantMemory.tenant_id == tenant_id,
            TenantMemory.project_id == project_id,
        )
    )
    if mem is None:
        raise NotFoundError("Project memory lesson not found.")
    mem.approved_by_user_id = actor_user_id
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="memory.approved",
        target_type="tenant_memory",
        target_id=mem.id,
        details={"project_id": project_id},
    )
    session.flush()
    return mem


def delete_tenant_memory(
    session: Session, *, tenant_id: str, project_id: str, memory_id: str, actor_user_id: str
) -> None:
    mem = session.scalar(
        select(TenantMemory).where(
            TenantMemory.id == memory_id,
            TenantMemory.tenant_id == tenant_id,
            TenantMemory.project_id == project_id,
        )
    )
    if mem is None:
        raise NotFoundError("Project memory lesson not found.")
    session.delete(mem)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="memory.deleted",
        target_type="tenant_memory",
        target_id=memory_id,
        details={"project_id": project_id},
    )
    session.flush()
