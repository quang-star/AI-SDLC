"""Offline unit and protocol compliance tests for `InstaBoxSandbox` (Spike 01).

Covers:
1. Protocol compliance with `deepagents.backends.protocol.SandboxBackendProtocol`
   and `execute_accepts_timeout(InstaBoxSandbox)`.
2. Timeout validation (1..600s limit of `machines.exec`), default timeout, and
   `ExecResult.timed_out` handling.
3. Bounded reconnect (`ensure_running`) ONLY on `ConflictError(code="machine_not_running")`
   (at most 1 wake retry).
4. Strict NO-RETRY guarantee when `execute()` or file operations fail with ambiguous
   execution state (`APIConnectionError`, `WaitTimeoutError`, `ServerError`, or
   `ConflictError(code="operation_conflict")`).
5. Output truncation (`ExecResult.truncated=True`), `stdout` + `stderr` combination,
   and accurate `signal` / `exit_code == 137` messaging without false OOM claims.
6. Binary file round-trip and partial success on `upload_files` / `download_files`
   for both HTTP file routes and the SDK's `exec` base64 fallback (`_routes = False`).
7. Strict separation of per-file errors vs. infrastructure/auth errors
   (`AuthenticationError`, `PermissionDeniedError(code="missing_scope")`,
   `NotFoundError(code="machine_not_found")`, `APIConnectionError`, `ServerError`).
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from unittest.mock import MagicMock

import instabox
import pytest
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    SandboxBackendProtocol,
    execute_accepts_timeout,
)
from instabox import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    ExecResult,
    FileWritten,
    Machine,
    NotFoundError,
    OperationFailedError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    WaitTimeoutError,
)

from instabox_sandbox import (
    DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS,
    MAX_INSTABOX_EXEC_TIMEOUT_SECONDS,
    InstaBoxSandbox,
    SandboxInitializationError,
)


def _make_mock_client() -> MagicMock:
    client = MagicMock(spec=instabox.Client)
    client.machines = MagicMock()
    client.files = MagicMock()
    client.files._routes = True
    return client


# ---------------------------------------------------------------------------
# 1. Protocol & Timeout validation tests
# ---------------------------------------------------------------------------


def test_protocol_compliance_and_timeout_signature() -> None:
    client = _make_mock_client()
    sb = InstaBoxSandbox(client, "m_test123", workdir="/data/workspace/repo")

    assert isinstance(sb, SandboxBackendProtocol)
    assert sb.id == "m_test123"
    assert sb.workdir == "/data/workspace/repo"
    assert execute_accepts_timeout(InstaBoxSandbox) is True


@pytest.mark.parametrize("bad_timeout", [0, -1, -100, 601, 3600])
def test_timeout_bounds_validation(bad_timeout: int) -> None:
    client = _make_mock_client()
    with pytest.raises(ValueError, match="default_timeout"):
        InstaBoxSandbox(client, "m_test123", default_timeout=bad_timeout)

    sb = InstaBoxSandbox(client, "m_test123")
    with pytest.raises(ValueError, match="timeout"):
        sb.execute("echo hi", timeout=bad_timeout)
    client.machines.exec.assert_not_called()


@pytest.mark.parametrize("bad_timeout", [True, False, 1.5, 0.5, "10", None])
def test_timeout_rejects_non_int_types(bad_timeout: Any) -> None:
    client = _make_mock_client()
    with pytest.raises(TypeError, match="default_timeout must be an int"):
        InstaBoxSandbox(client, "m_test123", default_timeout=bad_timeout)

    if bad_timeout is not None:
        sb = InstaBoxSandbox(client, "m_test123")
        with pytest.raises(TypeError, match="timeout must be an int"):
            sb.execute("echo hi", timeout=bad_timeout)
        client.machines.exec.assert_not_called()


@pytest.mark.parametrize("bad_timeout", [True, False, 0, -1.0, float("nan"), float("inf"), "30"])
def test_file_and_wake_timeout_validation(bad_timeout: Any) -> None:
    client = _make_mock_client()
    with pytest.raises((TypeError, ValueError), match="file_timeout"):
        InstaBoxSandbox(client, "m_test123", file_timeout=bad_timeout)
    with pytest.raises((TypeError, ValueError), match="wake_timeout"):
        InstaBoxSandbox(client, "m_test123", wake_timeout=bad_timeout)


def test_execute_uses_default_and_custom_timeout_and_handles_timed_out_flag() -> None:
    client = _make_mock_client()
    client.machines.exec.return_value = ExecResult(
        stdout="partial line\n",
        stderr="",
        exit_code=124,
        timed_out=True,
        truncated=False,
        signal="",
    )
    sb = InstaBoxSandbox(client, "m_test123", workdir="/data/workspace")

    res = sb.execute("sleep 70")
    client.machines.exec.assert_called_once_with(
        "m_test123",
        "sleep 70",
        cwd="/data/workspace",
        timeout=DEFAULT_INSTABOX_EXEC_TIMEOUT_SECONDS,
    )
    assert res.exit_code == 124
    assert res.truncated is False
    assert "partial line" in res.output
    assert "command timed out (exit_code=124)" in res.output

    client.machines.exec.reset_mock()
    sb.execute("pytest -q", timeout=MAX_INSTABOX_EXEC_TIMEOUT_SECONDS)
    client.machines.exec.assert_called_once_with(
        "m_test123",
        "pytest -q",
        cwd="/data/workspace",
        timeout=600,
    )


# ---------------------------------------------------------------------------
# 2. Output truncation & Signal / Exit 137 (no false OOM claim)
# ---------------------------------------------------------------------------


def test_execute_truncation_and_signal_without_false_oom_claim() -> None:
    client = _make_mock_client()
    client.machines.exec.return_value = ExecResult(
        stdout="A" * 1024,
        stderr="Killed",
        exit_code=137,
        timed_out=False,
        truncated=True,
        signal="SIGKILL",
    )
    sb = InstaBoxSandbox(client, "m_test123")
    res = sb.execute("python3 heavy.py")

    assert res.exit_code == 137
    assert res.truncated is True
    assert "Killed" in res.output
    assert "terminated by signal SIGKILL" in res.output
    assert "cause undetermined" in res.output


def test_execute_exit_137_without_signal_field() -> None:
    client = _make_mock_client()
    client.machines.exec.return_value = ExecResult(
        stdout="",
        stderr="",
        exit_code=137,
        timed_out=False,
        truncated=False,
        signal="",
    )
    sb = InstaBoxSandbox(client, "m_test123")
    res = sb.execute("python3 heavy.py")
    assert res.exit_code == 137
    assert "exited with code 137 (SIGKILL); cause undetermined" in res.output


# ---------------------------------------------------------------------------
# 3. Reconnect on `machine_not_running` vs. NO-RETRY on ambiguous errors
# ---------------------------------------------------------------------------


def test_wake_and_retry_once_on_machine_not_running() -> None:
    client = _make_mock_client()
    not_running_err = ConflictError(
        "machine is stopped; start it first",
        code="machine_not_running",
        status=409,
    )
    ok_result = ExecResult(
        stdout="woken up\n",
        stderr="",
        exit_code=0,
        timed_out=False,
        truncated=False,
        signal="",
    )
    client.machines.exec.side_effect = [not_running_err, ok_result]

    sb = InstaBoxSandbox(client, "m_paused")
    res = sb.execute("echo woken up")

    assert res.exit_code == 0
    assert res.output == "woken up\n"
    assert client.machines.exec.call_count == 2
    client.machines.ensure_running.assert_called_once_with("m_paused", timeout=300.0)


def test_wake_retry_is_bounded_to_one_attempt() -> None:
    client = _make_mock_client()
    not_running_err = ConflictError(
        "machine is still not running",
        code="machine_not_running",
        status=409,
    )
    client.machines.exec.side_effect = [not_running_err, not_running_err]

    sb = InstaBoxSandbox(client, "m_paused")
    with pytest.raises(ConflictError) as exc_info:
        sb.execute("echo try")

    assert exc_info.value.code == "machine_not_running"
    assert client.machines.ensure_running.call_count == 1
    assert client.machines.exec.call_count == 2


@pytest.mark.parametrize(
    "ambiguous_error",
    [
        APIConnectionError("POST /v1/machines/m_1/exec: ReadTimeout"),
        WaitTimeoutError("timed out waiting for operation"),
        ServerError("502 Bad Gateway", code="host_unavailable", status=502, retryable=True),
        ConflictError("another operation is in progress", code="operation_conflict", status=409),
        RateLimitError("rate limited", code="rate_limited", status=429, retryable=True),
    ],
)
def test_never_retries_execute_on_ambiguous_or_timeout_errors(
    ambiguous_error: Exception,
) -> None:
    """Critical invariant: if a command may have started (network drop, timeout, 502),
    `execute()` must NOT re-run the command or call `ensure_running`."""
    client = _make_mock_client()
    client.machines.exec.side_effect = ambiguous_error

    sb = InstaBoxSandbox(client, "m_1")
    with pytest.raises(type(ambiguous_error)):
        sb.execute("git commit -m 'mutating action'")

    assert client.machines.exec.call_count == 1
    client.machines.ensure_running.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Binary files, Partial Success, and Separation of File vs Infra Errors
#    (Direct HTTP Route)
# ---------------------------------------------------------------------------


def test_binary_roundtrip_and_partial_success_http_routes() -> None:
    client = _make_mock_client()
    storage: dict[str, bytes] = {}
    directories: set[str] = {"/data/workspace/subdir"}

    def fake_write(
        machine: str,
        path: str,
        data: bytes,
        *,
        mode: str | None = None,
        parents: bool = True,
        timeout: float = 300,
    ) -> FileWritten:
        if path in directories:
            raise ConflictError(f"{path} is a directory", code="not_a_file", status=409)
        if path.startswith("/root/"):
            raise PermissionDeniedError(
                "Permission denied: /root/secret.bin",
                code="file_permission_denied",
                status=403,
            )
        storage[path] = bytes(data)
        return FileWritten(
            path=path,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            mode=mode or "0644",
            created=True,
        )

    def fake_read_bytes(machine: str, path: str, *, max_bytes: int | None = None, timeout: float = 300) -> bytes:
        if path in directories:
            raise ConflictError(f"{path} is a directory", code="not_a_file", status=409)
        if path.startswith("/root/"):
            raise PermissionDeniedError(
                "Permission denied: /root/secret.bin",
                code="file_permission_denied",
                status=403,
            )
        if path not in storage:
            raise NotFoundError(f"{path} does not exist", code="file_not_found", status=404)
        return storage[path]

    client.files.write.side_effect = fake_write
    client.files.read_bytes.side_effect = fake_read_bytes

    sb = InstaBoxSandbox(client, "m_bin", workdir="/data/workspace")

    # Non-UTF8 binary payload (PNG header + arbitrary bytes 0x00..0xFF)
    binary_payload = b"\x89PNG\r\n\x1a\n\x00\xff\xfe\xfd" + bytes(range(256))

    up_res = sb.upload_files(
        [
            ("assets/image.png", binary_payload),
            ("/data/workspace/subdir", b"cannot overwrite dir"),
            ("/root/secret.bin", b"cannot write root"),
            ("bad\x00path.txt", b"null byte in path"),
            ("notes/ok.txt", b"hello world\n"),
        ]
    )

    assert [r.error for r in up_res] == [
        None,
        IS_DIRECTORY,
        PERMISSION_DENIED,
        INVALID_PATH,
        None,
    ]

    down_res = sb.download_files(
        [
            "assets/image.png",
            "missing/file.txt",
            "/data/workspace/subdir",
            "/root/secret.bin",
            "",
            "notes/ok.txt",
        ]
    )

    assert down_res[0].error is None
    assert down_res[0].content == binary_payload
    assert down_res[1].error == FILE_NOT_FOUND
    assert down_res[1].content is None
    assert down_res[2].error == IS_DIRECTORY
    assert down_res[3].error == PERMISSION_DENIED
    assert down_res[4].error == INVALID_PATH
    assert down_res[5].error is None
    assert down_res[5].content == b"hello world\n"


@pytest.mark.parametrize(
    "infra_error",
    [
        AuthenticationError("invalid API key", code="invalid_key", status=401),
        PermissionDeniedError(
            "API key lacks machines:write scope",
            code="missing_scope",
            status=403,
        ),
        PermissionDeniedError(
            "workspace access denied",
            code="permission_denied",
            status=403,
        ),
        PermissionDeniedError(
            "Permission denied: /root/secret.bin",
            code="permission_denied",
            status=403,
        ),
        NotFoundError("machine m_dead not found", code="machine_not_found", status=404),
        NotFoundError("machine m_dead does not exist", code="not_found", status=404),
        NotFoundError("sandbox m_dead does not exist", code="not_found", status=404),
        NotFoundError("/data/workspace/missing.txt does not exist", code="not_found", status=404),
        BadRequestError("invalid path query parameter", code="bad_request", status=400),
        APIError("unexpected API error", code="unexpected_api_error", status=400),
        OperationFailedError("operation failed", code="operation_failed"),
        APIConnectionError("connection reset by peer"),
        WaitTimeoutError("operation timed out"),
        ServerError("503 Service Unavailable", code="host_unavailable", status=503),
        RateLimitError("rate limited", code="rate_limited", status=429),
    ],
)
def test_infrastructure_and_ambiguous_errors_propagate_in_file_operations(
    infra_error: Exception,
) -> None:
    """Infrastructure, auth, and ambiguous API errors must NEVER be masked by message heuristics."""
    client = _make_mock_client()
    client.files.write.side_effect = infra_error
    client.files.read_bytes.side_effect = infra_error

    sb = InstaBoxSandbox(client, "m_dead")

    with pytest.raises(type(infra_error)):
        sb.upload_files([("a.txt", b"hello")])

    with pytest.raises(type(infra_error)):
        sb.download_files(["a.txt"])


# ---------------------------------------------------------------------------
# 5. SDK Exec Fallback Route (`_routes = False`) Tests
# ---------------------------------------------------------------------------


def test_sdk_exec_fallback_route_binary_and_errors() -> None:
    """Exercise the real `instabox.Client.files` fallback when the server returns 404
    `route not found` on `/v1/machines/{id}/files` and falls back to `machines.exec`."""
    real_client = instabox.Client(api_key="ibk_test_fallback")
    # Force fallback mode (`_routes = False`) so `files.write` and `files.read_bytes`
    # execute shell commands via `real_client.machines.exec`.
    real_client.files._routes = False
    real_client.files._homes["m_fallback"] = "/data/home"

    fs_files: dict[str, bytes] = {"/data/workspace/existing.bin": bytes(range(128))}
    fs_dirs: set[str] = {"/data/workspace", "/data/workspace/mydir"}

    def fake_exec(
        machine: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        # 1. Directory probe from `InstaBoxSandbox._DownloadOne`: `["test", "-d", path]`
        if isinstance(command, list) and command[:2] == ["test", "-d"]:
            target = command[2]
            return ExecResult(
                stdout="",
                stderr="",
                exit_code=0 if target in fs_dirs else 1,
                timed_out=False,
                truncated=False,
                signal="",
            )

        assert isinstance(command, str)

        # 2. `_exec_size`: `[ -f /path ] || { echo "no such file: "/path >&2; exit 3; }; s=$(wc -c < /path) ...`
        if command.startswith("[ -f "):
            path = command.split("[ -f ")[1].split(" ]")[0].strip("'")
            if path not in fs_files:
                return ExecResult(
                    stdout="",
                    stderr=f"no such file: {path}\n",
                    exit_code=3,
                    timed_out=False,
                    truncated=False,
                    signal="",
                )
            return ExecResult(
                stdout=str(len(fs_files[path])),
                stderr="",
                exit_code=0,
                timed_out=False,
                truncated=False,
                signal="",
            )

        # 3. `_exec_chunks`: `dd if=/path bs=524288 skip=0 count=1 2>/dev/null | base64 | tr -d '\n'`
        if command.startswith("dd if="):
            path = command.split("dd if=")[1].split(" bs=")[0].strip("'")
            b64 = base64.b64encode(fs_files[path]).decode("ascii")
            return ExecResult(
                stdout=b64,
                stderr="",
                exit_code=0,
                timed_out=False,
                truncated=False,
                signal="",
            )

        # 4. `_write_exec`: `if [ -d /path ]; then echo "is a directory: "/path >&2; exit 21; fi; ...`
        if "exit 21; fi;" in command:
            path = command.split("if [ -d ")[1].split(" ];")[0].strip("'")
            if path in fs_dirs:
                return ExecResult(
                    stdout="",
                    stderr=f"is a directory: {path}\n",
                    exit_code=21,
                    timed_out=False,
                    truncated=False,
                    signal="",
                )
            if path.startswith("/root/"):
                return ExecResult(
                    stdout="",
                    stderr=f"bash: {path}: Permission denied\n",
                    exit_code=1,
                    timed_out=False,
                    truncated=False,
                    signal="",
                )
            if "base64 -d" in command:
                b64_chunk = command.split("printf '%s' '")[1].split("' | base64 -d")[0]
                fs_files[path] = base64.b64decode(b64_chunk)
            return ExecResult(
                stdout="",
                stderr="",
                exit_code=0,
                timed_out=False,
                truncated=False,
                signal="",
            )

        return ExecResult(stdout="", stderr="", exit_code=0, timed_out=False, truncated=False, signal="")

    real_client.machines.exec = MagicMock(side_effect=fake_exec)  # type: ignore[method-assign]

    sb = InstaBoxSandbox(real_client, "m_fallback", workdir="/data/workspace")

    # Upload over exec fallback: success, directory conflict, and permission denied
    up_res = sb.upload_files(
        [
            ("new.bin", b"\x00\x01\x02\xff"),
            ("mydir", b"data"),
            ("/root/forbidden.txt", b"data"),
        ]
    )
    assert [r.error for r in up_res] == [None, IS_DIRECTORY, PERMISSION_DENIED]
    assert fs_files["/data/workspace/new.bin"] == b"\x00\x01\x02\xff"

    # Download over exec fallback: success, missing file, and directory disambiguation
    down_res = sb.download_files(
        [
            "existing.bin",
            "new.bin",
            "does_not_exist.txt",
            "mydir",
        ]
    )
    assert down_res[0].error is None
    assert down_res[0].content == bytes(range(128))
    assert down_res[1].error is None
    assert down_res[1].content == b"\x00\x01\x02\xff"
    assert down_res[2].error == FILE_NOT_FOUND
    assert down_res[3].error == IS_DIRECTORY


def test_sdk_exec_fallback_propagates_machine_not_found() -> None:
    """Even on `_routes = False`, a 404 from `machines.exec` (missing VM) must propagate."""
    real_client = instabox.Client(api_key="ibk_test_fallback")
    real_client.files._routes = False
    real_client.machines.exec = MagicMock(  # type: ignore[method-assign]
        side_effect=NotFoundError("sandbox m_dead does not exist", code="not_found", status=404)
    )

    sb = InstaBoxSandbox(real_client, "m_dead", workdir="/data/workspace")
    with pytest.raises(NotFoundError, match="sandbox m_dead does not exist"):
        sb.download_files(["missing.txt"])


# ---------------------------------------------------------------------------
# 6. `create_or_reconnect` and `delete_machine` lifecycle tests
# ---------------------------------------------------------------------------


def test_create_or_reconnect_and_delete_machine() -> None:
    client = _make_mock_client()
    client.machines.create.return_value = Machine.from_dict(
        {
            "id": "m_created",
            "name": "spike-vm",
            "state": "running",
        }
    )
    client.machines.ensure_running.return_value = Machine.from_dict(
        {
            "id": "m_existing",
            "name": "spike-vm",
            "state": "running",
        }
    )
    client.machines.exec.return_value = ExecResult(
        stdout="",
        stderr="",
        exit_code=0,
        timed_out=False,
        truncated=False,
        signal="",
    )

    # Create new
    sb_new = InstaBoxSandbox.create_or_reconnect(client, name="spike-vm", workdir="/data/workspace/app")
    assert sb_new.id == "m_created"
    client.machines.create.assert_called_once_with(
        name="spike-vm",
        template=None,
        snapshot_id=None,
    )
    client.machines.exec.assert_called_once_with(
        "m_created",
        ["mkdir", "-p", "--", "/data/workspace/app"],
        timeout=30,
    )

    # Reconnect existing
    sb_re = InstaBoxSandbox.create_or_reconnect(
        client,
        machine_id="m_existing",
        workdir="/data/workspace/app",
    )
    assert sb_re.id == "m_existing"
    client.machines.ensure_running.assert_called_once_with("m_existing")

    # Delete confirms with machine_id
    sb_re.delete_machine()
    client.machines.delete.assert_called_once_with(
        "m_existing",
        confirm="m_existing",
        wait=True,
        timeout=600.0,
    )


@pytest.mark.parametrize(
    ("kwargs", "exc_type"),
    [
        ({"workdir": "relative/path"}, ValueError),
        ({"workdir": ""}, ValueError),
        ({"workdir": "/bad\x00dir"}, ValueError),
        ({"default_timeout": True}, TypeError),
        ({"default_timeout": 1.5}, TypeError),
        ({"default_timeout": 0}, ValueError),
        ({"default_timeout": 601}, ValueError),
        ({"file_timeout": True}, TypeError),
        ({"wake_timeout": 0}, ValueError),
        ({"machine_id": ""}, ValueError),
        ({"machine_id": "   "}, ValueError),
        ({"machine_id": "m_1", "template": "agent-browser"}, ValueError),
        ({"template": "agent-browser", "snapshot_id": "snap_1"}, ValueError),
        ({"template": "   "}, ValueError),
    ],
)
def test_create_or_reconnect_validates_before_calling_api(
    kwargs: dict[str, Any],
    exc_type: type[Exception],
) -> None:
    client = _make_mock_client()
    with pytest.raises(exc_type):
        InstaBoxSandbox.create_or_reconnect(client, **kwargs)

    client.machines.create.assert_not_called()
    client.machines.ensure_running.assert_not_called()
    client.machines.exec.assert_not_called()


def test_create_or_reconnect_failure_after_create_includes_machine_id() -> None:
    client = _make_mock_client()
    client.machines.create.return_value = Machine.from_dict(
        {"id": "m_leaked_1", "name": "spike-vm", "state": "running"}
    )
    client.machines.exec.return_value = ExecResult(
        stdout="",
        stderr="Permission denied",
        exit_code=1,
        timed_out=False,
        truncated=False,
        signal="",
    )

    with pytest.raises(SandboxInitializationError) as exc_info:
        InstaBoxSandbox.create_or_reconnect(client, workdir="/root/blocked")

    assert exc_info.value.machine_id == "m_leaked_1"
    assert exc_info.value.created is True
    assert "m_leaked_1" in str(exc_info.value)


def test_create_or_reconnect_exception_during_workdir_init_includes_machine_id() -> None:
    client = _make_mock_client()
    client.machines.create.return_value = Machine.from_dict(
        {"id": "m_leaked_2", "name": "spike-vm", "state": "running"}
    )
    client.machines.exec.side_effect = APIConnectionError("connection reset")

    with pytest.raises(SandboxInitializationError) as exc_info:
        InstaBoxSandbox.create_or_reconnect(client, workdir="/data/workspace/app")

    assert exc_info.value.machine_id == "m_leaked_2"
    assert exc_info.value.created is True
    assert isinstance(exc_info.value.__cause__, APIConnectionError)


def test_create_or_reconnect_failure_after_reconnect_marks_created_false() -> None:
    client = _make_mock_client()
    client.machines.ensure_running.return_value = Machine.from_dict(
        {"id": "m_existing", "name": "spike-vm", "state": "running"}
    )
    client.machines.exec.side_effect = ServerError("500 internal", code="internal", status=500)

    with pytest.raises(SandboxInitializationError) as exc_info:
        InstaBoxSandbox.create_or_reconnect(client, machine_id="m_existing")

    assert exc_info.value.machine_id == "m_existing"
    assert exc_info.value.created is False
