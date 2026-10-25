"""The bundled admission skill must be discoverable and readable by agents."""

import json
from pathlib import Path

import pytest


@pytest.mark.platforms("linux")
def test_admission_skill_is_available_through_native_skill_reader(tmp_path, monkeypatch):
    from tools import skills_tool

    bundled = Path(__file__).resolve().parents[2] / "skills"
    active = tmp_path / "skills"
    monkeypatch.setattr(skills_tool, "_skill_search_dirs", lambda: ([], [bundled], active))
    indexed = {row["name"]: row for row in skills_tool._find_all_skills()}
    name = "heavy-job-admission"
    loaded = json.loads(skills_tool.skill_view(name, preprocess=False))
    assert loaded["success"] is True
    assert loaded["description"] == indexed[name]["description"]
    assert Path(loaded["skill_dir"]).is_relative_to(bundled)
    assert loaded["content"] == (Path(loaded["skill_dir"]) / "SKILL.md").read_text(encoding="utf-8")
