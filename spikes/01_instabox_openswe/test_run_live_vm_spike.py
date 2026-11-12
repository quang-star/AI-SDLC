"""Offline tests for `run_live_vm_spike.py` config preflight and VM cleanup."""

from __future__ import annotations

from unittest.mock import MagicMock

import instabox
import pytest
from instabox import (
    APIConnectionError,
    AuthenticationError,
    ExecResult,
    FileWritten,
    Machine,
    NotFoundError,
    ServerError,
    Template,
    WaitTimeoutError,
)

import run_live_vm_spike as spike


def _make_mock_client() -> MagicMock:
    client = MagicMock(spec=instabox.Client)
    client.machines = MagicMock()
    client.operations = MagicMock()
    client.templates = MagicMock()
    client.files = MagicMock()
    client.files._routes = True
    return client


def test_missing_api_key_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("INSTABOX_API_KEY", raising=False)
    called = False

    def fail_if_called(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("Client should not be created when INSTABOX_API_KEY is missing")

    monkeypatch.setattr(spike, "Client", fail_if_called)
    exit_code = spike.main([])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert not called
    assert "INSTABOX_API_KEY" in captured.err


def test_config_repr_hides_api_key() -> None:
    cfg = spike.load_and_validate_config(
        [],
        env={"INSTABOX_API_KEY": "ibk_super_secret_value"},
    )
    assert "ibk_super_secret_value" not in repr(cfg)


@pytest.mark.parametrize(
    ("argv", "env", "expected_text"),
    [
        ([], {"INSTABOX_API_KEY": "   "}, "INSTABOX_API_KEY"),
        ([], {"INSTABOX_API_KEY": "ibk_ok", "INSTABOX_API_URL": "ftp://bad"}, "INSTABOX_API_URL"),
        (["--workdir", "relative/dir"], {"INSTABOX_API_KEY": "ibk_ok"}, "workdir"),
        (["--timeout", "1.5"], {"INSTABOX_API_KEY": "ibk_ok"}, "Invalid timeout"),
        (["--timeout", "601"], {"INSTABOX_API_KEY": "ibk_ok"}, "600"),
        (["--machine-id", "   "], {"INSTABOX_API_KEY": "ibk_ok"}, "machine_id"),
        (
            ["--machine-id", "m_1", "--template", "agent-browser"],
            {"INSTABOX_API_KEY": "ibk_ok"},
            "Cannot combine",
        ),
        (
            ["--template", "agent-browser", "--snapshot-id", "snap_1"],
            {"INSTABOX_API_KEY": "ibk_ok"},
            "Cannot specify both",
        ),
        (
            ["--with-agent", "--model", "anthropic:claude-sonnet-4-6"],
            {"INSTABOX_API_KEY": "ibk_ok"},
            "ANTHROPIC_API_KEY",
        ),
        (
            ["--with-agent", "--model", "google_genai:gemini-2.5-flash"],
            {"INSTABOX_API_KEY": "ibk_ok"},
            "GOOGLE_API_KEY",
        ),
    ],
)
def test_invalid_or_missing_config_rejected_before_vm_creation(
    argv: list[str],
    env: dict[str, str],
    expected_text: str,
) -> None:
    with pytest.raises(spike.SpikeConfigError, match=expected_text):
        spike.load_and_validate_config(argv, env=env)


def test_preflight_only_checks_auth_without_creating_vm() -> None:
    mock_client = _make_mock_client()
    mock_client.templates.list.return_value = [
        Template.from_dict({"id": "agent-browser", "version": "1", "creatable": True})
    ]

    cfg = spike.load_and_validate_config(
        ["--preflight-only", "--template", "agent-browser"],
        env={"INSTABOX_API_KEY": "ibk_ok"},
    )
    mid = spike.run_spike(cfg, client_factory=lambda **_: mock_client)  # type: ignore[arg-type]

    assert mid == ""
    mock_client.templates.list.assert_called_once()
    mock_client.machines.create.assert_not_called()
    mock_client.close.assert_called_once()


def test_unknown_template_fails_before_creating_vm() -> None:
    mock_client = _make_mock_client()
    mock_client.templates.list.return_value = [
        Template.from_dict({"id": "agent-browser", "version": "1", "creatable": True})
    ]

    cfg = spike.load_and_validate_config(
        ["--template", "nonexistent-template"],
        env={"INSTABOX_API_KEY": "ibk_ok"},
    )
    with pytest.raises(spike.SpikeConfigError, match="nonexistent-template"):
        spike.run_spike(cfg, client_factory=lambda **_: mock_client)  # type: ignore[arg-type]

    mock_client.machines.create.assert_not_called()
    mock_client.close.assert_called_once()


def test_invalid_api_key_stops_in_preflight_before_create() -> None:
    mock_client = _make_mock_client()
    mock_client.templates.list.side_effect = AuthenticationError(
        "invalid key", code="invalid_key", status=401
    )

    cfg = spike.load_and_validate_config([], env={"INSTABOX_API_KEY": "ibk_bad"})
    with pytest.raises(AuthenticationError):
        spike.run_spike(cfg, client_factory=lambda **_: mock_client)  # type: ignore[arg-type]

    mock_client.machines.create.assert_not_called()
    mock_client.close.assert_called_once()


def test_run_spike_deletes_created_vm_if_workdir_init_fails() -> None:
    mock_client = _make_mock_client()
    mock_client.templates.list.return_value = []
    mock_client.machines.create.return_value = Machine.from_dict(
        {"id": "m_created_then_failed", "name": "spike01", "state": "running"}
    )
    mock_client.machines.ensure_running.return_value = Machine.from_dict(
        {"id": "m_created_then_failed", "name": "spike01", "state": "running"}
    )
    mock_client.machines.exec.return_value = ExecResult(
        stdout="",
        stderr="Permission denied",
        exit_code=1,
        timed_out=False,
        truncated=False,
        signal="",
    )

    cfg = spike.load_and_validate_config([], env={"INSTABOX_API_KEY": "ibk_ok"})
    with pytest.raises(spike.SandboxInitializationError) as exc_info:
        spike.run_spike(cfg, client_factory=lambda **_: mock_client)  # type: ignore[arg-type]

    assert exc_info.value.machine_id == "m_created_then_failed"
    mock_client.machines.delete.assert_called_once_with(
        "m_created_then_failed",
        confirm="m_created_then_failed",
        wait=True,
        timeout=600.0,
    )
    mock_client.close.assert_called_once()


def test_run_spike_never_deletes_preexisting_machine_on_failure() -> None:
    mock_client = _make_mock_client()
    mock_client.machines.get.return_value = Machine.from_dict(
        {"id": "m_existing", "name": "keep-me", "state": "running"}
    )
    mock_client.machines.ensure_running.return_value = Machine.from_dict(
        {"id": "m_existing", "name": "keep-me", "state": "running"}
    )
    mock_client.machines.exec.return_value = ExecResult(
        stdout="",
        stderr="disk full",
        exit_code=1,
        timed_out=False,
        truncated=False,
        signal="",
    )

    cfg = spike.load_and_validate_config(
        ["--machine-id", "m_existing"],
        env={"INSTABOX_API_KEY": "ibk_ok"},
    )
    with pytest.raises(spike.SandboxInitializationError) as exc_info:
        spike.run_spike(cfg, client_factory=lambda **_: mock_client)  # type: ignore[arg-type]

    assert exc_info.value.machine_id == "m_existing"
    assert exc_info.value.created is False
    mock_client.machines.delete.assert_not_called()
    mock_client.close.assert_called_once()


def test_run_spike_happy_path_and_keep_vm_flag() -> None:
    storage: dict[str, bytes] = {}

    def make_mock_client() -> MagicMock:
        client = _make_mock_client()
        client.templates.list.return_value = [
            Template.from_dict({"id": "agent-browser", "version": "1", "creatable": True})
        ]
        client.machines.create.return_value = Machine.from_dict(
            {
                "id": "m_happy",
                "name": "spike01",
                "state": "running",
                "operationId": "op_happy",
            }
        )
        client.machines.ensure_running.return_value = Machine.from_dict(
            {"id": "m_happy", "name": "spike01", "state": "running"}
        )

        def fake_exec(
            machine: str,
            command: str | list[str],
            *,
            cwd: str | None = None,
            timeout: int = 60,
        ) -> ExecResult:
            if isinstance(command, list) and command[:2] == ["mkdir", "-p"]:
                return ExecResult("", "", 0, False, False, "")
            if command == "pwd -L && uname -srm":
                return ExecResult("/data/workspace/spike01\nLinux 6.8.0 x86_64\n", "", 0, False, False, "")
            if command == "sleep 3" and timeout == 1:
                return ExecResult("", "", 124, True, False, "")
            if "os.scandir" in command:
                return ExecResult('{"path": "/data/workspace/spike01/notes", "is_dir": true}\n', "", 0, False, False, "")
            if "MAX_EXPANSIONS" in command:
                return ExecResult('{"path": "notes/demo.txt", "is_dir": false}\n', "", 0, False, False, "")
            if "grep " in command:
                return ExecResult("/data/workspace/spike01/notes/demo.txt\x002:beta-updated\n", "", 0, False, False, "")
            if "old_crlf" in command:
                storage["/data/workspace/spike01/notes/demo.txt"] = b"alpha\nbeta-updated\n"
                return ExecResult('{"count": 1}\n', "", 0, False, False, "")
            if "MAX_LINE_COUNT_BYTES" in command:
                return ExecResult(
                    '{"encoding": "utf-8", "content": "alpha\\nbeta-updated", "total_lines": 2, "start_line": 1, "end_line": 2, "next_offset": null}\n',
                    "",
                    0,
                    False,
                    False,
                    "",
                )
            return ExecResult("", "", 0, False, False, "")

        def fake_write(machine: str, path: str, data: bytes, **kwargs: object) -> FileWritten:
            storage[path] = bytes(data)
            return FileWritten(path=path, size=len(data), sha256="0" * 64, mode="0644", created=True)

        def fake_read_bytes(machine: str, path: str, **kwargs: object) -> bytes:
            if path not in storage:
                raise NotFoundError(f"{path} not found", code="not_found", status=404)
            return storage[path]

        client.machines.exec.side_effect = fake_exec
        client.files.write.side_effect = fake_write
        client.files.read_bytes.side_effect = fake_read_bytes
        return client

    # 1. Default deletes VM
    client1 = make_mock_client()
    cfg1 = spike.load_and_validate_config([], env={"INSTABOX_API_KEY": "ibk_ok"})
    mid1 = spike.run_spike(cfg1, client_factory=lambda **_: client1, run_id="happy1")  # type: ignore[arg-type]
    assert mid1 == "m_happy"
    assert client1.machines.create.call_args.kwargs["wait"] is False
    assert client1.machines.create.call_args.kwargs["metadata"] == {
        "spike": "01",
        "spike_run_id": "happy1",
    }
    client1.operations.wait.assert_called_once_with("op_happy", timeout=600.0)
    client1.machines.delete.assert_called_once_with(
        "m_happy", confirm="m_happy", wait=True, timeout=600.0
    )

    # 2. --keep-vm preserves VM
    storage.clear()
    client2 = make_mock_client()
    cfg2 = spike.load_and_validate_config(["--keep-vm"], env={"INSTABOX_API_KEY": "ibk_ok"})
    mid2 = spike.run_spike(cfg2, client_factory=lambda **_: client2, run_id="happy2")  # type: ignore[arg-type]
    assert mid2 == "m_happy"
    client2.operations.wait.assert_called_once_with("op_happy", timeout=600.0)
    client2.machines.delete.assert_not_called()


def test_preflight_only_with_skip_api_preflight_reports_local_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_client = _make_mock_client()
    cfg = spike.load_and_validate_config(
        ["--preflight-only", "--skip-api-preflight"],
        env={"INSTABOX_API_KEY": "ibk_ok"},
    )
    mid = spike.run_spike(cfg, client_factory=lambda **_: mock_client)  # type: ignore[arg-type]
    captured = capsys.readouterr()

    assert mid == ""
    mock_client.templates.list.assert_not_called()
    mock_client.machines.create.assert_not_called()
    assert (
        "[PREFLIGHT] Local configuration check passed ONLY (--preflight-only + --skip-api-preflight); "
        "API authentication was NOT verified; no VM created."
        in captured.out
    )


def test_create_timeout_after_early_id_still_deletes_vm_without_recreating() -> None:
    mock_client = _make_mock_client()
    mock_client.templates.list.return_value = []
    mock_client.machines.create.return_value = Machine.from_dict(
        {"id": "m_early", "name": "spike01", "state": "creating", "operationId": "op_early"}
    )
    mock_client.operations.wait.side_effect = WaitTimeoutError("operation timed out")

    cfg = spike.load_and_validate_config([], env={"INSTABOX_API_KEY": "ibk_ok"})
    with pytest.raises(WaitTimeoutError):
        spike.run_spike(cfg, client_factory=lambda **_: mock_client, run_id="early1")  # type: ignore[arg-type]

    mock_client.machines.create.assert_called_once()
    mock_client.machines.delete.assert_called_once_with(
        "m_early", confirm="m_early", wait=True, timeout=600.0
    )
    mock_client.close.assert_called_once()


def test_ambiguous_create_error_recovers_via_metadata_lookup_without_recreating() -> None:
    mock_client = _make_mock_client()
    mock_client.templates.list.return_value = []
    mock_client.machines.create.side_effect = APIConnectionError("read timeout")
    mock_client.machines.list.return_value = [
        Machine.from_dict(
            {
                "id": "m_recovered",
                "name": "spike01",
                "state": "creating",
                "operationId": "op_rec",
                "metadata": {"spike": "01", "spike_run_id": "run123"},
            }
        )
    ]

    cfg = spike.load_and_validate_config([], env={"INSTABOX_API_KEY": "ibk_ok"})
    with pytest.raises(APIConnectionError):
        spike.run_spike(cfg, client_factory=lambda **_: mock_client, run_id="run123")  # type: ignore[arg-type]

    mock_client.machines.create.assert_called_once()
    mock_client.operations.wait.assert_not_called()
    mock_client.machines.list.assert_called_once_with(metadata={"spike_run_id": "run123"})
    mock_client.machines.delete.assert_called_once_with(
        "m_recovered", confirm="m_recovered", wait=True, timeout=600.0
    )
    mock_client.close.assert_called_once()


def test_delete_failure_raises_cleanup_incomplete_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_client = _make_mock_client()
    mock_client.templates.list.return_value = []
    mock_client.machines.create.return_value = Machine.from_dict(
        {"id": "m_happy", "name": "spike01", "state": "running", "operationId": "op_happy"}
    )
    mock_client.machines.ensure_running.return_value = Machine.from_dict(
        {"id": "m_happy", "name": "spike01", "state": "running"}
    )

    storage: dict[str, bytes] = {}

    def fake_exec(
        machine: str,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        if isinstance(command, list) and command[:2] == ["mkdir", "-p"]:
            return ExecResult("", "", 0, False, False, "")
        if command == "pwd -L && uname -srm":
            return ExecResult("/data/workspace/spike01\nLinux 6.8.0 x86_64\n", "", 0, False, False, "")
        if command == "sleep 3" and timeout == 1:
            return ExecResult("", "", 124, True, False, "")
        if "os.scandir" in command:
            return ExecResult('{"path": "/data/workspace/spike01/notes", "is_dir": true}\n', "", 0, False, False, "")
        if "MAX_EXPANSIONS" in command:
            return ExecResult('{"path": "notes/demo.txt", "is_dir": false}\n', "", 0, False, False, "")
        if "grep " in command:
            return ExecResult("/data/workspace/spike01/notes/demo.txt\x002:beta-updated\n", "", 0, False, False, "")
        if "old_crlf" in command:
            storage["/data/workspace/spike01/notes/demo.txt"] = b"alpha\nbeta-updated\n"
            return ExecResult('{"count": 1}\n', "", 0, False, False, "")
        if "MAX_LINE_COUNT_BYTES" in command:
            return ExecResult(
                '{"encoding": "utf-8", "content": "alpha\\nbeta-updated", "total_lines": 2, "start_line": 1, "end_line": 2, "next_offset": null}\n',
                "",
                0,
                False,
                False,
                "",
            )
        return ExecResult("", "", 0, False, False, "")

    mock_client.machines.exec.side_effect = fake_exec
    mock_client.files.write.side_effect = lambda m, p, d, **kw: (
        storage.__setitem__(p, bytes(d))
        or FileWritten(path=p, size=len(d), sha256="0" * 64, mode="0644", created=True)
    )
    mock_client.files.read_bytes.side_effect = lambda m, p, **kw: storage[p]
    mock_client.machines.delete.side_effect = ServerError(
        "delete timed out", code="internal_error", status=500
    )

    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_ok")
    monkeypatch.setattr(spike, "Client", lambda **_: mock_client)

    exit_code = spike.main([])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert "m_happy" in captured.err
    assert "CLEANUP" in captured.err.upper()


def test_agent_smoke_unique_filename_and_exact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse

    run_id = "smoke99"
    stored: dict[str, bytes] = {}
    preexisting = False

    class _FakeSandbox:
        workdir = "/data/workspace/spike01"

        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            assert f"agent_smoke_{run_id}.txt" in command
            assert "test ! -e" in command and "test ! -L" in command
            return ExecuteResponse(output="", exit_code=1 if preexisting else 0, truncated=False)

        def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
            assert len(paths) == 1
            assert f"agent_smoke_{run_id}.txt" in paths[0]
            return [FileDownloadResponse(path=paths[0], content=stored.get(paths[0]), error=None)]

    sandbox = _FakeSandbox()
    agent_calls: list[dict[str, object]] = []

    class _FakeAgent:
        def invoke(self, payload: dict[str, object]) -> dict[str, object]:
            agent_calls.append(payload)
            return {"messages": [MagicMock(content="done")]}

    def fake_create_deep_agent(*, model: str, backend: object, system_prompt: str) -> _FakeAgent:
        assert model == "openai:gpt-5-mini"
        assert backend is sandbox
        return _FakeAgent()

    monkeypatch.setattr("deepagents.create_deep_agent", fake_create_deep_agent)

    # 1. Exact bytes match -> passes
    stored[f"/data/workspace/spike01/agent_smoke_{run_id}.txt"] = f"SPIKE_AGENT_OK_{run_id}\n".encode("utf-8")
    stored[f"agent_smoke_{run_id}.txt"] = f"SPIKE_AGENT_OK_{run_id}\n".encode("utf-8")
    spike._run_agent_smoke(sandbox, "openai:gpt-5-mini", run_id=run_id)  # type: ignore[arg-type]
    assert len(agent_calls) == 1

    # 2. Missing trailing newline -> fails exact byte check
    stored[f"/data/workspace/spike01/agent_smoke_{run_id}.txt"] = f"SPIKE_AGENT_OK_{run_id}".encode("utf-8")
    stored[f"agent_smoke_{run_id}.txt"] = f"SPIKE_AGENT_OK_{run_id}".encode("utf-8")
    with pytest.raises(AssertionError, match="Exact byte mismatch"):
        spike._run_agent_smoke(sandbox, "openai:gpt-5-mini", run_id=run_id)  # type: ignore[arg-type]

    # 3. Pre-existing file before agent turn -> fails before invoking agent
    preexisting = True
    calls_before = len(agent_calls)
    with pytest.raises(AssertionError, match="unexpectedly exists"):
        spike._run_agent_smoke(sandbox, "openai:gpt-5-mini", run_id=run_id)  # type: ignore[arg-type]
    assert len(agent_calls) == calls_before
