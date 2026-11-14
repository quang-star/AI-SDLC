"""MetaGPT-structured Business Analyst (BA) adapter over 9Router."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import APIConnectionError, APIError, AuthenticationError, OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from ai_sdlc.contracts import (  # noqa: E402
    AcceptanceCriterion,
    RepoContext,
    TaskSpec,
    TaskType,
    VerificationMethod,
    canonical_json_bytes,
    revise_task_spec,
    sha256_hex,
    validate_task_spec_against_repo,
)
from ai_sdlc.model_routing import RouterConnectionConfig, resolve_role_router_config  # noqa: E402
from ai_sdlc.repo_context import build_repo_context  # noqa: E402

try:  # Support both package import and direct script/pytest execution.
    from .action_node import ActionNode, extract_content_json
except ImportError:  # pragma: no cover - direct execution fallback
    from action_node import ActionNode, extract_content_json


PRODUCT_MANAGER_SYSTEM_PROMPT = """You are Alice, an experienced Product Manager and Business Analyst (MetaGPT ProductManager) preparing an implementation specification for an automated engineering pipeline.

Core rules:
1. Ground every single path and symbol strictly in the provided Repository Context. Never invent files that are not in `Tracked Files` unless explicitly placing a brand-new module in `expected_new_files` or a brand-new test file in `expected_new_test_files`.
2. Hard Gate compliance is mandatory:
   - NEVER put any file from `Existing Test Files` into `target_files`, `expected_new_files`, `expected_new_test_files`, or `acceptance_criteria[*].target_files`. Existing tests are immutable.
   - NEVER put any file from `Protected Config Paths` into `target_files` or `expected_new_files` unless `allow_config_changes` is explicitly justified by the user's requirement.
   - Unless `tdd_exempt` is true, propose at least one new test file path in `expected_new_test_files` (for example `tests/test_<topic>.py`) that does not collide with `Tracked Files`.
