"""Voice turns coordinate native workers; conversational holds stay in the call."""
from pathlib import Path
import re

_COORDINATOR_TOOLS = frozenset(('delegate_task', 'clarify', 'tool_search', 'tool_describe', 'voice_assistant'))
_HOLD = re.compile(
    r'(?:(?:ok|okay) )?(?:hermes )?(?:just )?'
    r'(?:(?:one|a) (?:moment|second|sec)|(?:hang|hold) on(?: (?:one|a) (?:moment|second|sec))?'
    r'|give me (?:one|a) (?:moment|second|sec))(?: please)?(?: hermes)?'
)


def is_conversational_hold(text):
    normalized = ' '.join(re.sub(r'[^\w\s]', '', text.casefold()).split())
    return _HOLD.fullmatch(normalized) is not None


def enforce_voice_coordination(tool_name, **kwargs):
    """A native pre-tool veto, not an approval request or a replacement executor."""
    from .session import DELEGATION_CONTEXT
    from agent.subagent_lifecycle import get_active_subagent_parent
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home
    context = DELEGATION_CONTEXT.get()
    if not context:
        return
    session = context[0]
    if (Path(get_hermes_home()).resolve() != session.home.resolve()
            or not session.backend_session_key
            or get_session_env('HERMES_SESSION_KEY', '') != session.backend_session_key):
        return
    parent = get_active_subagent_parent()
    if parent is not None and getattr(getattr(session, 'cfg', None), 'assistant_enabled', False):
        manager = getattr(parent, '_memory_manager', None)
        memory_tools = getattr(manager, '_tool_to_provider', {})
        if tool_name == 'memory' or tool_name in memory_tools:
            return  # Keep explicit memory writes on the normal provider/approval path.
        if session.cfg.assistant_role == 'personal' and tool_name == 'cronjob':
            return  # A reminder is a scheduler transaction, not an unprivileged research worker.
    if (parent is None or getattr(parent, '_delegate_depth', 0) > 0
            or tool_name in _COORDINATOR_TOOLS):
        return
    return {'action': 'block', 'message': (
        'This live voice turn is a coordinator. Run tool work in a native subagent: '
        'call delegate_task with the user request, relevant context and existing permissions. '
        'Top-level delegation returns a background handle automatically. After confirmed dispatch, '
        'name the job and finish this turn so other requests can be admitted. '
        'Use delegate_task list/steer/stop to manage owned jobs. Do not execute the work in the parent '
        'or claim it has happened when only dispatch is confirmed.'
    )}
