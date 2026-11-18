"""FR-01: child projects (the option, then the behaviour)."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from q3tui.core import settings
from q3tui.core.cli import main
from q3tui.core.config import load_config
from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.pipeline.engine import Engine


def test_the_option_is_a_list_of_project_folders(tmp_path):
    (tmp_path / "q3tui.yaml").write_text("pipeline: {step_options: {rtl: {child_projects: [../cha, /abs/chb]}}}\n")
    assert load_config(cwd=tmp_path).pipeline.step_options["rtl"]["child_projects"] == ["../cha", "/abs/chb"]
    (tmp_path / "q3tui.yaml").write_text("pipeline: {step_options: {rtl: {max_fix_rounds: 3}}}\n")
    load_config(cwd=tmp_path)  # absent is fine
    for bad in ("../cha", "[1, 2]", "['']"):
        (tmp_path / "q3tui.yaml").write_text(f"pipeline: {{step_options: {{rtl: {{child_projects: {bad}}}}}}}\n")
        with pytest.raises(ValueError, match="child_projects"):
            load_config(cwd=tmp_path)


def test_the_option_is_in_settings_and_config_show(tmp_path):
    root = tmp_path / "par"
    root.mkdir()
    (root / "q3tui.yaml").write_text("pipeline: {flow: rtl, step_options: {rtl: {child_projects: [../cha]}}}\n")
    eng = Engine(Project.open(root))
    s = settings.find("pipeline.step_options.rtl.child_projects", eng.flow.settings)
    assert s.kind == "lines" and "read-only" in s.help and "filelist.f" in s.help
    assert any(x.path == s.path for t in Ops(eng).settings_tabs() if t.id == "rtl" for x in t.settings)
    assert Ops(eng).settings()[s.path] == ["../cha"]
    r = CliRunner().invoke(main, ["-C", str(root), "config", "show"])
    assert r.exit_code == 0 and "child_projects" in r.output and "../cha" in r.output
