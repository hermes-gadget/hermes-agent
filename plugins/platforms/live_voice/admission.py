"""Bind live call objects to their own admitted turn, never the FIFO drain's caller.

Only private in-process adapter attributes enter this map. Persisted metadata
keeps correlation IDs; it can never recreate a live call or grant its authority.
"""
from contextlib import contextmanager


def remember_turn(authority, event, row):
    if row['status'] != 'queued':
        return
    voice = getattr(event, '_gpt_live_delegation', None)
    project = getattr(event, '_voice_project_receipt', None)
    if voice is None and project is None:
        return
    bindings = getattr(authority, '_voice_admission_bindings', None)
    if bindings is None:
        bindings = authority._voice_admission_bindings = {}
    # A provider re-delivery must never replace a committed turn's private owner.
    bindings.setdefault(row['admission_id'], (voice, project))


def forget_turn(authority, admission_id):
    return getattr(authority, '_voice_admission_bindings', {}).pop(admission_id, (None, None))


@contextmanager
def voice_turn(authority, row, event):
    from .projects import PROJECT_CONTEXT
    from .session import DELEGATION_CONTEXT
    voice, project = forget_turn(authority, row['admission_id'])
    if project and (project[1] != event.metadata.get('voice_request_id')
                    or project[2] != str(event.source.chat_id)):
        project = None
    if voice is not None:
        event._gpt_live_delegation = voice
    if project is not None:
        event._voice_project_receipt = project
    voice_token, project_token = DELEGATION_CONTEXT.set(voice), PROJECT_CONTEXT.set(project)
    try:
        yield
    finally:
        PROJECT_CONTEXT.reset(project_token)
        DELEGATION_CONTEXT.reset(voice_token)
