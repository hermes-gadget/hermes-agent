"""Freeze explicitly produced project files for later, owner-scoped handoff."""
import asyncio
import hashlib
from pathlib import Path


def _freeze(home, reference, source_path):
    source = Path(source_path)
    if source.stat().st_size > 20 * 1024 * 1024:
        return None
    with source.open("rb") as stream:
        data = stream.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        return None
    digest = hashlib.sha256(data).hexdigest()
    ident = hashlib.sha256((reference + ":" + digest + ":" + source.name).encode()).hexdigest()
    folder = home / "cache" / "voice-artifacts" / ident
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / source.name
    if not target.exists():
        with target.open("xb") as stream:
            target.chmod(0o600)
            stream.write(data)
    return ident, {"filename": source.name, "path": str(target), "sha256": digest, "size": len(data)}


async def capture_artifacts(context, adapter, event, reference, monitor_url):
    content = getattr(event, "_completed_response", "") or getattr(event, "_streamed_final_response", "")
    if not content:
        return
    with adapter._media_delivery_scope(event.source):
        files, _ = adapter.extract_media(content)
        files = adapter.filter_media_delivery_paths(files)
    for path, is_voice in files[:5]:
        frozen = await asyncio.to_thread(_freeze, context.home, reference, path)
        if frozen is None:
            continue
        ident, payload = frozen
        payload.update(reference=reference, source_url=monitor_url, channel_id=str(event.source.chat_id),
                       profile=event.source.profile or "default", platform=event.source.platform.value)
        await asyncio.to_thread(context.db.voice_create_record, "artifact", ident, context.owner_key, payload, status="ready")


def verified_file(home, record):
    payload = record["payload"]
    path = Path(payload["path"]).resolve()
    if not path.is_relative_to((home / "cache" / "voice-artifacts").resolve()):
        raise ValueError("Artifact is outside its owning cache.")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != payload["sha256"]:
        raise ValueError("The artifact expired or changed; ask its project agent to regenerate it.")
    return path
