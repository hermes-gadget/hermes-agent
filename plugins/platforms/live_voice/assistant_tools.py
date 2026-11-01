"""Optional platform-owned tools. A live authenticated call is required at execution."""
import asyncio
import concurrent.futures
import json

from tools.registry import registry, tool_error


def handle(args, **kwargs):
    from .session import DELEGATION_CONTEXT
    context = DELEGATION_CONTEXT.get()
    if not context or not getattr(context[0].cfg, "assistant_enabled", False):
        return tool_error("This tool requires an active live assistant call.")
    session, delegation_id = context
    if not isinstance(delegation_id, str):
        return tool_error("Background notifications cannot dispatch project work.")
    from hermes_constants import get_hermes_home
    from gateway.session_context import get_session_env
    from agent.subagent_lifecycle import get_active_subagent_parent
    parent = get_active_subagent_parent()
    if (get_hermes_home().resolve() != session.home.resolve()
            or get_session_env("HERMES_SESSION_KEY", "") != session.backend_session_key
            or parent is not None and getattr(parent, "_delegate_depth", 0) > 0):
        return tool_error("This tool belongs to the originating voice coordinator and profile.")
    try:
        # Model tools run in the agent thread; all gateway operations remain on its event loop.
        future = asyncio.run_coroutine_threadsafe(session.assistant.handle(args, delegation_id), session.loop)
        return json.dumps(future.result(timeout=45), ensure_ascii=False)
    except concurrent.futures.TimeoutError:
        # Do not cancel a dispatch whose admission may have succeeded. Its durable receipt settles it.
        return tool_error("Assistant request is still settling. Reuse the same request_id; do not start a duplicate.")
    except (ValueError, RuntimeError, OSError) as exc:
        return tool_error(str(exc))


def register_tools():
    registry.register(
        name="voice_assistant", toolset="live_voice", handler=handle,
        description="Live assistant project dispatch and verified agent status", emoji="🎙️",
        schema={
            "name": "voice_assistant",
            "description": (
                "Coordinate an authenticated live call: discover designated Discord project channels, "
                "start visible project work in their normal agent sessions, select a project, inspect "
                "all authorized agents, and steer or stop a dispatched job. Reuse request_id on retries. "
                "Dispatch acceptance is not completion; use returned monitoring links and verified status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["projects", "select", "dispatch", "continue", "agents", "steer", "stop"]},
                    "project": {"type": "string", "description": "Project name or designated Discord channel ID."},
                    "task": {"type": "string", "description": "The requested work, or correction for steer."},
                    "context": {"type": "string", "description": "Only context relevant to the project task."},
                    "request_id": {"type": "string", "description": "Stable task receipt ID; required for steer/stop and dispatch retries."},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        })
