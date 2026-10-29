"""Per-job conversational delivery must not alter other scheduled conversations."""
from types import SimpleNamespace

import pytest

from cron.jobs import create_job, get_job, update_job, use_cron_store
from cron.scheduler_delivery import _resolve_cron_surface_mode


def test_delivery_settings_survive_job_store_and_fall_back_independently(tmp_path):
    platform = SimpleNamespace(extra={"cron_continuable_surface": "thread"})
    with use_cron_store(tmp_path):
        selected = create_job("investigate", "every 1m", delivery_surface="in_channel", wrap_response=False)
        ordinary = create_job("reminder", "every 1h")
        selected = get_job(selected["id"])
        assert selected["wrap_response"] is False
        assert _resolve_cron_surface_mode(platform, "discord", selected) == "in_channel"
        assert _resolve_cron_surface_mode(platform, "discord", get_job(ordinary["id"])) == "thread"
        update_job(selected["id"], {"delivery_surface": None, "wrap_response": None})
        assert _resolve_cron_surface_mode(platform, "discord", get_job(selected["id"])) == "thread"
        with pytest.raises(ValueError):
            update_job(selected["id"], {"delivery_surface": "invalid"})
        with pytest.raises(ValueError):
            create_job("invalid", "1m", wrap_response="false")
