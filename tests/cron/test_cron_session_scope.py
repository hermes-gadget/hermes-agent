"""Backlog #228: scheduled jobs must not inherit interactive standing authority."""

from __future__ import annotations

import json
import logging
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hermes_yaml as yaml

from agent.secret_scope import (
    reset_secret_scope, set_multiplex_active, set_secret_scope,
)
from agent.system_prompt import build_system_prompt, invalidate_system_prompt
from cron.scheduler import _construct_cron_agent, _CronAgentSetup
from cron.scheduler_provider import _profile_cron_scope
from hermes_cli import plugins
from run_agent import AIAgent
from tools.delegate_tool import delegate_task


@pytest.fixture(autouse=True)
def offline_transport(monkeypatch):
    # Only the remote model transport is stubbed; home/config/plugin/tool/prompt paths are real.
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", MagicMock())


def _agent(platform="cli", skip_context_files=True, **kwargs):
    return AIAgent(
        api_key="offline-test-key", base_url="https://example.invalid/v1",
        provider="openrouter", api_mode="chat_completions", model="test/model",
        platform=platform, enabled_toolsets=["memory", "delegation", "file", "skills"],
        quiet_mode=True, skip_context_files=skip_context_files, skip_background_review=True,
        **kwargs,
    )


def _home(home, label):
    memories = home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(f"{label}: MissionDeck spawn authority granted.\n")
    (memories / "USER.md").write_text(f"{label}: investigate unrelated problems autonomously.\n")
    (home / "SOUL.md").write_text(f"{label}: persona standing authority.\n")
    plugin = home / "plugins" / "scope-standing"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: scope-standing\nversion: '1.0.0'\n")
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    ctx.register_system_prompt_section('scope.standing', {label + ': Full Auto Mode standing block'!r})\n"
    )
    provider = home / "plugins" / "scope-memory"
    provider.mkdir(parents=True)
    (provider / "plugin.yaml").write_text("name: scope-memory\nkind: exclusive\n")
    (provider / "__init__.py").write_text(
        "from agent.memory_provider import MemoryProvider\n"
        "class StandingMemory(MemoryProvider):\n"
        "    name = 'scope-memory'\n"
        "    def is_available(self): return True\n"
        "    def initialize(self, *args, **kwargs): pass\n"
        "    def get_tool_schemas(self): return []\n"
        f"    def system_prompt_block(self): return {label + ': provider standing authority'!r}\n"
        "def register(ctx):\n"
        "    ctx.register_memory_provider(StandingMemory())\n"
    )
    skill = home / "skills" / "standing-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: standing-skill\ndescription: Standing workflow\n---\n"
        f"{label}: automatically loaded standing authority.\n"
    )
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["scope-standing"]},
        "memory": {"provider": "scope-memory"},
        "model": {"context_length": 64000},
        "agent": {"environment_probe": False},
        "skills": {"auto_load": ["standing-skill"]},
        "tools": {"tool_search": {"enabled": "off"}},
    }))


