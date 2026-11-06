"""Discord configuration, PCM conversions and binding for the shared Live client."""
from __future__ import annotations
import logging
from dataclasses import dataclass, fields
from urllib.parse import urlparse
from plugins.platforms.live_voice.session import LiveVoiceConfig, LiveVoiceSession, GptLiveUnavailable
logger = logging.getLogger(__name__)

DEFAULT_INSTRUCTIONS = (
    "You are Hermes, talking with Ben in a Discord voice channel. Keep spoken replies short, "
    "natural and conversational. Delegate anything that needs information, tools, memory, "
    "or an action to the backend. Never claim work is finished until the backend confirms it. "
    "If the backend is still working, say so briefly and keep listening."
)

@dataclass(frozen=True)
class GptLiveConfig(LiveVoiceConfig):
    assistant_role: str = "project"
    voice_channel_id: str = ""
    text_channel_id: str = ""
    instructions: str = DEFAULT_INSTRUCTIONS

def load_gpt_live_config(raw: dict | None = None) -> GptLiveConfig:
    try:
        if raw is None:
            from hermes_cli.config import read_raw_config
            raw = read_raw_config() or {}
        block = raw.get("discord", {}).get("gpt_live", {})
        if not block:
            return GptLiveConfig()
        if not isinstance(block, dict):
            raise ValueError("expected a mapping")
        defaults = GptLiveConfig()
        values = {}
        for field in fields(defaults):
            value = block.get(field.name, getattr(defaults, field.name))
            default = getattr(defaults, field.name)
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"{field.name} must be a boolean")
            elif field.name.endswith("_id"):
                if value not in (None, ""):
                    if isinstance(value, bool) or not isinstance(value, (str, int)) or int(value) <= 0:
                        raise ValueError(f"invalid {field.name}")
                    value = str(int(value))
                else:
                    value = ""
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    raise ValueError(f"invalid {field.name}")
                value = int(value)
                minimum = 0 if field.name in ("barge_in_mute_ms", "prime_ms") else 1
                if not minimum <= value <= 86400:
                    raise ValueError(f"invalid {field.name}")
                if field.name == "prime_ms" and value > 2000:
                    raise ValueError("prime_ms exceeds the bounded playback buffer")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid {field.name}")
            values[field.name] = value
        cfg = GptLiveConfig(**values)
        if cfg.assistant_role not in {"personal", "project"}:
            raise ValueError("assistant_role must be personal or project")
        endpoint = urlparse(cfg.ws_url)
        if endpoint.scheme not in ("ws", "wss") or not endpoint.hostname or endpoint.query:
            raise ValueError("invalid ws_url")
        if cfg.enabled and not (cfg.voice_channel_id and cfg.user_id):
            raise ValueError("voice_channel_id and user_id are required")
        return cfg
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning("Invalid discord.gpt_live configuration; feature disabled (%s)", type(exc).__name__)
        return GptLiveConfig()

def pcm48_stereo_to_pcm24_mono(pcm: bytes) -> bytes:
    import numpy as np
    pcm = pcm[:len(pcm) // 8 * 8]
    x = np.frombuffer(pcm, "<i2").astype(np.float32).reshape(-1, 4)
    return x.mean(axis=1).clip(-32768, 32767).astype("<i2").tobytes()

def pcm24_mono_to_pcm48_stereo(pcm: bytes) -> bytes:
    import numpy as np
    x = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2")
    return np.repeat(x, 4).astype("<i2").tobytes()

class GptLiveSession(LiveVoiceSession):
    def __init__(self, adapter, guild_id, voice_channel, text_channel_id, cfg, mixer=None):
        self.guild_id, self.voice_channel = guild_id, voice_channel
        super().__init__(adapter, guild_id, text_channel_id, cfg, mixer=mixer,
                         dispatch=self._dispatch_backend,
                         input_to_pcm24=pcm48_stereo_to_pcm24_mono,
                         output_from_pcm24=pcm24_mono_to_pcm48_stereo,
                         input_frame_bytes=8,
                         usage_context={"platform":"discord", "guild_id":guild_id,
                                        "voice_channel_id":cfg.voice_channel_id})

    async def _dispatch_backend(self, text):
        runner = getattr(self.adapter, "gateway_runner", None)
        if runner is None:
            raise GptLiveUnavailable("gateway voice input handler unavailable")
        await runner._handle_voice_channel_input(
            self.guild_id, int(self.cfg.user_id), text, adapter=self.adapter, echo_transcript=False)
