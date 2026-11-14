"""CLI runner for the MetaGPT-structured Business Analyst (BA) spike."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_SRC = _REPO_ROOT / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from ai_sdlc.contracts import (  # noqa: E402
    RepoContext,
    TaskSpec,
    approve_task_spec,
    format_task_spec_for_review,
    reject_task_spec,
    validate_task_spec_against_repo,
)
from ai_sdlc.model_routing import resolve_role_router_config  # noqa: E402
from ai_sdlc.repo_context import build_repo_context  # noqa: E402

try:  # Support both package import and direct script execution.
    from .ba_agent import (
        SpecGenerationError,
        attempt_records_to_dicts,
        generate_task_spec,
        preflight_9router,
    )
except ImportError:  # pragma: no cover - direct execution fallback
    from ba_agent import (
        SpecGenerationError,
        attempt_records_to_dicts,
        generate_task_spec,
        preflight_9router,
    )

DEFAULT_REPO_DIR = _REPO_ROOT / "spikes" / "01_instabox_openswe" / "fixtures" / "mini_bug_repo"
DEFAULT_ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts" / "ba_run"


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser for BA spec generation and human review decisions."""
    parser = argparse.ArgumentParser(
        description="Generate, validate, and review a repo-grounded TaskSpec via 9Router BA model."
    )
    parser.add_argument("--task", help="Natural-language user requirement.")
    parser.add_argument("--task-file", type=Path, help="UTF-8 file containing the user requirement.")
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=DEFAULT_REPO_DIR,
        help=f"Target repository directory (default: {DEFAULT_REPO_DIR}).",
    )
    parser.add_argument("--role", default="ba", help="Role key in NINE_ROUTER_ROLE_MODELS (default: ba).")
    parser.add_argument("--model", help="Explicit model override (for example ag/gemini-3.8-flash).")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help=f"Output directory for repo_context.json, task_spec.json, and task_spec_review.md (default: {DEFAULT_ARTIFACT_DIR}).",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate 9Router BA routing and /v1/models availability, then exit.",
    )
    parser.add_argument(
        "--skip-api-preflight",
        action="store_true",
        help="Skip probing GET /v1/models before invoking the BA model.",
    )
    parser.add_argument("--max-repairs", type=int, default=2, help="Self-repair retries on invalid spec (default: 2).")
    parser.add_argument("--temperature", type=float, default=0.1, help="Sampling temperature (default: 0.1).")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds per LLM call (default: 120).")
    parser.add_argument("--revise-from", type=Path, help="Previous TaskSpec JSON path to revise into next version.")
    parser.add_argument("--feedback", help="Human/QA review feedback passed into BA revision.")
    parser.add_argument("--approve", type=Path, help="Approve an existing TaskSpec JSON file and write approval record.")
    parser.add_argument("--reject", type=Path, help="Reject an existing TaskSpec JSON file with mandatory --comment.")
    parser.add_argument("--reviewer", help="Human reviewer identifier for --approve/--reject/--interactive-review.")
    parser.add_argument("--comment", default="", help="Review comment for --approve or mandatory reason for --reject.")
    parser.add_argument(
        "--interactive-review",
        action="store_true",
        help="After generating the draft TaskSpec, interactively prompt to approve/reject/defer.",
    )
    return parser


def _resolve_requirement_text(args: argparse.Namespace, stdin: TextIO) -> str:
    if args.task and args.task_file:
        raise ValueError("Pass either --task or --task-file, not both.")
    if args.task is not None:
        text = args.task.strip()
        if not text:
            raise ValueError("--task must not be empty.")
        return text
    if args.task_file is not None:
        text = args.task_file.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"--task-file {args.task_file} is empty.")
        return text
    if not stdin.isatty():
        text = stdin.read().strip()
        if text:
            return text
    raise ValueError("Provide a requirement via --task, --task-file, or stdin.")


def _load_review_repo_context(spec_path: Path, repo_dir: Path | None) -> RepoContext:
    sibling_ctx = spec_path.parent / "repo_context.json"
    if sibling_ctx.is_file():
        return RepoContext.from_json(sibling_ctx.read_text(encoding="utf-8"))
    if repo_dir is None:
        raise ValueError("Cannot verify spec against repository: missing sibling repo_context.json and --repo-dir.")
    return build_repo_context(repo_dir)


def _persist_reviewed_spec(spec_path: Path, updated: TaskSpec) -> None:
    rendered = updated.to_json() + "\n"
    spec_path.write_text(rendered, encoding="utf-8")
    review_path = spec_path.parent / "task_spec_review.md"
    review_path.write_text(format_task_spec_for_review(updated) + "\n", encoding="utf-8")
    if updated.status == "approved":
        approved_copy = spec_path.parent / "task_spec.approved.json"
        approved_copy.write_text(rendered, encoding="utf-8")


