"""Live InstaBox VM smoke runner for Spike 01 (`InstaBoxSandbox` + `deepagents`).

Scope of this runner (Phase 1 smoke check):
- Validates configuration and credentials from environment variables BEFORE creating a VM.
- Exercises VM create/reconnect, `InstaBoxSandbox` (`execute`, timeout, binary/text
  `upload_files`/`download_files`, `BaseSandbox` file methods), and diagnostic
  server error-shape probes (`files.read_bytes` on missing path / directory).
- Optionally (`--with-agent`) runs a single `create_deep_agent` turn that writes a
  per-run unique file and verifies exact byte equality.
- Does NOT perform pre-agent snapshots, repo pytest baseline capture, or independent
  post-agent patch verification (those belong to the Phase 2 verification harness).

Environment variables:
    INSTABOX_API_KEY       Required API key for InstaBox.
    INSTABOX_API_URL       Optional API base URL (defaults to SDK default).
    INSTABOX_MACHINE_ID    Optional existing machine ID to reconnect to (not auto-deleted).
    INSTABOX_TEMPLATE      Optional template ID for new VMs.
    INSTABOX_SNAPSHOT_ID   Optional snapshot ID for new VMs.
    INSTABOX_WORKDIR       Optional POSIX workdir (default: /data/workspace/spike01).
    INSTABOX_EXEC_TIMEOUT  Optional default exec timeout in seconds (1..600, default: 120).
    NINE_ROUTER_MODEL      Default model ID when `--with-agent` is set.
    NINE_ROUTER_ROLE_MODELS JSON role -> model ID map (BA, Dev, Reviewer, Tester).
    DEEPAGENTS_MODEL       Legacy fallback for NINE_ROUTER_MODEL.
    NINE_ROUTER_BASE_URL   9Router OpenAI-compatible URL (default: http://localhost:20128/v1).
    NINE_ROUTER_API_KEY    9Router key (falls back to OPENAI_API_KEY).
    OPENAI_BASE_URL        Fallback URL for the 9Router model route.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import secrets
import shlex
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from instabox import APIError, Client

# Allow running both as `python spikes/01_instabox_openswe/run_live_vm_spike.py`
# and via pytest importing from repo root.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from instabox_sandbox import (  # noqa: E402
    DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS,
    InstaBoxSandbox,
    SandboxInitializationError,
)
from model_routing import ModelTarget, select_model_targets  # noqa: E402
from spike_common import (  # noqa: E402
    CREATE_WAIT_TIMEOUT_SECONDS,
    DEFAULT_SPIKE_WORKDIR,
    PROVIDER_REQUIREMENTS as _PROVIDER_REQUIREMENTS,
    CleanupIncompleteError,
    PreflightStatus,
    SpikeConfigError,
    build_router_model as _build_router_model,
    create_vm_with_early_tracking,
    delete_vm_strict,
    infer_provider as _infer_provider,
    opt_str as _opt_str,
    preflight_instabox_api,
    recover_machine_after_create_error as _recover_machine_after_create_error,
    resolve_chat_model,
    validate_instabox_base_env,
    validate_model_targets_config,
)


@dataclass(frozen=True)
class SpikeConfig:
    """Validated configuration for running the live InstaBox spike."""

    api_key: str = field(repr=False)
    base_url: str | None
    machine_id: str | None
    name: str | None
    template: str | None
    snapshot_id: str | None
    workdir: str
    default_timeout: int
    keep_vm: bool
    with_agent: bool
    model: str | None
    preflight_only: bool
    skip_api_preflight: bool
    router_base_url: str | None = None
    router_api_key: str | None = field(default=None, repr=False)
    model_targets: tuple[ModelTarget, ...] = ()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Spike 01 live VM verification for InstaBoxSandbox + Deep Agents.",
    )
    parser.add_argument(
        "--machine-id",
        default=None,
        help="Existing InstaBox machine ID to reconnect to (default: env INSTABOX_MACHINE_ID or create new).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Name for newly created VM (default: auto-generated spike01-<timestamp>-<run_id>).",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Template ID for newly created VM (default: env INSTABOX_TEMPLATE or SDK default).",
    )
    parser.add_argument(
        "--snapshot-id",
        default=None,
        help="Snapshot ID for newly created VM (default: env INSTABOX_SNAPSHOT_ID).",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help=f"Absolute POSIX working directory in the VM (default: {DEFAULT_SPIKE_WORKDIR}).",
    )
    parser.add_argument(
        "--timeout",
        default=None,
        help=f"Default exec timeout in seconds, 1..600 (default: {DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--keep-vm",
        action="store_true",
        help="Do not delete a newly created VM after the spike finishes.",
    )
    parser.add_argument(
        "--with-agent",
        action="store_true",
        help="Run an independent Deep Agents file-write smoke for each selected model/role.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="One model ID (9Router by default); env NINE_ROUTER_MODEL / DEEPAGENTS_MODEL.",
    )
    parser.add_argument("--models", nargs="+", help="Smoke-test several model IDs in order through 9Router.")
    parser.add_argument("--roles", nargs="+", help="Select roles from env NINE_ROUTER_ROLE_MODELS (JSON role -> model ID).")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate environment/config (and API auth unless --skip-api-preflight), then exit without creating a VM.",
    )
    parser.add_argument(
        "--skip-api-preflight",
        action="store_true",
        help="Skip pre-create `templates.list()` auth/template check.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def load_and_validate_config(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> SpikeConfig:
    """Load config from CLI flags + `env` and fail fast on any missing/invalid settings."""
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
        default_workdir=DEFAULT_SPIKE_WORKDIR,
    )

    router_base_url: str | None = None
    router_api_key: str | None = None
    model_targets: tuple[ModelTarget, ...] = ()
    if not args.with_agent and any(value is not None for value in (args.model, args.models, args.roles)):
        errors.append("--model, --models and --roles require --with-agent.")
    if args.with_agent:
        try:
            model_targets = select_model_targets(
                model=args.model, models=args.models, roles=args.roles, env=environ
            )
        except ValueError as exc:
            errors.append(str(exc))
        router_base_url, router_api_key, model_errors = validate_model_targets_config(
            model_targets,
            environ,
            find_spec=importlib.util.find_spec,
        )
        errors.extend(model_errors)

    if errors:
        bullet_list = "\n".join(f"  - {item}" for item in errors)
        raise SpikeConfigError(f"Configuration check failed before creating VM:\n{bullet_list}")

    return SpikeConfig(
        api_key=base_settings.api_key,
        base_url=base_settings.base_url,
        machine_id=base_settings.machine_id,
        name=base_settings.name,
        template=base_settings.template,
        snapshot_id=base_settings.snapshot_id,
        workdir=base_settings.workdir,
        default_timeout=base_settings.default_timeout,
        keep_vm=bool(args.keep_vm),
        with_agent=bool(args.with_agent),
        model=model_targets[0].model if model_targets else None,
        preflight_only=bool(args.preflight_only),
        skip_api_preflight=bool(args.skip_api_preflight),
        router_base_url=router_base_url,
        router_api_key=router_api_key,
        model_targets=model_targets,
    )


def _preflight_api(client: Client, config: SpikeConfig) -> PreflightStatus:
    """Verify API credentials and optional template/machine existence before creating a VM."""
    return preflight_instabox_api(
        client,
        machine_id=config.machine_id,
        template=config.template,
        skip_api_preflight=config.skip_api_preflight,
    )


def _run_sandbox_smoke(sandbox: InstaBoxSandbox, client: Client) -> None:
    """Exercise core `InstaBoxSandbox` + `BaseSandbox` operations against a live VM."""
    # 1. Execute command inside workdir
    res = sandbox.execute("pwd -L && uname -srm")
    print(f"[STEP 1] execute(pwd -L): exit_code={res.exit_code}, output={res.output.strip()!r}")
    if res.exit_code != 0 or sandbox.workdir not in res.output:
        raise AssertionError(f"Expected exit_code=0 and workdir {sandbox.workdir!r}, got {res!r}")

    # 2. Timeout enforcement
    timeout_res = sandbox.execute("sleep 3", timeout=1)
    print(f"[STEP 2] execute(timeout=1): exit_code={timeout_res.exit_code}, output={timeout_res.output.strip()!r}")
    if timeout_res.exit_code != 124 or "timed out" not in timeout_res.output:
        raise AssertionError(f"Expected timeout exit_code=124, got {timeout_res!r}")

    # 3. Binary + text upload/download round-trip
    binary_payload = b"\x89PNG\r\n\x1a\n\x00\xff\xfe\xfd" + bytes(range(64))
    text_payload = b"hello instabox spike\nline2\n"
    up_res = sandbox.upload_files(
        [
            ("spike_hello.txt", text_payload),
            ("assets/pixel.bin", binary_payload),
        ]
    )
    if any(r.error is not None for r in up_res):
        raise AssertionError(f"upload_files failed: {up_res!r}")

    down_res = sandbox.download_files(["spike_hello.txt", "assets/pixel.bin"])
    if [r.error for r in down_res] != [None, None]:
        raise AssertionError(f"download_files failed: {down_res!r}")
    if down_res[0].content != text_payload or down_res[1].content != binary_payload:
        raise AssertionError("Downloaded file bytes did not match uploaded payload!")
    print(f"[STEP 3] Binary + text round-trip OK (client.files._routes={client.files._routes!r}).")

    # 4. BaseSandbox high-level methods (write / read / edit / ls / glob / grep / delete)
    note_path = f"{sandbox.workdir}/notes/demo.txt"
    w = sandbox.write(note_path, "alpha\nbeta\n")
    if w.error:
        raise AssertionError(f"sandbox.write failed: {w!r}")

    e = sandbox.edit(note_path, "beta", "beta-updated")
    if e.error or e.occurrences != 1:
        raise AssertionError(f"sandbox.edit failed: {e!r}")

    r = sandbox.read(note_path)
    if r.error or r.file_data is None or "beta-updated" not in r.file_data["content"]:
        raise AssertionError(f"sandbox.read failed: {r!r}")

    ls_res = sandbox.ls(sandbox.workdir)
    if ls_res.error or not ls_res.entries:
        raise AssertionError(f"sandbox.ls failed: {ls_res!r}")

    glob_res = sandbox.glob("**/*.txt", path=sandbox.workdir)
    if glob_res.error or not any(m["path"].endswith("demo.txt") for m in (glob_res.matches or [])):
        raise AssertionError(f"sandbox.glob failed: {glob_res!r}")

    grep_res = sandbox.grep("beta-updated", path=sandbox.workdir)
    if grep_res.error or not grep_res.matches:
        raise AssertionError(f"sandbox.grep failed: {grep_res!r}")

    del_res = sandbox.delete(note_path)
    if del_res.error:
        raise AssertionError(f"sandbox.delete failed: {del_res!r}")
    print("[STEP 4] BaseSandbox write/edit/read/ls/glob/grep/delete OK.")

    # 5. Live server error-shape probes (diagnostic only; does not fail spike)
    for label, target_path in (
        ("missing_file", f"{sandbox.workdir}/__nonexistent_probe__.txt"),
        ("directory_read", sandbox.workdir),
    ):
        try:
            client.files.read_bytes(sandbox.id, target_path, timeout=30)
            print(f"[PROBE:{label}] unexpectedly succeeded for {target_path!r}")
        except APIError as exc:
            print(
                f"[PROBE:{label}] exc={type(exc).__name__} "
                f"code={exc.code!r} status={exc.status!r} message={exc.message!r}"
            )


def _run_agent_smoke(
    sandbox: InstaBoxSandbox,
    model_spec: str,
    *,
    run_id: str,
    router_base_url: str | None = None,
    router_api_key: str | None = None,
) -> None:
    """Run one minimal `create_deep_agent` turn using a per-run unique file and exact byte check."""
    from deepagents import create_deep_agent  # deferred so non-agent runs stay fast

    target_rel = f"agent_smoke_{run_id}.txt"
    target_abs = f"{sandbox.workdir}/{target_rel}"
    expected_bytes = f"SPIKE_AGENT_OK_{run_id}\n".encode("utf-8")

    # Verify target does not already exist (uses execute so it does not rely on HTTP 404 semantics).
    quoted_target = shlex.quote(target_abs)
    pre_check = sandbox.execute(f"test ! -e {quoted_target} && test ! -L {quoted_target}")
    if pre_check.exit_code != 0:
        raise AssertionError(
            f"Unique agent smoke file {target_abs!r} unexpectedly exists before agent turn"
        )

    model = resolve_chat_model(
        model_spec,
        router_base_url=router_base_url,
        router_api_key=router_api_key,
    )
    agent = create_deep_agent(
        model=model,
        backend=sandbox,
        system_prompt=(
            f"You are working inside a Linux sandbox with working directory {sandbox.workdir}. "
            "Use the available file/execute tools directly and concisely."
        ),
    )
    prompt = (
        f"Create the file `{target_abs}` containing the exact single line "
        f"`SPIKE_AGENT_OK_{run_id}` followed by a single trailing newline (`\\n`) and nothing else. "
        "Then verify the file exists."
    )
    print(f"[STEP 6] Invoking Deep Agent with model={model_spec!r}, target={target_abs!r}...")
    agent.invoke({"messages": [{"role": "user", "content": prompt}]})

    check = sandbox.download_files([target_rel])
    if check[0].error is not None or check[0].content is None:
        raise AssertionError(f"Agent smoke file missing at {target_abs!r}: {check[0]!r}")
    if check[0].content != expected_bytes:
        raise AssertionError(
            f"Exact byte mismatch in {target_abs!r}: expected {expected_bytes!r}, got {check[0].content!r}"
        )
    print(
        f"[STEP 6] Deep Agent smoke OK ({target_abs} verified: exact {len(expected_bytes)} bytes)."
    )


def run_spike(
    config: SpikeConfig,
    *,
    client_factory: Callable[..., Client] | None = None,
    run_id: str | None = None,
) -> str:
    """Execute the live VM spike with early machine/operation tracking and strict cleanup errors."""
    effective_run_id = run_id or secrets.token_hex(6)
    factory = Client if client_factory is None else client_factory
    client = factory(api_key=config.api_key, base_url=config.base_url)
    tracked_machines: list[str] = []
    create_operation_id: str | None = None
    primary_exc: BaseException | None = None

    try:
        preflight_status = _preflight_api(client, config)
        if config.preflight_only:
            if preflight_status == "skipped":
                print(
                    "[PREFLIGHT] Local configuration check passed ONLY (--preflight-only + --skip-api-preflight); "
                    "API authentication was NOT verified; no VM created."
                )
            elif preflight_status == "insufficient_scope":
                print(
                    "[PREFLIGHT] Local configuration OK; API key authenticated but lacked templates scope; "
                    "no VM created."
                )
            else:
                print("[PREFLIGHT] Configuration and API authentication verified (--preflight-only); no VM created.")
            return ""

        if config.machine_id is not None:
            sandbox = InstaBoxSandbox.create_or_reconnect(
                client,
                machine_id=config.machine_id,
                workdir=config.workdir,
                default_timeout=config.default_timeout,
                ensure_workdir=True,
            )
            action = "Reconnected"
        else:
            create_vm_name = config.name or f"spike01-{int(time.time())}-{effective_run_id}"
            metadata = {"spike": "01", "spike_run_id": effective_run_id}
            idempotency_key = f"ik_spike01_{effective_run_id}"

            created_machine_id, create_operation_id = create_vm_with_early_tracking(
                client,
                vm_name=create_vm_name,
                template=config.template,
                snapshot_id=config.snapshot_id,
                metadata=metadata,
                idempotency_key=idempotency_key,
                run_id=effective_run_id,
                has_explicit_name=config.name is not None,
                tracked_machine_ids=tracked_machines,
                wait_timeout=CREATE_WAIT_TIMEOUT_SECONDS,
            )

            try:
                sandbox = InstaBoxSandbox.create_or_reconnect(
                    client,
                    machine_id=created_machine_id,
                    workdir=config.workdir,
                    default_timeout=config.default_timeout,
                    ensure_workdir=True,
                )
            except SandboxInitializationError as exc:
                print(
                    f"[VM] Initialization failed on machine_id={exc.machine_id!r} "
                    f"(operation_id={create_operation_id!r}): {exc}",
                    file=sys.stderr,
                )
                raise
            action = "Created"

        print(f"[VM] {action} machine_id={sandbox.id!r}, workdir={sandbox.workdir!r}")

        _run_sandbox_smoke(sandbox, client)
        if config.with_agent:
            assert config.model is not None
            targets = config.model_targets or (ModelTarget("default", config.model),)
            failed: list[str] = []
            for index, target in enumerate(targets):
                agent_options = {}
                if _infer_provider(target.model) == "9router":
                    agent_options = {
                        "router_base_url": config.router_base_url,
                        "router_api_key": config.router_api_key,
                    }
                smoke_run_id = effective_run_id if len(targets) == 1 else f"{effective_run_id}-{index + 1}"
                print(f"[MODEL] Testing {target.label}: {target.model}")
                try:
                    _run_agent_smoke(sandbox, target.model, run_id=smoke_run_id, **agent_options)
                except Exception as exc:
                    if len(targets) == 1:
                        raise
                    failed.append(target.label)
                    print(f"[MODEL] FAIL {target.label}: {type(exc).__name__}", file=sys.stderr)
                else:
                    print(f"[MODEL] PASS {target.label}: {target.model}")
            if failed:
                raise RuntimeError(f"Model smoke failed for: {', '.join(failed)}. All selected models were attempted.")

        scope_note = (
            "sandbox smoke + selected model file-write turns (no pre-agent snapshot or independent repo verification)"
            if config.with_agent
            else "sandbox smoke only (no LLM, no pre-agent snapshot, no independent repo verification)"
        )
        print(f"[DONE] Spike 01 Phase 1 passed on machine_id={sandbox.id!r}: {scope_note}.")
        return sandbox.id
    except BaseException as exc:
        primary_exc = exc
        raise
    finally:
        cleanup_err: CleanupIncompleteError | None = None
        try:
            for created_id in tracked_machines:
                if config.keep_vm:
                    print(
                        f"[CLEANUP] Keeping created VM (--keep-vm): "
                        f"machine_id={created_id!r}, operation_id={create_operation_id!r}"
                    )
                else:
                    err = delete_vm_strict(
                        client,
                        machine_id=created_id,
                        operation_id=create_operation_id,
                        run_id=effective_run_id,
                    )
                    if err is not None and cleanup_err is None:
                        cleanup_err = err
        finally:
            client.close()

        if cleanup_err is not None and primary_exc is None:
            raise cleanup_err


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = load_and_validate_config(argv)
    except SpikeConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2

    try:
        run_spike(config)
    except SpikeConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2
    except CleanupIncompleteError as exc:
        print(
            f"[CLEANUP INCOMPLETE] {exc} (retained machine_id={exc.machine_id!r})",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"[SPIKE FAILED] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
