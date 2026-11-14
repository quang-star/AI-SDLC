"""Live/offline Spike 01 Phase 2 runner: bugfix in test repo + independent snapshot verification.

Phase 2 implements the Section 4 verification workflow from `docs/version-matrix.md`:
1. Provision a test git repo in an InstaBox VM (built-in `mini_bug_repo` fixture by default,
   or `--repo-dir` / `--repo-url`).
2. Capture `base_commit_sha`, `existing_test_files`, protected config SHA-256 hashes, and
   `baseline_status` via an isolated clean Python/pytest runner (`--junitxml`).
3. Flush disk state and create `pre_agent_snapshot_id`, waiting until status is `ready`.
4. Run a `deepagents` coding agent (`dev` role by default, capped by `--max-agent-steps`).
5. Extract the complete diff (commits + working tree + untracked files) via a non-destructive
   temporary git index, then run static Hard Gate checks.
6. Boot clean isolated verification environments from `pre_agent_snapshot_id`:
   - Post-hoc Red check (unless `--tdd-exempt`): apply only newly added test files on baseline and
     confirm pytest fails assertions (`exit_code == 1`) with new test ID(s) failing.
   - Green check (always in a fresh verify VM or fresh snapshot restore, never reusing Red's VM state):
     apply the full patch cleanly and require pytest `exit_code == 0` with zero regressions.
7. Strictly clean up all created VMs and snapshots in `finally`, failing non-zero if any
   resource cleanup is incomplete.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import secrets
import shlex
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlparse

from instabox import (
    AuthenticationError,
    BadRequestError,
    Client,
    NotFoundError,
    PermissionDeniedError,
)

_THIS_DIR = Path(__file__).resolve().parent
_REPO_SRC = _THIS_DIR.parents[1] / "src"
for _p in (_REPO_SRC, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ai_sdlc.repo_policy import (  # noqa: E402
    collect_safe_repo_files,
    upload_block_reason as _upload_block_reason,
)
from instabox_sandbox import InstaBoxSandbox, SandboxInitializationError, _validate_workdir  # noqa: E402
from model_routing import ModelTarget, select_model_targets  # noqa: E402
from phase2_verifier import (  # noqa: E402
    ChangedFileEntry,
    Phase2VerificationResult,
    TestOutcome,
    collect_existing_test_files,
    collect_protected_config_paths,
    evaluate_phase2_verification,
    evaluate_static_gates,
    is_protected_config_path,
    merge_git_changes,
    normalize_repo_rel_path,
    parse_junit_xml,
)
from spike_common import (  # noqa: E402
    CREATE_WAIT_TIMEOUT_SECONDS,
    CleanupIncompleteError,
    SpikeConfigError,
    create_vm_with_early_tracking,
    delete_snapshot_strict,
    delete_vm_strict,
    preflight_instabox_api,
    resolve_chat_model,
    validate_instabox_base_env,
    validate_model_targets_config,
)

DEFAULT_PHASE2_WORKDIR: Final[str] = "/data/workspace/phase2"
DEFAULT_FIXTURE_DIR: Final[Path] = _THIS_DIR / "fixtures" / "mini_bug_repo"
DEFAULT_FIXTURE_TASK: Final[str] = (
    "Fix the bug in `src/mini_ledger/billing.py` where `apply_discount_cents` does not match its "
    "documented contract: (1) percentage discounts > 100 must raise `ValueError` instead of returning "
    "negative amounts, and (2) half-cent discount remainders (.5 cent) are truncated instead of rounded "
    "half-up as specified by `(subtotal_cents * discount_percent + 50) // 100`. "
    "Do NOT edit `tests/test_billing.py` or `pyproject.toml`. Add new regression tests covering both cases "
    "in a NEW test file under `tests/` and verify tests pass."
)
DEFAULT_MAX_AGENT_STEPS: Final[int] = 60
DEFAULT_SNAPSHOT_TIMEOUT_SECONDS: Final[float] = 600.0

VerifyMode = Literal["fresh-vm", "restore"]


@dataclass(frozen=True)
class Phase2Config:
    """Validated configuration for a Spike 01 Phase 2 run."""

    api_key: str = field(repr=False)
    base_url: str | None
    machine_id: str | None
    name: str | None
    template: str | None
    snapshot_id: str | None
    workdir: str
    default_timeout: int
    keep_vm: bool
    preflight_only: bool
    skip_api_preflight: bool
    repo_dir: Path | None
    repo_url: str | None
    base_ref: str | None
    task: str
    model_target: ModelTarget
    router_base_url: str | None = None
    router_api_key: str | None = field(default=None, repr=False)
    python_bin: str | None = None
    verify_mode: VerifyMode = "fresh-vm"
    allow_config_changes: bool = False
    tdd_exempt: bool = False
    max_agent_steps: int = DEFAULT_MAX_AGENT_STEPS
    snapshot_timeout: float = DEFAULT_SNAPSHOT_TIMEOUT_SECONDS
    artifact_dir: Path | None = None


@dataclass(frozen=True)
class Phase2RunSummary:
    """Result and resource IDs produced by `run_phase2_spike`."""

    run_id: str
    agent_machine_id: str
    verify_machine_id: str | None
    pre_agent_snapshot_id: str | None
    base_commit_sha: str
    post_agent_head_sha: str
    verification: Phase2VerificationResult
    patch_bytes: bytes = field(repr=False)
    verify_red_machine_id: str | None = None


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Spike 01 Phase 2 bugfix + independent pre-agent snapshot verification.",
    )
    parser.add_argument("--machine-id", default=None, help="Existing InstaBox machine ID to reuse (not auto-deleted).")
    parser.add_argument("--name", default=None, help="Name for newly created VM.")
    parser.add_argument("--template", default=None, help="Template ID for newly created VM.")
    parser.add_argument("--snapshot-id", default=None, help="Initial snapshot ID for creating the agent VM.")
    parser.add_argument("--workdir", default=None, help=f"Absolute POSIX repo path in VM (default: {DEFAULT_PHASE2_WORKDIR}).")
    parser.add_argument("--timeout", default=None, help="Default exec timeout in seconds, 1..600.")
    parser.add_argument("--keep-vm", action="store_true", help="Keep created VMs and pre-agent snapshot for debugging.")
    parser.add_argument("--preflight-only", action="store_true", help="Validate config and InstaBox auth without creating VMs.")
    parser.add_argument("--skip-api-preflight", action="store_true", help="Skip `templates.list()` API check during preflight.")

    parser.add_argument("--repo-dir", default=None, help="Local directory on host to upload as git repo (defaults to built-in fixture).")
    parser.add_argument("--repo-url", default=None, help="HTTPS git repository URL to clone inside the VM.")
    parser.add_argument("--base-ref", default=None, help="Optional git branch/tag/SHA to checkout when using --repo-url.")
    parser.add_argument("--task", default=None, help="Bugfix task prompt for the coding agent (required for --repo-dir / --repo-url).")

    parser.add_argument("--model", default=None, help="Explicit model ID (`9router:<id>` or `provider:<model>`).")
    parser.add_argument("--role", default=None, help="Single role from NINE_ROUTER_ROLE_MODELS (defaults to `dev` when role map is active).")

    parser.add_argument("--python-bin", default=None, help="Absolute POSIX path to Python with pytest inside the VM.")
    parser.add_argument(
        "--verify-mode",
        choices=("fresh-vm", "restore"),
        default="fresh-vm",
        help="Verify patch in a fresh VM booted from `pre_agent_snapshot_id` (default) or in-place `machines.restore`.",
    )
    parser.add_argument("--allow-config-changes", action="store_true", help="Downgrade protected config edits from REJECT to NEEDS_HUMAN_REVIEW.")
    parser.add_argument("--tdd-exempt", action="store_true", help="Waive TDD Red failure requirement (post verification still enforced).")
    parser.add_argument("--max-agent-steps", type=int, default=DEFAULT_MAX_AGENT_STEPS, help="LangGraph recursion limit (default: 60).")
    parser.add_argument("--snapshot-timeout", type=float, default=DEFAULT_SNAPSHOT_TIMEOUT_SECONDS, help="Max seconds to wait for snapshot ready state.")
    parser.add_argument("--artifact-dir", default=None, help="Optional host directory to write patch, JUnit XMLs, and report JSON.")
    return parser.parse_args(list(argv) if argv is not None else None)


def _select_phase2_model_target(
    *,
    model: str | None,
    role: str | None,
    env: Mapping[str, str],
) -> ModelTarget:
    if model is not None and role is not None:
        raise ValueError("Choose only one of `--model` or `--role`.")
    if model is not None:
        return select_model_targets(model=model, models=None, roles=None, env=env)[0]
    if role is not None:
        return select_model_targets(model=None, models=None, roles=(role,), env=env)[0]
    if "NINE_ROUTER_MODEL" in env or "DEEPAGENTS_MODEL" in env:
        return select_model_targets(model=None, models=None, roles=None, env=env)[0]
    if "NINE_ROUTER_ROLE_MODELS" in env:
        return select_model_targets(model=None, models=None, roles=("dev",), env=env)[0]
    raise ValueError("Set NINE_ROUTER_ROLE_MODELS (with `dev`), NINE_ROUTER_MODEL, or pass `--model` / `--role`.")


def load_and_validate_phase2_config(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Phase2Config:
    """Validate all local configuration, repo source, and model credentials before any API call."""
    environ = os.environ if env is None else env
    args = _parse_args(argv)
    base_settings, errors = validate_instabox_base_env(
        environ=environ,
        machine_id_arg=args.machine_id,
        name_arg=args.name,
        template_arg=args.template,
        snapshot_id_arg=args.snapshot_id,
        workdir_arg=args.workdir,
        timeout_arg=args.timeout,
        default_workdir=DEFAULT_PHASE2_WORKDIR,
    )

    if args.repo_dir is not None and args.repo_url is not None:
        errors.append("Cannot combine `--repo-dir` and `--repo-url`.")

    repo_dir: Path | None = None
    repo_url: str | None = None
    base_ref: str | None = None

    if args.repo_dir is not None:
        candidate = Path(args.repo_dir).resolve()
        if not candidate.is_dir():
            errors.append(f"`--repo-dir` must be an existing directory, got {args.repo_dir!r}.")
        else:
            repo_dir = candidate
    elif args.repo_url is not None:
        raw_url = args.repo_url.strip()
        parsed = urlparse(raw_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            errors.append("`--repo-url` must be an http(s) URL without embedded credentials.")
        else:
            repo_url = raw_url
        if args.base_ref is not None:
            stripped_ref = args.base_ref.strip()
            if not stripped_ref:
                errors.append("`--base-ref` cannot be empty/whitespace.")
            else:
                base_ref = stripped_ref
    else:
        if args.base_ref is not None:
            errors.append("`--base-ref` requires `--repo-url`.")
        if not DEFAULT_FIXTURE_DIR.is_dir():
            errors.append(f"Built-in Phase 2 fixture directory missing at {DEFAULT_FIXTURE_DIR}.")
        else:
            repo_dir = DEFAULT_FIXTURE_DIR

    task_text = args.task.strip() if args.task is not None else ""
    if (args.repo_dir is not None or args.repo_url is not None) and not task_text:
        errors.append("`--task` is required when using custom `--repo-dir` or `--repo-url`.")
    elif args.task is not None and not task_text:
        errors.append("`--task` cannot be empty/whitespace.")
    effective_task = task_text or DEFAULT_FIXTURE_TASK

    python_bin: str | None = None
    if args.python_bin is not None:
        try:
            python_bin = _validate_workdir(args.python_bin)
        except (TypeError, ValueError) as exc:
            errors.append(f"Invalid `--python-bin`: {exc}")

    if args.max_agent_steps < 5 or args.max_agent_steps > 200:
        errors.append(f"`--max-agent-steps` must be between 5 and 200, got {args.max_agent_steps}.")
    if args.snapshot_timeout <= 0 or args.snapshot_timeout > 3600:
        errors.append(f"`--snapshot-timeout` must be in (0, 3600], got {args.snapshot_timeout}.")

    selected_target: ModelTarget | None = None
    router_base_url: str | None = None
    router_api_key: str | None = None
    try:
        selected_target = _select_phase2_model_target(model=args.model, role=args.role, env=environ)
    except ValueError as exc:
        errors.append(str(exc))
    else:
        router_base_url, router_api_key, model_errors = validate_model_targets_config(
            (selected_target,),
            environ,
            find_spec=importlib.util.find_spec,
        )
        errors.extend(model_errors)

    artifact_dir = Path(args.artifact_dir).resolve() if args.artifact_dir is not None else None

    if errors:
        bullet_list = "\n".join(f"  - {item}" for item in errors)
        raise SpikeConfigError(f"Phase 2 configuration check failed before creating VM:\n{bullet_list}")

    assert selected_target is not None
    return Phase2Config(
        api_key=base_settings.api_key,
        base_url=base_settings.base_url,
        machine_id=base_settings.machine_id,
        name=base_settings.name,
        template=base_settings.template,
        snapshot_id=base_settings.snapshot_id,
        workdir=base_settings.workdir,
        default_timeout=base_settings.default_timeout,
        keep_vm=bool(args.keep_vm),
        preflight_only=bool(args.preflight_only),
        skip_api_preflight=bool(args.skip_api_preflight),
        repo_dir=repo_dir,
        repo_url=repo_url,
        base_ref=base_ref,
        task=effective_task,
        model_target=selected_target,
        router_base_url=router_base_url,
        router_api_key=router_api_key,
        python_bin=python_bin,
        verify_mode=args.verify_mode,
        allow_config_changes=bool(args.allow_config_changes),
        tdd_exempt=bool(args.tdd_exempt),
        max_agent_steps=int(args.max_agent_steps),
        snapshot_timeout=float(args.snapshot_timeout),
        artifact_dir=artifact_dir,
    )


def _exec_checked(
    client: Client,
    machine_id: str,
    command: str,
    *,
    cwd: str,
    timeout: int = 120,
    env: Mapping[str, str] | None = None,
) -> str:
    res = client.machines.exec(machine_id, command, cwd=cwd, env=env, timeout=timeout)
    if res.exit_code != 0:
        raise RuntimeError(
            f"VM command failed (exit_code={res.exit_code}, timed_out={res.timed_out}): {command!r}\n"
            f"stdout={res.stdout}\nstderr={res.stderr}"
        )
    return res.stdout


def _collect_local_repo_files(repo_dir: Path) -> list[tuple[str, bytes]]:
    files = collect_safe_repo_files(
        repo_dir,
        on_skip_dir=lambda rel_dir: print(f"[UPLOAD] Skipping symlink directory before traversal: {rel_dir}"),
        on_skip_file=lambda rel, reason: print(f"[UPLOAD] Skipping blocked/secret file before read: {rel} ({reason})"),
    )
    if not files:
        raise SpikeConfigError(f"No uploadable files found in repo_dir {repo_dir}.")
    return files


def _provision_repo_in_vm(
    client: Client,
    sandbox: InstaBoxSandbox,
    config: Phase2Config,
) -> str:
    """Upload or clone target repo into `sandbox.workdir` and return clean `base_commit_sha`."""
    _exec_checked(client, sandbox.id, "git --version", cwd=sandbox.workdir, timeout=30)

    if config.repo_url is not None:
        quoted_url = shlex.quote(config.repo_url)
        _exec_checked(
            client,
            sandbox.id,
            f"git clone --quiet {quoted_url} .",
            cwd=sandbox.workdir,
            timeout=180,
        )
        if config.base_ref is not None:
            _exec_checked(
                client,
                sandbox.id,
                f"git checkout --quiet {shlex.quote(config.base_ref)}",
                cwd=sandbox.workdir,
                timeout=60,
            )
    else:
        assert config.repo_dir is not None
        local_files = _collect_local_repo_files(config.repo_dir)
        up_results = sandbox.upload_files(local_files)
        failed = [r for r in up_results if r.error is not None]
        if failed:
            raise RuntimeError(f"Failed uploading repo fixture to VM: {failed!r}")
        _exec_checked(
            client,
            sandbox.id,
            (
                "git init -q && "
                'git config user.name "Spike Bot" && '
                'git config user.email "spike@example.invalid" && '
                "git add -A && "
                'git commit -q -m "baseline"'
            ),
            cwd=sandbox.workdir,
            timeout=60,
        )

    sha = _exec_checked(client, sandbox.id, "git rev-parse HEAD", cwd=sandbox.workdir, timeout=30).strip()
    dirty = _exec_checked(client, sandbox.id, "git status --porcelain -uall", cwd=sandbox.workdir, timeout=30)
    if dirty.strip():
        raise RuntimeError(f"Repository is not clean after provisioning: {dirty!r}")
    return sha


def _resolve_clean_pytest_python(
    client: Client,
    machine_id: str,
    *,
    workdir: str,
    run_id: str,
    explicit_python: str | None,
) -> str:
    """Resolve an absolute Python path with pytest available, bootstrapping an isolated venv if missing."""
    if explicit_python is not None:
        _exec_checked(
            client,
            machine_id,
            f"{shlex.quote(explicit_python)} -m pytest --version",
            cwd=workdir,
            timeout=30,
        )
        return explicit_python

    base_py = _exec_checked(
        client,
        machine_id,
        "readlink -f \"$(command -v python3)\"",
        cwd=workdir,
        timeout=30,
    ).strip()
    check_res = client.machines.exec(
        machine_id,
        f"{shlex.quote(base_py)} -m pytest --version",
        cwd=workdir,
        timeout=30,
    )
    if check_res.exit_code == 0:
        return base_py

    # InstaBox's unprivileged guest user owns /data, unlike /opt. Keep the
    # runner outside the checkout so git clean cannot delete it during verify.
    venv_dir = f"/data/phase2_pytest_{run_id}"
    venv_py = f"{venv_dir}/bin/python"
    print(f"[PROVISION] System pytest not found; bootstrapping isolated runner venv at {venv_dir}...")
    _exec_checked(
        client,
        machine_id,
        f"{shlex.quote(base_py)} -m venv {shlex.quote(venv_dir)} && {shlex.quote(venv_py)} -m pip install -q pytest",
        cwd=workdir,
        timeout=180,
    )
    _exec_checked(client, machine_id, f"{shlex.quote(venv_py)} -m pytest --version", cwd=workdir, timeout=30)
    return venv_py


def _compute_file_hashes_in_vm(
    client: Client,
    machine_id: str,
    *,
    workdir: str,
    rel_paths: Sequence[str],
    output_path: str,
) -> dict[str, str]:
    """Hash repo-relative files in the VM via Python SHA-256 and download JSON from `output_path`."""
    if not rel_paths:
        return {}
    payload_b64 = base64.b64encode(json.dumps(list(rel_paths)).encode("utf-8")).decode("ascii")
    script = (
        "import base64, hashlib, json, pathlib; "
        f"paths = json.loads(base64.b64decode({payload_b64!r})); "
        "out = {}; "
        "root = pathlib.Path('.').resolve(); "
        "[out.__setitem__(p, hashlib.sha256((root / p).read_bytes()).hexdigest()) "
        "for p in paths if (root / p).is_file()]; "
        f"pathlib.Path({output_path!r}).write_text(json.dumps(out), encoding='utf-8')"
    )
    _exec_checked(client, machine_id, ["python3", "-c", script], cwd=workdir, timeout=60)
    raw = client.files.read_bytes(machine_id, output_path, timeout=60)
    return json.loads(raw.decode("utf-8"))


def _run_pytest_and_collect_junit(
    client: Client,
    machine_id: str,
    *,
    workdir: str,
    python_bin: str,
    junit_path: str,
    cache_dir: str,
    timeout: int,
    allow_missing_xml_on_nonzero: bool = False,
) -> tuple[int, bytes, dict[str, TestOutcome]]:
    """Run pytest cleanly outside repo cache and return `(exit_code, xml_bytes, outcome_map)`."""
    cmd = (
        f"rm -f {shlex.quote(junit_path)} && "
        f"{shlex.quote(python_bin)} -m pytest "
        f"-o {shlex.quote(f'cache_dir={cache_dir}')} "
        f"--junitxml={shlex.quote(junit_path)} -q"
    )
    res = client.machines.exec(
        machine_id,
        cmd,
        cwd=workdir,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        timeout=timeout,
    )
    if res.timed_out:
        raise RuntimeError(f"Pytest execution timed out after {timeout}s")
    try:
        xml_bytes = client.files.read_bytes(machine_id, junit_path, timeout=60)
    except NotFoundError:
        if allow_missing_xml_on_nonzero and res.exit_code != 0:
            return res.exit_code, b"", {}
        raise
    if not xml_bytes or not xml_bytes.strip():
        if allow_missing_xml_on_nonzero and res.exit_code != 0:
            return res.exit_code, b"", {}
    outcomes = parse_junit_xml(xml_bytes)
    return res.exit_code, xml_bytes, outcomes


def _create_and_wait_snapshot(
    client: Client,
    *,
    machine_id: str,
    snapshot_name: str,
    description: str,
    run_id: str,
    tracked_snapshot_ids: list[str],
    timeout_seconds: float,
    poll_interval: float = 2.0,
) -> str:
    """Create `pre_agent_snapshot_id`, track it for cleanup, and wait until `ready`."""
    idempotency_key = f"ik_snap_{run_id}"
    snap = None
    try:
        snap = client.snapshots.create(
            machine_id,
            snapshot_name,
            description=description,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        if not isinstance(
            exc,
            (BadRequestError, AuthenticationError, PermissionDeniedError, NotFoundError),
        ):
            try:
                for candidate in client.snapshots.list():
                    if candidate.name == snapshot_name and candidate.source_machine_id == machine_id:
                        if candidate.id and candidate.id not in tracked_snapshot_ids:
                            tracked_snapshot_ids.append(candidate.id)
                        break
            except Exception as list_exc:
                print(
                    f"[SNAPSHOT] Recovery list failed after ambiguous create error: "
                    f"{type(list_exc).__name__}: {list_exc}",
                    file=sys.stderr,
                )
        raise

    snap_id = (getattr(snap, "id", None) or "").strip()
    if not snap_id:
        raise RuntimeError("InstaBox API returned a snapshot without a valid id")
    if snap_id not in tracked_snapshot_ids:
        tracked_snapshot_ids.append(snap_id)

    deadline = time.monotonic() + timeout_seconds
    current = snap
    while True:
        status = (getattr(current, "status", "") or "").strip().lower()
        if status == "ready":
            print(f"[SNAPSHOT] Ready: pre_agent_snapshot_id={snap_id!r}")
            return snap_id
        if status in ("failed", "error"):
            err_msg = getattr(current, "error", "") or status
            raise RuntimeError(f"Snapshot {snap_id!r} failed: {err_msg}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting {timeout_seconds}s for snapshot {snap_id!r} (last status={status!r})"
            )
        time.sleep(poll_interval)
        current = client.snapshots.get(snap_id)


def _extract_post_agent_artifacts(
    client: Client,
    machine_id: str,
    *,
    workdir: str,
    base_commit_sha: str,
    baseline_protected_paths: Sequence[str],
    run_id: str,
) -> tuple[str, list[ChangedFileEntry], bytes, dict[str, str], dict[str, str]]:
    """Extract commits + staged + unstaged + untracked diff using a temporary git index."""
    idx_file = f"/tmp/phase2_idx_{run_id}"
    ns_path = f"/tmp/phase2_ns_{run_id}.bin"
    raw_path = f"/tmp/phase2_raw_{run_id}.bin"
    patch_path = f"/tmp/phase2_full_{run_id}.patch"
    hash_path = f"/tmp/phase2_post_hashes_{run_id}.json"

    post_head = _exec_checked(client, machine_id, "git rev-parse HEAD", cwd=workdir, timeout=30).strip()
    q_idx = shlex.quote(idx_file)
    q_base = shlex.quote(base_commit_sha)
    cmd = (
        f"rm -f {q_idx} && "
        f"GIT_INDEX_FILE={q_idx} git read-tree HEAD && "
        f"GIT_INDEX_FILE={q_idx} git add -A && "
        f"GIT_INDEX_FILE={q_idx} git diff --cached --name-status -M --no-ext-diff -z {q_base} > {shlex.quote(ns_path)} && "
        f"GIT_INDEX_FILE={q_idx} git diff --cached --raw --no-renames -z {q_base} > {shlex.quote(raw_path)} && "
        f"GIT_INDEX_FILE={q_idx} git diff --cached --binary --no-ext-diff --full-index {q_base} > {shlex.quote(patch_path)}"
    )
    _exec_checked(client, machine_id, cmd, cwd=workdir, timeout=120)

    ns_bytes = client.files.read_bytes(machine_id, ns_path, timeout=60)
    raw_bytes = client.files.read_bytes(machine_id, raw_path, timeout=60)
    patch_bytes = client.files.read_bytes(machine_id, patch_path, timeout=60)
    changed_entries = merge_git_changes(ns_bytes, raw_bytes)

    all_protected: set[str] = {normalize_repo_rel_path(p) for p in baseline_protected_paths}
    for entry in changed_entries:
        if is_protected_config_path(entry.path):
            all_protected.add(entry.path)
        if entry.old_path and is_protected_config_path(entry.old_path):
            all_protected.add(entry.old_path)
    post_config_hashes = _compute_file_hashes_in_vm(
        client,
        machine_id,
        workdir=workdir,
        rel_paths=sorted(all_protected),
        output_path=hash_path,
    )
    return post_head, changed_entries, patch_bytes, post_config_hashes, {"idx_file": idx_file}


def _extract_tests_only_patch(
    client: Client,
    machine_id: str,
    *,
    workdir: str,
    base_commit_sha: str,
    idx_file: str,
    new_test_files: Sequence[str],
    run_id: str,
) -> bytes:
    """Extract unified binary patch containing strictly the newly added test files."""
    if not new_test_files:
        return b""
    tests_patch_path = f"/tmp/phase2_tests_only_{run_id}.patch"
    quoted_paths = " ".join(shlex.quote(p) for p in new_test_files)
    cmd = (
        f"GIT_INDEX_FILE={shlex.quote(idx_file)} "
        f"git diff --cached --binary --no-ext-diff --full-index {shlex.quote(base_commit_sha)} -- {quoted_paths} "
        f"> {shlex.quote(tests_patch_path)}"
    )
    _exec_checked(client, machine_id, cmd, cwd=workdir, timeout=60)
    return client.files.read_bytes(machine_id, tests_patch_path, timeout=60)


def _run_agent_bugfix_turn(
    sandbox: InstaBoxSandbox,
    config: Phase2Config,
) -> None:
    """Invoke the configured coding agent inside `sandbox` with a bounded step budget."""
    from deepagents import create_deep_agent

    model = resolve_chat_model(
        config.model_target.model,
        router_base_url=config.router_base_url,
        router_api_key=config.router_api_key,
    )
    agent = create_deep_agent(
        model=model,
        backend=sandbox,
        system_prompt=(
            f"You are an autonomous software engineer working inside a Linux git repository at {sandbox.workdir}. "
            "Read the codebase, diagnose the reported bug, add regression tests in a NEW test file under `tests/` "
            "(never modify existing test files or protected config files such as `pyproject.toml`), fix the bug, "
            "and verify the test suite passes using the provided tools."
        ),
    )
    print(
        f"[AGENT] Running {config.model_target.label} ({config.model_target.model}), "
        f"recursion_limit={config.max_agent_steps}..."
    )
    agent.invoke(
        {"messages": [{"role": "user", "content": config.task}]},
        config={"recursion_limit": config.max_agent_steps},
    )


def _prepare_clean_snapshot_workspace(
    client: Client,
    machine_id: str,
    *,
    workdir: str,
    base_commit_sha: str,
) -> None:
    """Confirm the verification workspace is cleanly checked out at `base_commit_sha`."""
    actual_sha = _exec_checked(client, machine_id, "git rev-parse HEAD", cwd=workdir, timeout=30).strip()
    if actual_sha != base_commit_sha:
        raise RuntimeError(
            f"Clean verification VM HEAD mismatch: expected {base_commit_sha}, got {actual_sha}"
        )
    _exec_checked(
        client,
        machine_id,
        f"git reset -q --hard {shlex.quote(base_commit_sha)} && git clean -fdxq",
        cwd=workdir,
        timeout=60,
    )


def _apply_patch_and_run_pytest(
    client: Client,
    machine_id: str,
    *,
    workdir: str,
    base_commit_sha: str,
    clean_python: str,
    patch_bytes: bytes,
    stage_label: str,
    patch_path: str,
    junit_path: str,
    cache_dir: str,
    timeout: int,
) -> tuple[int, bytes, dict[str, TestOutcome]]:
    """Apply `patch_bytes` on a fresh snapshot checkout and run pytest with strict exit tracking."""
    _prepare_clean_snapshot_workspace(
        client,
        machine_id,
        workdir=workdir,
        base_commit_sha=base_commit_sha,
    )
    client.files.write(machine_id, patch_path, patch_bytes, timeout=60)
    _exec_checked(
        client,
        machine_id,
        f"git apply --check {shlex.quote(patch_path)} && git apply {shlex.quote(patch_path)}",
        cwd=workdir,
        timeout=60,
    )
    exit_code, xml_bytes, outcomes = _run_pytest_and_collect_junit(
        client,
        machine_id,
        workdir=workdir,
        python_bin=clean_python,
        junit_path=junit_path,
        cache_dir=cache_dir,
        timeout=timeout,
        allow_missing_xml_on_nonzero=True,
    )
    print(f"[VERIFY:{stage_label}] pytest exit_code={exit_code}, cases={len(outcomes)}")
    return exit_code, xml_bytes, outcomes


def _boot_fresh_verify_vm(
    client: Client,
    *,
    stage_tag: str,
    pre_agent_snapshot_id: str,
    workdir: str,
    default_timeout: int,
    run_id: str,
    tracked_machines: list[str],
    machine_ops: dict[str, str | None],
) -> str:
    """Create and connect a clean verify VM from `pre_agent_snapshot_id` for a single phase."""
    vm_name = f"spike01-p2-verify-{stage_tag}-{int(time.time())}-{run_id}"
    vm_id, op_id = create_vm_with_early_tracking(
        client,
        vm_name=vm_name,
        template=None,
        snapshot_id=pre_agent_snapshot_id,
        metadata={"spike": f"01-phase2-verify-{stage_tag}", "spike_run_id": run_id},
        idempotency_key=f"ik_p2_verify_{stage_tag}_{run_id}",
        run_id=run_id,
        has_explicit_name=False,
        tracked_machine_ids=tracked_machines,
        wait_timeout=CREATE_WAIT_TIMEOUT_SECONDS,
    )
    machine_ops[vm_id] = op_id
    InstaBoxSandbox.create_or_reconnect(
        client,
        machine_id=vm_id,
        workdir=workdir,
        default_timeout=default_timeout,
        ensure_workdir=False,
    )
    return vm_id


def _release_created_vm_now(
    client: Client,
    *,
    machine_id: str,
    run_id: str,
    keep_vm: bool,
    tracked_machines: list[str],
    machine_ops: Mapping[str, str | None],
    pre_retained_machines: list[str],
) -> None:
    """Delete a created temporary VM immediately so concurrent VM count stays <= 1."""
    if keep_vm or machine_id not in tracked_machines:
        return
    tracked_machines.remove(machine_id)
    err = delete_vm_strict(
        client,
        machine_id=machine_id,
        operation_id=machine_ops.get(machine_id),
        run_id=run_id,
    )
    if err is not None:
        pre_retained_machines.append(machine_id)
        raise err


def _restore_vm_to_snapshot(
    client: Client,
    *,
    machine_id: str,
    snapshot_id: str,
    workdir: str,
    default_timeout: int,
) -> None:
    """Restore an existing VM back to `snapshot_id` and reconnect its sandbox."""
    client.machines.restore(
        machine_id,
        snapshot_id,
        wait=True,
        timeout=CREATE_WAIT_TIMEOUT_SECONDS,
    )
    InstaBoxSandbox.create_or_reconnect(
        client,
        machine_id=machine_id,
        workdir=workdir,
        default_timeout=default_timeout,
        ensure_workdir=False,
    )


def _run_isolated_verification_stage(
    client: Client,
    *,
    stage_tag: Literal["red", "green"],
    verify_mode: VerifyMode,
    agent_machine_id: str,
    pre_agent_snapshot_id: str,
    workdir: str,
    default_timeout: int,
    base_commit_sha: str,
    clean_python: str,
    patch_bytes: bytes,
    run_id: str,
    keep_vm: bool,
    tracked_machines: list[str],
    machine_ops: dict[str, str | None],
    pre_retained_machines: list[str],
) -> tuple[str | None, int, bytes, dict[str, TestOutcome]]:
    """Run either the Red or Green stage against an untouched `pre_agent_snapshot_id` state."""
    is_red = stage_tag == "red"
    stage_label = "RED" if is_red else "POST"
    patch_file_tag = "red" if is_red else "full"
    junit_tag = "red" if is_red else "post"

    created_verify_id: str | None = None
    if verify_mode == "fresh-vm":
        created_verify_id = _boot_fresh_verify_vm(
            client,
            stage_tag=stage_tag,
            pre_agent_snapshot_id=pre_agent_snapshot_id,
            workdir=workdir,
            default_timeout=default_timeout,
            run_id=run_id,
            tracked_machines=tracked_machines,
            machine_ops=machine_ops,
        )
        target_machine_id = created_verify_id
    else:
        _restore_vm_to_snapshot(
            client,
            machine_id=agent_machine_id,
            snapshot_id=pre_agent_snapshot_id,
            workdir=workdir,
            default_timeout=default_timeout,
        )
        target_machine_id = agent_machine_id

    exit_code, xml_bytes, outcomes = _apply_patch_and_run_pytest(
        client,
        target_machine_id,
        workdir=workdir,
        base_commit_sha=base_commit_sha,
        clean_python=clean_python,
        patch_bytes=patch_bytes,
        stage_label=stage_label,
        patch_path=f"/tmp/phase2_verify_{patch_file_tag}_{run_id}.patch",
        junit_path=f"/tmp/phase2_{junit_tag}_{run_id}.xml",
        cache_dir=f"/tmp/phase2_cache_{junit_tag}_{run_id}",
        timeout=default_timeout,
    )
    if created_verify_id is not None and is_red:
        # Tear down Red VM before creating Green VM so max concurrent VMs stays 1.
        _release_created_vm_now(
            client,
            machine_id=created_verify_id,
            run_id=run_id,
            keep_vm=keep_vm,
            tracked_machines=tracked_machines,
            machine_ops=machine_ops,
            pre_retained_machines=pre_retained_machines,
        )
    return created_verify_id, exit_code, xml_bytes, outcomes


def _write_artifacts(
    artifact_dir: Path,
    *,
    summary: Phase2RunSummary,
    baseline_xml: bytes,
    red_xml: bytes | None,
    post_xml: bytes | None,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "patch.diff").write_bytes(summary.patch_bytes)
    (artifact_dir / "baseline_junit.xml").write_bytes(baseline_xml)
    if red_xml is not None:
        (artifact_dir / "red_junit.xml").write_bytes(red_xml)
    if post_xml is not None:
        (artifact_dir / "post_junit.xml").write_bytes(post_xml)
    report_payload: dict[str, Any] = {
        "run_id": summary.run_id,
        "agent_machine_id": summary.agent_machine_id,
        "verify_red_machine_id": summary.verify_red_machine_id,
        "verify_machine_id": summary.verify_machine_id,
        "pre_agent_snapshot_id": summary.pre_agent_snapshot_id,
        "base_commit_sha": summary.base_commit_sha,
        "post_agent_head_sha": summary.post_agent_head_sha,
        "verification": summary.verification.to_dict(),
    }
    (artifact_dir / "verification_report.json").write_text(
        json.dumps(report_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _format_tdd_exempt_evidence(
    *,
    changed_entries: Sequence[ChangedFileEntry],
    post_status: Mapping[str, TestOutcome],
) -> str:
    passed = sum(1 for v in post_status.values() if v == "passed")
    return (
        f"tdd_exempt=True: post suite on clean pre-agent snapshot ran {len(post_status)} tests "
        f"({passed} passed) across {len(changed_entries)} changed files."
    )


def run_phase2_spike(
    config: Phase2Config,
    *,
    client_factory: Callable[..., Client] | None = None,
    run_id: str | None = None,
) -> Phase2RunSummary:
    """Execute the end-to-end Phase 2 bugfix + clean snapshot verification pipeline."""
    effective_run_id = run_id or secrets.token_hex(6)
    factory = Client if client_factory is None else client_factory
    client = factory(api_key=config.api_key, base_url=config.base_url)

    tracked_machines: list[str] = []
    pre_retained_machines: list[str] = []
    machine_ops: dict[str, str | None] = {}
    tracked_snapshots: list[str] = []
    primary_exc: BaseException | None = None

    agent_machine_id = ""
    verify_red_machine_id: str | None = None
    verify_machine_id: str | None = None
    pre_agent_snapshot_id: str | None = None

    try:
        preflight_status = preflight_instabox_api(
            client,
            machine_id=config.machine_id,
            template=config.template,
            skip_api_preflight=config.skip_api_preflight,
        )
        if config.preflight_only:
            if preflight_status == "skipped":
                print(
                    "[PREFLIGHT] Local Phase 2 configuration check passed ONLY "
                    "(--preflight-only + --skip-api-preflight); API authentication was NOT verified; no VM created."
                )
            elif preflight_status == "insufficient_scope":
                print(
                    "[PREFLIGHT] Local Phase 2 configuration OK; API key authenticated but lacked templates scope; "
                    "no VM created."
                )
            else:
                print(
                    "[PREFLIGHT] Phase 2 configuration and API authentication verified (--preflight-only); "
                    "no VM created."
                )
            empty_verif = Phase2VerificationResult(
                verdict="PASS",
                reasons=(),
                new_test_files=(),
                new_test_ids=(),
                red_to_green_ids=(),
                regressed_ids=(),
                missing_baseline_ids=(),
                post_failing_ids=(),
                modified_existing_test_files=(),
                touched_protected_configs=(),
                forbidden_mode_entries=(),
                baseline_counts={},
                post_counts={},
                tdd_exempt=config.tdd_exempt,
                tdd_exempt_evidence=None,
            )
            return Phase2RunSummary(
                run_id=effective_run_id,
                agent_machine_id="",
                verify_machine_id=None,
                pre_agent_snapshot_id=None,
                base_commit_sha="",
                post_agent_head_sha="",
                verification=empty_verif,
                patch_bytes=b"",
            )

        # 1. Acquire / create agent VM with early tracking.
        if config.machine_id is not None:
            sandbox = InstaBoxSandbox.create_or_reconnect(
                client,
                machine_id=config.machine_id,
                workdir=config.workdir,
                default_timeout=config.default_timeout,
                ensure_workdir=True,
            )
            agent_machine_id = sandbox.id
        else:
            agent_vm_name = config.name or f"spike01-p2-{int(time.time())}-{effective_run_id}"
            agent_machine_id, agent_op_id = create_vm_with_early_tracking(
                client,
                vm_name=agent_vm_name,
                template=config.template,
                snapshot_id=config.snapshot_id,
                metadata={"spike": "01-phase2", "spike_run_id": effective_run_id},
                idempotency_key=f"ik_p2_agent_{effective_run_id}",
                run_id=effective_run_id,
                has_explicit_name=config.name is not None,
                tracked_machine_ids=tracked_machines,
                wait_timeout=CREATE_WAIT_TIMEOUT_SECONDS,
            )
            machine_ops[agent_machine_id] = agent_op_id
            try:
                sandbox = InstaBoxSandbox.create_or_reconnect(
                    client,
                    machine_id=agent_machine_id,
                    workdir=config.workdir,
                    default_timeout=config.default_timeout,
                    ensure_workdir=True,
                )
            except SandboxInitializationError as exc:
                print(
                    f"[VM] Initialization failed on agent machine_id={exc.machine_id!r} "
                    f"(operation_id={agent_op_id!r}): {exc}",
                    file=sys.stderr,
                )
                raise

        # 2. Provision repo + isolated pytest + baseline capture BEFORE snapshot.
        base_commit_sha = _provision_repo_in_vm(client, sandbox, config)
        clean_python = _resolve_clean_pytest_python(
            client,
            agent_machine_id,
            workdir=sandbox.workdir,
            run_id=effective_run_id,
            explicit_python=config.python_bin,
        )
        ls_raw = _exec_checked(client, agent_machine_id, "git ls-files -z", cwd=sandbox.workdir, timeout=30)
        repo_files = [p for p in ls_raw.split("\x00") if p]
        existing_test_files = collect_existing_test_files(repo_files)
        protected_configs = collect_protected_config_paths(repo_files)
        baseline_config_hashes = _compute_file_hashes_in_vm(
            client,
            agent_machine_id,
            workdir=sandbox.workdir,
            rel_paths=protected_configs,
            output_path=f"/tmp/phase2_base_hashes_{effective_run_id}.json",
        )
        base_exit, baseline_xml, baseline_status = _run_pytest_and_collect_junit(
            client,
            agent_machine_id,
            workdir=sandbox.workdir,
            python_bin=clean_python,
            junit_path=f"/tmp/phase2_base_{effective_run_id}.xml",
            cache_dir=f"/tmp/phase2_cache_base_{effective_run_id}",
            timeout=config.default_timeout,
        )
        print(
            f"[BASELINE] commit={base_commit_sha[:12]}, exit_code={base_exit}, "
            f"cases={len(baseline_status)}, existing_tests={len(existing_test_files)}"
        )
        if base_exit not in (0, 1):
            raise RuntimeError(
                f"Baseline pytest failed unexpectedly with exit_code={base_exit} (expected 0 or 1); "
                "cannot establish a clean baseline."
            )

        # Remove temp artifacts and sync before taking pre_agent_snapshot_id.
        _exec_checked(
            client,
            agent_machine_id,
            f"rm -rf /tmp/phase2_*_{shlex.quote(effective_run_id)}* && sync",
            cwd=sandbox.workdir,
            timeout=60,
        )
        pre_agent_snapshot_id = _create_and_wait_snapshot(
            client,
            machine_id=agent_machine_id,
            snapshot_name=f"spike01-p2-pre-{effective_run_id}",
            description=f"Spike 01 Phase 2 pre-agent clean baseline ({effective_run_id})",
            run_id=effective_run_id,
            tracked_snapshot_ids=tracked_snapshots,
            timeout_seconds=config.snapshot_timeout,
        )

        # 3. Invoke Deep Agent inside agent_vm.
        _run_agent_bugfix_turn(sandbox, config)

        # 4. Extract diff + static Hard Gates outside the agent.
        (
            post_agent_head_sha,
            changed_entries,
            patch_bytes,
            post_config_hashes,
            idx_info,
        ) = _extract_post_agent_artifacts(
            client,
            agent_machine_id,
            workdir=sandbox.workdir,
            base_commit_sha=base_commit_sha,
            baseline_protected_paths=protected_configs,
            run_id=effective_run_id,
        )
        reject_reasons, review_reasons, new_test_files, *_ = evaluate_static_gates(
            existing_test_files=existing_test_files,
            changed_entries=changed_entries,
            patch_bytes=patch_bytes,
            baseline_config_hashes=baseline_config_hashes,
            post_agent_config_hashes=post_config_hashes,
            allow_config_changes=config.allow_config_changes,
        )
        print(
            f"[EXTRACT] changed={len(changed_entries)}, new_test_files={list(new_test_files)}, "
            f"static_rejects={len(reject_reasons)}, static_reviews={len(review_reasons)}"
        )

        red_status: dict[str, TestOutcome] | None = None
        red_xml: bytes | None = None
        red_exit: int | None = None
        post_status: dict[str, TestOutcome] | None = None
        post_xml: bytes | None = None
        post_exit: int | None = None

        if not reject_reasons:
            tests_only_patch = b""
            if new_test_files and not config.tdd_exempt:
                tests_only_patch = _extract_tests_only_patch(
                    client,
                    agent_machine_id,
                    workdir=sandbox.workdir,
                    base_commit_sha=base_commit_sha,
                    idx_file=idx_info["idx_file"],
                    new_test_files=new_test_files,
                    run_id=effective_run_id,
                )
            run_red = bool(new_test_files and not config.tdd_exempt and tests_only_patch)

            if config.verify_mode == "fresh-vm":
                # Delete agent VM first so Red/Green verify VMs stay within 1 concurrent VM.
                _release_created_vm_now(
                    client,
                    machine_id=agent_machine_id,
                    run_id=effective_run_id,
                    keep_vm=config.keep_vm,
                    tracked_machines=tracked_machines,
                    machine_ops=machine_ops,
                    pre_retained_machines=pre_retained_machines,
                )

            if run_red:
                verify_red_machine_id, red_exit, red_xml, red_status = _run_isolated_verification_stage(
                    client,
                    stage_tag="red",
                    verify_mode=config.verify_mode,
                    agent_machine_id=agent_machine_id,
                    pre_agent_snapshot_id=pre_agent_snapshot_id,
                    workdir=config.workdir,
                    default_timeout=config.default_timeout,
                    base_commit_sha=base_commit_sha,
                    clean_python=clean_python,
                    patch_bytes=tests_only_patch,
                    run_id=effective_run_id,
                    keep_vm=config.keep_vm,
                    tracked_machines=tracked_machines,
                    machine_ops=machine_ops,
                    pre_retained_machines=pre_retained_machines,
                )

            verify_machine_id, post_exit, post_xml, post_status = _run_isolated_verification_stage(
                client,
                stage_tag="green",
                verify_mode=config.verify_mode,
                agent_machine_id=agent_machine_id,
                pre_agent_snapshot_id=pre_agent_snapshot_id,
                workdir=config.workdir,
                default_timeout=config.default_timeout,
                base_commit_sha=base_commit_sha,
                clean_python=clean_python,
                patch_bytes=patch_bytes,
                run_id=effective_run_id,
                keep_vm=config.keep_vm,
                tracked_machines=tracked_machines,
                machine_ops=machine_ops,
                pre_retained_machines=pre_retained_machines,
            )

        tdd_evidence = (
            _format_tdd_exempt_evidence(changed_entries=changed_entries, post_status=post_status)
            if config.tdd_exempt and post_status
            else None
        )
        verification = evaluate_phase2_verification(
            existing_test_files=existing_test_files,
            changed_entries=changed_entries,
            patch_bytes=patch_bytes,
            baseline_status=baseline_status,
            post_status=post_status,
            red_status=red_status,
            red_exit_code=red_exit,
            post_exit_code=post_exit,
            baseline_config_hashes=baseline_config_hashes,
            post_agent_config_hashes=post_config_hashes,
            allow_config_changes=config.allow_config_changes,
            tdd_exempt=config.tdd_exempt,
            tdd_exempt_evidence=tdd_evidence,
        )
        summary = Phase2RunSummary(
            run_id=effective_run_id,
            agent_machine_id=agent_machine_id,
            verify_machine_id=verify_machine_id,
            pre_agent_snapshot_id=pre_agent_snapshot_id,
            base_commit_sha=base_commit_sha,
            post_agent_head_sha=post_agent_head_sha,
            verification=verification,
            patch_bytes=patch_bytes,
            verify_red_machine_id=verify_red_machine_id,
        )
        if config.artifact_dir is not None:
            _write_artifacts(
                config.artifact_dir,
                summary=summary,
                baseline_xml=baseline_xml,
                red_xml=red_xml,
                post_xml=post_xml,
            )

        print(
            f"[PHASE2] Verdict={verification.verdict} "
            f"baseline={verification.baseline_counts.get('passed', 0)}/{verification.baseline_counts.get('total', 0)} "
            f"post={verification.post_counts.get('passed', 0)}/{verification.post_counts.get('total', 0)} "
            f"red_to_green={list(verification.red_to_green_ids)}"
        )
        for reason in verification.reasons:
            print(f"[PHASE2] Reason: {reason}")
        return summary
    except BaseException as exc:
        primary_exc = exc
        raise
    finally:
        cleanup_err: CleanupIncompleteError | None = None
        retained_machines: list[str] = list(pre_retained_machines)
        retained_snapshots: list[str] = []
        errors: list[str] = []
        try:
            # Always delete VMs before deleting snapshots built/used by them.
            for mid in tracked_machines:
                if config.keep_vm:
                    print(
                        f"[CLEANUP] Keeping created VM (--keep-vm): "
                        f"machine_id={mid!r}, operation_id={machine_ops.get(mid)!r}"
                    )
                else:
                    err = delete_vm_strict(
                        client,
                        machine_id=mid,
                        operation_id=machine_ops.get(mid),
                        run_id=effective_run_id,
                    )
                    if err is not None:
                        retained_machines.append(mid)
                        errors.append(str(err))

            for sid in tracked_snapshots:
                if config.keep_vm:
                    print(f"[CLEANUP] Keeping snapshot (--keep-vm): snapshot_id={sid!r}")
                else:
                    err = delete_snapshot_strict(client, snapshot_id=sid, run_id=effective_run_id)
                    if err is not None:
                        retained_snapshots.append(sid)
                        errors.append(str(err))

            if errors:
                cleanup_err = CleanupIncompleteError(
                    " ; ".join(errors),
                    machine_id=retained_machines[0] if retained_machines else "",
                    run_id=effective_run_id,
                    retained_machine_ids=retained_machines,
                    retained_snapshot_ids=retained_snapshots,
                )
        finally:
            client.close()

        if cleanup_err is not None and (primary_exc is None or isinstance(primary_exc, CleanupIncompleteError)):
            raise cleanup_err


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = load_and_validate_phase2_config(argv)
    except SpikeConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2

    try:
        summary = run_phase2_spike(config)
    except SpikeConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2
    except CleanupIncompleteError as exc:
        print(
            f"[CLEANUP INCOMPLETE] {exc} "
            f"(retained_machines={exc.retained_machine_ids!r}, retained_snapshots={exc.retained_snapshot_ids!r})",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"[PHASE2 FAILED] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if summary.verification.verdict == "PASS":
        return 0
    if summary.verification.verdict == "NEEDS_HUMAN_REVIEW":
        return 4
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
