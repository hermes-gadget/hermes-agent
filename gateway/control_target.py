"""Process-local execution guards for controls aimed at a previously observed run."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionTarget:
    session_key: str
    session_id: str
    generation: int
    agent: object

    def validate(self, runner, source):
        key = runner._session_key_for_source(source)
        state = runner._peek_session_state(key)
        adapter = runner._delivery_adapter_for(source)
        store = getattr(adapter, "_session_store", None)
        sid = store.peek_session_id(key) if store is not None else None
        if (key != self.session_key or str(sid or "") != self.session_id or state is None
                or state.persistent.run_generation != self.generation or state.turn.agent is not self.agent
                or self.agent is None):
            raise ValueError("The observed execution ended or changed; attach the current job before controlling it.")


def validate_execution(event, runner):
    target = event._execution_target
    if target is not None:
        if not isinstance(target, ExecutionTarget):
            raise ValueError("Invalid execution control target.")
        target.validate(runner, event.source)
