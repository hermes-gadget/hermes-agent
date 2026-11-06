"""Opt-in, per-target cron presentation hooks. Transport remains owned by cron.

Preparation callbacks must finish synchronously: they may durably queue a notice,
so abandoning one on a timeout and sending the original could deliver it twice.
No callback means byte-identical legacy delivery. Receipts describe evidence, not
merely adapter acceptance; an uncertain send must never be automatically replayed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def prepare(
    job: dict,
    target: dict,
    content: str,
    *,
    config: dict | None,
    media_files: list,
    for_failure: bool,
) -> dict | None:
    from hermes_cli.plugins import has_hook, invoke_hook

    try:
        if not has_hook("prepare_cron_delivery"):
            return None
        results = invoke_hook(
            "prepare_cron_delivery",
            job=dict(job),
            target=dict(target),
            content=content,
            config=config or {},
            media_files=list(media_files),
            for_failure=for_failure,
        )
        for result in results:
            if not isinstance(result, dict) or result.get("action") not in {
                "send",
                "defer",
                "suppress",
            }:
                continue
            if result["action"] == "send" and not isinstance(
                result.get("content", content), str
            ):
                continue
            if (
                result["action"] == "send"
                and not result.get("content", content).strip()
            ):
                continue
            return result
    except Exception:
        logger.exception("Cron presentation unavailable; using original factual output")
    return None


def receipt(
    job: dict,
    target: dict,
    decision: dict | None,
    *,
    status: str,
    content: str,
    message_id=None,
    error: str | None = None,
) -> None:
    if decision is None:
        return
    try:
        from hermes_cli.plugins import invoke_hook
        from agent.redact import redact_sensitive_text

        invoke_hook(
            "on_cron_delivery",
            job=dict(job),
            target=dict(target),
            presentation=dict(decision),
            status=status,
            content=content,
            message_id=message_id,
            error=(
                redact_sensitive_text(
                    str(error), force=True, redact_url_credentials=True
                )
                if error
                else None
            ),
        )
    except Exception:
        logger.exception("Could not record cron presentation receipt")


def queued(job: dict, target: dict, decision: dict) -> None:
    value = {
        "kind": "presentation",
        "execution_id": job.get("execution_id"),
        "notice_id": decision.get("notice_id"),
        "target": dict(target),
    }
    job["last_delivery_queued"] = value
    try:
        from cron.jobs import update_job

        update_job(job["id"], {"last_delivery_queued": value})
    except Exception:
        logger.warning(
            "Could not persist queued presentation for job %s", job.get("id"), exc_info=True
        )
