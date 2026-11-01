"""Accepted artifact requests finish their explicit delivery after hangup."""
import asyncio
import hashlib

from gateway.session import SessionSource
from .delivery import send_text, send_file


async def request_artifact(assistant, args, did, source, receipt):
    a = assistant
    task = str(args.get("task") or "").strip()
    if receipt is None or "request_id" not in receipt or not task or len(task) > 16000:
        raise ValueError("Producing an artifact requires an existing project request and the requested artifact description.")
    ident = str(args.get("request_id") or hashlib.sha256(
        f"{a.owner_key}:produce:{a.session.logical_session_id}:{did}".encode()).hexdigest())
    payload = {"artifact_id": args["artifact_id"], "task": task, "source": source.to_dict()}
    claim = await asyncio.to_thread(a.db.voice_create_record, "delivery", ident, a.owner_key, payload)
    if not claim["claimed"]:
        return {"request_id": ident, "status": claim["status"], "followup": claim["payload"].get("followup")}
    try:
        followup = await a._continue({"request_id": receipt["request_id"], "task": task +
            "\nProduce the requested artifact as a file and attach it to this project conversation using an explicit MEDIA: tag."},
            "produce:" + ident)
        status = "awaiting_artifact" if followup["status"] in {"accepted", "reported"} else "uncertain"
        await asyncio.to_thread(a.db.voice_change_record, "delivery", ident, a.owner_key, {"pending"}, status,
            {**payload, "followup": followup})
        await complete_handoffs(a, followup["request_id"])
        return {"request_id": ident, "status": status, "followup": followup}
    except Exception:
        await asyncio.to_thread(a.db.voice_change_record, "delivery", ident, a.owner_key, {"pending"}, "uncertain")
        raise


async def complete_handoffs(context, request_id):
    c = context
    requests = await asyncio.to_thread(c.db.voice_records, "delivery", c.owner_key, limit=500)
    waiting = [r for r in requests if r["status"] == "awaiting_artifact"
               and (r["payload"].get("followup") or {}).get("request_id") == request_id]
    if not waiting:
        return
    artifacts = [r for r in await asyncio.to_thread(c.db.voice_records, "artifact", c.owner_key)
                 if r["payload"]["reference"] == request_id]
    receipts = await asyncio.to_thread(c.db.voice_dispatches, c.owner_key, 100)
    receipt = next((r for r in receipts if r["request_id"] == request_id), None)
    if not artifacts and (receipt is None or receipt["status"] != "reported"):
        return
    for record in waiting:
        claimed = await asyncio.to_thread(c.db.voice_change_record, "delivery", record["id"], c.owner_key,
            {"awaiting_artifact"}, "delivering")
        if claimed is None:
            continue
        try:
            source = SessionSource.from_dict(record["payload"]["source"])
            result = []
            if artifacts:
                for artifact in artifacts[:5]:
                    result.append(await send_file(c, source, artifact, artifact["payload"]["filename"] +
                        "\nSource: " + artifact["payload"]["source_url"]))
            else:
                result.append(await send_text(c, source,
                    "The project agent reported without a file attachment. Its report is here: " + receipt["payload"]["monitor_url"]))
            await asyncio.to_thread(c.db.voice_change_record, "delivery", record["id"], c.owner_key,
                {"delivering"}, "delivered", {**record["payload"], "delivery": result})
        except Exception:
            await asyncio.to_thread(c.db.voice_change_record, "delivery", record["id"], c.owner_key,
                {"delivering"}, "uncertain")
            raise