3. Every Acceptance Criterion (`AC-1`, `AC-2`, ...) must describe concrete, testable behavior and edge cases so a downstream QA agent can derive executable test cases directly from it.
4. Keep scope focused: explicitly put unrelated refactors or out-of-scope changes into `scope_out`.
5. Output ONLY a single JSON object wrapped inside [CONTENT] and [/CONTENT] tags matching the schema."""


class AcceptanceCriterionDraft(BaseModel):
    """Pydantic schema validating one LLM-generated AcceptanceCriterion."""

    model_config = ConfigDict(extra="forbid")

    ac_id: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
    description: str = Field(min_length=3)
    expected_behavior: str = Field(min_length=3)
    verification_method: VerificationMethod = "unit_test"
    target_files: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)


class TaskSpecDraft(BaseModel):
    """Pydantic schema validating the structured LLM response before TaskSpec binding."""

    model_config = ConfigDict(extra="forbid")

    task_type: TaskType
    title: str = Field(min_length=3)
    summary: str = Field(min_length=5)
    problem_statement: str = Field(min_length=5)
    scope_in: list[str] = Field(min_length=1)
    scope_out: list[str] = Field(default_factory=list)
    target_files: list[str] = Field(default_factory=list)
    expected_new_files: list[str] = Field(default_factory=list)
    expected_new_test_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterionDraft] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    tdd_exempt: bool = False
    allow_config_changes: bool = False


TASK_SPEC_AN = ActionNode(
    key="TaskSpecDraft",
    expected_type=dict,
    instruction="Repository-grounded task specification for engineering handoff.",
    example={},
    children=(
        ActionNode(
            key="task_type",
            expected_type=Literal["bugfix", "feature", "refactor", "chore"],
            instruction="Classify the requirement as one of: 'bugfix', 'feature', 'refactor', or 'chore'.",
            example="bugfix",
        ),
        ActionNode(
            key="title",
            expected_type=str,
            instruction="Concise 4-12 word title summarizing the change.",
            example="Fix negative discount validation in calculate_total",
        ),
        ActionNode(
            key="summary",
            expected_type=str,
            instruction="1-2 sentence executive summary of the problem and desired change.",
            example="Reject negative discount_percent values and handle invalid inputs consistently in billing.",
        ),
        ActionNode(
            key="problem_statement",
            expected_type=str,
            instruction="Detailed technical statement referencing the affected module, functions, and root cause or requirement.",
            example="In src/mini_ledger/billing.py, calculate_total currently permits negative discounts that inflate totals.",
        ),
        ActionNode(
            key="scope_in",
            expected_type=list[str],
            instruction="Concrete deliverables and behaviors included in this task.",
            example=[
                "Validate discount_percent bounds in src/mini_ledger/billing.py",
                "Add regression unit tests in a new test file",
            ],
        ),
        ActionNode(
            key="scope_out",
            expected_type=list[str],
            instruction="Explicit non-goals and untouched files (including existing test files).",
            example=[
                "Do not modify tests/test_billing.py or pyproject.toml",
                "Do not change public function signatures",
            ],
        ),
        ActionNode(
            key="target_files",
            expected_type=list[str],
            instruction="Existing Tracked Files to modify (must exist in RepoContext and NOT be existing tests or protected configs).",
            example=["src/mini_ledger/billing.py"],
        ),
        ActionNode(
            key="expected_new_files",
            expected_type=list[str],
            instruction="Optional new non-test files to create, usually empty [] for localized bugfixes.",
            example=[],
        ),
        ActionNode(
            key="expected_new_test_files",
            expected_type=list[str],
            instruction="New test file path(s) like 'tests/test_<topic>.py' (must not collide with Existing Test Files unless tdd_exempt=true).",
            example=["tests/test_billing_discount_bounds.py"],
        ),
        ActionNode(
            key="constraints",
            expected_type=list[str],
            instruction="Technical, backward-compatibility, and Hard Gate constraints Dev and QA must respect.",
            example=[
                "Do not edit tests/test_billing.py",
                "Keep existing valid discount behavior unchanged",
            ],
        ),
        ActionNode(
            key="tdd_exempt",
            expected_type=bool,
            instruction="Set true only for docs-only/non-code tasks that need no new tests; otherwise false.",
            example=False,
        ),
        ActionNode(
            key="allow_config_changes",
            expected_type=bool,
            instruction="Set true only if user explicitly asked to alter build/test configuration or dependencies; otherwise false.",
            example=False,
        ),
        ActionNode(
            key="acceptance_criteria",
            expected_type=list[dict[str, Any]],
            instruction="List of 2-5 testable AcceptanceCriterion objects with keys: ac_id (e.g. 'AC-1'), description, expected_behavior, verification_method ('unit_test'|'integration_test'|'static_check'|'manual_review'), target_files, edge_cases.",
            example=[
                {
                    "ac_id": "AC-1",
                    "description": "Reject negative discount_percent values.",
                    "expected_behavior": "Calling calculate_total(subtotal, -1) raises ValueError.",
                    "verification_method": "unit_test",
                    "target_files": ["src/mini_ledger/billing.py"],
                    "edge_cases": ["discount_percent=-0.01", "discount_percent=-100"],
                }
            ],
        ),
    ),
)


@dataclass(frozen=True)
class AttemptRecord:
    """Audit record for one BA LLM generation or repair attempt."""

    attempt: int
    finish_reason: str | None
    errors: tuple[str, ...]
    raw_excerpt: str


@dataclass(frozen=True)
class SpecGenerationResult:
    """Output bundle produced by `generate_task_spec`."""

    task_spec: TaskSpec
    repo_context: RepoContext
    model_id: str
    base_url: str
    repair_attempts: int
    attempt_logs: tuple[AttemptRecord, ...]
    prompt_sha256: str


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of probing 9Router `/v1/models` for the resolved BA model."""

    ok: bool
    base_url: str
    model_id: str
    available_models: tuple[str, ...]
    message: str


