"""The round-trip emitter must reproduce the document's own style, not impose one.

ruamel cannot restore a multi-line plain scalar's fold points once loaded, so a single
global width/indent setting reflows every long value it did not touch. A one-item
``plugins.enabled`` edit rewrote ~988 lines of the real config.yaml this way.
"""

import io

import pytest
from ruamel.yaml import YAML

import hermes_yaml

DOC = {
    "personalities": {
        "kawaii": "You are a kawaii assistant. Use cute expressions like (◕‿◕), ★, ♪, and ~! "
                  "Add sparkles and be super enthusiastic about everything. Every response "
                  "should feel warm and adorable desu~! ヽ(>∀<☆)ノ"
    },
    "plugins": {"enabled": ["argus-dashboard", "delete-guard"]},
}


def _authored(width, sequence, offset):
    """A document exactly as some other writer (or an older Hermes) produced it."""
    y = YAML(typ="rt")
    y.width = width
    y.preserve_quotes = True
    y.allow_unicode = True
    y.default_flow_style = False
    y.indent(mapping=2, sequence=sequence, offset=offset)
    buf = io.StringIO()
    y.dump(DOC, buf)
    return buf.getvalue()


@pytest.mark.parametrize("width,sequence,offset", [
    (120, 2, 0),      # Ben's authored style: folded at 120, zero-indent sequences
    (80, 4, 2),       # ruamel's stock style
    (100, 2, 0),      # another plausible width
    (2**31 - 1, 4, 2),  # current hard-coded no-fold settings
])
def test_probe_reproduces_the_document_it_was_given(width, sequence, offset):
    text = _authored(width, sequence, offset)
    editor = hermes_yaml.roundtrip_yaml_for(text)
    buf = io.StringIO()
    editor.dump(editor.load(text), buf)
    assert buf.getvalue() == text
