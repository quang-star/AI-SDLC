"""9Router configuration and OpenAI wire-format tests; no external requests."""

import json
from unittest.mock import MagicMock

import httpx
import langchain_openai
import pytest

import run_live_vm_spike as spike


def test_router_config_does_not_require_upstream_provider_keys() -> None:
    cfg = spike.load_and_validate_config(
        ["--with-agent", "--model", "9router:cc/example-model"],
        env={"INSTABOX_API_KEY": "ibk_test", "NINE_ROUTER_API_KEY": "router-test-key"},
    )
    assert cfg.router_base_url == "http://localhost:20128/v1"
    assert cfg.router_api_key == "router-test-key"
    assert cfg.model == "9router:cc/example-model"
    assert "router-test-key" not in repr(cfg)


def test_router_config_standard_openai_env_and_override_precedence() -> None:
    env = {
        "INSTABOX_API_KEY": "ibk_test",
        "OPENAI_API_KEY": "fallback-key",
        "OPENAI_BASE_URL": "http://localhost:9000/v1",
    }
    args = ["--with-agent", "--model", "9router:custom/combo"]
    cfg = spike.load_and_validate_config(args, env=env)
    assert cfg.router_base_url == "http://localhost:9000/v1"
    assert cfg.router_api_key == "fallback-key"
    cfg = spike.load_and_validate_config(args, env={
        **env,
        "NINE_ROUTER_BASE_URL": "http://localhost:20128/v1/",
        "NINE_ROUTER_API_KEY": "dedicated-key",
    })
    assert cfg.router_base_url == "http://localhost:20128/v1"
    assert cfg.router_api_key == "dedicated-key"


@pytest.mark.parametrize("url", ["ftp://localhost/v1", "   ", "http://", "http://localhost:bad/v1", "http://user:secret@localhost/v1", "http://localhost/v1?key=secret"])
def test_bad_router_url_rejected_without_echoing_credentials(url: str) -> None:
    with pytest.raises(spike.SpikeConfigError) as info:
        spike.load_and_validate_config(
            ["--with-agent", "--model", "9router:cc/model"],
            env={"INSTABOX_API_KEY": "ibk_test", "NINE_ROUTER_API_KEY": "test-key", "NINE_ROUTER_BASE_URL": url},
        )
    assert "BASE_URL" in str(info.value)
    assert "secret" not in str(info.value)


def test_missing_router_key_and_model_rejected() -> None:
    with pytest.raises(spike.SpikeConfigError, match="NINE_ROUTER_API_KEY"):
        spike.load_and_validate_config(["--with-agent", "--model", "9router:cc/model"], env={"INSTABOX_API_KEY": "ibk_test"})
    with pytest.raises(spike.SpikeConfigError, match="model ID"):
        spike.load_and_validate_config(["--with-agent", "--model", "9router:"], env={"INSTABOX_API_KEY": "ibk_test", "NINE_ROUTER_API_KEY": "test-key"})


def test_missing_router_package_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spike.importlib.util, "find_spec", lambda _: None)
    with pytest.raises(spike.SpikeConfigError, match="langchain-openai"):
        spike.load_and_validate_config(["--with-agent", "--model", "9router:cc/model"], env={"INSTABOX_API_KEY": "ibk_test", "NINE_ROUTER_API_KEY": "test-key"})


def test_router_chat_completions_and_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the installed client, including tool schema/response handling."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "cc/example-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_test", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"path":"sample.txt"}',
                }}],
            }}],
        })

    real_class = langchain_openai.ChatOpenAI
    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kw: real_class(**kw, http_client=http_client))
        model = spike._build_router_model("9router:cc/example-model", "http://localhost:20128/v1", "test-router-key")
        from deepagents import create_deep_agent

        # Ensure Deep Agents accepts the configured instance without resolving it again.
        assert create_deep_agent(model=model) is not None
        result = model.bind_tools([{
            "type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {
                "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
            }},
        }]).invoke("Read sample.txt")
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://localhost:20128/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-router-key"
    body = json.loads(request.content)
    assert body["model"] == "cc/example-model"
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert result.tool_calls[0]["args"] == {"path": "sample.txt"}


def test_runner_passes_router_config_to_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = spike.load_and_validate_config(
        ["--with-agent", "--machine-id", "m_existing", "--model", "9router:cc/model"],
        env={"INSTABOX_API_KEY": "ibk_test", "NINE_ROUTER_API_KEY": "test-key"},
    )
    client = MagicMock()
    sandbox = MagicMock(id="m_existing")
    monkeypatch.setattr(spike.InstaBoxSandbox, "create_or_reconnect", lambda *a, **k: sandbox)
    monkeypatch.setattr(spike, "_run_sandbox_smoke", lambda *a: None)
    agent = MagicMock()
    monkeypatch.setattr(spike, "_run_agent_smoke", agent)
    spike.run_spike(cfg, client_factory=lambda **k: client, run_id="test-run")
    agent.assert_called_once_with(sandbox, "9router:cc/model", run_id="test-run", router_base_url="http://localhost:20128/v1", router_api_key="test-key")
