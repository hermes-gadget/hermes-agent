"""Reserved channel credentials isolate inference, retries, concurrent workers and auxiliary routes."""

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from agent.channel_credentials import ChannelCredentialUnavailable, auxiliary_route, guard_auxiliary_request, worker_credentials
from gateway.channel_delegation import credential_coordinator_for_source, delegation_for_source, scoped_source_credentials
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run_config_loaders import GatewayConfigLoadersMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource
from tests.fakes.fake_llm_provider import FakeLLMServer
from tools.delegation_scope import scoped_channel_delegation


MODEL = "longcat-2.5-preview-free"
LABELS = ["main", "worker-a", "worker-b", "worker-c"]


def _setup(tmp_path, monkeypatch, base_url, name="profile"):
    home = tmp_path / name
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = {
        "model": {"default": "paid-default", "provider": "missing-paid-provider", "context_length": 128000},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "delegation": {"model": "paid-worker", "provider": "missing-paid-provider", "max_iterations": 3},
        "auxiliary": {"compression": {"provider": "openai-codex", "model": "paid-aux"}},
    }
    with (home / "config.yaml").open("w") as stream:
        YAML().dump(cfg, stream)
    rows = [{"id": label, "label": label, "auth_type": "api_key", "source": "manual",
             "priority": i, "access_token": f"{name}-{label}", "base_url": base_url,
             "last_status": "exhausted", "last_status_at": time.time(), "last_error_code": 429,
             "last_error_reason": "GoUsageLimitError", "last_error_reset_at": time.time() + 86400}
            for i, label in enumerate(LABELS)]
    # Legacy pool writers sometimes kept the typed error only in its body.
    rows[1].update(last_error_reason=None, last_error_message="GoUsageLimitError: Go usage limit exceeded")
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"opencode-go": rows}}))
    platform = PlatformConfig.from_dict({"channel_overrides": {"experimental": {
        "model": MODEL, "provider": "opencode-go", "fallback_providers": [],
        "credentials": {"main": LABELS[0], "subagents": LABELS[1:], "allow_exhausted_free_models": True},
        "delegation": {"enabled": True, "model": MODEL, "provider": "opencode-go",
                       "max_concurrent_children": len(LABELS) - 1, "max_spawn_depth": 1},
    }}})
    platform = PlatformConfig.from_dict(platform.to_dict())
    source = SessionSource(platform=Platform.DISCORD, chat_id="thread", parent_chat_id="experimental", user_id="user")
    return home, cfg, GatewayConfig(platforms={Platform.DISCORD: platform}), source


