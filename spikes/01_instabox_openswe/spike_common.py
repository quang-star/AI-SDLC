"""Shared configuration, InstaBox VM/snapshot lifecycle, and 9Router helpers for Spike 01."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlparse

from instabox import (
    AuthenticationError,
    BadRequestError,
    Client,
    NotFoundError,
    PermissionDeniedError,
)
from langchain_core.language_models import BaseChatModel

from instabox_sandbox import (
    DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS,
    DEFAULT_WORKDIR,
    _validate_exec_timeout,
    _validate_machine_id,
    _validate_workdir,
)
from model_routing import ModelTarget

DEFAULT_SPIKE_WORKDIR: Final[str] = f"{DEFAULT_WORKDIR}/spike01"
CREATE_WAIT_TIMEOUT_SECONDS: Final[float] = 600.0
DEFAULT_ROUTER_BASE_URL: Final[str] = "http://localhost:20128/v1"

# Provider -> (python package module, tuple of accepted API key env vars)
PROVIDER_REQUIREMENTS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "anthropic": ("langchain_anthropic", ("ANTHROPIC_API_KEY",)),
    "google_genai": ("langchain_google_genai", ("GOOGLE_API_KEY", "GEMINI_API_KEY")),
    "openai": ("langchain_openai", ("OPENAI_API_KEY",)),
}

PreflightStatus = Literal["verified", "insufficient_scope", "skipped"]


class SpikeConfigError(ValueError):
    """Raised when required environment variables or CLI flags are missing/invalid."""


class CleanupIncompleteError(RuntimeError):
    """Raised when deleting a VM or snapshot created during a spike run fails."""

    def __init__(
        self,
        message: str,
        *,
        machine_id: str,
        operation_id: str | None = None,
        run_id: str | None = None,
        retained_machine_ids: Sequence[str] = (),
        retained_snapshot_ids: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.machine_id = machine_id
        self.operation_id = operation_id
        self.run_id = run_id
        machines = list(retained_machine_ids)
        if machine_id and machine_id not in machines:
            machines.insert(0, machine_id)
        self.retained_machine_ids: tuple[str, ...] = tuple(machines)
        self.retained_snapshot_ids: tuple[str, ...] = tuple(retained_snapshot_ids)


@dataclass(frozen=True)
class InstaboxBaseSettings:
    """Validated InstaBox connection and sandbox settings."""

    api_key: str
    base_url: str | None
    machine_id: str | None
    name: str | None
    template: str | None
    snapshot_id: str | None
    workdir: str
    default_timeout: int


def opt_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def infer_provider(model_spec: str) -> str | None:
    """Infer LangChain chat model provider from `provider:model` or bare model name."""
    if ":" in model_spec:
        prefix = model_spec.split(":", 1)[0].strip().lower()
        return prefix or None
    lower = model_spec.strip().lower()
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("gemini"):
        return "google_genai"
    if lower.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return None


def validate_instabox_base_env(
    *,
    environ: Mapping[str, str],
    machine_id_arg: str | None,
    name_arg: str | None,
    template_arg: str | None,
    snapshot_id_arg: str | None,
    workdir_arg: str | None,
    timeout_arg: str | None,
    default_workdir: str = DEFAULT_SPIKE_WORKDIR,
) -> tuple[InstaboxBaseSettings, list[str]]:
    """Validate InstaBox credentials and sandbox parameters without calling any API."""
    errors: list[str] = []

    api_key = environ.get("INSTABOX_API_KEY", "").strip()
    if not api_key:
        errors.append("Missing required environment variable `INSTABOX_API_KEY`.")

    raw_url = environ.get("INSTABOX_API_URL")
    base_url = opt_str(raw_url)
    if raw_url is not None and base_url is None:
        errors.append("`INSTABOX_API_URL` is set but empty/whitespace.")
    elif base_url is not None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append(
                f"Invalid `INSTABOX_API_URL`: expected http(s) URL with host, got {base_url!r}."
            )

    raw_machine_id = machine_id_arg if machine_id_arg is not None else environ.get("INSTABOX_MACHINE_ID")
    machine_id: str | None = None
    if raw_machine_id is not None:
        try:
            machine_id = _validate_machine_id(raw_machine_id)
        except (TypeError, ValueError) as exc:
            errors.append(f"Invalid machine_id: {exc}")

    raw_template = template_arg if template_arg is not None else environ.get("INSTABOX_TEMPLATE")
    template: str | None = None
    if raw_template is not None:
        template = raw_template.strip()
        if not template:
            errors.append("`template` / `INSTABOX_TEMPLATE` cannot be empty/whitespace.")

    raw_snapshot = snapshot_id_arg if snapshot_id_arg is not None else environ.get("INSTABOX_SNAPSHOT_ID")
    snapshot_id: str | None = None
    if raw_snapshot is not None:
        snapshot_id = raw_snapshot.strip()
        if not snapshot_id:
            errors.append("`snapshot_id` / `INSTABOX_SNAPSHOT_ID` cannot be empty/whitespace.")

    explicit_name: str | None = None
    if name_arg is not None:
        explicit_name = name_arg.strip()
        if not explicit_name:
            errors.append("`--name` cannot be empty/whitespace.")

    if machine_id is not None:
        if any(v is not None for v in (template, snapshot_id, explicit_name)):
            errors.append(
                "Cannot combine `--machine-id` / `INSTABOX_MACHINE_ID` with `--name`, `--template`, or `--snapshot-id`."
            )
    elif template is not None and snapshot_id is not None:
        errors.append("Cannot specify both `template` and `snapshot_id`.")

    raw_workdir = (
        workdir_arg
        if workdir_arg is not None
        else (environ.get("INSTABOX_WORKDIR") or default_workdir)
    )
    workdir = default_workdir
    try:
        workdir = _validate_workdir(raw_workdir)
    except (TypeError, ValueError) as exc:
        errors.append(f"Invalid workdir: {exc}")

    raw_timeout = timeout_arg if timeout_arg is not None else environ.get("INSTABOX_EXEC_TIMEOUT")
    default_timeout = DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS
    if raw_timeout is not None:
        try:
            parsed_timeout = int(str(raw_timeout).strip())
        except ValueError:
            errors.append(f"Invalid timeout {raw_timeout!r}: must be an integer (1..600).")
        else:
            try:
                default_timeout = _validate_exec_timeout(parsed_timeout, param_name="timeout")
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))

    return (
        InstaboxBaseSettings(
            api_key=api_key,
            base_url=base_url,
            machine_id=machine_id,
            name=explicit_name,
            template=template,
            snapshot_id=snapshot_id,
            workdir=workdir,
            default_timeout=default_timeout,
        ),
        errors,
    )


def validate_model_targets_config(
    targets: Sequence[ModelTarget],
    environ: Mapping[str, str],
    *,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> tuple[str | None, str | None, list[str]]:
    """Validate 9Router or direct provider credentials/packages for selected `ModelTarget`s."""
    errors: list[str] = []
    router_base_url: str | None = None
    router_api_key: str | None = None

    for target in targets:
        model = target.model
        provider = infer_provider(model)
        if provider == "9router":
            if not model.split(":", 1)[1].strip():
                errors.append("9Router requires a model ID: `9router:<model-id>`.")
            router_base_url = (
                environ.get("NINE_ROUTER_BASE_URL")
                or environ.get("OPENAI_BASE_URL")
                or DEFAULT_ROUTER_BASE_URL
            ).strip().rstrip("/")
            try:
                router_url = urlparse(router_base_url)
                valid_url = (
                    router_url.scheme in ("http", "https")
                    and bool(router_url.hostname)
                    and router_url.username is None
                    and router_url.password is None
                    and not router_url.query
                    and not router_url.fragment
                )
                router_url.port  # Validate the port without logging the URL.
            except ValueError:
                valid_url = False
            if not valid_url:
                errors.append(
                    "NINE_ROUTER_BASE_URL / OPENAI_BASE_URL must be an http(s) URL without credentials, query or fragment."
                )
            router_api_key = (
                environ.get("NINE_ROUTER_API_KEY") or environ.get("OPENAI_API_KEY") or ""
            ).strip()
            if not router_api_key:
                errors.append(
                    "9Router requires NINE_ROUTER_API_KEY or OPENAI_API_KEY (router key, not upstream provider key)."
                )
            if find_spec("langchain_openai") is None:
                errors.append("9Router requires package `langchain-openai` in this Python environment.")
        elif provider in PROVIDER_REQUIREMENTS:
            pkg_name, key_names = PROVIDER_REQUIREMENTS[provider]
            if find_spec(pkg_name) is None:
                errors.append(
                    f"Model {model!r} requires package `{pkg_name}`, which is not installed in this Python environment."
                )
            if not any((environ.get(k) or "").strip() for k in key_names):
                joined = " or ".join(f"`{k}`" for k in key_names)
                errors.append(
                    f"`--with-agent` with model {model!r} requires environment variable {joined}."
                )

    return router_base_url, router_api_key, errors


def build_router_model(model_spec: str, base_url: str | None, api_key: str | None) -> BaseChatModel:
    """Build an explicit Chat Completions client for 9Router, bypassing provider inference."""
    from langchain_openai import ChatOpenAI

    if not base_url or not api_key:
        raise SpikeConfigError("9Router URL and key must be configured before running the agent.")
    return ChatOpenAI(
        model=model_spec.split(":", 1)[1].strip(),
        base_url=base_url,
        api_key=api_key,
        use_responses_api=False,
        timeout=120,
    )


def resolve_chat_model(
    model_spec: str,
    *,
    router_base_url: str | None = None,
    router_api_key: str | None = None,
) -> str | BaseChatModel:
    """Return a `ChatOpenAI` client for `9router:*` specs or pass direct provider specs through."""
    if infer_provider(model_spec) == "9router":
        return build_router_model(model_spec, router_base_url, router_api_key)
    return model_spec


def preflight_instabox_api(
    client: Client,
    *,
    machine_id: str | None,
    template: str | None,
    skip_api_preflight: bool,
) -> PreflightStatus:
    """Verify InstaBox API credentials and optional template/machine existence before creating a VM."""
    if machine_id is not None:
        machine = client.machines.get(machine_id)
        print(f"[PREFLIGHT] Existing machine found: id={machine.id}, state={machine.state}")
        return "verified"

    if skip_api_preflight:
        print(
            "[PREFLIGHT] Local configuration valid; skipping API check (--skip-api-preflight). "
            "API key and scopes were NOT verified."
        )
        return "skipped"

    try:
        templates = client.templates.list()
    except AuthenticationError:
        raise
    except PermissionDeniedError as exc:
        if (exc.code or "").strip().lower() == "missing_scope":
            print(
                "[PREFLIGHT] Warning: API key reached server but lacks templates read scope "
                "(`missing_scope`); template existence and machine scopes were NOT verified."
            )
            return "insufficient_scope"
        raise

    print(f"[PREFLIGHT] Auth OK ({len(templates)} templates visible).")
    if template is not None:
        target_id = template.split("@", 1)[0]
        known_ids = {t.id for t in templates}
        if target_id not in known_ids:
            raise SpikeConfigError(
                f"Template {template!r} not found in InstaBox templates: {sorted(known_ids)}"
            )
    return "verified"


def recover_machine_after_create_error(
    client: Client,
    *,
    exc: Exception,
    vm_name: str,
    run_id: str,
    has_explicit_name: bool,
) -> tuple[str | None, str | None]:
    """Look up a possibly-created machine after an ambiguous `create(wait=False)` failure."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        maybe_id = body.get("machineId")
        maybe_op = body.get("id") if body.get("machineId") else None
        if isinstance(maybe_id, str) and maybe_id.strip():
            return (
                maybe_id.strip(),
                maybe_op.strip() if isinstance(maybe_op, str) and maybe_op.strip() else None,
            )

    try:
        matches = client.machines.list(metadata={"spike_run_id": run_id})
    except Exception as list_exc:
        print(
            f"[VM] Lookup by metadata spike_run_id={run_id!r} failed: "
            f"{type(list_exc).__name__}: {list_exc}",
            file=sys.stderr,
        )
        matches = []

    for m in matches:
        mid = (getattr(m, "id", None) or "").strip()
        if mid:
            op_id = getattr(m, "operation_id", None)
            return mid, op_id.strip() if isinstance(op_id, str) and op_id.strip() else None

    try:
        machine = client.machines.get(vm_name)
    except NotFoundError:
        return None, None
    except Exception as get_exc:
        print(
            f"[VM] Lookup by name={vm_name!r} failed: {type(get_exc).__name__}: {get_exc}",
            file=sys.stderr,
        )
        return None, None

    meta = getattr(machine, "metadata", None) or {}
    mid = (getattr(machine, "id", None) or "").strip()
    if mid and (meta.get("spike_run_id") == run_id or not has_explicit_name):
        op_id = getattr(machine, "operation_id", None)
        return mid, op_id.strip() if isinstance(op_id, str) and op_id.strip() else None
    return None, None


