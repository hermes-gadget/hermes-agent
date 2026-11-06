"""Private cron presentation decisions, separate from transport delivery and receipts."""
from cron import presentation


def prepare_presented_target(job, target, mirror_text, user_cfg, media_files,
                             for_failure, single_target, cleaned_delivery_content, mirror_enabled):
    from cron.scheduler_delivery import _redact_cron_payload
    suppressed = 0
    decision = presentation.prepare(
        job, target, mirror_text, config=user_cfg, media_files=media_files, for_failure=for_failure)
    if decision and decision["action"] in {"defer", "suppress"}:
        if decision["action"] == "defer":
            presentation.queued(job, target, decision)
            if decision.get("not_before") and single_target:
                job["_presentation_not_before"] = float(decision["not_before"])
        else:
            suppressed = 1
            presentation.receipt(job, target, decision, status="suppressed", content=mirror_text)
        return suppressed, None
    target_job, target_content, target_mirror = job, cleaned_delivery_content, mirror_text
    target_mirror_enabled = mirror_enabled
    if decision:
        target_content = _redact_cron_payload(decision.get("content", mirror_text), "presentation")
        target_mirror = target_content
        target_job = dict(job)
        target_job["_presentation_requires_evidence"] = True
        if decision.get("continuation"):
            target_job.update(attach_to_session=True,
                              delivery_surface="thread" if target.get("thread_id") else "in_channel")
            target_job["origin"] = {
                "platform": target["platform"], "chat_id": target["chat_id"],
                "thread_id": target.get("thread_id"), "chat_type": "dm",
                "user_id": str(decision["continuation"]),
            }
            target_mirror_enabled = True
    return suppressed, (decision, target_job, target_content, target_mirror, target_mirror_enabled)


def send_presented_target(job, target, prepared, adapters, loop, config, notify_delivery,
                          media_files, delivery_errors, unverified_targets):
    from cron.scheduler_delivery import _prepare_target_delivery, _deliver_via_live_adapter, _deliver_standalone
    decision, target_job, target_content, target_mirror, target_mirror_enabled = prepared
    t = _prepare_target_delivery(
        target_job, target, adapters=adapters, loop=loop, config=config,
        notify_delivery=notify_delivery,
        mirror_enabled=target_mirror_enabled, mirror_text=target_mirror, delivery_errors=delivery_errors)
    if t is None:
        presentation.receipt(job, target, decision, status="failed", content=target_content,
                             error="delivery target unavailable before send")
        return
    target_errors: list = []
    delivered = t.live_adapter_ready and _deliver_via_live_adapter(
        t, target_content, media_files,
        target_errors=target_errors, delivery_errors=delivery_errors,
        unverified_targets=unverified_targets,
    )
    if not delivered:
        _deliver_standalone(
            t, target_content, media_files, target_errors, delivery_errors)
    presentation.receipt(job, target, decision, status=t.receipt_status, content=target_content,
                         message_id=t.receipt_message_id,
                         error="; ".join(target_errors) or None)


def clear_presentation_deferral(job):
    queued = job.get("last_delivery_queued")
    if isinstance(queued, dict) and queued.get("kind") == "presentation":
        job["last_delivery_queued"] = None