def test_channel_real_agents_use_distinct_keys_and_cannot_fall_back(tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    from agent.auxiliary_client import _resolve_task_provider_model, _try_configured_fallback_chain
    from run_agent import AIAgent
    from tools.delegate_tool import delegate_task

    with FakeLLMServer(api_key=[f"profile-{label}" for label in LABELS]) as host:
        home, cfg, config, source = _setup(tmp_path, monkeypatch, host.base_url)
        runner = SimpleNamespace(config=config, _resolve_session_key_or_none=lambda *args: None)
        with scoped_source_credentials(config, source), scoped_channel_delegation(delegation_for_source(config, source)):
            model, runtime = GatewayTurnMixin._resolve_session_agent_runtime(runner, source=source, user_config=cfg)
            parent = AIAgent(
                model=model, **runtime, enabled_toolsets=["delegation"], quiet_mode=True,
                skip_context_files=True, skip_memory=True, max_iterations=3,
                platform="discord", chat_id=source.chat_id,
                fallback_model=[{"provider": "missing-paid-provider", "model": "paid-backup"}],
            )
            try:
                assert parent._fallback_chain == []
                parent.run_conversation(user_message="Return a short answer.")
                result = json.loads(delegate_task(tasks=[{"goal": f"Return a short answer for {label}."} for label in LABELS[1:]],
                                                  background=False, parent_agent=parent))
                assert "results" in result, result
                assert all(r["status"] == "completed" for r in result["results"]), result
                requests = [r for r in host.requests if r.get("body")]
                assert {r["body"]["model"] for r in requests} == {MODEL}
                assert {r["auth"] for r in requests} == {f"Bearer profile-{label}" for label in LABELS}
                assert len(requests) == len(LABELS)
                assert parent.api_key == "profile-main"
                assert not parent._credential_pool.coordinator.busy()
                GatewayConfigLoadersMixin._apply_fallback_chain_to_agent(
                    parent, [{"provider": "missing-paid-provider", "model": "paid-backup"}])
                assert parent._fallback_chain == []
                sibling = next(e for e in load_pool("opencode-go").entries() if e.label == "worker-a")
                assert parent._swap_credential(sibling) is False
                assert parent._try_refresh_env_client_credentials() is False
                assert _resolve_task_provider_model("compression")[:2] == ("opencode-go", "deepseek-v4.1-flash")
                assert _try_configured_fallback_chain("compression", "opencode-go") == (None, None, "")
                allowed = SimpleNamespace(base_url="https://opencode.ai/zen/go/v1")
                guard_auxiliary_request(allowed, {"model": "deepseek-v4.1-flash"})
                with pytest.raises(ValueError, match="only"):
                    guard_auxiliary_request(allowed, {"model": "paid-backup"})
                recovered, _retried = parent._recover_with_credential_pool(
                    status_code=429, has_retried_429=True, error_context={"message": "Endpoint unavailable"})
                assert recovered is False
                assert parent._credential_pool.select(model=MODEL) is None
                assert parent.api_key == "profile-main"
                with pytest.raises(ChannelCredentialUnavailable, match="No fallback"):
                    parent._credential_pool.runtime()
            finally:
                parent.close()
        assert auxiliary_route() is None
        assert _resolve_task_provider_model("compression")[:2] == ("openai-codex", "paid-aux")
        assert credential_coordinator_for_source(config, SessionSource(platform=Platform.DISCORD, chat_id="ordinary")) is None
        assert not load_pool("opencode-go").has_available(model="deepseek-v4.1-flash")
        rows = json.loads((home / "auth.json").read_text())["credential_pool"]["opencode-go"]
        assert all(r.get("last_error_reason") == "GoUsageLimitError"
                   or "GoUsageLimitError" in r.get("last_error_message", "") for r in rows)


def test_channel_reservations_queue_cancel_and_wait_for_deferred_exit_without_profile_leaks(tmp_path, monkeypatch):
    home_a, _, config, source = _setup(tmp_path, monkeypatch, "https://opencode.ai/zen/go/v1", "a")
    coordinator = credential_coordinator_for_source(config, source)
    home_b, _, config_b, source_b = _setup(tmp_path, monkeypatch, "https://opencode.ai/zen/go/v1", "b")
    other = credential_coordinator_for_source(config_b, source_b)
    assert other is not coordinator
    assert other.main_pool().current().runtime_api_key == "b-main"
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    assert credential_coordinator_for_source(config, source) is coordinator
    assert coordinator.main_pool().current().runtime_api_key == "a-main"

    def child():
        obj = SimpleNamespace(_credential_pool=coordinator.worker_pool(), _interrupt_requested=False)
        obj._swap_credential = lambda entry: setattr(obj, "key_id", entry.id)
        return obj

    children = [child() for _ in coordinator.workers]
    scopes = [worker_credentials(obj) for obj in children]
    contexts = [copy_context() for _ in scopes]
    for context, scope in zip(contexts, scopes):
        context.run(scope.__enter__)
    held = {obj.key_id for obj in children}
    assert len(held) == len(coordinator.workers) and "main" not in held
    queued, cancelled = child(), child()
    entered, release = Event(), Event()

    def wait_worker(obj, announce=False):
        with worker_credentials(obj):
            if announce:
                entered.set()
                assert release.wait(20)

    future = Future()
    children[0]._channel_credential_future = future
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        waiting = executor.submit(wait_worker, queued, True)
        stopping = executor.submit(wait_worker, cancelled)
        # Cancellation must not borrow the parent or an occupied key.
        cancelled._interrupt_requested = True
        with pytest.raises(ValueError, match="stopped"):
            stopping.result(timeout=10)
        contexts[0].run(scopes[0].__exit__, None, None, None)
        with coordinator.condition:
            assert coordinator.leases == held
            assert queued._credential_pool.label is None
        future.set_result(None)
        assert entered.wait(10)
        assert queued.key_id == children[0].key_id
        release.set()
        waiting.result(timeout=10)
    finally:
        release.set()
        if not future.done():
            future.set_result(None)
        queued._interrupt_requested = cancelled._interrupt_requested = True
        for context, scope in zip(contexts[1:], scopes[1:]):
            context.run(scope.__exit__, None, None, None)
        executor.shutdown(wait=True)
    assert not coordinator.busy()
