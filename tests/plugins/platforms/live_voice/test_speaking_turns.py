"""Playback permits are independent of quiet context and listening acknowledgments."""
import struct

from plugins.platforms.live_voice.turns import SpeakingTurn, requires_backend

PCM = struct.pack("<h", 1000) * 480


def test_context_cannot_speak_until_a_new_user_turn_and_acknowledgments_do_not_reopen_it():
    turn = SpeakingTurn()
    assert not turn.audio(PCM, 1, 0)
    assert turn.correction() and turn.correction() is None
    turn.input("Hello Hermes", 1000, 1200, 2)
    assert turn.audio(PCM, 3, 2)
    turn.settle(6)
    turn.input("Okay", 5000, 5200, 7)
    assert not turn.audio(PCM, 8, 7)
    turn.input("Mm-h", 7000, 7200, 9)
    turn.input("mm", 7200, 7400, 9.2)
    assert not turn.audio(PCM, 10, 9.2)
    turn.result()
    assert turn.audio(PCM, 11, 9.2)


def test_real_project_query_waits_for_backend_and_listening_does_not_reset_speech_budget():
    turn = SpeakingTurn(max_seconds=1)
    turn.input("What project have we got at the moment?", 0, 1000, 1)
    assert requires_backend(turn.utterance) and not turn.audio(PCM, 2, 1)
    turn.result()
    for i in range(50):
        assert turn.audio(PCM, 3 + i * .02, 1)
        turn.input("mm-hmm", 2000 + i * 1000, 2200 + i * 1000, 3 + i, playing=True)
    assert not turn.audio(PCM, 60, 1)
    assert turn.reason == "response_limit"
    assert not requires_backend("Hello Hermes, can you hear me properly?")
    assert not requires_backend("Mm-hmm. Okay, good.")


def test_barge_in_suppresses_pcm_while_user_speaks_but_does_not_cancel_work():
    turn = SpeakingTurn()
    turn.input("Please explain", 0, 400, 1)
    turn.barged = True
    assert not turn.audio(PCM, 2, 1.9)
    assert turn.audio(PCM, 3, 1.9)
    turn.wait_for_backend()
    turn.input("How are you?", 3000, 4000, 5)
    assert turn.audio(PCM, 6, 5)


def test_continuous_silence_cannot_keep_a_completed_reply_open():
    turn = SpeakingTurn()
    turn.input("Hello Hermes", 0, 400, 1)
    assert turn.audio(PCM, 2, 1)
    assert turn.audio(bytes(len(PCM)), 2.5, 1)
    assert not turn.audio(bytes(len(PCM)), 3.5, 1)
    turn.settle(5, playing=False)
    assert not turn.audio(PCM, 6, 1)
    turn.input("What is the weather like?", 5000, 6000, 7)
    assert turn.audio(PCM, 8, 7)


def test_idle_history_and_timestamp_changes_do_not_generate_new_fleet_context():
    from plugins.platforms.live_voice.fleet import FleetSnapshot
    row = {"project": "Radio", "state": "idle", "freshness": "current", "needs_input": False,
           "last_result": "Old workers were running", "observed_at": 1, "activity_at": 1}
    first = FleetSnapshot([row], 1, {"gateway": "current"}).quiet_summary()
    second = FleetSnapshot([{**row, "last_result": "Another old campaign report", "observed_at": 999,
                             "activity_at": 999}], 999, {"gateway": "current"}).quiet_summary()
    assert first == second and "Old workers" not in first and len(first.encode()) < 480
    running = FleetSnapshot([{**row, "state": "running"}], 2, {"gateway": "current"}).quiet_summary()
    assert running != first and "Radio" in running
