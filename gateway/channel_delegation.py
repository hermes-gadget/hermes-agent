"""Resolve native-delegation settings with the gateway's channel/thread precedence."""

import logging


logger = logging.getLogger(__name__)


def credential_coordinator_for_source(config, source):
    from agent.channel_credentials import coordinator_for
    from gateway.run import _channel_override_lookup_keys, _get_channel_override

    if config is None or source is None:
        return None
    lookup = dict(thread_id=getattr(source, "thread_id", None), parent_id=getattr(source, "parent_chat_id", None))
    channel = _get_channel_override(config, source.platform, source.chat_id or "", **lookup)
    if channel is None or channel.credentials is None:
        return None
    if channel.fallback_providers:
        raise ValueError("Reserved channel credentials cannot be combined with model fallbacks.")
    if channel.credentials.fallbacks and (channel.delegation is None or channel.delegation.enabled is not False):
        raise ValueError("Channel key rotation requires delegation.enabled: false.")
    overrides = config.platforms[source.platform].channel_overrides
    key = next(k for k in _channel_override_lookup_keys(source.chat_id or "", **lookup) if k in overrides)
    return coordinator_for((source.platform.value, key), channel.provider, channel.model, channel.credentials.to_dict())


def delegation_for_source(config, source):
    from gateway.run import _get_channel_override

    if config is None:
        return None
    channel = _get_channel_override(
        config, source.platform, source.chat_id or "",
        thread_id=getattr(source, "thread_id", None), parent_id=getattr(source, "parent_chat_id", None),
    )
    delegation = channel.delegation.to_dict() if channel and channel.delegation else None
    if delegation:
        logger.debug(
            "Channel delegation override: platform=%s channel=%s enabled=%s model=%s "
            "provider=%s max_concurrent_children=%s",
            source.platform.value, source.chat_id, delegation.get("enabled", True),
            delegation.get("model"), delegation.get("provider"), delegation.get("max_concurrent_children"),
        )
    return delegation