def _handle_review_action(
    *,
    spec_path: Path,
    approve: bool,
    reviewer: str,
    comment: str,
    repo_dir: Path | None,
    out: TextIO,
) -> int:
    if not spec_path.is_file():
        print(f"Error: TaskSpec file not found: {spec_path}", file=out)
        return 2
    spec = TaskSpec.from_json(spec_path.read_text(encoding="utf-8"))
    repo_context = _load_review_repo_context(spec_path, repo_dir)
    errors = validate_task_spec_against_repo(spec, repo_context)
    if errors:
        print(f"Error: TaskSpec {spec_path} failed validation:", file=out)
        for err in errors:
            print(f"  - {err}", file=out)
        return 2

    if approve:
        updated = approve_task_spec(spec, reviewer=reviewer, comment=comment)
        _persist_reviewed_spec(spec_path, updated)
        print(
            f"Approved {updated.spec_id} v{updated.spec_version} "
            f"(sha256={updated.spec_sha256[:12]}) -> {spec_path}",
            file=out,
        )
        return 0

    if not comment.strip():
        print("Error: --reject requires a non-empty --comment explaining what to revise.", file=out)
        return 2
    updated = reject_task_spec(spec, reviewer=reviewer, comment=comment)
    _persist_reviewed_spec(spec_path, updated)
    print(
        f"Rejected {updated.spec_id} v{updated.spec_version}: {updated.approval.comment if updated.approval else ''}",
        file=out,
    )
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    http_client: httpx.Client | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Execute the BA spike CLI."""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    environ = os.environ if env is None else env
    reviewer = (args.reviewer or getpass.getuser() or "human").strip()

    mode_flags = sum(bool(x) for x in (args.preflight_only, args.approve, args.reject))
    if mode_flags > 1:
        print("Error: Choose only one of --preflight-only, --approve, or --reject.", file=out_stream)
        return 2

    try:
        if args.approve is not None:
            return _handle_review_action(
                spec_path=args.approve,
                approve=True,
                reviewer=reviewer,
                comment=args.comment,
                repo_dir=args.repo_dir,
                out=out_stream,
            )
        if args.reject is not None:
            return _handle_review_action(
                spec_path=args.reject,
                approve=False,
                reviewer=reviewer,
                comment=args.comment,
                repo_dir=args.repo_dir,
                out=out_stream,
            )

        router_config = resolve_role_router_config(role=args.role, model=args.model, env=environ)
        if not args.skip_api_preflight or args.preflight_only:
            preflight = preflight_9router(router_config, http_client=http_client)
            print(preflight.message, file=out_stream)
            if not preflight.ok:
                return 2
            if args.preflight_only:
                return 0

        requirement = _resolve_requirement_text(args, in_stream)
        previous_spec: TaskSpec | None = None
        if args.revise_from is not None:
            previous_spec = TaskSpec.from_json(args.revise_from.read_text(encoding="utf-8"))

        repo_context = build_repo_context(args.repo_dir)
        result = generate_task_spec(
            user_requirement=requirement,
            repo_context=repo_context,
            router_config=router_config,
            previous_spec=previous_spec,
            revision_feedback=args.feedback,
            http_client=http_client,
            max_repairs=args.max_repairs,
            temperature=args.temperature,
            timeout_seconds=args.timeout,
        )
    except (OSError, ValueError, SpecGenerationError) as exc:
        print(f"BA spike failed: {exc}", file=out_stream)
        return 2

    artifact_dir = args.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ctx_path = artifact_dir / "repo_context.json"
    spec_path = artifact_dir / "task_spec.json"
    review_path = artifact_dir / "task_spec_review.md"
    meta_path = artifact_dir / "ba_run_metadata.json"

    ctx_path.write_text(result.repo_context.to_json() + "\n", encoding="utf-8")
    spec_path.write_text(result.task_spec.to_json() + "\n", encoding="utf-8")
    review_markdown = format_task_spec_for_review(result.task_spec)
    review_path.write_text(review_markdown + "\n", encoding="utf-8")
    metadata = {
        "base_url": result.base_url,
        "model_id": result.model_id,
        "role": router_config.role,
        "repair_attempts": result.repair_attempts,
        "prompt_sha256": result.prompt_sha256,
        "spec_id": result.task_spec.spec_id,
        "spec_version": result.task_spec.spec_version,
        "spec_sha256": result.task_spec.spec_sha256,
        "repo_commit_sha": result.task_spec.repo_commit_sha,
        "repo_tree_sha256": result.task_spec.repo_tree_sha256,
        "attempt_logs": attempt_records_to_dicts(result.attempt_logs),
    }
    meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("", file=out_stream)
    print(review_markdown, file=out_stream)
    print("", file=out_stream)
    print(f"Saved artifacts in: {artifact_dir}", file=out_stream)
    print(f"  - repo_context.json : {ctx_path}", file=out_stream)
    print(f"  - task_spec.json    : {spec_path} (status={result.task_spec.status})", file=out_stream)
    print(f"  - task_spec_review  : {review_path}", file=out_stream)

    if args.interactive_review:
        choice = input_fn("Decision ([a]pprove / [r]eject / [l]ater): ").strip().lower()
        if choice in ("a", "approve"):
            comment = input_fn("Optional approval comment: ").strip()
            return _handle_review_action(
                spec_path=spec_path,
                approve=True,
                reviewer=reviewer,
                comment=comment,
                repo_dir=args.repo_dir,
                out=out_stream,
            )
        if choice in ("r", "reject"):
            reason = ""
            while not reason:
                reason = input_fn("Rejection reason (required): ").strip()
            return _handle_review_action(
                spec_path=spec_path,
                approve=False,
                reviewer=reviewer,
                comment=reason,
                repo_dir=args.repo_dir,
                out=out_stream,
            )

    print(
        f"Next step: review `{review_path}` then run `--approve \"{spec_path}\"` or `--reject \"{spec_path}\" --comment \"...\"`.",
        file=out_stream,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
