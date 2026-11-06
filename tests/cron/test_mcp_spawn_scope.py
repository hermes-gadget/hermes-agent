"""Per-job MCP process admission is as narrow as the declared tool scope."""
from cron.scheduler import _cron_mcp_allowed_names


class TestMcpSpawnScope:
    CFG = {"mcp_servers": {"finnhub": {"command": "finnhub"}, "other": {"command": "other"}}}
    def test_mcp_spawn_allowlist_honours_no_mcp(self):
        job = {"enabled_toolsets": ["web", "no_mcp"]}
        assert _cron_mcp_allowed_names(job, self.CFG) == []

    def test_mcp_spawn_allowlist_names_only_requested_servers(self):
        job = {"enabled_toolsets": ["web", "finnhub"]}
        assert _cron_mcp_allowed_names(job, self.CFG) == ["finnhub"]

    def test_mcp_spawn_allowlist_preserves_unfiltered_default(self):
        assert _cron_mcp_allowed_names({"prompt": "x"}, self.CFG) is None

