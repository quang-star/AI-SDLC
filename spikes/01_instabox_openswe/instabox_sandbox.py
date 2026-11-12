"""InstaBox Sandbox Adapter for Deep Agents (Spike 01 - Option 1A).

Implements `deepagents.backends.sandbox.BaseSandbox` on top of `instabox-sdk==0.5.2`.

Key design invariants:
1. Bounded wake-and-retry ONLY on `APIError` with `code == "machine_not_running"`.
   Never auto-retries on timeout (`APIConnectionError`, `WaitTimeoutError`),
   5xx (`ServerError`), or ambiguous failures where a command might have started.
2. Enforces InstaBox's `machines.exec` timeout ceiling (1..600 seconds) consistently
   for both `default_timeout` at construction time and per-call `execute(timeout=...)`.
3. Accurately formats `ExecResult` (`stdout`, `stderr`, `exit_code`, `timed_out`,
   `truncated`, `signal`). Does not assume `exit_code == 137` or `SIGKILL` is OOM
   without evidence, because `instabox.ExecResult` in SDK 0.5.2 does not expose
   `oom_killed`.
4. Separates explicit per-file recoverable errors (`file_not_found`, `permission_denied`,
   `is_directory`, `invalid_path`) from infrastructure/auth or ambiguous API errors
   (`AuthenticationError`, `PermissionDeniedError(code="permission_denied")`,
   `NotFoundError(code="not_found")` on HTTP routes, `APIConnectionError`, `ServerError`,
   `missing_scope`, `machine_not_found`), covering both direct HTTP file routes and
   the SDK's `exec`-based base64 fallback path.
5. Validates all configuration before creating/acquiring machines in
   `create_or_reconnect()`, and raises `SandboxInitializationError(machine_id=...)`
   if post-create initialization fails so callers never lose track of a created VM.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Final, TypeVar

import instabox
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    ExecuteResponse,
    FileDownloadResponse,
    FileOperationError,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from instabox import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    Client,
    ExecResult,
    OperationFailedError,
    RateLimitError,
    ServerError,
    WaitTimeoutError,
)

T = TypeVar("T")

MAX_INSTABOX_EXEC_TIMEOUT_SECONDS: Final[int] = 600
DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS: Final[int] = 120
DEFAULT_INSTABOX_FILE_TIMEOUT_SECONDS: Final[float] = 300.0
DEFAULT_INSTABOX_WAKE_TIMEOUT_SECONDS: Final[float] = 300.0
DEFAULT_WORKDIR: Final[str] = "/data/workspace"


class SandboxInitializationError(RuntimeError):
    """Raised when sandbox initialization fails after a machine ID was acquired or created.

    Carries `machine_id` and `created` so callers can clean up or inspect the VM.
    """

    def __init__(
        self,
        message: str,
        *,
        machine_id: str,
        created: bool,
    ) -> None:
        super().__init__(message)
        self.machine_id = machine_id
        self.created = created


def _validate_machine_id(machine_id: str) -> str:
    """Validate and normalize a machine ID."""
    if not isinstance(machine_id, str):
        raise TypeError(f"machine_id must be a str, got {type(machine_id).__name__}")
    stripped = machine_id.strip()
    if not stripped or "\x00" in stripped or "\n" in stripped or "\r" in stripped:
        raise ValueError("machine_id must be a non-empty single-line string")
    return stripped


def _validate_workdir(workdir: str) -> str:
    """Validate and normalize `workdir` as an absolute POSIX path."""
    if not isinstance(workdir, str):
        raise TypeError(f"workdir must be a str, got {type(workdir).__name__}")
    if (
        not workdir
        or not workdir.startswith("/")
        or "\x00" in workdir
        or "\n" in workdir
        or "\r" in workdir
    ):
        raise ValueError(f"workdir must be an absolute POSIX path, got {workdir!r}")
    return str(PurePosixPath(workdir))


def _validate_exec_timeout(timeout: int, *, param_name: str = "timeout") -> int:
    """Validate an exec timeout in whole seconds within InstaBox's 1..600s limit."""
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise TypeError(f"{param_name} must be an int, got {type(timeout).__name__}")
    if timeout <= 0:
        raise ValueError(f"{param_name} must be > 0 seconds, got {timeout}")
    if timeout > MAX_INSTABOX_EXEC_TIMEOUT_SECONDS:
        raise ValueError(
            f"{param_name}={timeout}s exceeds InstaBox machines.exec maximum of "
            f"{MAX_INSTABOX_EXEC_TIMEOUT_SECONDS}s"
        )
    return timeout


