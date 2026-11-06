"""Generic cron presentation contracts; personal policy stays in standalone plugins."""

from cron import presentation


def test_optional_preparation_fails_open_and_receipt_is_redacted(monkeypatch):
    from hermes_cli import plugins

    job = {"id": "abc123"}
    target = {"platform": "matrix", "chat_id": "!pa:test"}
    monkeypatch.setattr(plugins, "has_hook", lambda _: False)
    assert (
        presentation.prepare(
            job, target, "Original facts", config={}, media_files=[], for_failure=False
        )
        is None
    )
    monkeypatch.setattr(plugins, "has_hook", lambda _: True)

    def broken(*args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(plugins, "invoke_hook", broken)
    assert (
        presentation.prepare(
            job, target, "Original facts", config={}, media_files=[], for_failure=False
        )
        is None
    )
    calls = []
    monkeypatch.setattr(
        plugins, "invoke_hook", lambda hook, **kwargs: calls.append((hook, kwargs))
    )
    presentation.receipt(
        job,
        target,
        {"action": "send"},
        status="unknown",
        content="Original facts",
        error="request failed: https://username:private-password@example.org/api",
    )
    assert calls[0][1]["status"] == "unknown"
    assert "private-password" not in calls[0][1]["error"]


def test_native_defer_waits_without_replaying_or_marking_delivered(monkeypatch):
    from cron import delivery_queue as queue

    clock = [1000.0]
    monkeypatch.setattr(queue.time, "time", lambda: clock[0])
    queue.enqueue("one-batch", {"id": "abc123"}, "Original facts")

    def defer(job, content, for_failure):
        job["_presentation_not_before"] = 2000.0

    assert queue.drain(defer) == 1
    assert queue.get_status("one-batch")["status"] == "pending"
    assert queue.claim_next() is None
    clock[0] = 2001.0
    assert queue.drain(lambda *args: None) == 1
    assert queue.get_status("one-batch")["status"] == "delivered"
    assert (
        queue.drain(
            lambda *args: (_ for _ in ()).throw(AssertionError("duplicate send"))
        )
        == 0
    )
