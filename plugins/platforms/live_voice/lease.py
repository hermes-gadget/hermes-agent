"""One active live voice conversation per profile, shared by all transports."""
from pathlib import Path
import threading


class LiveVoiceLease:
    _lock = threading.Lock()
    _owners = {}

    @classmethod
    def claim(cls, home, owner):
        key = Path(home).resolve()
        with cls._lock:
            current = cls._owners.get(key)
            if current is not None and current is not owner:
                return False
            cls._owners[key] = owner
            return True

    @classmethod
    def release(cls, home, owner):
        key = Path(home).resolve()
        with cls._lock:
            if cls._owners.get(key) is owner:
                del cls._owners[key]
