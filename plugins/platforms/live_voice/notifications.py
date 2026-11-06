"""Owner-configured unsolicited notices; direct replies retain normal delivery."""
import asyncio
from datetime import datetime, timezone, UTC
from zoneinfo import ZoneInfo


class NotificationPolicy:
    MODES = {"decisions", "watched", "milestones", "muted"}

    def __init__(self, config):
        self.settings = (config.get("voice", {}).get("assistant") or {}).get("notifications") or {}
        self.timezone = self.settings.get("timezone") or config.get("timezone") or "Europe/London"

    def mode(self, now):
        temporary = self.settings.get("temporary") or {}
        if temporary.get("until"):
            expiry = datetime.fromisoformat(temporary["until"])
            if expiry.tzinfo and expiry.timestamp() > now:
                mode = temporary["mode"]
                if mode not in self.MODES:
                    raise ValueError("Unknown temporary notification mode.")
                return mode
        mode = self.settings.get("mode", "watched")
        if mode not in self.MODES:
            raise ValueError("Unknown notification mode.")
        return mode

    def quiet(self, now):
        block = self.settings.get("quiet_hours", {"start": "22:00", "end": "08:00"})
        if block is False:
            return False
        current = datetime.fromtimestamp(now, ZoneInfo(self.timezone))
        minute = current.hour * 60 + current.minute
        def minutes(value):
            parsed = datetime.strptime(value, "%H:%M")
            return parsed.hour * 60 + parsed.minute
        start, end = minutes(block["start"]), minutes(block["end"])
        if start == end:
            return False
        return start <= minute < end if start < end else minute >= start or minute < end

    def allows(self, kind, now):
        mode = self.mode(now)
        return mode != "muted" and not self.quiet(now) and (
            kind == "input" or kind == "completion" and mode in {"watched", "milestones"}
            or kind == "milestone" and mode == "milestones")


async def configure_notifications(assistant, args, did):
    from hermes_cli.config import atomic_config_write, read_user_config_raw
    mode, until = args.get("mode"), args.get("until")
    if mode is not None and mode not in NotificationPolicy.MODES:
        raise ValueError("Choose decisions, watched, milestones, or muted.")
    expiry = None
    if until:
        expiry = datetime.fromisoformat(until)
        if not expiry.tzinfo or expiry <= datetime.now(UTC) or mode is None:
            raise ValueError("Temporary modes require a future timestamp with a timezone and a mode.")
    def configure():
        path = assistant.session.home / "config.yaml"
        raw = read_user_config_raw(path)
        settings = raw.setdefault("voice", {}).setdefault("assistant", {}).setdefault("notifications", {})
        if mode is not None:
            if expiry:
                settings["temporary"] = {"mode": mode, "until": expiry.isoformat()}
            else:
                settings["mode"] = mode
                settings.pop("temporary", None)
            atomic_config_write(path, raw)
        return raw, settings
    raw, settings = await asyncio.to_thread(configure)
    assistant.config = raw
    if assistant.observer is not None:
        assistant.observer.context.config = raw
    return {"status": "configured" if mode else "current", "notifications": settings,
            "effective_mode": NotificationPolicy(raw).mode(datetime.now(UTC).timestamp())}
