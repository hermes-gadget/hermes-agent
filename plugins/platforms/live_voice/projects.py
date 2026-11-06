"""Visible Discord dispatch using the target's canonical gateway conversation."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
import re
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.wake import admit_internal_event

PROJECT_CONTEXT = ContextVar("live_project_dispatch", default=None)


def discord_link(guild_id, channel_id, message_id=""):
    return f"https://discord.com/channels/{guild_id}/{channel_id}" + (f"/{message_id}" if message_id else "")


def safe_text(text):
    return str(text).replace("@", "@\u200b")


@dataclass
class ProjectAgentBinding:
    project: str
    channel_id: str
    guild_id: str
    profile: str
    designation: str
    adapter: object
    channel: object
    user_id: str

    def payload(self):
        return {"project": self.project, "channel_id": self.channel_id, "guild_id": self.guild_id,
                "profile": self.profile, "designation": self.designation,
                "monitor_url": discord_link(self.guild_id, self.channel_id)}


@dataclass(frozen=True)
class ProjectDispatchRequest:
    request_id: str
    task: str
    context: str = ""


@dataclass(frozen=True)
class ProjectDispatchReceipt:
    request_id: str
    status: str
    payload: dict

    def as_dict(self):
        return {"request_id": self.request_id, "status": self.status, **self.payload}


class ProjectDispatcher:
    def __init__(self, assistant):
        self.assistant = assistant

    def adapters(self):
        runner = self.assistant.runner
        maps = [getattr(runner, "adapters", {})]
        maps.extend(getattr(runner, "_profile_adapters", {}).values())
        found = {}
        for mapping in maps:
            adapter = mapping.get(Platform.DISCORD)
            if adapter is not None:
                found[id(adapter)] = adapter
        return list(found.values())

    def source(self, binding, channel=None, message_id=None):
        channel = channel or binding.channel
        parent_id = str(getattr(channel, "parent_id", "") or "")
        source = binding.adapter.build_source(
            chat_id=str(channel.id), chat_name=getattr(channel, "name", binding.project),
            chat_type="thread" if parent_id else "group", user_id=binding.user_id,
            user_name="Ben", guild_id=binding.guild_id, parent_chat_id=parent_id or None,
            thread_id=str(channel.id) if parent_id else None, message_id=message_id,
            chat_topic=binding.adapter._get_effective_topic(channel, is_thread=bool(parent_id)))
        if binding.adapter._canonicalize(source) is None:
            raise ValueError("Project channel routes to an unavailable profile.")
        with self.assistant.runner._profile_scope_for_source(source):
            keys = binding.adapter._discord_channel_keys(SimpleNamespace(channel=channel, guild=channel.guild), parent_id or None)
            allowed, ignored = binding.adapter._get_allowed_channels(), binding.adapter._get_ignored_channels()
            if "*" in ignored or keys & set(ignored) or allowed and "*" not in allowed and not keys & set(allowed):
                raise ValueError("This project channel is excluded by the Discord channel policy.")
            if binding.adapter._is_allowed_user(binding.user_id, guild=channel.guild, is_dm=False):
                source.role_authorized = True
        if not self.assistant.runner._is_user_authorized_for_source(source):
            raise ValueError("The voice owner is not authorized in this project channel.")
        return source

    def bindings(self):
        raw = self.assistant.config
        owner_id = str((raw.get("discord", {}).get("gpt_live") or {}).get("user_id") or "")
        if not owner_id.isdigit():
            return []
        configured = (raw.get("voice", {}).get("assistant") or {}).get("projects") or {}
        if not isinstance(configured, dict):
            raise ValueError("voice.assistant.projects must map project names to channel settings.")
        result = []
        for adapter in self.adapters():
            client = getattr(adapter, "_client", None)
            if client is None:
                continue
            channels = {}
            for guild in client.guilds:
                for channel in [*guild.text_channels, *getattr(guild, "forum_channels", []), *getattr(guild, "threads", [])]:
                    channels[str(channel.id)] = channel
            names = {str(v.get("channel_id")): (k, v) for k, v in configured.items() if isinstance(v, dict)}
            for cid, channel in channels.items():
                project, settings = names.get(cid, (channel.name, {}))
                if settings.get("profile"):
                    preferred = self.assistant.runner._adapters_for_profile(settings["profile"]).get(Platform.DISCORD)
                    if adapter is not preferred:
                        continue
                binding = ProjectAgentBinding(str(project), cid, str(channel.guild.id),
                    getattr(adapter, "_owner_profile", None) or "default",
                    "configured project" if cid in names else "Discord channel", adapter, channel, owner_id)
                try:
                    source = self.source(binding)
                except ValueError:
                    continue
                runtime_profile = source.profile or binding.profile
                if settings.get("profile") and settings["profile"] != runtime_profile:
                    continue
                binding.profile = runtime_profile
                result.append(binding)
        return result

    async def resolve(self, project):
        bindings = self.bindings()
        token = str(project or self.assistant.selected_project or "").strip().lstrip("#")
        matches = [b for b in bindings if token in {b.channel_id, f"<#{b.channel_id}>", f"{b.profile}:{b.channel_id}"}
                   or token.casefold() in {b.project.casefold(), f"{b.profile}:{b.project}".casefold()}]
        if len(matches) != 1:
            raise ValueError("Choose one designated project channel using projects; the destination is missing or ambiguous.")
        binding = matches[0]
        explicit_destination = token in {binding.channel_id, f"<#{binding.channel_id}>", f"{binding.profile}:{binding.channel_id}"}
        # A project's channel may explicitly point at its execution channel.
        prompt = binding.adapter._resolve_channel_prompt(binding.channel_id,
            str(getattr(binding.channel, "parent_id", "") or "") or None) or ""
        topic = getattr(binding.channel, "topic", "") or ""
        refs = set(re.findall(r"<#(\d+)>", prompt + "\n" + topic)) - {binding.channel_id}
        if not refs and binding.designation != "configured project" and not explicit_destination:
            pins = getattr(binding.channel, "pins", None)
            if callable(pins):
                async def read_briefs():
                    found = set()
                    async for message in pins(limit=20):
                        if str(getattr(getattr(message, "author", None), "id", "")) == binding.user_id:
                            found.update(re.findall(r"<#(\d+)>", message.content or ""))
                    return found - {binding.channel_id}
                refs = await asyncio.wait_for(read_briefs(), 5)
        follow_reference = binding.designation != "configured project" and not explicit_destination
        if follow_reference and len(refs) > 1:
            raise ValueError("Project instructions name multiple channels; specify the execution channel explicitly.")
        if refs and follow_reference:
            targets = [b for b in bindings if b.channel_id in refs]
            if len(targets) != 1:
                raise ValueError("The designated execution channel is unavailable or unauthorized.")
            target = targets[0]
            target.project, target.designation = binding.project, f"channel reference in {binding.channel_id}"
            return target
        return binding

    async def publish(self, binding, request):
        import discord
        channel, adapter = binding.channel, binding.adapter
        text = f"**Task requested via Hermes voice**\n{safe_text(request.task)[:1700]}"
        options = {"allowed_mentions": discord.AllowedMentions.none()}
        if isinstance(channel, discord.ForumChannel):
            created = await channel.create_thread(name=adapter._derive_auto_thread_name(request.task), content=text, **options)
            return created.thread, created.message
        message = await channel.send(text, **options)
        if isinstance(channel, discord.Thread) or getattr(channel, "parent_id", None):
            return channel, message
        keys = adapter._discord_channel_keys(SimpleNamespace(channel=channel, guild=channel.guild), None)
        free = adapter._discord_free_response_channels()
        skip = bool(keys & set(adapter._get_no_thread_channels())) or (
            ("*" in free or bool(keys & set(free))) and not adapter._discord_free_response_auto_thread())
        linked = str(channel.id) in {str(v) for v in adapter._voice_text_channels.values()}
        if adapter._extra_or_env_flag("auto_thread", "DISCORD_AUTO_THREAD", "true", truthy=True) and not skip and not linked:
            thread = await adapter._auto_create_thread(message, thread_name=adapter._derive_auto_thread_name(request.task))
            if thread is None:
                raise RuntimeError("Discord could not create the configured monitoring thread; no work was started.")
            return thread, message
        return channel, message

    async def dispatch(self, binding, request):
        service, db = self.assistant, self.assistant.db
        if db is None:
            raise RuntimeError("Durable project dispatch is unavailable; no work was started.")
        payload = {**binding.payload(), "task": request.task, "context": request.context}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        claim = await asyncio.to_thread(db.claim_voice_dispatch, request.request_id, service.owner_key, fingerprint, payload)
        if not claim["claimed"]:
            return ProjectDispatchReceipt(request.request_id, claim["status"], claim["payload"]).as_dict()
        admitted = attempted = False
        message = None
        try:
            target_source = self.source(binding)
            with service.runner._profile_scope_for_source(target_source):
                channel, message = await self.publish(binding, request)
            source = self.source(binding, channel, str(message.id))
            if source.parent_chat_id and not getattr(binding.channel, "parent_id", None):
                source.auto_thread_created = True
                source.auto_thread_initial_name = getattr(channel, "_hermes_auto_thread_initial_name", None)
            key = binding.adapter._source_session_key(source)
            payload.update(channel_id=str(channel.id), thread_id=getattr(source, "thread_id", None),
                message_id=str(message.id), session_key=key,
                monitor_url=discord_link(binding.guild_id, str(channel.id), str(message.id)))
            store = getattr(service.runner, "async_session_store", None)
            if store is not None:
                with service.runner._profile_scope_for_source(source):
                    entry = await store.get_or_create_session(source)
                payload["session_id"] = entry.session_id
            await asyncio.to_thread(db.update_voice_dispatch, request.request_id, "admitting", payload)
            prompt = binding.adapter._resolve_channel_prompt(str(channel.id), source.parent_chat_id)
            event = MessageEvent(text=request.task, source=source, message_type=MessageType.TEXT,
                message_id=str(message.id), raw_message=message, channel_prompt=prompt,
                auto_skill=binding.adapter._resolve_channel_skills(str(channel.id), source.parent_chat_id),
                channel_context=("[Relevant context from the requesting voice conversation]\n" + request.context
                                 if request.context else None), queue_only=True, allow_gateway_control=False,
                metadata={"voice_request_id": request.request_id})
            event._voice_project_receipt = (service, request.request_id, str(channel.id))
            service.authorize()
            self.source(binding, channel, str(message.id))  # Recheck after Discord I/O.
            from .session import DELEGATION_CONTEXT
            token = DELEGATION_CONTEXT.set(None)
            try:
                attempted = True
                await admit_internal_event(binding.adapter, event)
                admitted = True
            finally:
                DELEGATION_CONTEXT.reset(token)
            store = getattr(binding.adapter, "_session_store", None)
            payload["session_id"] = store.peek_session_id(key) if store is not None else ""
            await asyncio.to_thread(db.update_voice_dispatch, request.request_id, "accepted", payload)
            return ProjectDispatchReceipt(request.request_id, "accepted", payload).as_dict()
        except BaseException:
            # If admission's durable write fails, the prior 'admitting' receipt blocks replay.
            if not admitted:
                status = "uncertain" if attempted else "failed"
                await asyncio.to_thread(db.update_voice_dispatch, request.request_id, status, payload)
                if message is not None:
                    try:
                        await message.edit(content=safe_text(message.content) + (
                            "\nAdmission is uncertain; check this conversation before retrying." if attempted else
                            "\nThis request was not admitted; no project work was started."))
                    except Exception as exc:  # health: allow BLE001 -- preserve the original admission failure; a secondary SDK failure can contain auth headers
                        logging.getLogger(__name__).warning("Could not publish voice admission failure (%s)", type(exc).__name__)
            raise

    async def control(self, receipt, action, text):
        payload = receipt["payload"]
        binding = await self.resolve(f"{payload['profile']}:{payload['channel_id']}")
        source = self.source(binding)
        if binding.adapter._source_session_key(source) != payload.get("session_key"):
            raise ValueError("Project routing changed; attach the current project session before controlling it.")
        state = self.assistant.runner._peek_session_state(payload["session_key"])
        current = getattr(getattr(state, "turn", None), "event", None)
        if current is None or current.metadata.get("voice_request_id") != receipt["request_id"]:
            raise ValueError("This task is queued, finished, or no longer the active channel task; no other job was changed.")
        event = MessageEvent(text=("/steer " + text if action == "steer" else "/stop"),
                             source=source, message_type=MessageType.COMMAND)
        token = PROJECT_CONTEXT.set(None)
        from .session import DELEGATION_CONTEXT
        voice_token = DELEGATION_CONTEXT.set(None)
        try:
            await admit_internal_event(binding.adapter, event)
        finally:
            DELEGATION_CONTEXT.reset(voice_token)
            PROJECT_CONTEXT.reset(token)
        return {"request_id": receipt["request_id"], "status": action + "_requested",
                "monitor_url": payload["monitor_url"], "note": "The owning project agent confirms the result in Discord."}
