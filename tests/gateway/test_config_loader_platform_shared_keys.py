"""Platform-specific shared-key bridges must match actual adapter support."""

from gateway.config import Platform
from gateway.config_loader import (
    bridge_platform_shared_keys,
    load_yaml_layer,
    merge_platform_sections,
)


def test_matrix_channel_prompts_are_not_bridged():
    """Matrix never reads channel_prompts, so the config key is ignored at runtime."""
    for yaml_cfg in (
        {"platforms": {"matrix": {"channel_prompts": {"!room:example.org": "dead"}}}},
        {"matrix": {"channel_prompts": {"!room:example.org": "dead"}}},
    ):
        gateway_data = {}
        platforms_data = merge_platform_sections(yaml_cfg, {}, gateway_data)

        bridge_platform_shared_keys(
            yaml_cfg,
            {},
            {},
            platforms_data,
            [Platform.MATRIX],
        )

        assert "channel_prompts" not in platforms_data.get("matrix", {})
        assert "channel_prompts" not in platforms_data.get("matrix", {}).get("extra", {})


def test_channel_prompts_still_bridge_to_supported_platforms():
    platforms_data = {}

    bridge_platform_shared_keys(
        {"platforms": {"discord": {"channel_prompts": {"123": "research"}}}},
        {},
        {},
        platforms_data,
        [Platform.DISCORD],
    )

    assert platforms_data["discord"]["extra"]["channel_prompts"] == {"123": "research"}


def test_load_yaml_layer_drops_matrix_channel_prompts(tmp_path, monkeypatch):
    """Exercise the real YAML load/merge/bridge path, not only the helper seam."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  matrix:\n"
        "    channel_prompts:\n"
        "      '!room:example.org': dead\n",
        encoding="utf-8",
    )
    gateway_data = {}

    load_yaml_layer(tmp_path, gateway_data)

    matrix = gateway_data["platforms"]["matrix"]
    assert "channel_prompts" not in matrix
    assert "channel_prompts" not in matrix.get("extra", {})
