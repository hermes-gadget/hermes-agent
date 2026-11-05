"""Native delivery to verified owner conversations and project artifact handoffs."""
import asyncio
import hashlib
from dataclasses import replace

from gateway.config import Platform


def matrix_destination(context):
    settings = (context.config.get("voice", {}).get("assistant") or {}).get("notifications") or {}
    explicit = settings.get("matrix_room_id")
    candidates = {}
    runner = context.runner
    for entry in getattr(runner.session_store, "_entries", {}).values():
        source = runner._restored_source(entry) if callable(getattr(runner, "_restored_source", None)) else entry.origin
        if (source and source.platform == Platform.MATRIX and source.chat_type == "dm"
                and context.authorized_source(source) and (not explicit or str(source.chat_id) == str(explicit))):
            adapter = runner._delivery_adapter_for(source)
            identity = adapter._canonicalize(source) if adapter else None
            if identity is not None and str(identity.runtime_home) == str(context.home):
                candidates[str(source.chat_id)] = replace(source, thread_id=None, parent_chat_id=None)
    if len(candidates) != 1:
        raise ValueError("Choose an existing verified personal Matrix DM for voice notifications.")
    return next(iter(candidates.values()))


async def send_text(context, source, text):
    if not context.authorized_source(source):
        raise ValueError("The destination is no longer authorized.")
    adapter = context.runner._delivery_adapter_for(source)
    if adapter is None:
        raise ValueError("The destination transport is unavailable.")
    with context.runner._profile_scope_for_source(source):
        metadata = {"thread_id": source.thread_id} if source.thread_id else None
        result = await adapter.send(source.chat_id, text, metadata=metadata)
    if result is None or not result.success:
        raise OSError("Delivery was not confirmed; inspect the destination before retrying.")
    return delivery_result(source, result)


def delivery_result(source, result):
    message_id = str(result.message_id or "")
    if source.platform == Platform.DISCORD:
        from .projects import discord_link
        url = discord_link(source.guild_id, source.chat_id, message_id)
    else:
        url = f"https://matrix.to/#/{source.chat_id}" + (f"/{message_id}" if message_id else "")
    return {"platform": source.platform.value, "chat_id": source.chat_id, "message_id": message_id, "url": url}


async def send_file(context, source, record, caption):
    from .artifacts import verified_file
    path = await asyncio.to_thread(verified_file, context.home, record)
    adapter = context.runner._delivery_adapter_for(source)
    if adapter is None or not context.authorized_source(source):
        raise ValueError("The destination is unavailable or unauthorized.")
    with context.runner._profile_scope_for_source(source):
        result = await adapter.send_document(source.chat_id, str(path), caption=caption,
            metadata={"thread_id": source.thread_id} if source.thread_id else None)
    if result is None or not result.success:
        raise OSError("Artifact delivery was not confirmed; inspect the destination before retrying.")
    return delivery_result(source, result)


async def handoff(assistant, args, did):
    a = assistant
    if a.db is None:
        raise ValueError("Durable delivery receipts are unavailable.")
    artifact = str(args.get("artifact_id") or "")
    receipts = await asyncio.to_thread(a.db.voice_dispatches, a.owner_key, 100)
    record = next((r for r in receipts if r["request_id"] == artifact), None)
    if record is None and artifact:
        attached = await asyncio.to_thread(a.db.voice_record, "attachment", artifact, a.owner_key)
        if attached and attached["payload"].get("result"):
            record = attached
    files = await asyncio.to_thread(a.db.voice_records, "artifact", a.owner_key)
    selected = [r for r in files if r["id"] == artifact] or [r for r in files if r["payload"]["reference"] == artifact]
    if len(selected) > 1:
        raise ValueError("That result has multiple files; choose an artifact_id from agents.")
    file_record = selected[0] if selected else None
    if a.session.cfg.assistant_role == "project" and (
            file_record and file_record["payload"].get("platform") == "matrix"
            or record and record["payload"].get("source", {}).get("platform") == "matrix"):
        raise ValueError("Use the personal Matrix conversation for personal artifacts.")
    if record is None and file_record is None and artifact:
        raise ValueError("Choose a verified project result from agents.")
    destination = args.get("destination", "current")
    if destination == "matrix":
        source = matrix_destination(a)
    elif destination == "current":
        source = a.session.backend_source
    elif destination == "project":
        origin = record["payload"] if record else file_record["payload"] if file_record else {}
        project = args.get("project") or (f"{origin.get('profile', 'default')}:{origin['channel_id']}" if origin else None)
        binding = await a.projects.resolve(project)
        source = a.projects.source(binding)
    else:
        raise ValueError("Choose current, project or matrix as the destination.")
    if args.get("produce"):
        from .fulfillment import request_artifact
        return await request_artifact(a, args, did, source, record)
    if file_record:
        text = file_record["payload"]["filename"] + "\nSource: " + file_record["payload"]["source_url"]
    elif record:
        content = record["payload"].get("result")
        if not content:
            raise ValueError("That job has not reported its artifact yet. Continue its project conversation to request one.")
        text = str(content) + "\n\nSource: " + record["payload"]["monitor_url"]
    else:
        text = str(args.get("task") or "").strip()
        if not text:
            raise ValueError("Choose a result or provide the requested text to deliver.")
    if len(text) > 16000:
        text = text[:15800] + "\n[Full result is in the source conversation.]"
    ident = str(args.get("request_id") or "") or hashlib.sha256(
        f"{a.owner_key}:handoff:{a.session.logical_session_id}:{did}".encode()).hexdigest()
    payload = {"artifact_id": artifact, "destination": destination, "chat_id": str(source.chat_id), "text": text}
    claim = await asyncio.to_thread(a.db.voice_create_record, "delivery", ident, a.owner_key, payload)
    if not claim["claimed"]:
        return {"request_id": ident, "status": claim["status"], "delivery": claim["payload"].get("delivery")}
    try:
        a.authorize()
        if file_record:
            result = await send_file(a, source, file_record, text)
        else:
            result = await send_text(a, source, text)
        await asyncio.to_thread(a.db.voice_change_record, "delivery", ident, a.owner_key, {"pending"},
            "delivered", {**payload, "delivery": result})
    except Exception:
        await asyncio.to_thread(a.db.voice_change_record, "delivery", ident, a.owner_key, {"pending"}, "uncertain")
        raise
    return {"request_id": ident, "status": "delivered", "delivery": result}
