"""Offline tests for `run_phase2_spike.py` config, snapshot lifecycle, verification, and cleanup."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import instabox
import pytest
from instabox import APIConnectionError, ExecResult, FileWritten, Machine, ServerError, Snapshot, Template

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import run_phase2_spike as p2  # noqa: E402

_ROLE_ENV = {
    "INSTABOX_API_KEY": "ibk_ok",
    "NINE_ROUTER_API_KEY": "router_ok",
    "NINE_ROUTER_ROLE_MODELS": json.dumps(
        {
            "ba": "ag/gemini-3.8-flash",
            "dev": "cx/gpt-6.1-sol",
            "reviewer": "ag/claude-sonnet-4-6",
        }
    ),
}

_BASE_JUNIT = (
    b'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="4">'
    b'<testcase classname="tests.test_billing" name="test_a" />'
    b'<testcase classname="tests.test_billing" name="test_b" />'
    b"</testsuite></testsuites>"
)
_RED_JUNIT = (
    b'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="3">'
    b'<testcase classname="tests.test_billing" name="test_a" />'
    b'<testcase classname="tests.test_billing" name="test_b" />'
    b'<testcase classname="tests.test_discount_regression" name="test_rounding"><failure message="95 != 94" /></testcase>'
    b"</testsuite></testsuites>"
)
_POST_JUNIT = (
    b'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="3">'
    b'<testcase classname="tests.test_billing" name="test_a" />'
    b'<testcase classname="tests.test_billing" name="test_b" />'
    b'<testcase classname="tests.test_discount_regression" name="test_rounding" />'
    b"</testsuite></testsuites>"
)


def _make_mock_client() -> MagicMock:
    client = MagicMock(spec=instabox.Client)
    client.machines = MagicMock()
    client.operations = MagicMock()
    client.snapshots = MagicMock()
    client.templates = MagicMock()
    client.files = MagicMock()
    client.files._routes = True
    return client


def test_phase2_config_validation_and_role_selection(tmp_path: Path) -> None:
    # Default role picks `dev` from NINE_ROUTER_ROLE_MODELS (ignoring single-model fallback only when --role set)
    cfg = p2.load_and_validate_phase2_config([], env=_ROLE_ENV)
    assert cfg.model_target.label == "dev"
    assert cfg.model_target.model == "9router:cx/gpt-6.1-sol"
    assert cfg.repo_dir is not None and cfg.repo_dir.name == "mini_bug_repo"

    # --role overrides NINE_ROUTER_MODEL
    cfg_rev = p2.load_and_validate_phase2_config(
        ["--role", "reviewer"],
        env={**_ROLE_ENV, "NINE_ROUTER_MODEL": "cx/gpt-6.1-sol"},
    )
    assert cfg_rev.model_target.label == "reviewer"
    assert cfg_rev.model_target.model == "9router:ag/claude-sonnet-4-6"

    # Custom repo-dir requires --task
    with pytest.raises(p2.SpikeConfigError, match="--task"):
        p2.load_and_validate_phase2_config(["--repo-dir", str(tmp_path)], env=_ROLE_ENV)

    # Rejects missing repo-dir, invalid repo-url, --model + --role conflict, bad steps
    with pytest.raises(p2.SpikeConfigError, match="existing directory"):
        p2.load_and_validate_phase2_config(
            ["--repo-dir", str(tmp_path / "missing"), "--task", "fix bug"], env=_ROLE_ENV
        )
    with pytest.raises(p2.SpikeConfigError, match="repo-url"):
        p2.load_and_validate_phase2_config(
            ["--repo-url", "https://user:secret@github.com/org/repo.git", "--task", "fix"],
            env=_ROLE_ENV,
        )
    with pytest.raises(p2.SpikeConfigError, match="Choose only one"):
        p2.load_and_validate_phase2_config(["--model", "x", "--role", "dev"], env=_ROLE_ENV)


def test_phase2_preflight_only_and_skip_api_preflight(capsys: pytest.CaptureFixture[str]) -> None:
    mock_client = _make_mock_client()
    cfg = p2.load_and_validate_phase2_config(["--preflight-only", "--skip-api-preflight"], env=_ROLE_ENV)
    summary = p2.run_phase2_spike(cfg, client_factory=lambda **_: mock_client)  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert summary.agent_machine_id == ""
    mock_client.templates.list.assert_not_called()
    mock_client.machines.create.assert_not_called()
    mock_client.snapshots.create.assert_not_called()
    assert "API authentication was NOT verified" in captured.out


def _setup_full_lifecycle_mock(
    *,
    ns_bytes: bytes,
    modes_bytes: bytes,
    patch_bytes: bytes,
    tests_only_patch: bytes = b"diff --git tests/test_discount_regression.py",
    post_hashes: dict[str, str] | None = None,
    red_xml: bytes = _RED_JUNIT,
    post_xml: bytes = _POST_JUNIT,
    timeline: list[str] | None = None,
) -> MagicMock:
    client = _make_mock_client()
    client.templates.list.return_value = [Template.from_dict({"id": "default", "version": "1", "creatable": True})]

    vm_seq = [
        Machine.from_dict({"id": "m_agent", "name": "agent", "state": "running", "operationId": "op_agent"}),
        Machine.from_dict({"id": "m_verify", "name": "verify", "state": "running", "operationId": "op_verify"}),
    ]
    create_count = 0

    def fake_create(**kwargs: object) -> Machine:
        nonlocal create_count
        m = vm_seq[create_count]
        create_count += 1
        if timeline is not None:
            timeline.append(f"create:{m.id}:snap={kwargs.get('snapshot_id')}")
        return m

    client.machines.create.side_effect = fake_create
    client.machines.ensure_running.side_effect = lambda mid, **kw: Machine.from_dict(
        {"id": mid, "name": mid, "state": "running"}
    )

    def fake_delete(mid: str, **kw: object) -> MagicMock:
        if timeline is not None:
            timeline.append(f"delete_vm:{mid}")
        return MagicMock()

    client.machines.delete.side_effect = fake_delete

    snap_seq = [
        Snapshot.from_dict({"id": "snap_pre", "name": "n", "status": "pending", "sourceMachineId": "m_agent"}),
        Snapshot.from_dict({"id": "snap_pre", "name": "n", "status": "ready", "sourceMachineId": "m_agent"}),
    ]
    client.snapshots.create.return_value = snap_seq[0]
    client.snapshots.get.return_value = snap_seq[1]

    def fake_snap_delete(sid: str, **kw: object) -> None:
        if timeline is not None:
            timeline.append(f"delete_snap:{sid}")

    client.snapshots.delete.side_effect = fake_snap_delete

    storage: dict[tuple[str, str], bytes] = {}
    base_hashes = json.dumps({"pyproject.toml": "hash_orig"}).encode("utf-8")
    effective_post_hashes = json.dumps(post_hashes or {"pyproject.toml": "hash_orig"}).encode("utf-8")

    def fake_exec(machine: str, cmd: str | list[str], *, cwd: str | None = None, env: object = None, timeout: int = 60) -> ExecResult:
        if isinstance(cmd, list) and cmd[:2] == ["mkdir", "-p"]:
            return ExecResult("", "", 0, False, False, "")
        if isinstance(cmd, list) and cmd[:2] == ["python3", "-c"]:
            # Hash output path is embedded at end of one-liner
            m = re.search(r"'(/tmp/phase2_(base|post)_hashes_[^']+\.json)'", cmd[2])
            assert m is not None, f"Unexpected python3 hash script: {cmd[2]}"
            data = effective_post_hashes if m.group(2) == "post" else base_hashes
            storage[(machine, m.group(1))] = data
            return ExecResult("", "", 0, False, False, "")
        if cmd == 'readlink -f "$(command -v python3)"':
            return ExecResult("/usr/bin/python3\n", "", 0, False, False, "")
        if cmd == "git rev-parse HEAD":
            return ExecResult("deadbeef1234567890\n", "", 0, False, False, "")
        if cmd == "git ls-files -z":
            return ExecResult("pyproject.toml\x00src/mini_ledger/billing.py\x00tests/test_billing.py\x00", "", 0, False, False, "")
        junit_m = re.search(r"--junitxml=(/tmp/phase2_(base|red|post)_[^\s'\"]+\.xml)", cmd) if isinstance(cmd, str) else None
        if junit_m is not None:
            phase = junit_m.group(2)
            xml_map = {"base": (_BASE_JUNIT, 0), "red": (red_xml, 1), "post": (post_xml, 0)}
            xml_bytes, code = xml_map[phase]
            storage[(machine, junit_m.group(1))] = xml_bytes
            return ExecResult("", "", code, False, False, "")
        if isinstance(cmd, str) and "GIT_INDEX_FILE=" in cmd and "--name-status" in cmd:
            ns_m = re.search(r"(/tmp/phase2_ns_([^.\s'\"]+)\.bin)", cmd)
            assert ns_m is not None, f"Unexpected diff cmd: {cmd}"
            rid = ns_m.group(2)
            storage[(machine, f"/tmp/phase2_ns_{rid}.bin")] = ns_bytes
            storage[(machine, f"/tmp/phase2_raw_{rid}.bin")] = modes_bytes
            storage[(machine, f"/tmp/phase2_full_{rid}.patch")] = patch_bytes
            return ExecResult("", "", 0, False, False, "")
        if isinstance(cmd, str) and "GIT_INDEX_FILE=" in cmd and "phase2_tests_only_" in cmd:
            tp_m = re.search(r"(/tmp/phase2_tests_only_[^\s'\"]+\.patch)", cmd)
            assert tp_m is not None, f"Unexpected tests-only diff cmd: {cmd}"
            storage[(machine, tp_m.group(1))] = tests_only_patch
            return ExecResult("", "", 0, False, False, "")
        return ExecResult("", "", 0, False, False, "")

    client.machines.exec.side_effect = fake_exec
    client.files.write.side_effect = lambda m, p, d, **kw: (
        storage.__setitem__((m, p), bytes(d))
        or FileWritten(path=p, size=len(d), sha256="0" * 64, mode="0644", created=True)
    )
    client.files.read_bytes.side_effect = lambda m, p, **kw: storage[(m, p)]
    return client


def _patch_agent(monkeypatch: pytest.MonkeyPatch, captured_limits: list[int] | None = None) -> None:
    class _FakeAgent:
        def invoke(self, payload: object, config: dict[str, object] | None = None) -> dict[str, object]:
            if captured_limits is not None and config is not None:
                captured_limits.append(int(config["recursion_limit"]))  # type: ignore[arg-type]
            return {"messages": [MagicMock(content="fixed")]}

    monkeypatch.setattr("deepagents.create_deep_agent", lambda **kw: _FakeAgent())


def test_phase2_happy_path_snapshot_fresh_vm_and_cleanup_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    timeline: list[str] = []
    limits: list[int] = []
    mock_client = _setup_full_lifecycle_mock(
        ns_bytes=b"M\x00src/mini_ledger/billing.py\x00A\x00tests/test_discount_regression.py\x00",
        modes_bytes=(
            b":100644 100644 a b M\x00src/mini_ledger/billing.py\x00"
            b":000000 100644 0 c A\x00tests/test_discount_regression.py\x00"
        ),
        patch_bytes=b"diff --git a/src/mini_ledger/billing.py ...",
        timeline=timeline,
    )
    _patch_agent(monkeypatch, limits)
    monkeypatch.setattr(p2.time, "sleep", lambda _: None)
    monkeypatch.setattr(p2, "Client", lambda **_: mock_client)
    for k, v in _ROLE_ENV.items():
        monkeypatch.setenv(k, v)

    art_dir = tmp_path / "artifacts"
    cfg = p2.load_and_validate_phase2_config(["--artifact-dir", str(art_dir), "--max-agent-steps", "42"], env=_ROLE_ENV)
    summary = p2.run_phase2_spike(cfg, client_factory=lambda **_: mock_client, run_id="r1")

    assert summary.verification.verdict == "PASS"
    assert summary.pre_agent_snapshot_id == "snap_pre"
    assert summary.verify_machine_id == "m_verify"
    assert limits == [42]
    # 1 VM concurrent max: m_agent deleted BEFORE m_verify created, VMs deleted BEFORE snap_pre deleted
    assert timeline == [
        "create:m_agent:snap=None",
        "delete_vm:m_agent",
        "create:m_verify:snap=snap_pre",
        "delete_vm:m_verify",
        "delete_snap:snap_pre",
    ]
    assert (art_dir / "patch.diff").exists()
    assert (art_dir / "red_junit.xml").exists()
    assert (art_dir / "post_junit.xml").exists()
    report = json.loads((art_dir / "verification_report.json").read_text(encoding="utf-8"))
    assert report["verification"]["verdict"] == "PASS"


def test_phase2_static_gate_rejects_and_skips_verify_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[str] = []
    mock_client = _setup_full_lifecycle_mock(
        ns_bytes=b"M\x00pyproject.toml\x00A\x00tests/test_discount_regression.py\x00",
        modes_bytes=b":100644 100644 a b M\x00pyproject.toml\x00",
        patch_bytes=b"diff --git a/pyproject.toml ...",
        post_hashes={"pyproject.toml": "hash_modified"},
        timeline=timeline,
    )
    _patch_agent(monkeypatch)
    monkeypatch.setattr(p2.time, "sleep", lambda _: None)
    monkeypatch.setattr(p2, "Client", lambda **_: mock_client)
    for k, v in _ROLE_ENV.items():
        monkeypatch.setenv(k, v)

    exit_code = p2.main([])
    # Only agent VM created (no verify VM spun up), and both VM + snapshot cleaned up
    assert exit_code == 3
    assert mock_client.machines.create.call_count == 1
    assert mock_client.machines.delete.call_count == 1
    assert mock_client.snapshots.delete.call_count == 1


def test_phase2_existing_test_edit_returns_needs_human_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_client = _setup_full_lifecycle_mock(
        ns_bytes=b"M\x00src/mini_ledger/billing.py\x00M\x00tests/test_billing.py\x00A\x00tests/test_discount_regression.py\x00",
        modes_bytes=b"",
        patch_bytes=b"diff --git ...",
    )
    _patch_agent(monkeypatch)
    monkeypatch.setattr(p2.time, "sleep", lambda _: None)
    monkeypatch.setattr(p2, "Client", lambda **_: mock_client)
    for k, v in _ROLE_ENV.items():
        monkeypatch.setenv(k, v)

    exit_code = p2.main([])
    assert exit_code == 4


def test_phase2_restore_mode_and_tdd_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_client = _setup_full_lifecycle_mock(
        ns_bytes=b"M\x00src/mini_ledger/billing.py\x00",
        modes_bytes=b":100644 100644 a b M\x00src/mini_ledger/billing.py\x00",
        patch_bytes=b"diff --git ...",
        post_xml=_BASE_JUNIT,
    )
    _patch_agent(monkeypatch)
    monkeypatch.setattr(p2.time, "sleep", lambda _: None)

    cfg = p2.load_and_validate_phase2_config(["--verify-mode", "restore", "--tdd-exempt"], env=_ROLE_ENV)
    summary = p2.run_phase2_spike(cfg, client_factory=lambda **_: mock_client, run_id="r1")

    assert summary.verification.verdict == "PASS"
    mock_client.machines.create.assert_called_once()
    mock_client.machines.restore.assert_called_once_with("m_agent", "snap_pre", wait=True, timeout=600.0)


def test_phase2_snapshot_recovery_and_cleanup_failure_exit_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1. Ambiguous snapshot create error still recovers snapshot ID and cleans up both VM + snapshot
    mock_client1 = _setup_full_lifecycle_mock(ns_bytes=b"", modes_bytes=b"", patch_bytes=b"")
    mock_client1.snapshots.create.side_effect = APIConnectionError("snap timeout")
    mock_client1.snapshots.list.return_value = [
        Snapshot.from_dict(
            {
                "id": "snap_recovered",
                "name": "spike01-p2-pre-rec1",
                "status": "pending",
                "sourceMachineId": "m_agent",
            }
        )
    ]
    cfg1 = p2.load_and_validate_phase2_config([], env=_ROLE_ENV)
    with pytest.raises(APIConnectionError):
        p2.run_phase2_spike(cfg1, client_factory=lambda **_: mock_client1, run_id="rec1")
    mock_client1.machines.delete.assert_called_once()
    mock_client1.snapshots.delete.assert_called_once_with("snap_recovered", confirm="snap_recovered")

    # 2. Snapshot deletion failure returns exit code 1 with retained snapshot ID
    mock_client2 = _setup_full_lifecycle_mock(
        ns_bytes=b"M\x00src/mini_ledger/billing.py\x00A\x00tests/test_discount_regression.py\x00",
        modes_bytes=b"",
        patch_bytes=b"diff --git ...",
    )
    mock_client2.snapshots.delete.side_effect = ServerError("delete failed", code="internal", status=500)
    _patch_agent(monkeypatch)
    monkeypatch.setattr(p2.time, "sleep", lambda _: None)
    monkeypatch.setattr(p2, "Client", lambda **_: mock_client2)
    for k, v in _ROLE_ENV.items():
        monkeypatch.setenv(k, v)

    code = p2.main([])
    captured = capsys.readouterr()
    assert code == 1
    assert "snap_pre" in captured.err
    assert "CLEANUP INCOMPLETE" in captured.err