class SpecGenerationError(RuntimeError):
    """Raised when the BA agent fails to produce a repo-valid TaskSpec."""

    def __init__(self, message: str, *, attempt_logs: Sequence[AttemptRecord] = ()) -> None:
        super().__init__(message)
        self.attempt_logs = tuple(attempt_logs)


def build_ba_context(
    repo_context: RepoContext,
    user_requirement: str,
    *,
    previous_spec: TaskSpec | None = None,
    revision_feedback: str | None = None,
    max_files: int = 25,
) -> str:
    """Assemble grounded context passed into MetaGPT's ActionNode prompt compiler."""
    cleaned_req = user_requirement.strip()
    if not cleaned_req:
        raise ValueError("User requirement must be a non-empty string.")

    sections = [
        "### User Requirement",
        cleaned_req,
    ]
    if previous_spec is not None:
        feedback = (revision_feedback or "").strip()
        if not feedback and previous_spec.approval is not None:
            feedback = previous_spec.approval.comment
        sections.extend(
            [
                "",
                "### Previous Spec Revision Context",
                f"Revising spec_id={previous_spec.spec_id} (previous version={previous_spec.spec_version}, status={previous_spec.status}).",
                f"Previous Title: {previous_spec.title}",
                f"Human/QA Feedback: {feedback or '(revise carefully for clarity and grounding)'}",
            ]
        )

    sections.extend(["", "### Grounded Repository Context", repo_context.to_prompt_context(max_files=max_files)])
    return "\n".join(sections)


def compile_ba_prompt(
    repo_context: RepoContext,
    user_requirement: str,
    *,
    previous_spec: TaskSpec | None = None,
    revision_feedback: str | None = None,
) -> str:
    """Compile the full MetaGPT ActionNode prompt for BA TaskSpec generation."""
    context = build_ba_context(
        repo_context,
        user_requirement,
        previous_spec=previous_spec,
        revision_feedback=revision_feedback,
    )
    return TASK_SPEC_AN.compile(context=context)


def preflight_9router(
    config: RouterConnectionConfig,
    *,
    http_client: httpx.Client | None = None,
    timeout_seconds: float = 10.0,
) -> PreflightResult:
    """Verify 9Router `/v1/models` reachability and check that `config.model_id` exists."""
    models_url = f"{config.base_url}/models"
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout_seconds)
    try:
        response = client.get(
            models_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return PreflightResult(
            ok=False,
            base_url=config.base_url,
            model_id=config.model_id,
            available_models=(),
            message=f"Could not reach 9Router at {models_url}: {exc}",
        )
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        detail = response.text.strip()[:240]
        return PreflightResult(
            ok=False,
            base_url=config.base_url,
            model_id=config.model_id,
            available_models=(),
            message=f"9Router {models_url} returned HTTP {response.status_code}: {detail}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return PreflightResult(
            ok=False,
            base_url=config.base_url,
            model_id=config.model_id,
            available_models=(),
            message=f"9Router {models_url} returned invalid JSON: {exc}",
        )

    raw_data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_data, list):
        return PreflightResult(
            ok=False,
            base_url=config.base_url,
            model_id=config.model_id,
            available_models=(),
            message=f"9Router {models_url} JSON missing list field `data`.",
        )

    available = tuple(
        str(item["id"]).strip()
        for item in raw_data
        if isinstance(item, dict) and item.get("id")
    )
    if config.model_id in available:
        return PreflightResult(
            ok=True,
            base_url=config.base_url,
            model_id=config.model_id,
            available_models=available,
            message=f"9Router ready: model {config.model_id!r} found ({len(available)} models listed).",
        )

    hint_prefix = config.model_id.split("/", 1)[0]
    related = [m for m in available if config.model_id in m or m.startswith(f"{hint_prefix}/")][:8]
    sample = related or list(available[:8])
    return PreflightResult(
        ok=False,
        base_url=config.base_url,
        model_id=config.model_id,
        available_models=available,
        message=(
            f"Model {config.model_id!r} was not listed by {models_url}. "
            f"Candidate models: {', '.join(sample) if sample else '(none)'}"
        ),
    )