def test_cron_prompt_withholds_standing_authority_while_interactive_keeps_it(
    tmp_path, monkeypatch, caplog,
):
    """Real scheduler -> AIAgent -> plugin/memory/prompt assembly, two homes A→B→A."""
    homes = [tmp_path / "a", tmp_path / "b"]
    for home, label in zip(homes, ("scope-A", "scope-B")):
        _home(home, label)
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("Job workspace: the queue is stored in queue.json.\n")
    monkeypatch.setattr(plugins, "_plugin_manager", plugins.PluginManager())
    caplog.set_level(logging.INFO)
    set_multiplex_active(True)
    try:
        for home, label, other in ((homes[0], "scope-A", "scope-B"),
                                   (homes[1], "scope-B", "scope-A"),
                                   (homes[0], "scope-A", "scope-B")):
            with _profile_cron_scope(home):
                secret_token = set_secret_scope({}, profile_home=home)
                try:
                    # Interactive still receives each ambient source, with no cross-profile leakage.
                    interactive = _agent(load_soul_identity=True, skip_context_files=False)
                    interactive_prompt = build_system_prompt(interactive)
                    for text in ("Full Auto Mode standing block", "MissionDeck spawn authority granted",
                                 "persona standing authority", "automatically loaded standing authority",
                                 "provider standing authority"):
                        assert f"{label}: {text}" in interactive_prompt
                    assert other not in interactive_prompt
                    assert "delegate_task" in interactive.valid_tool_names

                    setup = _CronAgentSetup(
                        model="test/model", max_iterations=2,
                        runtime={"api_key": "offline-test-key", "base_url": "https://example.invalid/v1",
                                 "provider": "openrouter", "api_mode": "chat_completions"},
                        prefill_messages=[{"role": "system", "content": "inherited prefill authority"}],
                    )
                    cron = _construct_cron_agent(
                        partial(AIAgent, ephemeral_system_prompt="inherited ephemeral authority"),
                        {"enabled_toolsets": ["memory", "delegation", "file", "skills"]},
                        {}, setup, workdir=tmp_path, session_id="cron_scope_job", session_db=None,
                    )
                    prompt = build_system_prompt(cron, "Declared job: report the queue length.")
                    assert "Full Auto Mode standing block" not in prompt
                    assert "MissionDeck spawn authority granted" not in prompt
                    assert "persona standing authority" not in prompt
                    assert "automatically loaded standing authority" not in prompt
                    assert "investigate unrelated problems autonomously" not in prompt
                    assert "provider standing authority" not in prompt
                    assert label not in prompt and other not in prompt
                    assert "Declared job: report the queue length." in prompt
                    assert "Job workspace: the queue is stored in queue.json." in prompt
                    assert "only the declared task" in prompt
                    assert not cron.prefill_messages
                    assert cron.ephemeral_system_prompt is None
                    assert cron.session_scope == "cron"
                    assert cron._session_init_model_config["session_scope"] == "cron"
                    assert "delegate_task" not in cron.valid_tool_names
                    assert "memory" in cron.valid_tool_names
                    assert cron._memory_store is not None  # memory writes remain available (#91447)
                    assert cron._memory_manager is None  # no automatic provider recall of standing authority
                    assert build_system_prompt(cron, "Declared job: report the queue length.") == prompt
                    invalidate_system_prompt(cron)
                    assert build_system_prompt(cron, "Declared job: report the queue length.") == prompt
                    interactive.close()
                    cron.close()
                finally:
                    reset_secret_scope(secret_token)
    finally:
        set_multiplex_active(False)
    assert "cron scope: standing instructions withheld" in caplog.text


def test_cron_spawn_is_refused_before_dispatch(tmp_path, monkeypatch):
    """A direct handler call and both agent executors must refuse without constructing a child."""
    monkeypatch.chdir(tmp_path)
    from hermes_constants import get_hermes_home
    from tools.registry import registry
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({
        "agent": {"environment_probe": False}, "model": {"context_length": 64000},
    }))
    cron = _agent(platform="cron")

    def unexpected_spawn(*args, **kwargs):
        raise AssertionError("cron attempted to construct a delegated child")

    monkeypatch.setattr("tools.delegate_tool._build_children", unexpected_spawn)
    for kwargs in ({"goal": "Investigate an unrelated watcher bug"},
                   {"tasks": [{"goal": "Spawn a repair session"}]},
                   {"goal": "Background repair", "background": True}):
        result = json.loads(delegate_task(parent_agent=cron, **kwargs))
        assert "cron scope" in result["error"].lower()
        assert "delegation" in result["error"].lower()
        dispatched = json.loads(registry.dispatch("delegate_task", kwargs, parent_agent=cron))
        assert "cron scope" in dispatched["error"].lower()

    for concurrent in (False, True):
        call = SimpleNamespace(
            id=f"scope-spawn-{concurrent}", type="function",
            function=SimpleNamespace(name="delegate_task", arguments=json.dumps({"goal": "Repair the watcher"})),
        )
        message = SimpleNamespace(tool_calls=[call])
        messages = [{"role": "assistant", "content": None, "tool_calls": [
            {"id": call.id, "type": "function", "function": {
                "name": "delegate_task", "arguments": call.function.arguments,
            }}
        ]}]
        execute = cron._execute_tool_calls_concurrent if concurrent else cron._execute_tool_calls_sequential
        execute(message, messages, "cron-scope-test")
        results = [m for m in messages if m["role"] == "tool"]
        assert len(results) == 1
        assert "cron scope" in json.loads(results[0]["content"])["error"].lower()
    cron.close()
    # The explicit scope flag also binds authority on a different transport.
    scoped_cli = _agent(session_scope="cron")
    result = json.loads(delegate_task(parent_agent=scoped_cli, goal="Spawn a repair session"))
    assert "cron scope" in result["error"].lower()
    scoped_cli.close()
