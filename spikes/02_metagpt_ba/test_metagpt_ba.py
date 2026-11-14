"""Offline tests for Spike 02 MetaGPT BA ActionNode adapter and CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from action_node import ActionNode, extract_content_json
from ai_sdlc.contracts import TaskSpec, reject_task_spec, validate_task_spec_against_repo
from ai_sdlc.model_routing import RouterConnectionConfig
from ai_sdlc.repo_context import build_repo_context
from ba_agent import (
    TASK_SPEC_AN,
    SpecGenerationError,
    generate_task_spec,
    preflight_9router,
)
import run_ba_spike

MINI_BUG_REPO = Path(__file__).resolve().parents[1] / "01_instabox_openswe" / "fixtures" / "mini_bug_repo"

TEST_ENV = {
    "NINE_ROUTER_BASE_URL": "http://127.0.0.1:20128/v1",
    "NINE_ROUTER_API_KEY": "sk-9router-secret-test",
    "NINE_ROUTER_ROLE_MODELS": json.dumps(
        {
            "ba": "ag/gemini-3.8-flash",
            "dev": "cx/gpt-6.1-sol",
            "reviewer": "ag/claude-sonnet-4-6",
            "tester": "glm/glm-5.3-flash",
        }
    ),
}


def _valid_draft_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_type": "bugfix",
        "title": "Fix negative and upper-bound discount validation in billing",
        "summary": "Reject negative discounts and enforce valid discount bounds cleanly in billing.",
        "problem_statement": (
            "In src/mini_ledger/billing.py, calculate_total currently accepts negative "
            "discount_percent values and produces inflated totals."
        ),
        "scope_in": [
            "Validate discount_percent bounds inside src/mini_ledger/billing.py",
            "Add unit tests in a brand-new test module",
        ],
        "scope_out": [
            "Do not edit existing tests/test_billing.py",
            "Do not alter pyproject.toml",
        ],
        "target_files": ["src/mini_ledger/billing.py"],
        "expected_new_files": [],
        "expected_new_test_files": ["tests/test_billing_discount_bounds.py"],
        "constraints": [
            "Existing tests in tests/test_billing.py must stay untouched",
            "Keep public function signatures backward-compatible",
        ],
        "tdd_exempt": False,
        "allow_config_changes": False,
        "acceptance_criteria": [
            {
                "ac_id": "AC-1",
                "description": "Reject negative discount_percent.",
                "expected_behavior": "Calling calculate_total(100, -5) raises ValueError.",
                "verification_method": "unit_test",
                "target_files": ["src/mini_ledger/billing.py"],
                "edge_cases": ["discount_percent=-0.01", "discount_percent=-100"],
            },
            {
                "ac_id": "AC-2",
                "description": "Reject or handle discount_percent > 100 cleanly.",
                "expected_behavior": "Calling calculate_total(100, 120) raises ValueError.",
                "verification_method": "unit_test",
                "target_files": ["src/mini_ledger/billing.py"],
                "edge_cases": ["discount_percent=100", "discount_percent=100.01"],
            },
        ],
    }
    payload.update(overrides)
    return payload


def _chat_completion_response(content: str, *, model: str = "ag/gemini-3.8-flash") -> httpx.Response:
    body = {
        "id": "chatcmpl-test-1",
        "object": "chat.completion",
        "created": 1760000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
    }
    return httpx.Response(200, json=body)


def _models_response(model_ids: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"object": "list", "data": [{"id": mid, "object": "model"} for mid in model_ids]},
    )


def test_action_node_compile_and_extract_content_json() -> None:
    repo_context = build_repo_context(MINI_BUG_REPO)
    prompt = TASK_SPEC_AN.compile(context=repo_context.to_prompt_context())

    assert "## context" in prompt
    assert "## format example" in prompt
    assert "## nodes:" in prompt
    assert "Literal['bugfix', 'feature', 'refactor', 'chore']" in prompt
    assert "[CONTENT]" in prompt and "[/CONTENT]" in prompt
    assert "src/mini_ledger/billing.py" in prompt
    assert "tests/test_billing.py" in prompt

    wrapped = "Some reasoning\n[CONTENT]\n```json\n{\"hello\": \"world\", \"n\": 1}\n```\n[/CONTENT]\nDone."
    assert extract_content_json(wrapped) == {"hello": "world", "n": 1}

    bare_fence = "Here is the output:\n```json\n{\"task_type\": \"bugfix\"}\n```"
    assert extract_content_json(bare_fence) == {"task_type": "bugfix"}

    with pytest.raises(ValueError, match="empty"):
        extract_content_json("   ")
    with pytest.raises(ValueError, match="Could not extract"):
        extract_content_json("not json at all")

    leaf = ActionNode(key="title", expected_type=str, instruction="Short title", example="Fix bug")
    assert '"title": "Fix bug"' in leaf.compile_example()


def test_preflight_9router_success_and_missing_model() -> None:
    cfg = RouterConnectionConfig(
        base_url="http://127.0.0.1:20128/v1",
        api_key="sk-9router-secret-test",
        model_id="ag/gemini-3.8-flash",
        role="ba",
    )

    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer sk-9router-secret-test"
        return _models_response(["ag/gemini-3.8-flash", "cx/gpt-6.1-sol"])

    with httpx.Client(transport=httpx.MockTransport(ok_handler)) as client:
        ok = preflight_9router(cfg, http_client=client)
    assert ok.ok is True
    assert "ag/gemini-3.8-flash" in ok.available_models

    def missing_handler(_: httpx.Request) -> httpx.Response:
        return _models_response(["ag/gemini-3.8-flash-high", "glm/glm-5.3-flash"])

    with httpx.Client(transport=httpx.MockTransport(missing_handler)) as client:
        missing = preflight_9router(cfg, http_client=client)
    assert missing.ok is False
    assert "ag/gemini-3.8-flash-high" in missing.message


def test_generate_task_spec_happy_path_wire_level() -> None:
    recorded_requests: list[tuple[httpx.Request, dict[str, Any]]] = []
    draft_json = json.dumps(_valid_draft_payload(), indent=2)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        recorded_requests.append((request, payload))
        return _chat_completion_response(f"[CONTENT]\n{draft_json}\n[/CONTENT]")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = generate_task_spec(
            user_requirement="Reject negative or >100% discounts in billing.",
            repo_dir=MINI_BUG_REPO,
            env=TEST_ENV,
            http_client=client,
        )

    assert len(recorded_requests) == 1
    req, body = recorded_requests[0]
    assert req.method == "POST"
    assert str(req.url) == "http://127.0.0.1:20128/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-9router-secret-test"
    assert body["model"] == "ag/gemini-3.8-flash"
    assert body["messages"][0]["role"] == "system"
    assert "src/mini_ledger/billing.py" in body["messages"][1]["content"]
    assert "tests/test_billing.py" in body["messages"][1]["content"]

    assert result.repair_attempts == 0
    assert result.model_id == "ag/gemini-3.8-flash"
    assert result.task_spec.status == "draft"
    assert result.task_spec.author_agent == "metagpt:ProductManager"
    assert result.task_spec.repo_commit_sha == result.repo_context.repo_commit_sha
    assert result.task_spec.repo_tree_sha256 == result.repo_context.repo_tree_sha256
    assert validate_task_spec_against_repo(result.task_spec, result.repo_context) == []


def test_generate_task_spec_self_repairs_invalid_draft() -> None:
    call_bodies: list[dict[str, Any]] = []
    bad_payload = _valid_draft_payload(
        target_files=["src/mini_ledger/billing.py", "tests/test_billing.py", "src/does_not_exist.py"]
    )
    good_payload = _valid_draft_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        call_bodies.append(body)
        if len(call_bodies) == 1:
            return _chat_completion_response(f"[CONTENT]\n{json.dumps(bad_payload)}\n[/CONTENT]")
        return _chat_completion_response(f"[CONTENT]\n{json.dumps(good_payload)}\n[/CONTENT]")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = generate_task_spec(
            user_requirement="Fix discount validation.",
            repo_dir=MINI_BUG_REPO,
            env=TEST_ENV,
            http_client=client,
            max_repairs=2,
        )

    assert len(call_bodies) == 2
    assert result.repair_attempts == 1
    assert len(result.attempt_logs) == 2
    assert result.attempt_logs[0].errors
    assert result.attempt_logs[1].errors == ()

    repair_prompt = call_bodies[1]["messages"][-1]["content"]
    assert "tests/test_billing.py" in repair_prompt
    assert "src/does_not_exist.py" in repair_prompt
    assert validate_task_spec_against_repo(result.task_spec, result.repo_context) == []


def test_generate_task_spec_exhausted_repairs_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _chat_completion_response("Sorry, here is plain text with no JSON.")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SpecGenerationError, match="2 attempts") as exc_info:
            generate_task_spec(
                user_requirement="Fix discount validation.",
                repo_dir=MINI_BUG_REPO,
                env=TEST_ENV,
                http_client=client,
                max_repairs=1,
            )
    assert len(exc_info.value.attempt_logs) == 2


def test_generate_task_spec_revise_from_rejected_spec() -> None:
    repo_context = build_repo_context(MINI_BUG_REPO)
    seen_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_prompts.append(body["messages"][1]["content"])
        if len(seen_prompts) == 1:
            return _chat_completion_response(f"[CONTENT]\n{json.dumps(_valid_draft_payload())}\n[/CONTENT]")
        revised = _valid_draft_payload(
            title="Fix negative, NaN, and >100% discount validation in billing",
            task_type="bugfix",
        )
        return _chat_completion_response(f"[CONTENT]\n{json.dumps(revised)}\n[/CONTENT]")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        v1 = generate_task_spec(
            user_requirement="Fix discount validation.",
            repo_context=repo_context,
            env=TEST_ENV,
            http_client=client,
        )
        rejected = reject_task_spec(v1.task_spec, reviewer="alice", comment="Clarify >100% and NaN cases")
        v2 = generate_task_spec(
            user_requirement="Fix discount validation including NaN and >100%.",
            repo_context=repo_context,
            previous_spec=rejected,
            env=TEST_ENV,
            http_client=client,
        )

    assert "Clarify >100% and NaN cases" in seen_prompts[1]
    assert v2.task_spec.spec_id == v1.task_spec.spec_id
    assert v2.task_spec.spec_version == 2
    assert v2.task_spec.status == "draft"
    assert v2.task_spec.approval is None
    assert v2.task_spec.spec_sha256 != v1.task_spec.spec_sha256


def test_cli_end_to_end_preflight_generate_reject_revise_and_approve(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "ba_run"
    round_num = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal round_num
        if request.method == "GET" and request.url.path == "/v1/models":
            return _models_response(["ag/gemini-3.8-flash", "cx/gpt-6.1-sol"])
        round_num += 1
        title = (
            "Fix negative discount validation in billing"
            if round_num == 1
            else "Fix negative and >100% discount validation in billing"
        )
        return _chat_completion_response(
            f"[CONTENT]\n{json.dumps(_valid_draft_payload(title=title))}\n[/CONTENT]"
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pre_out = io.StringIO()
        code = run_ba_spike.main(
            ["--preflight-only", "--repo-dir", str(MINI_BUG_REPO)],
            env=TEST_ENV,
            http_client=client,
            stdout=pre_out,
        )
        assert code == 0
        assert "9Router ready" in pre_out.getvalue()

        gen_out = io.StringIO()
        code = run_ba_spike.main(
            [
                "--task",
                "Fix negative discount validation in billing.",
                "--repo-dir",
                str(MINI_BUG_REPO),
                "--artifact-dir",
                str(artifact_dir),
            ],
            env=TEST_ENV,
            http_client=client,
            stdout=gen_out,
        )
        assert code == 0
        spec_path = artifact_dir / "task_spec.json"
        review_path = artifact_dir / "task_spec_review.md"
        meta_path = artifact_dir / "ba_run_metadata.json"
        assert (artifact_dir / "repo_context.json").is_file()
        assert spec_path.is_file()
        assert review_path.is_file()
        assert meta_path.is_file()

        for file_path in artifact_dir.iterdir():
            assert TEST_ENV["NINE_ROUTER_API_KEY"] not in file_path.read_text(encoding="utf-8")

        v1 = TaskSpec.from_json(spec_path.read_text(encoding="utf-8"))
        assert v1.status == "draft"
        assert v1.spec_version == 1

        # --reject without --comment must fail
        err_out = io.StringIO()
        assert run_ba_spike.main(["--reject", str(spec_path)], stdout=err_out) == 2

        # --reject with comment persists rejected state
        rej_out = io.StringIO()
        assert (
            run_ba_spike.main(
                ["--reject", str(spec_path), "--reviewer", "qa-lead", "--comment", "Cover >100%"],
                stdout=rej_out,
            )
            == 0
        )
        rejected = TaskSpec.from_json(spec_path.read_text(encoding="utf-8"))
        assert rejected.status == "rejected"
        assert rejected.approval is not None and rejected.approval.comment == "Cover >100%"

        # Revise from rejected v1 -> creates v2 draft
        rev_out = io.StringIO()
        code = run_ba_spike.main(
            [
                "--task",
                "Fix negative and >100% discount validation in billing.",
                "--repo-dir",
                str(MINI_BUG_REPO),
                "--artifact-dir",
                str(artifact_dir),
                "--revise-from",
                str(spec_path),
                "--skip-api-preflight",
            ],
            env=TEST_ENV,
            http_client=client,
            stdout=rev_out,
        )
        assert code == 0
        v2 = TaskSpec.from_json(spec_path.read_text(encoding="utf-8"))
        assert v2.spec_version == 2
        assert v2.status == "draft"

        # Tampering with task_spec.json must block approval
        tampered = json.loads(spec_path.read_text(encoding="utf-8"))
        tampered["title"] = "Tampered title"
        tampered_path = artifact_dir / "tampered.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        assert run_ba_spike.main(["--approve", str(tampered_path)], stdout=io.StringIO()) == 2

        # Valid approval updates spec and writes task_spec.approved.json
        app_out = io.StringIO()
        assert (
            run_ba_spike.main(
                ["--approve", str(spec_path), "--reviewer", "owner", "--comment", "Approved for QA"],
                stdout=app_out,
            )
            == 0
        )
        approved = TaskSpec.from_json(spec_path.read_text(encoding="utf-8"))
        assert approved.status == "approved"
        assert approved.approval is not None
        assert approved.approval.approved_spec_sha256 == approved.spec_sha256
        assert (artifact_dir / "task_spec.approved.json").is_file()