def _extract_message_text(response: Any) -> tuple[str, str | None]:
    choices = getattr(response, "choices", None) or ()
    if not choices:
        raise ValueError("Chat completion response contained no choices.")
    first = choices[0]
    finish_reason = getattr(first, "finish_reason", None)
    message = getattr(first, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if isinstance(content, str):
        return content, finish_reason
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text") or ""))
        return "\n".join(parts), finish_reason
    raise ValueError(f"Unsupported chat message content type: {type(content).__name__}.")


def _draft_to_task_spec(
    draft: TaskSpecDraft,
    *,
    repo_context: RepoContext,
    user_requirement: str,
    model_id: str,
    author_role: str,
    previous_spec: TaskSpec | None,
) -> TaskSpec:
    criteria = tuple(
        AcceptanceCriterion(
            ac_id=item.ac_id,
            description=item.description,
            expected_behavior=item.expected_behavior,
            verification_method=item.verification_method,
            target_files=tuple(item.target_files),
            edge_cases=tuple(item.edge_cases),
        )
        for item in draft.acceptance_criteria
    )
    if previous_spec is not None:
        return revise_task_spec(
            previous_spec,
            repo_context=repo_context,
            user_requirement=user_requirement,
            task_type=draft.task_type,
            title=draft.title,
            summary=draft.summary,
            problem_statement=draft.problem_statement,
            scope_in=draft.scope_in,
            scope_out=draft.scope_out,
            target_files=draft.target_files,
            expected_new_files=draft.expected_new_files,
            expected_new_test_files=draft.expected_new_test_files,
            acceptance_criteria=criteria,
            constraints=draft.constraints,
            tdd_exempt=draft.tdd_exempt,
            allow_config_changes=draft.allow_config_changes,
            model_id=model_id,
        )
    return TaskSpec.create(
        repo_context=repo_context,
        user_requirement=user_requirement,
        task_type=draft.task_type,
        title=draft.title,
        summary=draft.summary,
        problem_statement=draft.problem_statement,
        scope_in=draft.scope_in,
        scope_out=draft.scope_out,
        target_files=draft.target_files,
        expected_new_files=draft.expected_new_files,
        expected_new_test_files=draft.expected_new_test_files,
        acceptance_criteria=criteria,
        constraints=draft.constraints,
        tdd_exempt=draft.tdd_exempt,
        allow_config_changes=draft.allow_config_changes,
        author_role=author_role,
        author_agent="metagpt:ProductManager",
        model_id=model_id,
    )


def _format_repair_feedback(errors: Sequence[str], repo_context: RepoContext) -> str:
    bullet_errors = "\n".join(f"- {err}" for err in errors)
    tracked = ", ".join(repo_context.tracked_files[:30])
    existing_tests = ", ".join(repo_context.existing_test_files) or "(none)"
    protected = ", ".join(repo_context.protected_config_paths) or "(none)"
    return (
        "Your previous [CONTENT] draft failed validation against the repository Hard Gates:\n"
        f"{bullet_errors}\n\n"
        f"Valid Tracked Files: {tracked}\n"
        f"Forbidden Existing Test Files: {existing_tests}\n"
        f"Protected Config Paths: {protected}\n\n"
        "Revise ONLY the invalid fields and return a complete corrected JSON object inside "
        "[CONTENT]...[/CONTENT] tags."
    )


def generate_task_spec(
    *,
    user_requirement: str,
    repo_dir: Path | None = None,
    repo_context: RepoContext | None = None,
    router_config: RouterConnectionConfig | None = None,
    role: str = "ba",
    model: str | None = None,
    env: Mapping[str, str] | None = None,
    previous_spec: TaskSpec | None = None,
    revision_feedback: str | None = None,
    http_client: httpx.Client | None = None,
    max_repairs: int = 2,
    temperature: float = 0.1,
    timeout_seconds: float = 120.0,
) -> SpecGenerationResult:
    """Run the MetaGPT-structured BA step to generate a repo-validated draft TaskSpec."""
    if max_repairs < 0:
        raise ValueError("max_repairs must be >= 0.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0.")
    if repo_context is None:
        if repo_dir is None:
            raise ValueError("Provide either `repo_dir` or `repo_context`.")
        repo_context = build_repo_context(repo_dir)

    config = router_config or resolve_role_router_config(role=role, model=model, env=env)
    user_prompt = compile_ba_prompt(
        repo_context,
        user_requirement,
        previous_spec=previous_spec,
        revision_feedback=revision_feedback,
    )
    prompt_digest = sha256_hex(
        canonical_json_bytes({"system": PRODUCT_MANAGER_SYSTEM_PROMPT, "user": user_prompt})
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": PRODUCT_MANAGER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    owns_client = http_client is None
    client_http = http_client or httpx.Client(timeout=timeout_seconds)
    attempt_logs: list[AttemptRecord] = []

    try:
        llm_client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            http_client=client_http,
            timeout=timeout_seconds,
            max_retries=0,
        )

        for attempt in range(max_repairs + 1):
            try:
                response = llm_client.chat.completions.create(
                    model=config.model_id,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=temperature,
                )
            except AuthenticationError as exc:
                raise SpecGenerationError(
                    f"9Router authentication failed for model {config.model_id!r}: {exc}",
                    attempt_logs=attempt_logs,
                ) from exc
            except (APIConnectionError, RateLimitError, APIError) as exc:
                raise SpecGenerationError(
                    f"9Router request failed on attempt {attempt + 1} for model {config.model_id!r}: {exc}",
                    attempt_logs=attempt_logs,
                ) from exc

            raw_text, finish_reason = _extract_message_text(response)
            excerpt = raw_text.strip()[:400]
            errors: list[str] = []
            spec: TaskSpec | None = None

            try:
                raw_payload = extract_content_json(raw_text)
                draft = TaskSpecDraft.model_validate(raw_payload)
                spec = _draft_to_task_spec(
                    draft,
                    repo_context=repo_context,
                    user_requirement=user_requirement,
                    model_id=config.model_id,
                    author_role=config.role,
                    previous_spec=previous_spec,
                )
                errors = validate_task_spec_against_repo(spec, repo_context)
            except (ValidationError, ValueError, KeyError, TypeError) as exc:
                errors = [str(exc)]

            attempt_logs.append(
                AttemptRecord(
                    attempt=attempt + 1,
                    finish_reason=finish_reason,
                    errors=tuple(errors),
                    raw_excerpt=excerpt,
                )
            )

            if spec is not None and not errors:
                return SpecGenerationResult(
                    task_spec=spec,
                    repo_context=repo_context,
                    model_id=config.model_id,
                    base_url=config.base_url,
                    repair_attempts=attempt,
                    attempt_logs=tuple(attempt_logs),
                    prompt_sha256=prompt_digest,
                )

            if attempt < max_repairs:
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({"role": "user", "content": _format_repair_feedback(errors, repo_context)})
    finally:
        if owns_client:
            client_http.close()

    last_errors = "; ".join(attempt_logs[-1].errors) if attempt_logs else "unknown error"
    raise SpecGenerationError(
        f"BA failed to generate a repo-valid TaskSpec after {max_repairs + 1} attempts: {last_errors}",
        attempt_logs=attempt_logs,
    )


def attempt_records_to_dicts(records: Sequence[AttemptRecord]) -> list[dict[str, Any]]:
    """Serialize AttemptRecord tuple cleanly without exposing credentials."""
    return [asdict(record) for record in records]
