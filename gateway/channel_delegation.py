"""Resolve native-delegation settings with the gateway's channel/thread precedence."""

import logging


logger = logging.getLogger(__name__)


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
