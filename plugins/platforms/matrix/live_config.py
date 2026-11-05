"""Profile-local Matrix live voice settings; disabled until explicitly enabled."""
from dataclasses import dataclass, fields
import logging
from urllib.parse import urlparse

from plugins.platforms.live_voice.session import LiveVoiceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatrixLiveConfig(LiveVoiceConfig):
    helper_path: str = ""
    ca_file: str = ""


def load_matrix_live_config(raw=None):
    try:
        if raw is None:
            from hermes_cli.config import read_raw_config
            raw = read_raw_config() or {}
        block = (raw.get("matrix") or {}).get("gpt_live") or {}
        if not isinstance(block, dict):
            raise ValueError("matrix.gpt_live must be a mapping")
        defaults = MatrixLiveConfig()
        values = {}
        for field in fields(defaults):
            default = getattr(defaults, field.name)
            value = block.get(field.name, default)
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"invalid {field.name}")
            elif isinstance(default, int):
                minimum = 0 if field.name in {"prime_ms", "barge_in_mute_ms"} else 1
                if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 86400:
                    raise ValueError(f"invalid {field.name}")
                if field.name == "prime_ms" and value > 2000:
                    raise ValueError("prime_ms exceeds the bounded playback buffer")
            elif not isinstance(value, str):
                raise ValueError(f"invalid {field.name}")
            elif field.name not in {"user_id", "helper_path", "ca_file"} and not value.strip():
                raise ValueError(f"invalid {field.name}")
            values[field.name] = value
        cfg = MatrixLiveConfig(**values)
        if cfg.assistant_role not in {"personal", "project"}:
            raise ValueError("assistant_role must be personal or project")
        url = urlparse(cfg.ws_url)
        if url.scheme not in {"ws", "wss"} or not url.hostname or url.query:
            raise ValueError("invalid Live endpoint")
        if cfg.enabled and (not cfg.user_id.startswith("@") or ":" not in cfg.user_id or any(c.isspace() for c in cfg.user_id)):
            raise ValueError("a Matrix user_id is required")
        return cfg
    except (TypeError, ValueError, AttributeError) as exc:
        logger.warning("Invalid matrix.gpt_live configuration; disabled (%s)", type(exc).__name__)
        return MatrixLiveConfig()
