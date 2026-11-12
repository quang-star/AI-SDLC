"""Multi-model selection, routing validation and independent smoke execution."""

import json
from unittest.mock import MagicMock

import pytest

from model_routing import normalize_model, select_model_targets
import run_live_vm_spike as spike


ENV = {"INSTABOX_API_KEY": "test-vm-key", "NINE_ROUTER_API_KEY": "test-router-key"}
ROLE_MODELS = {"ba": "cc/ba-model", "dev": "cx/dev-model", "reviewer": "review-model", "tester": "test-model"}


def test_bare_ids_default_to_router_and_explicit_providers_still_work() -> None:
    assert normalize_model(" cc/example ") == "9router:cc/example"
    assert normalize_model("gpt-example") == "9router:gpt-example"
    assert normalize_model("anthropic:claude-example") == "anthropic:claude-example"


@pytest.mark.parametrize("value", ["", " ", None, 12, "9router:", ":model", "cc/model with space"])
def test_invalid_model_ids(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_model(value)  # type: ignore[arg-type]


def test_default_model_env_is_router_not_an_upstream_provider() -> None:
    cfg = spike.load_and_validate_config(["--with-agent"], env={**ENV, "NINE_ROUTER_MODEL": "cc/default"})
    assert cfg.model == "9router:cc/default"
    assert cfg.router_api_key == ENV["NINE_ROUTER_API_KEY"]


def test_multi_model_cli_overrides_env() -> None:
    cfg = spike.load_and_validate_config(
        ["--with-agent", "--models", "cc/model-a", "9router:cx/model-b"],
        env={**ENV, "NINE_ROUTER_MODEL": "ignored"},
    )
    assert [target.model for target in cfg.model_targets] == ["9router:cc/model-a", "9router:cx/model-b"]


def test_role_selection_and_order() -> None:
    env = {**ENV, "NINE_ROUTER_ROLE_MODELS": json.dumps(ROLE_MODELS)}
    cfg = spike.load_and_validate_config(["--with-agent"], env=env)
    assert [target.label for target in cfg.model_targets] == list(ROLE_MODELS)
    selected = spike.load_and_validate_config(["--with-agent", "--roles", "reviewer", "dev"], env={**env, "NINE_ROUTER_MODEL": "ignored"})
    assert [target.label for target in selected.model_targets] == ["reviewer", "dev"]
    assert [target.model for target in selected.model_targets] == ["9router:review-model", "9router:cx/dev-model"]


@pytest.mark.parametrize("args", [
    ["--with-agent", "--model", "a", "--models", "b"],
    ["--with-agent", "--model", "a", "--roles", "dev"],
    ["--with-agent", "--models", "a", "--roles", "dev"],
    ["--model", "a"],
])
def test_conflicting_or_unused_flags_fail_before_vm(args: list[str]) -> None:
    with pytest.raises(spike.SpikeConfigError):
        spike.load_and_validate_config(args, env=ENV)


@pytest.mark.parametrize("role_config", ["not json", "[]", "{}", '{"ba":null}', '{"ba":12}', '{"ba":""}', '{"../dev":"model"}'])
def test_invalid_role_map(role_config: str) -> None:
    with pytest.raises(spike.SpikeConfigError):
        spike.load_and_validate_config(["--with-agent"], env={**ENV, "NINE_ROUTER_ROLE_MODELS": role_config})


@pytest.mark.parametrize("roles", [["missing"], ["dev", "dev"]])
def test_missing_or_repeated_roles_rejected(roles: list[str]) -> None:
    with pytest.raises(ValueError):
        select_model_targets(model=None, models=None, roles=roles, env={"NINE_ROUTER_ROLE_MODELS": json.dumps(ROLE_MODELS)})


def test_all_targets_validated_before_vm_creation() -> None:
    with pytest.raises(spike.SpikeConfigError, match="ANTHROPIC_API_KEY"):
        spike.load_and_validate_config(["--with-agent", "--models", "cc/good", "anthropic:claude-example"], env=ENV)
    with pytest.raises(spike.SpikeConfigError, match="NINE_ROUTER_MODEL"):
        spike.load_and_validate_config(["--with-agent"], env=ENV)


@pytest.mark.parametrize("fail_first", [False, True])
def test_every_model_attempted_with_unique_file_and_shared_vm_cleanup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fail_first: bool,
) -> None:
    cfg = spike.load_and_validate_config(["--with-agent", "--models", "cc/first", "cx/second"], env=ENV)
    client = MagicMock()
    client.machines.create.return_value = MagicMock(id="m_created", operation_id=None)
    sandbox = MagicMock(id="m_created")
    monkeypatch.setattr(spike, "_preflight_api", lambda *a: "verified")
    monkeypatch.setattr(spike.InstaBoxSandbox, "create_or_reconnect", lambda *a, **k: sandbox)
    monkeypatch.setattr(spike, "_run_sandbox_smoke", lambda *a: None)
    attempts: list[tuple[str, str]] = []

    def smoke(sb: object, model: str, *, run_id: str, **kwargs: str) -> None:
        assert sb is sandbox
        assert kwargs["router_api_key"] == ENV["NINE_ROUTER_API_KEY"]
        attempts.append((model, run_id))
        if fail_first and len(attempts) == 1:
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(spike, "_run_agent_smoke", smoke)
    if fail_first:
        with pytest.raises(RuntimeError, match="model_1"):
            spike.run_spike(cfg, client_factory=lambda **k: client, run_id="batch")
    else:
        assert spike.run_spike(cfg, client_factory=lambda **k: client, run_id="batch") == "m_created"
    assert attempts == [("9router:cc/first", "batch-1"), ("9router:cx/second", "batch-2")]
    client.machines.create.assert_called_once()
    client.machines.delete.assert_called_once_with("m_created", confirm="m_created", wait=True, timeout=600.0)
    client.close.assert_called_once()
    captured = capsys.readouterr()
    assert "PASS model_2" in captured.out
    if fail_first:
        assert "FAIL model_1" in captured.err
