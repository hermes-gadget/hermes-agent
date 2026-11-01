"""Application-owned speaking turns; context updates cannot open a new reply."""
import re
import struct

ACK_WORDS = frozenset({"yes", "yeah", "yep", "yup", "no", "okay", "ok", "good", "great", "fine",
    "right", "alright", "cool", "thanks", "thank", "you", "mm", "mmm", "mmh", "mmhm", "mmhmm",
    "mhm", "hmm", "hm", "uh", "huh", "m", "h"})


def normalized(text):
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def acknowledgment(text):
    words = normalized(text).split()
    return not words or all(w in ACK_WORDS for w in words)


def requires_backend(text):
    """Clear inventory/status queries must not depend on the frontend emitting a token."""
    value = normalized(text)
    return bool(re.search(
        r"\b(?:what|which|how many)\b.{0,70}\b(?:projects?|agents?|jobs?|tasks?)\b|"
        r"\b(?:project|agent|job|task)\b.{0,35}\b(?:status|progress)\b|"
        r"\b(?:what s|what is|who s|who is)\s+(?:currently\s+)?(?:running|working|blocked)\b|"
        r"\b(?:status|progress)\s+(?:of|on)\b", value))


def voiced(pcm):
    return any(abs(sample[0]) >= 128 for sample in struct.iter_unpack("<h", pcm))


class SpeakingTurn:
    def __init__(self, max_seconds=20):
        self.max_seconds = max_seconds
        self.open = False
        self.awaiting_backend = False
        self.barged = False
        self.reason = "startup"
        self.number = 0
        self.spoken_seconds = 0.0
        self.spoken_bytes = 0
        self.last_voiced = None
        self.last_input_at = None
        self.last_input_end = None
        self.utterance = ""
        self.granted = False
        self.corrected = False
        self.suppressed_bytes = 0

    def input(self, text, start_ms, end_ms, now, *, playing=False):
        gap = (self.last_input_end is not None and type(start_ms) in (int, float)
               and start_ms - self.last_input_end > 600)
        new = self.last_input_at is None or gap or now - self.last_input_at > .8
        if new:
            self.utterance = ""
            self.granted = False
        self.utterance = (self.utterance + text)[-4000:]
        self.last_input_at = now
        self.last_input_end = end_ms if type(end_ms) in (int, float) else None
        if acknowledgment(self.utterance):
            if not playing:
                self.close("acknowledgment")
            return
        from .coordination import is_conversational_hold
        if is_conversational_hold(self.utterance):
            self.close("hold")
            return
        if not self.granted:
            self.number += 1
            self.open = True
            self.awaiting_backend = False
            self.spoken_seconds = 0
            self.spoken_bytes = 0
            self.last_voiced = None
            self.corrected = False
            self.reason = "user_request"
            self.granted = True
        if requires_backend(self.utterance):
            self.wait_for_backend()

    def close(self, reason):
        self.open = False
        self.reason = reason

    def wait_for_backend(self):
        self.awaiting_backend = True
        self.close("backend_pending")

    def result(self):
        self.number += 1
        self.open = True
        self.awaiting_backend = False
        self.spoken_seconds = 0
        self.spoken_bytes = 0
        self.last_voiced = None
        self.corrected = False
        self.reason = "verified_result"

    def audio(self, pcm, now, last_user_audio_at):
        speaking = voiced(pcm)
        if self.barged and now - last_user_audio_at < .3:
            self.suppressed_bytes += len(pcm)
            return False
        if not self.open or self.awaiting_backend:
            self.suppressed_bytes += len(pcm)
            return False
        # Live can stream silence continuously. Stop replenishing playback after
        # a quiet tail so the buffer can drain and settle() can finish the reply.
        if not speaking and self.last_voiced is not None and now - self.last_voiced >= 1:
            return False
        if speaking:
            if self.spoken_bytes + len(pcm) > self.max_seconds * 48000:
                self.close("response_limit")
                self.suppressed_bytes += len(pcm)
                return False
            self.spoken_bytes += len(pcm)
            self.spoken_seconds = self.spoken_bytes / 48000
            self.last_voiced = now
        return True

    def settle(self, now, *, playing=False):
        if self.open and self.last_voiced is not None and not playing and now - self.last_voiced >= 2:
            self.close("reply_finished")

    def correction(self):
        if self.open or self.corrected:
            return None
        self.corrected = True
        if self.reason == "startup":
            return "This is a new call. Stop speaking and wait for Ben to speak. Historical notes are reference data; do not resume or recap old conversations."
        if self.reason == "backend_pending":
            return "A fresh backend lookup is pending. Stop answering from history and wait for its verified result. Do not repeat acknowledgments or status."
        return "This reply is complete. Stop speaking and wait for a new substantive user request. Listening sounds and acknowledgments do not request another update. Background work continues."
