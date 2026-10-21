"""Voice-note replies are SUMMARIES, not readings (Ben 2026-10-01).

Long replies get condensed to a short spoken summary (~30 seconds) before TTS
synthesis; the full text reply still follows in the chat. Used by both
auto-TTS lanes: the adapter path (``gateway/platforms/base.py::_synthesize_auto_tts``)
and the runner path (``gateway/run_voice.py::_send_voice_reply``).

Config (``voice`` section):
  ``tts_summary``           bool, default True — set False to disable (read full text).
  ``tts_summary_max_chars`` int, default 500 — roughly 30 seconds of speech.

Aux task ``voice_summary`` (``auxiliary`` section) selects the model; when the
LLM call fails, we fall back to a sentence-boundary truncation so the voice note
still stays short.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 380  # ~30s spoken at normal pace (bf_emma measured ~2.3 words/s)

_SUMMARY_PROMPT = (
    "You condense an assistant's reply into a SPOKEN SUMMARY for a voice note. "
    "The full text is delivered separately in the chat, so this is a recap, not a reading. "
    "Rules: at most ~55 words (under 30 seconds spoken); plain conversational prose; "
    "no lists, no markdown, no emoji; same language as the reply; lead with what happened; "
    "keep exact numbers, names, dates and any action the user must take; drop pleasantries, "
    "repetition and meta-commentary."
)

_SHORTEN_PROMPT = (
    "This spoken summary for a voice note is too long. Rewrite it in at most 55 words "
    "(under 30 seconds spoken), keeping the most important facts, numbers, names and any "
    "action the user must take. Plain conversational prose, no lists, no markdown, no emoji, "
    "same language as the input. Return only the rewritten summary."
)


def _voice_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config().get("voice") or {}
    except Exception:
        return {}


def _extract_text(response) -> str:
    try:
        return (response.choices[0].message.content or "").strip()
    except Exception:
        return ""


def truncate_for_voice(text: str, max_chars: int) -> str:
    """Cut *text* to <= max_chars at a sentence boundary (hard cut + ellipsis if none)."""
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    best = -1
    for sep in (". ", "! ", "? "):
        best = max(best, window.rfind(sep))
    if best >= max_chars // 2:
        return window[: best + 1].strip()
    return window.rstrip() + "…"


async def summarize_for_voice(text: str) -> str:
    """Return a short spoken version of *text* for a voice note.

    Short text passes through unchanged. Long text is summarized via the
    auxiliary LLM (task ``voice_summary``); on any failure it is truncated at a
    sentence boundary. Disabled entirely by ``voice.tts_summary: false``.
    """
    cfg = _voice_config()
    if cfg.get("tts_summary", True) is False:
        return text
    try:
        max_chars = int(cfg.get("tts_summary_max_chars", DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    summary = ""
    try:
        from agent.auxiliary_client import async_call_llm
        for attempt, system_prompt in ((1, _SUMMARY_PROMPT), (2, _SHORTEN_PROMPT)):
            user_content = text[:8000] if attempt == 1 else summary[:2000]
            response = await async_call_llm(
                task="voice_summary",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=300,
                timeout=30,
                reasoning_config={"enabled": False},
            )
            summary = _extract_text(response) or ""
            if not summary:
                logger.warning("Voice summary attempt %d returned empty content", attempt)
                break
            if len(summary) <= max_chars * (1.0 if attempt == 1 else 1.1):
                return summary
    except Exception as exc:
        logger.warning("Voice summary failed (%s); truncating instead", exc)

    return truncate_for_voice(summary or text, max_chars)
