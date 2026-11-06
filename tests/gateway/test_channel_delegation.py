"""Channel delegation routes and limits must survive workers without leaking to other chats."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

from ruamel.yaml import YAML

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall


def _home(tmp_path, monkeypatch, delegation, providers=None):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = {
        "model": {"default": "parent-model", "context_length": 128000},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "delegation": {"max_concurrent_children": 4, "max_spawn_depth": 1,
                       "max_iterations": 3, **delegation},
        "providers": providers or {},
    }
    with (home / "config.yaml").open("w") as stream:
        YAML().dump(config, stream)
    platform = PlatformConfig.from_dict({"channel_overrides": {"experimental": {
        "delegation": {"model": "channel-worker", "provider": "channel-host",
                       "max_concurrent_children": 8},
    }}})
    # Exercise the persisted schema as well as the runtime object.
    platform = PlatformConfig.from_dict(platform.to_dict())
    return GatewayConfig(platforms={Platform.DISCORD: platform})


def _turn(config, source, probe):
    class ProbeTurn(TurnRunner):
        def _run_sync_scoped(self):
            return probe()

    runner = object.__new__(GatewayRunner)
    runner.config = config
    return ProbeTurn(runner, SimpleNamespace(source=source, session_id=source.chat_id)).run_sync()


def _task_description():
    from model_tools import get_tool_definitions

    tools = get_tool_definitions(["delegation"], quiet_mode=True, skip_tool_search_assembly=True)
    tool = next(t["function"] for t in tools if t["function"]["name"] == "delegate_task")
    return tool["parameters"]["properties"]["tasks"]["description"]


def test_channel_turn_sends_native_children_to_its_model_and_accepts_larger_batch(tmp_path, monkeypatch):
    """Real agent/tool/child/HTTP paths; only inference is served by loopback fixtures."""
    with FakeLLMServer(api_key="global-test-key") as global_host, \
         FakeLLMServer(api_key="channel-test-key") as channel_host:
        monkeypatch.setenv("CHANNEL_TEST_KEY", "channel-test-key")
        config = _home(tmp_path, monkeypatch, {
            "model": "global-worker", "provider": "global-host",
            "base_url": global_host.base_url, "api_key": "global-test-key",
        }, {"channel-host": {"base_url": channel_host.base_url, "key_env": "CHANNEL_TEST_KEY"}})

        def run(source, count):
            tasks = [{"goal": f"Return worker {n}", "context": "local fixture"} for n in range(count)]
            with FakeLLMServer([ToolCall("delegate_task", {"tasks": tasks}), Text("joined")],
                               api_key="parent-test-key") as parent_host:
                def probe():
                    from run_agent import AIAgent

                    parent = AIAgent(
                        model="parent-model", provider="custom", base_url=parent_host.base_url,
                        api_key="parent-test-key", api_mode="chat_completions", max_iterations=4,
                        enabled_toolsets=["delegation"], quiet_mode=True, skip_context_files=True,
                        skip_memory=True, platform="discord", chat_id=source.chat_id,
                    )
                    try:
                        return parent.run_conversation(user_message="Delegate these tasks and join their answers.")
                    finally:
                        parent.close()

                result = _turn(config, source, probe)
                tools = parent_host.main_requests()[0]["tools"]
                tool = next(t["function"] for t in tools if t["function"]["name"] == "delegate_task")
                return result, tool["parameters"]["properties"]["tasks"]["description"]

        experimental = SessionSource(platform=Platform.DISCORD, chat_id="experimental", user_id="user")
        ordinary = SessionSource(platform=Platform.DISCORD, chat_id="ordinary", user_id="user")
        result, description = run(experimental, 5)
        assert result["final_response"] == "joined"
        assert "up to 8 in parallel" in description
        requests = [r for r in channel_host.requests if r["body"]]
        assert len(requests) == 5
        assert {r["body"]["model"] for r in requests} == {"channel-worker"}
        assert {r["auth"] for r in requests} == {"Bearer channel-test-key"}
        assert not global_host.requests

        result, description = run(ordinary, 1)
        assert result["final_response"] == "joined"
        assert "up to 4 in parallel" in description
        assert {r["body"]["model"] for r in global_host.requests if r["body"]} == {"global-worker"}
        assert len(channel_host.requests) == 5


def test_channel_scope_isolated_between_concurrent_turns_and_retained_by_detached_context(tmp_path, monkeypatch):
    config = _home(tmp_path, monkeypatch, {"model": "global-worker", "provider": "global-host"})
    barrier = Barrier(2)

    def settings():
        from tools.delegate_tool_config import _get_max_concurrent_children, _load_config

        cfg = _load_config()
        return cfg["model"], cfg["provider"], _get_max_concurrent_children(), _task_description()

    def run(source):
        def probe():
            barrier.wait(timeout=20)
            return settings(), copy_context()

        return _turn(config, source, probe)

    sources = [
        SessionSource(platform=Platform.DISCORD, chat_id="thread", parent_chat_id="experimental", user_id="user"),
        SessionSource(platform=Platform.DISCORD, chat_id="ordinary", user_id="user"),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        experimental, ordinary = list(executor.map(run, sources))
    assert experimental[0][:3] == ("channel-worker", "channel-host", 8)
    assert ordinary[0][:3] == ("global-worker", "global-host", 4)
    assert "up to 8 in parallel" in experimental[0][3]
    assert "up to 4 in parallel" in ordinary[0][3]
    assert settings()[:3] == ("global-worker", "global-host", 4)
    # Background and nested workers retain the originating turn after it has returned.
    assert experimental[1].run(settings)[:3] == ("channel-worker", "channel-host", 8)
    assert settings()[:3] == ("global-worker", "global-host", 4)