def _validate_positive_float_timeout(timeout: float, *, param_name: str) -> float:
    """Validate a positive finite timeout (seconds) for file/wake operations."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError(f"{param_name} must be an int or float, got {type(timeout).__name__}")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{param_name} must be a finite number > 0 seconds, got {timeout!r}")
    return value


def _validate_path(path: str) -> str | None:
    """Return an error literal if `path` is syntactically invalid, else `None`."""
    if not path or not path.strip():
        return INVALID_PATH
    if "\x00" in path or "\n" in path or "\r" in path:
        return INVALID_PATH
    return None


def _resolve_posix_path(workdir: str, path: str) -> str:
    """Resolve `path` against `workdir` as an absolute POSIX path inside the VM."""
    if path.startswith("/"):
        return str(PurePosixPath(path))
    return str(PurePosixPath(workdir) / PurePosixPath(path))


def _format_exec_output(res: ExecResult) -> str:
    """Combine `stdout`, `stderr`, and status annotations from `instabox.ExecResult`."""
    parts: list[str] = []
    if res.stdout:
        parts.append(res.stdout)
    if res.stderr:
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")
        parts.append(res.stderr)

    notes: list[str] = []
    if res.timed_out:
        notes.append(f"[instabox: command timed out (exit_code={res.exit_code})]")
    if res.signal:
        notes.append(
            f"[instabox: process terminated by signal {res.signal} "
            f"(exit_code={res.exit_code}); cause undetermined (could be external signal or kernel OOM kill)]"
        )
    elif res.exit_code == 137 and not res.timed_out:
        notes.append(
            "[instabox: process exited with code 137 (SIGKILL); "
            "cause undetermined (could be external kill or kernel OOM kill)]"
        )

    if notes:
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")
        parts.append("\n".join(notes))

    return "".join(parts)


def _classify_file_error(
    exc: APIError,
    *,
    is_download: bool,
) -> FileOperationError | str:
    """Classify an `instabox.APIError` into a `FileOperationError` or re-raise if
    infrastructure-level or ambiguous.

    Only unambiguous file-level error codes (and the SDK's guest-exec fallback
    `transfer_failed`) are converted to `FileOperationError` / per-file errors.
    Ambiguous API/resource codes (`permission_denied`, `not_found`, `bad_request`,
    unknown codes) are re-raised unchanged so API auth/machine errors are never masked.
    """
    if isinstance(
        exc,
        (
            AuthenticationError,
            ServerError,
            RateLimitError,
            OperationFailedError,
        ),
    ):
        raise exc

    code = (exc.code or "").strip().lower()

    # Unambiguous per-file error codes:
    if code in ("file_not_found", "enoent"):
        return FILE_NOT_FOUND

    if code in ("not_a_file", "is_directory", "eisdir"):
        return IS_DIRECTORY

    if code in ("eacces", "eperm", "file_permission_denied"):
        return PERMISSION_DENIED

    if code in ("invalid_path", "bad_path", "nametoolong"):
        return INVALID_PATH

    if code in ("file_too_large", "file_changed", "transfer_mismatch"):
        return f"{code}: {exc.message}" if exc.message else code

    # Exec fallback path (`_exec_size`, `_exec_chunks`, `_write_exec` in `instabox-sdk==0.5.2`):
    # Only raised client-side by SDK guest transfer scripts after `/exec` succeeded HTTP-wise.
    if code == "transfer_failed":
        msg = (exc.message or "").strip().lower()
        if "is a directory" in msg:
            return IS_DIRECTORY
        if (
            "permission denied" in msg
            or "operation not permitted" in msg
            or "read-only file system" in msg
        ):
            return PERMISSION_DENIED
        if "no such file or directory" in msg or "does not exist" in msg:
            return FILE_NOT_FOUND if is_download else INVALID_PATH
        if "invalid" in msg or "file name too long" in msg or "not a directory" in msg:
            return INVALID_PATH
        return f"transfer_failed: {exc.message}" if exc.message else "transfer_failed"

    # Ambiguous or infrastructure-level errors (`permission_denied`, `not_found`,
    # `bad_request`, `missing_scope`, `machine_not_found`, `machine_not_running`,
    # or unrecognized API codes) must propagate unchanged.
    raise exc


class InstaBoxSandbox(BaseSandbox):
    """`BaseSandbox` adapter backed by an InstaBox cloud Linux VM (`instabox-sdk==0.5.2`)."""

    def __init__(
        self,
        client: Client,
        machine_id: str,
        *,
        workdir: str = DEFAULT_WORKDIR,
        default_timeout: int = DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS,
        file_timeout: float = DEFAULT_INSTABOX_FILE_TIMEOUT_SECONDS,
        wake_timeout: float = DEFAULT_INSTABOX_WAKE_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._machine_id = _validate_machine_id(machine_id)
        self._workdir = _validate_workdir(workdir)
        self._default_timeout = _validate_exec_timeout(
            default_timeout, param_name="default_timeout"
        )
        self._file_timeout = _validate_positive_float_timeout(
            file_timeout, param_name="file_timeout"
        )
        self._wake_timeout = _validate_positive_float_timeout(
            wake_timeout, param_name="wake_timeout"
        )

    @property
    def id(self) -> str:
        """Unique identifier for the InstaBox machine."""
        return self._machine_id

    @property
    def workdir(self) -> str:
        """Default working directory inside the InstaBox machine."""
        return self._workdir

    @classmethod
    def create_or_reconnect(
        cls,
        client: Client,
        *,
        machine_id: str | None = None,
        name: str | None = None,
        template: str | None = None,
        snapshot_id: str | None = None,
        workdir: str = DEFAULT_WORKDIR,
        default_timeout: int = DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS,
        file_timeout: float = DEFAULT_INSTABOX_FILE_TIMEOUT_SECONDS,
        wake_timeout: float = DEFAULT_INSTABOX_WAKE_TIMEOUT_SECONDS,
        ensure_workdir: bool = True,
    ) -> InstaBoxSandbox:
        """Create a new InstaBox machine or reconnect to an existing one (`ensure_running`).

        Validates all configuration before invoking any InstaBox API. If post-create
        or post-reconnect initialization fails, raises `SandboxInitializationError`
        containing `machine_id` and `created` so callers can clean up resources.
        """
        validated_workdir = _validate_workdir(workdir)
        validated_default_timeout = _validate_exec_timeout(
            default_timeout, param_name="default_timeout"
        )
        validated_file_timeout = _validate_positive_float_timeout(
            file_timeout, param_name="file_timeout"
        )
        validated_wake_timeout = _validate_positive_float_timeout(
            wake_timeout, param_name="wake_timeout"
        )
        if not isinstance(ensure_workdir, bool):
            raise TypeError(
                f"ensure_workdir must be a bool, got {type(ensure_workdir).__name__}"
            )

        validated_machine_id: str | None = None
        if machine_id is not None:
            validated_machine_id = _validate_machine_id(machine_id)
            if any(arg is not None for arg in (name, template, snapshot_id)):
                raise ValueError(
                    "Cannot pass name, template, or snapshot_id when reconnecting with machine_id"
                )
        else:
            for arg_name, arg_val in (
                ("name", name),
                ("template", template),
                ("snapshot_id", snapshot_id),
            ):
                if arg_val is not None:
                    if not isinstance(arg_val, str) or not arg_val.strip():
                        raise ValueError(f"{arg_name} must be a non-empty string when provided")
            if template is not None and snapshot_id is not None:
                raise ValueError("Cannot specify both template and snapshot_id")

        created = validated_machine_id is None
        if validated_machine_id is not None:
            machine = client.machines.ensure_running(validated_machine_id)
        else:
            machine = client.machines.create(
                name=name,
                template=template,
                snapshot_id=snapshot_id,
            )

        raw_id = getattr(machine, "id", None)
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise RuntimeError("InstaBox API returned a machine without a valid id")
        actual_id = raw_id.strip()

        try:
            sandbox = cls(
                client=client,
                machine_id=actual_id,
                workdir=validated_workdir,
                default_timeout=validated_default_timeout,
                file_timeout=validated_file_timeout,
                wake_timeout=validated_wake_timeout,
            )
            if ensure_workdir:
                res = sandbox._call_with_wake_once(
                    lambda: client.machines.exec(
                        actual_id,
                        ["mkdir", "-p", "--", sandbox.workdir],
                        timeout=30,
                    )
                )
                if not res.ok:
                    raise RuntimeError(
                        f"mkdir -p {sandbox.workdir!r} failed with "
                        f"exit_code={res.exit_code}, timed_out={res.timed_out}, stderr={res.stderr!r}"
                    )
        except SandboxInitializationError:
            raise
        except Exception as exc:
            raise SandboxInitializationError(
                f"Failed to initialize sandbox on machine {actual_id!r} "
                f"(created={created}): {exc}",
                machine_id=actual_id,
                created=created,
            ) from exc

        return sandbox

    def delete_machine(self, *, timeout: float = 600.0) -> None:
        """Delete the underlying InstaBox machine with mandatory confirmation header."""
        self._client.machines.delete(
            self._machine_id,
            confirm=self._machine_id,
            wait=True,
            timeout=timeout,
        )

    def _call_with_wake_once(self, fn: Callable[[], T]) -> T:
        """Execute `fn()`, waking the machine and retrying at most ONCE if and only if
        InstaBox returns `APIError` with `code == "machine_not_running"`.

        Never retries on `APIConnectionError`, `WaitTimeoutError`, `ServerError`, or
        any other error where execution state is unknown.
        """
        try:
            return fn()
        except APIError as exc:
            if exc.code != "machine_not_running":
                raise
            # Machine is paused or stopped; command was NOT executed.
            self._client.machines.ensure_running(self._machine_id, timeout=self._wake_timeout)
            return fn()

    def _resolve_timeout(self, timeout: int | None) -> int:
        """Validate and resolve `timeout` within InstaBox's 1..600s limit."""
        if timeout is None:
            return self._default_timeout
        return _validate_exec_timeout(timeout, param_name="timeout")

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a shell command in the InstaBox machine (`bash -lc`) inside `self.workdir`.

        Does not retry if the request times out or loses network connectivity mid-flight.
        Only wakes and retries once if the API immediately rejects with `machine_not_running`.
        """
        resolved_timeout = self._resolve_timeout(timeout)

        res = self._call_with_wake_once(
            lambda: self._client.machines.exec(
                self._machine_id,
                command,
                cwd=self._workdir,
                timeout=resolved_timeout,
            )
        )
        return ExecuteResponse(
            output=_format_exec_output(res),
            exit_code=res.exit_code,
            truncated=bool(res.truncated),
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files to the sandbox with per-file error isolation.

        Infrastructure/authentication errors propagate immediately; file-level errors
        are returned in `FileUploadResponse.error`.
        """
        responses: list[FileUploadResponse] = []
        for raw_path, content in files:
            path_err = _validate_path(raw_path)
            if path_err is not None:
                responses.append(FileUploadResponse(path=raw_path, error=path_err))
                continue

            if not isinstance(content, (bytes, bytearray, memoryview)):
                raise TypeError(
                    f"upload_files expects bytes content for {raw_path!r}, got {type(content).__name__}"
                )

            abs_path = _resolve_posix_path(self._workdir, raw_path)
            payload = bytes(content)

            try:
                self._call_with_wake_once(
                    lambda p=abs_path, b=payload: self._client.files.write(
                        self._machine_id,
                        p,
                        b,
                        parents=True,
                        timeout=self._file_timeout,
                    )
                )
                responses.append(FileUploadResponse(path=raw_path, error=None))
            except (APIConnectionError, WaitTimeoutError):
                # Network/timeout errors are infrastructure failures; do not mask as file errors.
                raise
            except APIError as exc:
                err_code = _classify_file_error(exc, is_download=False)
                responses.append(FileUploadResponse(path=raw_path, error=err_code))

        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from the sandbox with per-file error isolation.

        Handles both the HTTP `/files/content` route and the SDK's `exec` fallback route
        (where `_exec_size` uses `[ -f path ] || exit 3` and raises `NotFoundError` even
        when `path` is a directory).
        """
        responses: list[FileDownloadResponse] = []
        for raw_path in paths:
            path_err = _validate_path(raw_path)
            if path_err is not None:
                responses.append(FileDownloadResponse(path=raw_path, content=None, error=path_err))
                continue

            abs_path = _resolve_posix_path(self._workdir, raw_path)
            try:
                data = self._call_with_wake_once(
                    lambda p=abs_path: self._DownloadOne(p)
                )
                responses.append(FileDownloadResponse(path=raw_path, content=data, error=None))
            except (APIConnectionError, WaitTimeoutError):
                raise
            except APIError as exc:
                err_code = _classify_file_error(exc, is_download=True)
                responses.append(FileDownloadResponse(path=raw_path, content=None, error=err_code))

        return responses

    def _DownloadOne(self, abs_path: str) -> bytes:
        """Read bytes for a single absolute path, disambiguating directories and missing files on exec fallback."""
        try:
            return self._client.files.read_bytes(
                self._machine_id,
                abs_path,
                timeout=self._file_timeout,
            )
        except instabox.NotFoundError as exc:
            # When the server lacks HTTP file routes (`self._client.files._routes is False`),
            # `files._exec_size` runs `[ -f path ] || exit 3`, which raises client-side
            # `NotFoundError(code="not_found", body=None)` both when `path` does not exist
            # AND when `path` is a directory. Probing `test -d` via `machines.exec` verifies
            # the machine is reachable and separates directory vs missing file without
            # relying on exception message text.
            if (
                (exc.code or "").strip().lower() == "not_found"
                and exc.body is None
                and getattr(self._client.files, "_routes", None) is False
            ):
                probe = self._client.machines.exec(
                    self._machine_id,
                    ["test", "-d", abs_path],
                    timeout=15,
                )
                if probe.exit_code == 0:
                    raise instabox.ConflictError(
                        f"{abs_path} is a directory",
                        code="not_a_file",
                        status=409,
                    ) from exc
                if probe.exit_code == 1 and not probe.timed_out:
                    raise instabox.NotFoundError(
                        f"{abs_path} does not exist",
                        code="file_not_found",
                        status=404,
                    ) from exc
            raise
