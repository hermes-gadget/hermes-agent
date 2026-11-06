"""Present bounded MCP startup diagnostics on the owning session's event stream."""
from agent.redact import redact_sensitive_text
from gateway.warning_notifications import render_notification


def publish_worker_mcp_warning(authority, session_id, generation, frame, policy, secrets=()) -> bool:
    if (frame.get('type') != 'mcp.warning' or set(frame) != {'type', 'message'}
            or not isinstance(frame['message'], str) or len(frame['message']) > 16384):
        return False
    message = frame['message']
    for secret in secrets:
        if secret:
            message = message.replace(secret, '***')
    message = redact_sensitive_text(message, force=True)
    render_notification(
        lambda: authority.publish_execution(session_id, generation, 'status.update',
                                             {'kind': 'mcp_warning', 'text': message}),
        platform=policy.platform, user_config=policy.config(authority))
    return True