def create_vm_with_early_tracking(
    client: Client,
    *,
    vm_name: str,
    template: str | None,
    snapshot_id: str | None,
    metadata: Mapping[str, str],
    idempotency_key: str,
    run_id: str,
    has_explicit_name: bool,
    tracked_machine_ids: list[str],
    wait_timeout: float = CREATE_WAIT_TIMEOUT_SECONDS,
) -> tuple[str, str | None]:
    """Call `machines.create(..., wait=False)`, record machine ID immediately, and wait for boot."""
    try:
        machine = client.machines.create(
            name=vm_name,
            template=template,
            snapshot_id=snapshot_id,
            metadata=metadata,
            wait=False,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        if not isinstance(
            exc,
            (BadRequestError, AuthenticationError, PermissionDeniedError, NotFoundError),
        ):
            recovered_id, recovered_op = recover_machine_after_create_error(
                client,
                exc=exc,
                vm_name=vm_name,
                run_id=run_id,
                has_explicit_name=has_explicit_name,
            )
            if recovered_id is not None:
                if recovered_id not in tracked_machine_ids:
                    tracked_machine_ids.append(recovered_id)
                print(
                    f"[VM] Recovered created machine_id={recovered_id!r} "
                    f"(operation_id={recovered_op!r}) after create error.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[CLEANUP] ERROR: Create failed ambiguously before machine_id was returned and "
                    f"lookup could not rule out an orphaned VM (name={vm_name!r}, "
                    f"spike_run_id={run_id!r}). Check InstaBox manually.",
                    file=sys.stderr,
                )
        raise

    raw_id = getattr(machine, "id", None)
    if not isinstance(raw_id, str) or not raw_id.strip():
        print(
            f"[CLEANUP] ERROR: Create returned machine without valid id (name={vm_name!r}, "
            f"spike_run_id={run_id!r}). Check InstaBox manually.",
            file=sys.stderr,
        )
        raise RuntimeError("InstaBox API returned a machine without a valid id")

    machine_id = raw_id.strip()
    if machine_id not in tracked_machine_ids:
        tracked_machine_ids.append(machine_id)

    raw_op_id = getattr(machine, "operation_id", None)
    operation_id = raw_op_id.strip() if isinstance(raw_op_id, str) and raw_op_id.strip() else None
    print(
        f"[VM] Create accepted: machine_id={machine_id!r}, "
        f"operation_id={operation_id!r}, spike_run_id={run_id!r}"
    )

    if operation_id is not None:
        client.operations.wait(operation_id, timeout=wait_timeout)

    return machine_id, operation_id


def delete_vm_strict(
    client: Client,
    *,
    machine_id: str,
    operation_id: str | None,
    run_id: str,
) -> CleanupIncompleteError | None:
    """Delete a created VM, returning `CleanupIncompleteError` instead of swallowing failures."""
    print(f"[CLEANUP] Deleting created VM machine_id={machine_id!r}...")
    try:
        client.machines.delete(
            machine_id,
            confirm=machine_id,
            wait=True,
            timeout=600.0,
        )
        print(f"[CLEANUP] Deleted machine_id={machine_id!r}.")
        return None
    except Exception as exc:
        msg = (
            f"Cleanup incomplete: failed to delete VM machine_id={machine_id!r} "
            f"(operation_id={operation_id!r}, spike_run_id={run_id!r}); "
            f"resource may still exist on InstaBox: {type(exc).__name__}: {exc}"
        )
        print(f"[CLEANUP] ERROR: {msg}", file=sys.stderr)
        err = CleanupIncompleteError(
            msg,
            machine_id=machine_id,
            operation_id=operation_id,
            run_id=run_id,
            retained_machine_ids=(machine_id,),
        )
        err.__cause__ = exc
        return err


def delete_snapshot_strict(
    client: Client,
    *,
    snapshot_id: str,
    run_id: str,
) -> CleanupIncompleteError | None:
    """Delete a created snapshot, returning `CleanupIncompleteError` on failure."""
    print(f"[CLEANUP] Deleting snapshot snapshot_id={snapshot_id!r}...")
    try:
        client.snapshots.delete(snapshot_id, confirm=snapshot_id)
        print(f"[CLEANUP] Deleted snapshot_id={snapshot_id!r}.")
        return None
    except Exception as exc:
        msg = (
            f"Cleanup incomplete: failed to delete snapshot snapshot_id={snapshot_id!r} "
            f"(spike_run_id={run_id!r}); resource may still exist on InstaBox: "
            f"{type(exc).__name__}: {exc}"
        )
        print(f"[CLEANUP] ERROR: {msg}", file=sys.stderr)
        err = CleanupIncompleteError(
            msg,
            machine_id="",
            run_id=run_id,
            retained_snapshot_ids=(snapshot_id,),
        )
        err.__cause__ = exc
        return err
