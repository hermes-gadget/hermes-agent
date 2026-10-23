"""Opaque signature envelopes must round-trip through tool output (#644)."""

import base64
import json
import logging

import pytest

from agent.redact import RedactingFormatter, redact_sensitive_text, redact_terminal_output


@pytest.mark.parametrize("surface", ["text", "terminal", "file", "log"])
@pytest.mark.parametrize("encode", [base64.b64encode, base64.urlsafe_b64encode])
def test_signature_payloads_survive_while_identical_outside_bytes_are_masked(surface, encode):
    pubkey = "AIza" + "A" * 32
    # The binary envelope deliberately contains the literal maskable pubkey in its base64.
    encoded = encode(b"public key: " + base64.b64decode(pubkey) + b"\xff\xff").decode()
    assert pubkey in encoded
    json_encoded = base64.b64encode(json.dumps({"pubkey": pubkey}).encode()).decode()
    tokens = [f"[sig1:{body}]" for body in (encoded, encoded.rstrip("="), json_encoded)]
    source = f"pubkey: {pubkey}\n" + "".join(tokens) + f"\noutside: {json_encoded}"

    def render(text):
        if surface == "terminal":
            return redact_terminal_output(text, "printf signatures")
        if surface == "log":
            record = logging.LogRecord("test", logging.INFO, "", 0, text, (), None)
            return RedactingFormatter("%(message)s").format(record)
        return redact_sensitive_text(text, force=True, file_read=surface == "file")

    result = render(source)
    assert result.splitlines()[1] == "".join(tokens)
    assert pubkey not in result.splitlines()[0]
    assert json_encoded not in result.splitlines()[2]
    assert render(result) == result


@pytest.mark.parametrize("wrapper", ["{}", "[sig1:{}", "[sig1: {}]", "[sig1:{}!]"])
def test_only_complete_opaque_signature_spans_are_exempt(wrapper):
    value = "eyJwdWJrZXkiOiJwdWJsaWMta2V5LXZhbHVlIn0="
    source = wrapper.format(value)
    assert value not in redact_sensitive_text(source, force=True)
