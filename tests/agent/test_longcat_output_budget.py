"""LongCat gets its full output allowance without disabling its reasoning."""

from ruamel.yaml import YAML

from tests.fakes.fake_llm_provider import FakeLLMServer, Text


def test_longcat_full_output_budget_survives_continuations(tmp_path, monkeypatch):
    from run_agent import AIAgent

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with (home / "config.yaml").open("w") as stream:
        YAML().dump({
            "model": {"context_length": 1048576},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
        }, stream)

    limit = 262144
    exhausted = Text("", reasoning="Thinking about the answer.",
                     completion_tokens=limit, finish_reason="length")
    with FakeLLMServer([exhausted, exhausted, Text("The answer is ready."),
                        Text("The next answer is ready.")], api_key="fixture-key") as host:
        agent = AIAgent(
            model="longcat-2.5-preview-free", provider="opencode-go",
            api_key="fixture-key", base_url=host.base_url, api_mode="chat_completions",
            enabled_toolsets=["terminal"], reasoning_config={"enabled": True, "effort": "high"},
            max_iterations=8, quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False,
        )
        try:
            agent.compression_enabled = False
            result = agent.run_conversation("Give me the answer.")
            assert result["completed"] is True
            assert result["final_response"] == "The answer is ready."
            assert len(host.main_requests()) == 3
            next_result = agent.run_conversation("And the next one?",
                                                 conversation_history=result["messages"])
            assert next_result["completed"] is True
            requests = host.main_requests()
            assert len(requests) == 4
            # A retry must neither fall back to 8K nor double past the model ceiling.
            assert all(r.get("max_tokens") == limit for r in requests)
            assert all(r["model"] == agent.model for r in requests)
            assert all((r.get("thinking") or {}).get("type") != "disabled" for r in requests)
            assert all(r.get("reasoning_effort") not in ("none", "minimal", "low") for r in requests)
            assert agent.reasoning_config == {"enabled": True, "effort": "high"}
            prefixes = [[m for m in r["messages"] if m["role"] in ("system", "developer")]
                        for r in requests]
            assert all(prefix == prefixes[0] for prefix in prefixes)
            assert not any(m["role"] == "assistant" and not (m.get("content") or "").strip()
                           and not m.get("tool_calls") for m in result["messages"])
        finally:
            agent.client.close()
