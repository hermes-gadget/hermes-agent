"""Unavailable selected backends must report setup errors instead of keyless results.

Regression for HermesMind #471. Exercise real config, plugin discovery, registry
dispatch and doctor readiness; only the optional import and search I/O are faked.
"""

import json
import sys
import types
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("config_key", ["backend", "search_backend"])
def test_selected_ddgs_readiness_gates_rescue_and_cached_results(tmp_path, monkeypatch, config_key):
    from hermes_cli.doctor_tools import _doctor_web_capability_rows
    from plugins.web import keyless_mcp
    from plugins.web.ddgs import provider as ddgs_provider
    from tools import web_tools
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"web:\n  {config_key}: ddgs\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "ddgs", None)
    ring = Mock(return_value={"success": True, "data": {"web": []}})
    worker = Mock(return_value=[{"title": "DDGS", "url": "https://ddgs.example", "description": "hit"}])
    monkeypatch.setattr(keyless_mcp, "search_with_failover", ring)
    monkeypatch.setattr(ddgs_provider, "_run_ddgs_search_bounded", worker)

    # Both first use and a later loss of the package must report its absence,
    # even if this exact query previously populated the successful-result cache.
    for available in (False, True, False):
        monkeypatch.setitem(sys.modules, "ddgs", types.ModuleType("ddgs") if available else None)
        result = json.loads(registry.dispatch("web_search", {"query": "readiness", "limit": 2}))
        assert result["success"] is available, result
        if available:
            assert result["data"]["web"][0]["url"] == "https://ddgs.example"
        else:
            assert "ddgs" in result["error"] and "not installed" in result["error"]
            assert "hermes pm install --extra ddgs" in result["error"]
        status, _, detail = next(row for row in _doctor_web_capability_rows() if row[1] == "web search")
        assert status == ("ok" if available else "warn")
        assert "ddgs" in detail
        assert web_tools._get_search_backend() == "ddgs"
    ring.assert_not_called()
    worker.assert_called_once()


def test_ready_ddgs_runtime_failure_still_gets_annotated_rescue(tmp_path, monkeypatch):
    from plugins.web import keyless_mcp
    from plugins.web.ddgs import provider as ddgs_provider
    from tools import web_tools
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("web:\n  search_backend: ddgs\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "ddgs", types.ModuleType("ddgs"))
    monkeypatch.setattr(ddgs_provider, "_run_ddgs_search_bounded", Mock(side_effect=RuntimeError("upstream failed")))
    ring = Mock(return_value={"success": True, "data": {"web": [{"url": "https://rescue.example"}]}})
    monkeypatch.setattr(keyless_mcp, "search_with_failover", ring)

    result = json.loads(registry.dispatch("web_search", {"query": "runtime failure", "limit": 2}))
    assert result["success"] is True
    assert result["data"]["rescued_from"] == "ddgs"
    assert "upstream failed" in result["data"]["backend_error"]
    assert web_tools._get_search_backend() == "ddgs"
    ring.assert_called_once()
