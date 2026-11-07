"""Private display behavior survives upstream's media-cleanup refactoring."""

import pytest

from gateway.platforms.base import BasePlatformAdapter


@pytest.mark.parametrize("verdict", ["true<ds_safety>[classifier result]Safe", "<ds_safety>internal</ds_safety>"])
def test_complete_provider_verdict_is_not_displayed(verdict):
    assert BasePlatformAdapter.strip_media_directives_for_display(verdict) == ""
    assert BasePlatformAdapter.strip_media_directives_for_display(verdict + "\nUseful answer.") == "Useful answer."


@pytest.mark.parametrize("text", ["A useful answer without media.", "An incomplete <ds_safety> marker."])
def test_ordinary_text_and_incomplete_markers_survive(text):
    assert BasePlatformAdapter.strip_media_directives_for_display(text) == text
