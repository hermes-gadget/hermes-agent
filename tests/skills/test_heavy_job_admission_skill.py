"""The bundled admission skill must be discoverable and readable by agents."""

import json
from pathlib import Path

import pytest


@pytest.mark.platforms("linux")
def test_admission_skill_is_available_through_native_skill_reader(tmp_path, monkeypatch):
    from tools import skills_tool

    bundled = Path(__file__).resolve().parents[2] / "skills"
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    (home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": [str(bundled)]}}), encoding="utf-8")
    indexed = {row["name"]: row for row in skills_tool.find_all_skills()}
    name = "heavy-job-admission"
    loaded = json.loads(skills_tool.skill_view(name, preprocess=False))
    assert loaded["success"] is True
    assert loaded["description"] == indexed[name]["description"]
    assert Path(loaded["skill_dir"]).is_relative_to(bundled)
    assert loaded["content"] == (Path(loaded["skill_dir"]) / "SKILL.md").read_text(encoding="utf-8")
