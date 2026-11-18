"""FR-01: child projects (the option, then the behaviour)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from q3tui.core import history, settings
from q3tui.core.cli import main
from q3tui.core.config import Config, load_config
from q3tui.core.ops import Ops
from q3tui.core.project import Project, sha256_file
from q3tui.flows.rtl.lib import children
from q3tui.flows.spec.lib import artifacts
from q3tui.pipeline.base import StepFailed
from q3tui.pipeline.engine import Engine
from tests.fakes import use_test_flow
from tests.test_rtl_gen import calls, config_json, llm, run_step, tools_dir, tools_yaml  # noqa: F401 (fixtures)

# the child modules of FR-01 §8 (projects ../cha and ../chb)
CHA = """module m_cha_top #(
  parameter int unsigned PARA_W = 8
) (
  input  logic              i_clk,
  input  logic              i_rst_n,
  input  logic              i_en,
  output logic [PARA_W-1:0] o_cnt
);
  always_ff @(posedge i_clk or negedge i_rst_n) begin
    if (!i_rst_n)  o_cnt <= '0;
    else if (i_en) o_cnt <= o_cnt + 1'b1;
  end
endmodule
"""

CHB = """module m_chb_top (
  input  logic       i_clk,
  input  logic       i_rst_n,
  input  logic [7:0] i_d,
  output logic [7:0] o_q
);
  always_ff @(posedge i_clk or negedge i_rst_n) begin
    if (!i_rst_n) o_q <= '0;
    else          o_q <= i_d;
  end
endmodule
"""


def make_child(root: Path, name: str, files: dict[str, str], lines: list[str] | None = None, registry: dict | None = None,
               yaml: str = "{}\n") -> Path:
    """A child project `<root>/<name>` as its rtl flow leaves it: q3tui.yaml, src/rtl/<file> and src/rtl/filelist.f (default: each file,
    `src/rtl/<file>` per line), plus src/rtl/child/provided.json when given. Returns its root."""
    child = root / name
    rtl = child / "src" / "rtl"
    rtl.mkdir(parents=True, exist_ok=True)
    (child / "q3tui.yaml").write_text(yaml)
    for f, text in files.items():
        (rtl / f).parent.mkdir(parents=True, exist_ok=True)
        (rtl / f).write_text(text)
    (rtl / "filelist.f").write_text("\n".join(lines if lines is not None else [f"src/rtl/{f}" for f in files]) + "\n")
    if registry is not None:
        (rtl / "child").mkdir(exist_ok=True)
        (rtl / "child" / "provided.json").write_text(json.dumps(registry))
    return child


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


# -- children.py: resolve, copy, registry, clashes, nesting, child status ---------------------------------------------

X = "module m_x (\n  input  logic i_a,\n  output logic o_b\n);\n  assign o_b = i_a;\nendmodule\n"
PE = "module m_pe_top (\n  input  logic i_a,\n  output logic o_b\n);\n  assign o_b = ~i_a;\nendmodule\n"
CORE = "module m_core_top (\n  input  logic i_a,\n  output logic o_b\n);\n  m_pe_top u_pe (.i_a(i_a), .o_b(o_b));\nendmodule\n"


def _par(tmp_path) -> tuple[Path, Project, Path]:
    """This project for the copy's own tests (`<tmp>/par`, beside its children): its root, the Project, its RTL folder."""
    par = (tmp_path / "par").resolve()
    par.mkdir()
    return par, Project(par, Config()), par / "src" / "rtl"


def _pe_registry(pe: Path) -> dict:
    """npu_core's registry (as its own rtl step left it): `m_pe_top` came from its child ../npu_pe."""
    return {"m_pe_top": {"kind": "child", "owner": "../npu_pe", "child": "../npu_pe", "source": "../npu_pe/src/rtl/m_pe_top.sv",
                         "sha256": sha256_file(pe / "src/rtl/m_pe_top.sv"), "file": "m_pe_top.sv", "unit": "module", "instances": [], "top": True}}


def test_copy_puts_the_listed_files_and_records_them(tmp_path):
    """T2: the files a child's filelist lists are copied byte for byte and recorded (owner, source, sha256); what it does not list is not."""
    cha = make_child(tmp_path, "cha", {"m_cha_top.sv": CHA, "tb_smoke.sv": "module tb_smoke; endmodule\n"},
                     lines=["// the design", "", "src/rtl/m_cha_top.sv"])
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    par, project, rtl = _par(tmp_path)
    res = children.copy(project, rtl, children.resolve(par, ["../cha", "../chb"]), set())
    assert (rtl / "m_cha_top.sv").read_bytes() == (cha / "src/rtl/m_cha_top.sv").read_bytes() and not (rtl / "tb_smoke.sv").exists()
    reg = children.read_registry(rtl)
    assert list(reg) == ["m_cha_top", "m_chb_top"] and reg["m_cha_top"].items() >= {
        "kind": "child", "owner": "../cha", "child": "../cha", "source": "../cha/src/rtl/m_cha_top.sv", "file": "m_cha_top.sv"}.items()
    assert reg["m_cha_top"]["sha256"] == sha256_file(cha / "src/rtl/m_cha_top.sv") and reg["m_cha_top"]["top"] is True
    assert reg["m_cha_top"]["unit"] == "module" and reg["m_chb_top"]["top"] is True and (rtl / "m_chb_top.sv").read_text() == CHB
    assert res.registry == reg and res.copied == ["m_cha_top", "m_chb_top"] and not (res.restored or res.removed or res.warnings)
    again = children.copy(project, rtl, children.resolve(par, ["../cha", "../chb"]), set())
    assert again.registry == reg and not (again.copied or again.restored or again.removed)  # (nothing changed: nothing written)


def test_the_child_rtl_folder_comes_from_its_own_config(tmp_path):
    """The child's RTL folder is where its own rtl step keeps it: its q3tui.yaml's `project.rtl_dir`, or the rtl step's option `rtl_dir`."""
    cha = tmp_path / "cha"
    (cha / "hw").mkdir(parents=True)
    (cha / "q3tui.yaml").write_text("project: {rtl_dir: hw}\n")
    (cha / "hw" / "m_cha_top.sv").write_text(CHA)
    (cha / "hw" / "filelist.f").write_text("hw/m_cha_top.sv\n")
    par, project, rtl = _par(tmp_path)
    (child,) = children.resolve(par, ["../cha"])
    assert child.rtl_dir == cha.resolve() / "hw" and child.filelist == cha.resolve() / "hw" / "filelist.f" and children.problems(par, [child]) == []
    children.copy(project, rtl, [child], set())
    assert (rtl / "m_cha_top.sv").read_text() == CHA and children.read_registry(rtl)["m_cha_top"]["source"] == "../cha/hw/m_cha_top.sv"
    (cha / "q3tui.yaml").write_text("project: {rtl_dir: hw}\npipeline: {step_options: {rtl: {rtl_dir: ip/rtl}}}\n")
    assert children.resolve(par, ["../cha"])[0].rtl_dir == cha.resolve() / "ip" / "rtl"  # (the rtl step's own option first)


def test_problems_name_the_child(tmp_path):
    """A child that cannot be used stops the copy before anything is written, with a message naming it as this project names it."""
    par, project, rtl = _par(tmp_path)
    (tmp_path / "plain").mkdir()  # (a folder, not a Q3TUI project)
    (tmp_path / "fresh").mkdir()
    (tmp_path / "fresh" / "q3tui.yaml").write_text("{}\n")  # (its rtl flow never ran: no filelist)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "q3tui.yaml").write_text("pipeline: {nope: 1}\n")
    make_child(tmp_path, "loop", {"m_l.sv": "module m_l; endmodule\n"}, yaml="pipeline: {step_options: {rtl: {child_projects: [../par]}}}\n")
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    kids = children.resolve(par, ["../gone", "../plain", "../fresh", "../bad", ".", "../loop", "../cha"])
    found = children.problems(par, kids)
    assert len(found) == 6 and all(p.startswith(f"child project {k}") for k, p in zip(["../gone", "../plain", "../fresh", "../bad", ".", "../loop"], found))
    gone, plain, fresh, bad, itself, loop = found
    assert "no folder" in gone and "no q3tui.yaml" in plain and fresh == "child project ../fresh: no src/rtl/filelist.f (run its rtl flow first)"
    assert "pipeline.nope" in bad and itself == "child project . is this project itself" and "cycle" in loop
    with pytest.raises(children.ChildError, match="run its rtl flow first"):
        children.copy(project, rtl, kids, set())
    assert not rtl.exists()


def test_filelist_options_and_misnamed_files_stop(tmp_path):
    """The copy is flat and keeps the files as they are: an option in the filelist, a file that does not hold the unit it is named after, or a
    file that is not SystemVerilog / Verilog stops it."""
    par, project, rtl = _par(tmp_path)
    make_child(tmp_path, "inc", {"m_cha_top.sv": CHA}, lines=["+incdir+x", "src/rtl/m_cha_top.sv"])
    make_child(tmp_path, "sub", {"m_cha_top.sv": CHA}, lines=["-f other.f"])
    make_child(tmp_path, "misnamed", {"foo.sv": "module bar; endmodule\n"})
    make_child(tmp_path, "two", {"m_two.sv": "module m_two; endmodule\nmodule m_helper; endmodule\n"})
    make_child(tmp_path, "hdr", {"m_defs.svh": "`define W 8\n"})

    def stop(entry: str) -> str:
        with pytest.raises(children.ChildError) as e:
            children.copy(project, rtl, children.resolve(par, [entry]), set())
        return str(e.value)

    assert stop("../inc") == "../inc: its filelist.f has the option `+incdir+x`"
    assert stop("../sub") == "../sub: its filelist.f has the option `-f other.f`"
    assert stop("../misnamed") == "../misnamed: src/rtl/foo.sv declares `bar`: a child's file must hold the unit it is named after"
    assert stop("../two") == "../two: src/rtl/m_two.sv declares `m_helper`, `m_two`: a child's file must hold the unit it is named after, and only it"
    assert stop("../hdr") == "../hdr: its filelist.f lists src/rtl/m_defs.svh, not a .sv / .v file"
    assert not rtl.exists()  # (nothing was copied)


def test_a_missing_listed_file_stops_with_its_name(tmp_path):
    cha = make_child(tmp_path, "cha", {"m_cha_top.sv": CHA}, lines=["src/rtl/m_cha_top.sv", "src/rtl/m_gone.sv"])
    par, project, rtl = _par(tmp_path)
    kids = children.resolve(par, ["../cha"])
    with pytest.raises(children.ChildError, match=r"^\.\./cha: its filelist\.f lists src/rtl/m_gone\.sv, which is not there \(/.*/cha/src/rtl/m_gone\.sv\)$"):
        children.copy(project, rtl, kids, set())
    assert not (rtl / "m_cha_top.sv").exists()  # (everything is checked before anything is copied)
    c = cha.resolve()  # the step's inputs never raise, and hold the missing file too: the step goes stale when it appears
    assert children.input_files(kids) == [c / "q3tui.yaml", c / "src/rtl/filelist.f", c / "src/rtl/m_cha_top.sv", c / "src/rtl/m_gone.sv"]


def test_the_same_child_listed_twice_is_one_child(tmp_path, monkeypatch):
    cha = make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    par, project, rtl = _par(tmp_path)
    monkeypatch.setenv("CHILDREN", str(tmp_path))
    kids = children.resolve(par, ["../cha", " ../cha/ ", str(cha), "$CHILDREN/cha", "../par/../cha"])
    assert [(c.entry, c.label, c.root) for c in kids] == [("../cha", "../cha", cha.resolve())]
    res = children.copy(project, rtl, kids, set())
    assert list(res.registry) == ["m_cha_top"] and res.copied == ["m_cha_top"]


def test_an_identical_module_in_two_children_is_copied_once_and_different_ones_stop(tmp_path):
    """T7 (R8)."""
    make_child(tmp_path, "a", {"m_x.sv": X})
    b = make_child(tmp_path, "b", {"m_x.sv": X})
    par, project, rtl = _par(tmp_path)
    res = children.copy(project, rtl, children.resolve(par, ["../a", "../b"]), set())
    assert list(res.registry) == ["m_x"] and res.registry["m_x"]["child"] == "../a" and res.copied == ["m_x"]
    (b / "src/rtl/m_x.sv").write_text(X.replace("assign o_b = i_a;", "assign o_b = !i_a;"))
    with pytest.raises(children.ChildError) as e:
        children.copy(project, rtl, children.resolve(par, ["../a", "../b"]), set())
    assert "../a/src/rtl/m_x.sv" in str(e.value) and "../b/src/rtl/m_x.sv" in str(e.value)
    assert (rtl / "m_x.sv").read_text() == X  # (nothing changed here)


def test_a_child_module_named_like_this_projects_own_module_stops(tmp_path):
    """R8: a unit this project makes itself (`own`: the csr step's), or a file of that name here that is not a copy, stops the copy naming both;
    a file here with the child's very bytes is taken as the copy."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    par, project, rtl = _par(tmp_path)
    kids = children.resolve(par, ["../cha"])
    with pytest.raises(children.ChildError) as e:
        children.copy(project, rtl, kids, {"m_cha_top"})
    assert str(e.value) == ("`m_cha_top` of the child project ../cha (../cha/src/rtl/m_cha_top.sv) is not the same file as this project's own "
                            "src/rtl/m_cha_top.sv (made here by the csr step, with the APB-CSR-Generator): both projects must use the same "
                            "APB-CSR-Generator version, or rename one of them")
    rtl.mkdir(parents=True)
    mine = "module m_cha_top (input logic i_clk);\nendmodule\n"
    (rtl / "m_cha_top.sv").write_text(mine)  # (this project's own file, not a copy)
    with pytest.raises(children.ChildError, match=r"\(\.\./cha/src/rtl/m_cha_top\.sv\) has the name of this project's own module \(src/rtl/m_cha_top\.sv\)"):
        children.copy(project, rtl, kids, set())
    assert (rtl / "m_cha_top.sv").read_text() == mine and not children.registry_file(rtl).exists()
    (rtl / "m_cha_top.sv").write_text(CHA)  # (the child's very bytes: adopted)
    res = children.copy(project, rtl, kids, set())
    assert list(res.registry) == ["m_cha_top"] and not (res.copied or res.restored)


SYNC = "module m_csr_sync (\n  input  logic i_clk,\n  input  logic i_d,\n  output logic o_q\n);\n  assign o_q = i_d;\nendmodule\n"  # (the generator's model)
CHS = "module m_cha_top (\n  input  logic i_clk,\n  input  logic i_d,\n  output logic o_q\n);\n  m_csr_sync u_sync (.i_clk(i_clk), .i_d(i_d), .o_q(o_q));\nendmodule\n"


def test_the_generators_model_made_here_and_by_a_child_is_this_projects(tmp_path):
    """Ruling 14 (I2): a unit this project makes itself (`own`: the csr step's, here the APB-CSR-Generator's synchronizer model, whose name is the
    tool's) that a child provides with the very same bytes is no clash: the file stays this project's, neither copied nor recorded (the child's
    module that instantiates it still names it). So it goes for a copy recorded before this project made it (its async CSR turned on later): it
    leaves the registry, and its file stays."""
    make_child(tmp_path, "cha", {"m_csr_sync.sv": SYNC, "m_cha_top.sv": CHS})
    par, project, rtl = _par(tmp_path)
    rtl.mkdir(parents=True)
    (rtl / "m_csr_sync.sv").write_text(SYNC)  # (this project's csr step made the very same file)
    kids = children.resolve(par, ["../cha"])
    res = children.copy(project, rtl, kids, {"m_csr_sync", "m_par_csr"})
    assert res.kept == ["m_csr_sync"] and res.copied == ["m_cha_top"] and list(children.read_registry(rtl)) == ["m_cha_top"]
    assert (rtl / "m_csr_sync.sv").read_text() == SYNC and children.read_registry(rtl)["m_cha_top"]["instances"] == ["m_csr_sync"]
    # the model was a copy first (no CSR here yet), then this project's csr step made the same file
    (rtl / "m_csr_sync.sv").unlink()
    children.copy(project, rtl, kids, set())
    assert list(children.read_registry(rtl)) == ["m_csr_sync", "m_cha_top"]
    res = children.copy(project, rtl, kids, {"m_csr_sync", "m_par_csr"})
    assert res.kept == ["m_csr_sync"] and list(children.read_registry(rtl)) == ["m_cha_top"] and (rtl / "m_csr_sync.sv").read_text() == SYNC
    assert not (res.copied or res.restored or res.removed) and not list((par / ".q3tui").rglob("m_csr_sync.sv"))  # (not removed, not backed up)


def test_the_generators_model_made_here_and_by_a_child_with_other_bytes_stops(tmp_path):
    """Ruling 14 (I2): the same generator model with other bytes (another APB-CSR-Generator version) cannot be both: the copy stops naming both
    files, the model and what to do, before anything is written."""
    make_child(tmp_path, "cha", {"m_csr_sync.sv": SYNC.replace("assign o_q = i_d;", "always_ff @(posedge i_clk) o_q <= i_d;"), "m_cha_top.sv": CHA})
    par, project, rtl = _par(tmp_path)
    rtl.mkdir(parents=True)
    (rtl / "m_csr_sync.sv").write_text(SYNC)
    with pytest.raises(children.ChildError) as e:
        children.copy(project, rtl, children.resolve(par, ["../cha"]), {"m_csr_sync", "m_par_csr"})
    assert str(e.value) == ("`m_csr_sync` of the child project ../cha (../cha/src/rtl/m_csr_sync.sv) is not the same file as this project's own "
                            "src/rtl/m_csr_sync.sv (the APB-CSR-Generator's synchronizer model, made here by the csr step): both projects must use "
                            "the same APB-CSR-Generator version, or rename one of them")
    assert (rtl / "m_csr_sync.sv").read_text() == SYNC and not (rtl / "m_cha_top.sv").exists() and not children.registry_file(rtl).exists()


def test_a_hand_edited_copy_is_restored(tmp_path):
    """R7: a copy edited (or deleted) here is the child's file again, backed up first; a changed child file is a new copy, not a restore."""
    cha = make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    par, project, rtl = _par(tmp_path)
    kids = children.resolve(par, ["../cha"])
    children.copy(project, rtl, kids, set())
    (rtl / "m_cha_top.sv").write_text(CHA.replace("endmodule", "  // edited here\nendmodule"))
    res = children.copy(project, rtl, kids, set())
    assert res.restored == ["m_cha_top"] and not res.copied and (rtl / "m_cha_top.sv").read_text() == CHA
    (kept,) = (par / ".q3tui" / "bkp").rglob("m_cha_top.sv")
    assert "// edited here" in kept.read_text()
    (cha / "src/rtl/m_cha_top.sv").write_text(CHA + "// v2\n")
    res = children.copy(project, rtl, kids, set())
    assert res.copied == ["m_cha_top"] and not res.restored and (rtl / "m_cha_top.sv").read_text() == CHA + "// v2\n"
    (rtl / "m_cha_top.sv").unlink()
    assert children.copy(project, rtl, kids, set()).restored == ["m_cha_top"] and (rtl / "m_cha_top.sv").read_text() == CHA + "// v2\n"


def test_a_unit_no_longer_provided_is_removed(tmp_path):
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    par, project, rtl = _par(tmp_path)
    children.copy(project, rtl, children.resolve(par, ["../cha", "../chb"]), set())
    res = children.copy(project, rtl, children.resolve(par, ["../cha"]), set())
    assert res.removed == ["m_chb_top"] and not (rtl / "m_chb_top.sv").exists() and list(children.read_registry(rtl)) == ["m_cha_top"]
    assert [f.read_text() for f in (par / ".q3tui" / "bkp").rglob("m_chb_top.sv")] == [CHB]  # (backed up first)
    res = children.copy(project, rtl, [], set())  # (no child project any more: the copies and the registry go)
    assert res.removed == ["m_cha_top"] and not (rtl / "m_cha_top.sv").exists() and not (rtl / "child").exists()


def test_nested_files_keep_their_owner(tmp_path):
    """T8 (R9): a child that is itself a parent passes on what it got from its own children, recorded with their owner (its registry says
    who); a copy of it there that is not its owner's file is used, with a warning naming both projects."""
    pe = make_child(tmp_path, "npu_pe", {"m_pe_top.sv": PE})
    make_child(tmp_path, "npu_core", {"m_pe_top.sv": PE, "m_core_top.sv": CORE}, registry=_pe_registry(pe))
    par, project, rtl = _par(tmp_path)
    kids = children.resolve(par, ["../npu_core"])
    res = children.copy(project, rtl, kids, set())
    assert list(res.registry) == ["m_pe_top", "m_core_top"] and not res.warnings
    assert res.registry["m_pe_top"].items() >= {"owner": "../npu_pe", "child": "../npu_core", "source": "../npu_core/src/rtl/m_pe_top.sv"}.items()
    assert "top" not in res.registry["m_pe_top"]  # (this project instantiates the middle project's top)
    assert res.registry["m_core_top"].items() >= {"owner": "../npu_core", "child": "../npu_core", "instances": ["m_pe_top"], "top": True}.items()
    (pe / "src/rtl/m_pe_top.sv").write_text(PE.replace("~i_a", "i_a"))  # (the owner changed; npu_core did not run again)
    res = children.copy(project, rtl, kids, set())
    (w,) = res.warnings
    assert "`m_pe_top`" in w and "../npu_core" in w and "../npu_pe" in w
    assert (rtl / "m_pe_top.sv").read_text() == PE  # (what npu_core lists)
    assert res.registry["m_pe_top"]["source"] == "../npu_core/src/rtl/m_pe_top.sv" and res.registry["m_pe_top"]["sha256"] == sha256_file(rtl / "m_pe_top.sv")


def test_a_damaged_registry_stops_the_copy_and_stays_protected(tmp_path):
    """A registry that cannot be read (a merge conflict, a hand edit) is not taken as none: the copy stops naming it, and every stage is
    kept off it; deleted, the next copy writes it again. A child's damaged registry stops the copy too (its owners would not be known)."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    root = tmp_path / "par"
    root.mkdir()
    (root / "q3tui.yaml").write_text("pipeline: {flow: rtl, step_options: {rtl: {child_projects: [../cha]}}}\n")
    eng = Engine(Project.open(root))
    rtl = children.rtl_dir(eng)
    kids = children.resolve(eng.project.root, children.configured(eng))
    children.copy(eng.project, rtl, kids, set())
    reg = children.registry_file(rtl)
    for damaged in ("{not json", '{"m_cha_top": "not a record"}'):
        reg.write_text(damaged)
        with pytest.raises(children.ChildError, match=r"src/rtl/child/provided\.json is damaged \(.+\): delete it, then run again$"):
            children.copy(eng.project, rtl, kids, set())
        (only,) = children.protected(eng).items()
        assert only[0] == reg and "damaged" in only[1]
    assert (rtl / "m_cha_top.sv").read_text() == CHA  # (nothing was touched)
    reg.unlink()
    assert list(children.copy(eng.project, rtl, kids, set()).registry) == ["m_cha_top"]  # (the copy, identical: taken as the copy again)
    (tmp_path / "cha" / "src/rtl/child").mkdir()
    (tmp_path / "cha" / "src/rtl/child/provided.json").write_text("{not json")
    with pytest.raises(children.ChildError, match=r"^child project \.\./cha: its child/provided\.json is damaged"):
        children.copy(eng.project, rtl, kids, set())
    assert children.units_by_owner(eng.project.root, ["../cha"]) == {"m_cha_top": "../cha"}  # (never raises)


def test_a_child_that_is_this_project_is_refused(tmp_path):
    """A child that has a file from this project (its registry names this project as the owner) is a cycle; so is this project as its own child."""
    par, project, rtl = _par(tmp_path)
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA, "m_par_top.sv": "module m_par_top; endmodule\n"},
               registry={"m_par_top": {"kind": "child", "owner": "../par", "child": "../par", "source": "../par/src/rtl/m_par_top.sv", "file": "m_par_top.sv"}})
    with pytest.raises(children.ChildError, match="cycle"):
        children.copy(project, rtl, children.resolve(par, ["../cha"]), set())
    with pytest.raises(children.ChildError, match="is this project itself"):
        children.copy(project, rtl, children.resolve(par, ["."]), set())
    assert not rtl.exists()


def test_header_is_the_interface_only():
    """What an own module that instantiates a child module depends on: its header, not its body or its comments."""
    h = children.header(CHA, "m_cha_top")
    assert h == ("module m_cha_top #( parameter int unsigned PARA_W = 8 ) ( input logic i_clk, input logic i_rst_n, input logic i_en, "
                 "output logic [PARA_W-1:0] o_cnt );")
    assert children.header(CHA.replace("o_cnt + 1'b1", "o_cnt - 1'b1") + "// v2\n", "m_cha_top") == h
    assert children.header("// m_cha_top counts\n/* module m_cha_top (x); */\n" + CHA.replace("i_rst_n,", "i_rst_n,  // reset"), "m_cha_top") == h
    assert children.header(CHA.replace("i_en", "i_enable"), "m_cha_top") != h
    rule_style = "module m_x\n  import p_pkg::*;\n#(\n  parameter int PR_W = 2\n) (\n  input logic [PR_W-1:0] i_a\n);\nendmodule\n"  # (Q3TUI's RTL rules)
    assert children.header(rule_style, "m_x") == "module m_x import p_pkg::*; #( parameter int PR_W = 2 ) ( input logic [PR_W-1:0] i_a );"
    assert children.header(CHA, "m_chb_top") == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_child_status(tmp_path):
    """R11: a child used before its own rtl flow passed, or changed since, is used with a warning (its history's mark passed/rtl)."""
    cha = make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    par, project, rtl = _par(tmp_path)
    (child,) = children.resolve(par, ["../cha"])
    files = children.listed(child)
    assert children.status_warnings(child, files) == ["child project ../cha has not passed its own rtl flow (no history mark passed/rtl)"]
    mine = Project(child.root, Config())
    rev = history.record(mine, "x", mark="rtl")
    assert history.mark(mine, "rtl") == rev and children.status_warnings(child, files) == []
    (cha / "src/rtl/m_cha_top.sv").write_text(CHA + "// v2\n")
    assert history.changed_since(mine, rev, ["src/rtl/m_cha_top.sv", "q3tui.yaml"]) == ["src/rtl/m_cha_top.sv"]
    assert history.changed_since(mine, rev, []) == []  # (no paths: none, not every file)
    assert children.status_warnings(child, files) == [
        f"child project ../cha: src/rtl/m_cha_top.sv changed since it passed its rtl flow (passed/rtl {rev[:8]})"]


def test_a_run_finds_the_child_option_and_its_rtl_folder(tmp_path):
    """The option and this project's RTL folder (where the copies are) as each flow sees them: the rtl step's when the flow has it, else
    q3tui.yaml's and the project's; a step named rtl that is not the rtl flow's (the test flow's) has the project's folder."""
    root = tmp_path / "par"
    root.mkdir()
    (root / "q3tui.yaml").write_text("pipeline: {flow: rtl, step_options: {rtl: {child_projects: [../cha], rtl_dir: hw}}}\n")
    eng = Engine(Project.open(root))
    assert children.configured(eng) == ["../cha"] and children.rtl_dir(eng) == eng.project.root / "hw"
    (root / "q3tui.yaml").write_text("pipeline: {flow: spec, step_options: {rtl: {child_projects: [../chb]}}}\n")
    eng = Engine(Project.open(root))
    assert children.configured(eng) == ["../chb"] and children.rtl_dir(eng) == eng.project.rtl_dir
    (root / "q3tui.yaml").write_text("pipeline: {flow: spec}\n")
    assert children.configured(Engine(Project.open(root))) == []
    t = tmp_path / "t"
    t.mkdir()
    use_test_flow(t)
    eng = Engine(Project.open(t))
    assert children.rtl_dir(eng) == eng.project.rtl_dir and children.configured(eng) == [] and children.protected(eng) == {}


def test_the_child_copies_are_protected_and_provided(tmp_path):
    """Every copy and the registry are read-only for every stage (why: the owner to fix it in); the rtl step's provided modules are the csr
    step's and the children's."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    root = tmp_path / "par"
    root.mkdir()
    (root / "q3tui.yaml").write_text("pipeline: {flow: rtl, step_options: {rtl: {child_projects: [../cha]}}}\n")
    eng = Engine(Project.open(root))
    rtl = children.rtl_dir(eng)
    assert children.protected(eng) == {} and children.read_all_provided(rtl) == {}  # (nothing copied yet)
    children.copy(eng.project, rtl, children.resolve(eng.project.root, children.configured(eng)), set())
    prot = children.protected(eng)
    assert list(prot) == [rtl / "m_cha_top.sv", rtl / "child" / "provided.json"] and "fix it in ../cha" in prot[rtl / "m_cha_top.sv"]
    (rtl / "csr").mkdir()
    (rtl / "csr" / "provided.json").write_text(json.dumps({"m_blk_csr": {"kind": "csr", "parent": "m_par_top", "req_ids": [], "instances": []}}))
    assert list(children.read_all_provided(rtl)) == ["m_blk_csr", "m_cha_top"]
    assert children.refusal("m_cha_top", children.read_registry(rtl)["m_cha_top"]) == (
        "`m_cha_top` belongs to the child project ../cha (read-only here): fix it in ../cha, then run this project again")


def test_units_by_owner_come_from_the_child_filelists(tmp_path):
    """Before any copy (parse), a child's units are known by its filelist's names, and what it got from its own children by its registry."""
    pe = make_child(tmp_path, "npu_pe", {"m_pe_top.sv": PE})
    make_child(tmp_path, "npu_core", {"m_pe_top.sv": PE, "m_core_top.sv": CORE}, registry=_pe_registry(pe))
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA}, lines=["+incdir+x", "src/rtl/m_cha_top.sv", "src/rtl/m_gone.sv"])
    par, project, rtl = _par(tmp_path)
    assert children.units_by_owner(par, ["../npu_core", "../cha", "../nowhere", "."]) == {
        "m_pe_top": "../npu_pe", "m_core_top": "../npu_core", "m_cha_top": "../cha", "m_gone": "../cha"}


def test_no_stage_may_write_a_child_file(tmp_path):
    from q3tui.llm.runtime import Stage, check_tool_paths

    st = Stage(name="s", system_prompt="", prompt="", cwd=tmp_path,
               read_only_files={tmp_path / "src/rtl/m_cha_top.sv": "a file of the child project ../cha"})
    assert "read-only" in check_tool_paths(st, {"file_path": "src/rtl/m_cha_top.sv"}, "Edit")
    assert check_tool_paths(st, {"file_path": "src/rtl/m_cha_top.sv"}, "Read") is None


def test_every_stage_of_a_step_gets_the_copies_as_read_only(tmp_path, monkeypatch):
    import anyio

    from q3tui.llm import runtime
    from q3tui.llm.runtime import Stage
    from q3tui.pipeline.base import StepContext
    from tests.fakes import FakeLLM

    root = tmp_path / "par"
    (root / "src" / "rtl" / "child").mkdir(parents=True)
    (root / "q3tui.yaml").write_text("pipeline: {flow: spec}\n")
    (root / "src/rtl/m_cha_top.sv").write_text(CHA)
    (root / "src/rtl/child/provided.json").write_text(json.dumps({"m_cha_top": {"kind": "child", "owner": "../cha", "file": "m_cha_top.sv"}}))
    fake = FakeLLM()
    monkeypatch.setattr(runtime, "run_stage", fake)
    eng = Engine(Project.open(root))
    anyio.run(lambda: StepContext(eng, eng.bus.scoped("spec")).llm(Stage(name="x", system_prompt="", prompt="", cwd=eng.project.root)))
    assert eng.project.rtl_dir / "m_cha_top.sv" in fake.calls[-1].read_only_files


def test_a_step_can_refuse_a_change_request(tmp_path):
    from q3tui.pipeline.base import StepDef

    class Refusing(StepDef):
        name = kind = "s"

        def refuse_change(self, engine, text):
            return "it is a child file" if text.startswith("[s:ro]") else None

    root = tmp_path / "p"
    root.mkdir()
    (root / "q3tui.yaml").write_text("{}\n")
    eng = Engine(Project.open(root), steps=[Refusing()])
    seen = []
    eng.bus.subscribe(seen.append)
    assert eng.request_change("s", "[s:ro] edit it") == "refused: it is a child file"
    assert not eng.state.feedback.get("s")
    assert any(e.kind == "warning" and "change request refused: it is a child file" in e.data["message"] for e in seen)
    assert eng.request_change("s", "other").startswith("queued")


# -- the rtl step with child projects (FR-01 §8: ../cha, ../chb and the parent par) -------------------------------------

def make_parent(root: Path, tools_dir: Path, children: list[str]) -> Engine:
    """The integration project `<root>/par` of FR-01 §8, an rtl-flow project whose rtl step lists `children` as child projects: its spec has the
    top `m_par_top` (REQ-001), which instantiates the child tops `m_cha_top` and `m_chb_top`; the final config; the fake tools. The children
    are not made here: make_child makes them (`make_child(root, "cha", {"m_cha_top.sv": CHA})`, `make_child(root, "chb", {"m_chb_top.sv": CHB})`),
    and the test tells the fake LLM the module it writes (`llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": [...]}}`)."""
    par = root / "par"
    (par / "spec").mkdir(parents=True)
    (par / "q3tui.yaml").write_text(f"pipeline: {{flow: rtl, step_options: {{rtl: {{rules: [rtl_rule.md], child_projects: {json.dumps(children)}}}}}}}\n"
                                    + tools_yaml(tools_dir))
    (par / "spec" / "spec.md").write_text("# par: integration of cha and chb\n")
    project = Project.open(par)
    spec = {"metadata": {"ip_name": "par", "spec_revision": "0.1", "phase": 1, "gate_status": "approved", "top_module": "m_par_top"},
            "requirements": [{"req_id": "REQ-001", "text": "u_cha.o_cnt drives u_chb.i_d, u_chb.o_q is o_q", "category": "functional",
                              "ambiguity_score": 0.1}],
            "parameters": [],
            "modules": [{"name": "m_par_top", "description": "the top: the child projects' tops, connected", "req_ids": ["REQ-001"],
                         "instances": ["m_cha_top", "m_chb_top"]}]}
    artifacts.write(project, "structured_spec.json", spec, check=False)
    artifacts.write(project, "final_config.json", config_json(), check=False)
    return Engine(project)


def _cha_chb(root: Path) -> tuple[Path, Path]:
    """The child projects of FR-01 §8, beside the parent: ../cha (m_cha_top) and ../chb (m_chb_top)."""
    return make_child(root, "cha", {"m_cha_top.sv": CHA}), make_child(root, "chb", {"m_chb_top.sv": CHB})


def _events(eng: Engine) -> list:
    seen: list = []
    eng.bus.subscribe(seen.append)
    return seen


def _said(seen: list, kind: str) -> list[str]:
    return [e.data.get("message", "") for e in seen if e.kind == kind]


def test_the_parent_writes_only_its_own_module(tmp_path, tools_dir, llm):
    """T3: the rtl step writes only the parent's own module; the child files stay as their projects made them (no REQ tags, no rule check),
    first in the filelist, reported apart from the own modules with their owner."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    par, rtl = tmp_path / "par", tmp_path / "par" / "src" / "rtl"
    assert [c.name for c in llm.calls if c.name.startswith("rtl_")] == ["rtl_m_par_top"]
    assert (rtl / "m_cha_top.sv").read_text() == CHA and (rtl / "m_chb_top.sv").read_text() == CHB
    assert (rtl / "filelist.f").read_text().split() == ["src/rtl/m_cha_top.sv", "src/rtl/m_chb_top.sv", "src/rtl/m_par_top.sv"]
    rep = json.loads((par / "schemas/synth_report.json").read_text())
    assert list(rep["modules"]) == ["m_par_top"] and set(rep["child_modules"]) == {"m_cha_top", "m_chb_top"} and rep["child_modules"]["m_cha_top"]["owner"] == "../cha"
    assert rep["child_modules"]["m_cha_top"] == {"file": "src/rtl/m_cha_top.sv", "owner": "../cha", "child": "../cha", "source": "../cha/src/rtl/m_cha_top.sv",
                                                "sha256": sha256_file(rtl / "m_cha_top.sv"), "lint_findings": 0}
    assert rep["synthesis"] == "pass" and rep["lint"]["status"] == "pass" and rep["req_ids_distinct"] == 1
    assert "REQ-001" in (rtl / "m_par_top.sv").read_text()
    assert not [f for f in eng.by_name["rtl"].outputs(eng) if f.stem in ("m_cha_top", "m_chb_top")]
    assert eng.state.steps["rtl"].status == "done" and set(eng.state.step_meta["rtl"]["modules"]) == {"m_par_top"}


def test_the_writer_of_the_top_is_told_the_child_files(tmp_path, tools_dir, llm):
    """R5: the stage that writes the module instantiating the child tops reads their files (their ports and parameters), told they are
    read-only: the only file it may write is its own."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    (stage,) = calls(llm, "rtl_m_par_top")
    assert "`m_cha_top` → src/rtl/m_cha_top.sv (dự án con ../cha: chỉ đọc, không sửa)" in stage.prompt
    assert "`m_chb_top` → src/rtl/m_chb_top.sv (dự án con ../chb: chỉ đọc, không sửa)" in stage.prompt
    assert stage.write_files == [eng.project.rtl_dir / "m_par_top.sv"] and eng.project.rtl_dir / "m_cha_top.sv" in stage.read_only_files


def test_a_requirement_the_plan_gives_a_child_goes_to_its_parent(tmp_path, tools_dir, llm):
    """A requirement the spec maps to a child module (parsed before the children were listed) is implemented and tagged in the own module
    that instantiates it: REQ tags go only into this project's files."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    spec = artifacts.read(eng.project, "structured_spec.json")
    spec["requirements"].append({"req_id": "REQ-002", "text": "the counter counts the enabled cycles", "category": "functional",
                                 "ambiguity_score": 0.1, "rtl_modules": ["m_cha_top"]})
    artifacts.write(eng.project, "structured_spec.json", spec, check=False)
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001", "REQ-002"], "children": ["m_cha_top", "m_chb_top"]}}
    seen = _events(eng)
    run_step(eng, "rtl")
    rtl = eng.project.rtl_dir
    (stage,) = calls(llm, "rtl_m_par_top")
    assert "- REQ-002: the counter counts the enabled cycles" in stage.prompt and "REQ-002" in (rtl / "m_par_top.sv").read_text()
    assert "REQ-002 is on m_cha_top, a module of the child project ../cha: moved to m_par_top" in _said(seen, "warning")
    assert (rtl / "m_cha_top.sv").read_text() == CHA and [c.name for c in llm.calls if c.name.startswith("rtl_")] == ["rtl_m_par_top"]
    assert (rtl / "filelist.f").read_text().split() == ["src/rtl/m_cha_top.sv", "src/rtl/m_chb_top.sv", "src/rtl/m_par_top.sv"]


def test_a_child_change_makes_rtl_stale_and_is_copied_again(tmp_path, tools_dir, llm):
    """T6 (R7): a child's file is an input: changed, the rtl step is stale and the next run copies it again. The module that instantiates it
    depends on its interface: a change inside its body writes nothing, a renamed port updates the parent's module."""
    cha, _ = _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    rtl, step = eng.project.rtl_dir, eng.by_name["rtl"]
    assert eng.evaluate(step).status == "done"
    v2 = CHA.replace("endmodule", "  // v2\nendmodule")
    (cha / "src/rtl/m_cha_top.sv").write_text(v2)
    assert eng.evaluate(step).status == "stale"
    seen = _events(eng)
    run_step(eng, "rtl")
    assert (rtl / "m_cha_top.sv").read_text() == v2 and len(calls(llm, "rtl_m_par_top")) == 1
    assert "child projects: copied m_cha_top" in _said(seen, "log") and eng.evaluate(step).status == "done"
    assert children.read_registry(rtl)["m_cha_top"]["sha256"] == sha256_file(cha / "src/rtl/m_cha_top.sv")
    renamed = v2.replace("i_en", "i_enable")
    (cha / "src/rtl/m_cha_top.sv").write_text(renamed)
    assert eng.evaluate(step).status == "stale"
    run_step(eng, "rtl")
    first, second = calls(llm, "rtl_m_par_top")
    assert (rtl / "m_cha_top.sv").read_text() == renamed and "`m_cha_top` changed" in second.prompt and "`m_chb_top`" not in second.prompt


def test_a_hand_edit_of_a_copy_is_restored(tmp_path, tools_dir, llm):
    """T6 (R7): a copy edited here makes the rtl step stale; the run restores the child's file, with a warning naming the project to fix it
    in, and the design is as it was (nothing to write)."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    rtl, step = eng.project.rtl_dir, eng.by_name["rtl"]
    (rtl / "m_cha_top.sv").write_text(CHA.replace("o_cnt + 1'b1", "o_cnt + 2'd2"))
    assert eng.evaluate(step).status == "stale"
    seen = _events(eng)
    run_step(eng, "rtl")
    assert (rtl / "m_cha_top.sv").read_text() == CHA
    (w,) = [m for m in _said(seen, "warning") if "restored" in m]
    assert "m_cha_top" in w and "../cha" in w
    assert any("2'd2" in f.read_text() for f in (eng.project.root / ".q3tui" / "bkp").rglob("m_cha_top.sv"))  # (the edit is backed up)
    assert len(calls(llm, "rtl_m_par_top")) == 1 and eng.evaluate(step).status == "done"


def test_removing_the_option_removes_the_copies(tmp_path, tools_dir, llm):
    """Review Focus 3: the option removed after a run: the copies and the registry go (backed up), and the step no longer counts those modules
    as provided (inputs, outputs, read-only files)."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    par = eng.project.root
    cfg = par / "q3tui.yaml"
    cfg.write_text(cfg.read_text().replace(', child_projects: ["../cha", "../chb"]', ""))
    assert "child_projects" not in cfg.read_text()
    eng = Engine(Project.open(par))
    rtl, step = eng.project.rtl_dir, eng.by_name["rtl"]
    assert eng.evaluate(step).status == "stale"
    llm.vlsit_modules["m_par_top"]["children"] = []  # (the top has nothing to instantiate any more)
    seen = _events(eng)
    run_step(eng, "rtl")
    assert not (rtl / "m_cha_top.sv").exists() and not (rtl / "m_chb_top.sv").exists() and not (rtl / "child").exists()
    assert sorted(f.read_text() for f in (par / ".q3tui" / "bkp").rglob("m_ch?_top.sv")) == [CHA, CHB]
    assert any("m_cha_top" in m and "removed" in m for m in _said(seen, "log"))
    assert (rtl / "filelist.f").read_text().split() == ["src/rtl/m_par_top.sv"]
    assert step._provided(eng) == {} and children.protected(eng) == {}
    assert not [f for f in step.inputs(eng) if f.name in ("m_cha_top.sv", "m_chb_top.sv", "provided.json", "filelist.f")]
    assert "child_modules" not in json.loads((par / "schemas/synth_report.json").read_text()) and eng.evaluate(step).status == "done"


def test_the_top_instantiates_an_unplaced_child_top_and_is_never_a_child(tmp_path, tools_dir, llm):
    """A child's top that no module of the spec instantiates is the top's (logged); a spec whose top is a child module stops the step: this
    project's top is its own module."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    spec = artifacts.read(eng.project, "structured_spec.json")
    spec["modules"][0]["instances"] = ["m_cha_top"]
    artifacts.write(eng.project, "structured_spec.json", spec, check=False)
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    seen = _events(eng)
    run_step(eng, "rtl")
    assert "the spec names no module that instantiates m_chb_top: the top m_par_top does" in _said(seen, "log")
    assert "`m_chb_top` → src/rtl/m_chb_top.sv" in calls(llm, "rtl_m_par_top")[0].prompt
    spec["metadata"]["top_module"] = "m_cha_top"
    spec["modules"].append({"name": "m_cha_top", "description": "the counter", "req_ids": [], "instances": []})
    artifacts.write(eng.project, "structured_spec.json", spec, check=False)
    with pytest.raises(StepFailed, match=r"the top module m_cha_top is a module of the child project \.\./cha"):
        run_step(eng, "rtl")


def test_a_verilog_file_of_a_child_keeps_its_name(tmp_path, tools_dir, llm):
    """A child's `.v` file is copied under its own name: the filelist, the synthesis, the prompt and the report name that file."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    make_child(tmp_path, "chb", {"m_chb_top.v": CHB})
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    rtl = eng.project.rtl_dir
    assert (rtl / "m_chb_top.v").read_text() == CHB and not (rtl / "m_chb_top.sv").exists() and rtl / "m_chb_top.v" in eng.by_name["rtl"].inputs(eng)
    assert (rtl / "filelist.f").read_text().split() == ["src/rtl/m_cha_top.sv", "src/rtl/m_chb_top.v", "src/rtl/m_par_top.sv"]
    assert "`m_chb_top` → src/rtl/m_chb_top.v" in calls(llm, "rtl_m_par_top")[0].prompt
    assert str((rtl / "m_chb_top.v").resolve()) in (rtl / "synth_default.ys").read_text()
    assert json.loads((eng.project.root / "schemas/synth_report.json").read_text())["child_modules"]["m_chb_top"]["file"] == "src/rtl/m_chb_top.v"


def test_the_rtl_step_waits_for_its_children_and_a_readable_registry(tmp_path, tools_dir, llm):
    """A child that cannot be used, or a damaged registry, is a missing input: the step waits, naming it. A refresh (inputs, outputs) never
    fails on the damage, and a run on it stops with the same message, the copies untouched."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    step, rtl = eng.by_name["rtl"], eng.project.rtl_dir
    view = eng.evaluate(step)
    assert view.status == "missing_input" and view.detail.startswith("child project ../chb: no folder")
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    assert eng.evaluate(step).status == "pending"
    run_step(eng, "rtl")
    reg = children.registry_file(rtl)
    reg.write_text("{not json")
    assert reg in step.inputs(eng) and step._provided(eng) == {} and rtl / "m_par_top.sv" in step.outputs(eng)
    view = eng.evaluate(step)
    assert view.status == "missing_input" and "provided.json is damaged" in view.detail
    with pytest.raises(StepFailed, match="provided.json is damaged"):
        run_step(eng, "rtl")
    assert (rtl / "m_cha_top.sv").read_text() == CHA and len(calls(llm, "rtl_m_par_top")) == 1


def test_a_middle_project_and_its_own_child_both_listed(tmp_path, tools_dir, llm):
    """Ruling 6 (R9): a middle project and its own child both listed: the top instantiates the middle project's top only. The child's top is
    instantiated inside the middle one already; a second instance in the top is one nobody asked for."""
    pe = make_child(tmp_path, "npu_pe", {"m_pe_top.sv": PE})
    make_child(tmp_path, "npu_core", {"m_pe_top.sv": PE, "m_core_top.sv": CORE}, lines=["src/rtl/m_pe_top.sv", "src/rtl/m_core_top.sv"],
               registry=_pe_registry(pe))
    eng = make_parent(tmp_path, tools_dir, ["../npu_core", "../npu_pe"])
    spec = artifacts.read(eng.project, "structured_spec.json")
    spec["modules"][0]["instances"] = []
    artifacts.write(eng.project, "structured_spec.json", spec, check=False)
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_core_top"]}}
    seen = _events(eng)
    run_step(eng, "rtl")
    placed = [m for m in _said(seen, "log") if m.startswith("the spec names no module that instantiates")]
    assert placed == ["the spec names no module that instantiates m_core_top: the top m_par_top does"]
    (stage,) = calls(llm, "rtl_m_par_top")
    assert "`m_core_top` → src/rtl/m_core_top.sv" in stage.prompt and "`m_pe_top` →" not in stage.prompt


def test_a_parent_with_its_own_async_csr_takes_a_child_with_the_same_model(tmp_path, tools_dir, llm):
    """Ruling 14 (I2): this project's csr step made the synchronizer model a child has too, with the same bytes (the same APB-CSR-Generator
    version): the rtl step keeps this project's file (logged), copies the rest of the child, and the design has the model once."""
    make_child(tmp_path, "cha", {"m_csr_sync.sv": SYNC, "m_cha_top.sv": CHS})
    eng = make_parent(tmp_path, tools_dir, ["../cha"])
    rtl = eng.project.rtl_dir
    (rtl / "csr").mkdir(parents=True)
    (rtl / "m_csr_sync.sv").write_text(SYNC)
    (rtl / "m_par_csr.sv").write_text("module m_par_csr (\n  input  logic i_clk,\n  output logic o_q\n);\n"
                                      "  m_csr_sync u_sync (.i_clk(i_clk), .i_d(1'b1), .o_q(o_q));\nendmodule\n")
    (rtl / "csr" / "provided.json").write_text(json.dumps({"m_par_csr": {"kind": "csr", "parent": "m_par_top", "req_ids": [], "instances": ["m_csr_sync"]},
                                                          "m_csr_sync": {"kind": "model", "parent": "m_par_csr", "req_ids": [], "instances": []}}))
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top"]}}
    seen = _events(eng)
    run_step(eng, "rtl")
    assert "`m_csr_sync`: the same file as this project's own (csr step): kept as this project's" in _said(seen, "log")
    assert list(children.read_registry(rtl)) == ["m_cha_top"] and (rtl / "m_csr_sync.sv").read_text() == SYNC
    assert (rtl / "filelist.f").read_text().split().count("src/rtl/m_csr_sync.sv") == 1 and eng.state.steps["rtl"].status == "done"
    assert rtl / "m_csr_sync.sv" not in children.protected(eng)  # (this project's file: its lint findings are fixed here, as the csr step's)


# -- findings and change requests on the child projects' units (R3, R4, R11) --------------------------------------------

CHP = {  # a child whose module imports two packages (one not named *_pkg) and has an interface port
    "chp_pkg.sv": "package chp_pkg;\n  localparam int unsigned PA_W = 8;\nendpackage\n",
    "chp_types.sv": "package chp_types;\n  typedef logic [7:0] byte_t;\nendpackage\n",
    "chp_if.sv": "interface chp_if;\n  logic v;\nendinterface\n",
    "m_chp_top.sv": "module m_chp_top\n  import chp_pkg::*;\n  import chp_types::*;\n(\n  input  logic  i_clk,\n  chp_if        bus,\n"
                    "  output byte_t o_q\n);\n  assign o_q = '0;\nendmodule\n",
}


def test_a_lint_finding_in_a_child_is_reported_not_fixed(tmp_path, tools_dir, llm):
    """T5 (a), R4: a lint finding inside a child's module is reported for the child project (one warning naming it and where to fix it) and
    counted in the report; no stage writes or fixes the child, and the design lint passes: this project's own code is clean."""
    warned = CHA.replace("endmodule", "  // LINT_WARN\nendmodule")
    make_child(tmp_path, "cha", {"m_cha_top.sv": warned})
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    seen = _events(eng)
    run_step(eng, "rtl")
    assert eng.state.steps["rtl"].status == "done"
    (w,) = [m for m in _said(seen, "warning") if "lint finding" in m]
    assert w == ("m_cha_top (child project ../cha): 1 lint finding(s), not fixed here — fix it in ../cha, then run this project again: "
                 "design lint warning UNUSED line 3: Signal is not used: 'x'")
    assert not calls(llm, "rtl_m_cha_top") and len(calls(llm, "rtl_m_par_top")) == 1
    assert (eng.project.rtl_dir / "m_cha_top.sv").read_text() == warned
    rep = json.loads((eng.project.root / "schemas/synth_report.json").read_text())
    assert rep["child_modules"]["m_cha_top"]["lint_findings"] == 1 and rep["child_modules"]["m_chb_top"]["lint_findings"] == 0
    assert rep["lint"]["status"] == "pass" and rep["synthesis"] == "pass"


def test_a_lint_error_in_a_child_is_no_problem_of_the_module_using_it(tmp_path, tools_dir, llm):
    """R4: a lint error located in a child's file fails the lint run of the own module that reads it, but it is the child's: that module gets
    no fix round for it (it is clean), and the design lint reports it for the child."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA.replace("endmodule", "  // LINT_ERR\nendmodule")})
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    seen = _events(eng)
    run_step(eng, "rtl")
    assert eng.state.steps["rtl"].status == "done" and len(calls(llm, "rtl_m_par_top")) == 1
    (w,) = [m for m in _said(seen, "warning") if "lint finding" in m]
    assert w.startswith("m_cha_top (child project ../cha): 1 lint finding(s), not fixed here — fix it in ../cha") and "syntax error" in w
    assert not [m for m in _said(seen, "warning") if "m_par_top" in m]


def test_a_childs_lint_warnings_do_not_count_against_this_projects_limit(tmp_path, tools_dir, llm):
    """Ruling 11: the design lint's warnings located in a child's unit do not count against `lint_max_warnings` (this project's limit for its
    own code): the own module's single warning is within a limit of 1, so no stage fixes it, and the child's warning is still reported."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA.replace("endmodule", "  // LINT_WARN\nendmodule")})
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    eng.by_name["rtl"].options["lint_max_warnings"] = 1
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    llm.vlsit_bad = {"m_par_top": ["lint"]}  # (its first write has one lint warning)
    seen = _events(eng)
    run_step(eng, "rtl")
    assert eng.state.steps["rtl"].status == "done" and len(calls(llm, "rtl_m_par_top")) == 1
    assert "LINT_WARN" in (eng.project.rtl_dir / "m_par_top.sv").read_text()  # (within the limit: kept)
    (w,) = [m for m in _said(seen, "warning") if "lint finding" in m]
    assert w.startswith("m_cha_top (child project ../cha): 1 lint finding(s), not fixed here — fix it in ../cha")
    rep = json.loads((eng.project.root / "schemas/synth_report.json").read_text())
    assert rep["lint"]["status"] == "pass" and rep["lint"]["warnings"] == 1 and rep["lint"]["max_warnings"] == 1
    assert rep["child_modules"]["m_cha_top"]["lint_findings"] == 1 and rep["modules"]["m_par_top"]["lint_warnings"] == 1


def test_a_synthesis_error_in_a_child_fails_the_step_naming_it(tmp_path, tools_dir, llm):
    """R4: a synthesis error located in a child's module is never sent to a fix (no round can fix it here): the step fails at once, naming the
    module and the project to fix it in; this project's top, which synthesizes, is not blamed."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA.replace("endmodule", "  // SYNTH_ERR\nendmodule")})
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    seen = _events(eng)
    with pytest.raises(StepFailed, match=r"m_cha_top.*\.\./cha") as exc:
        run_step(eng, "rtl")
    assert "m_cha_top is read-only here: a module of the child project ../cha: fix it in ../cha, then run this project again" in str(exc.value)
    assert "Unsupported construct" in str(exc.value) and "synthesis fails" not in str(exc.value)
    assert not calls(llm, "rtl_m_cha_top") and len(calls(llm, "rtl_m_par_top")) == 1
    assert not [m for m in _said(seen, "warning") if m.startswith("synthesis failed")]  # (no fix round was announced)
    assert json.loads((eng.project.root / "schemas/synth_report.json").read_text())["synthesis"] == "fail"


def test_a_synthesis_error_in_a_child_fails_again_when_nothing_changed(tmp_path, tools_dir, llm):
    """Ruling 10: a synthesis that failed on a child's error is not kept as this design's result: a run with nothing changed synthesizes
    again and fails again naming the child, never "done" on the failed report."""
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA.replace("endmodule", "  // SYNTH_ERR\nendmodule")})
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    failures = []
    for _ in range(2):
        with pytest.raises(StepFailed) as exc:
            run_step(eng, "rtl")
        failures.append(str(exc.value))
    first, again = failures
    assert "m_cha_top is read-only here: a module of the child project ../cha: fix it in ../cha, then run this project again" in again
    assert again == first and eng.state.steps["rtl"].status == "failed" and len(calls(llm, "rtl_m_par_top")) == 1


def test_a_change_request_for_a_child_is_refused(tmp_path, tools_dir, llm):
    """T5 (b), R3: a change request scoped to a child's module is refused, naming the project to make it in: never queued (before the first
    copy the children's filelists name their units, after it the registry does), and dropped with a warning by a run that finds it queued
    anyway. A request for this project's own module is queued as before."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    said = eng.request_change("rtl", "[rtl:m_cha_top] faster")
    assert said.startswith("refused:") and "fix it in ../cha" in said and not eng.state.feedback.get("rtl")
    eng.state.feedback["rtl"] = ["[rtl:m_cha_top] faster"]
    seen = _events(eng)
    run_step(eng, "rtl")
    assert [m for m in _said(seen, "warning") if "refused" in m] == [
        "change request refused: `m_cha_top` belongs to the child project ../cha (read-only here): fix it in ../cha, then run this project again"]
    assert not calls(llm, "rtl_m_cha_top") and (eng.project.rtl_dir / "m_cha_top.sv").read_text() == CHA
    assert not eng.state.feedback.get("rtl") and eng.state.steps["rtl"].status == "done"  # (consumed: the run said why)
    assert "fix it in ../chb" in eng.request_change("rtl", "[rtl:m_chb_top] wider")  # (from the registry now)
    assert eng.request_change("rtl", "[rtl:m_par_top] faster").startswith("queued")


def test_the_parent_warns_about_a_child_that_did_not_pass(tmp_path, tools_dir, llm):
    """R11: a child project whose own rtl flow has not passed (no history mark passed/rtl) is used anyway, with a warning."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    seen = _events(eng)
    run_step(eng, "rtl")
    warned = _said(seen, "warning")
    assert "child project ../cha has not passed its own rtl flow (no history mark passed/rtl)" in warned
    assert "child project ../chb has not passed its own rtl flow (no history mark passed/rtl)" in warned
    assert eng.state.steps["rtl"].status == "done"


def test_child_packages_and_interfaces_are_linted_with_the_module_using_them_and_update_it(tmp_path, tools_dir, llm):
    """Ruling 7: an own module that uses a child unit is linted with the child projects' packages and interfaces (their files first, in the
    registry's order, a package named *_pkg once), and is updated when one of them changes (by its text: a type it may use changed)."""
    child = make_child(tmp_path, "chp", CHP)
    eng = make_parent(tmp_path, tools_dir, ["../chp"])
    spec = artifacts.read(eng.project, "structured_spec.json")
    spec["modules"][0]["instances"] = ["m_chp_top"]
    artifacts.write(eng.project, "structured_spec.json", spec, check=False)
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_chp_top"]}}
    run_step(eng, "rtl")
    rtl = eng.project.rtl_dir
    units = children.read_registry(rtl)
    assert [units[u]["unit"] for u in ("chp_pkg", "chp_types", "chp_if", "m_chp_top")] == ["package", "package", "interface", "module"]
    listed = (eng.project.root / "build/sim/rtl/lint_m_par_top.f").read_text().split()
    assert listed == [str((rtl / f).resolve()) for f in ("chp_pkg.sv", "chp_types.sv", "chp_if.sv", "m_chp_top.sv", "m_par_top.sv")]
    assert set(eng.state.step_meta["rtl"]["modules"]["m_par_top"]["built_from"]["children"]) == {"chp_pkg", "chp_types", "chp_if", "m_chp_top"}
    (child / "src/rtl/chp_types.sv").write_text(CHP["chp_types.sv"].replace("[7:0]", "[15:0]"))
    assert eng.evaluate(eng.by_name["rtl"]).status == "stale"
    run_step(eng, "rtl")
    first, second = calls(llm, "rtl_m_par_top")
    assert "`chp_types` changed" in second.prompt and "`m_chp_top` changed" not in second.prompt


# -- parse keeps the parent's spec (R6, T4) ---------------------------------------------------------------------

@pytest.fixture
def parse_llm(monkeypatch):
    """The fake LLM of tests/test_spec_parse_config.py (its `llm` fixture is the rtl one here)."""
    from q3tui.llm import runtime
    from tests import vlsit_fakes
    from tests.fakes import FakeLLM
    from tests.test_spec_parse_config import VlsitFake

    fake = FakeLLM()
    vf = VlsitFake()
    saved = list(vlsit_fakes.HANDLERS)
    vlsit_fakes.HANDLERS.clear()
    vlsit_fakes.register(vf)
    fake.vf = vf
    monkeypatch.setattr(runtime, "run_stage", fake)
    yield fake
    vlsit_fakes.HANDLERS[:] = saved


def _par_spec(**over) -> dict:
    from tests.test_spec_parse_config import _spec

    base = _spec(ip_name="par", top_module="m_par_top",
                 requirements=[{"text": "The counter shall count enabled cycles.", "category": "functional", "rtl_modules": ["m_cha_top"]},
                               {"text": "The parent shall wrap the counter.", "category": "functional", "rtl_modules": ["m_par_top"]}],
                 parameters=[], constraints=[],
                 modules=[{"name": "m_par_top", "description": "top", "instances": ["m_cha_top"]},
                          {"name": "m_cha_top", "description": "counter"}])
    base.update(over)
    return base


def _parse_project(tmp_path) -> Engine:
    make_child(tmp_path, "cha", {"m_cha_top.sv": CHA})
    par = tmp_path / "par"
    (par / "spec").mkdir(parents=True)
    (par / "q3tui.yaml").write_text("pipeline: {flow: spec, step_options: {rtl: {child_projects: [../cha]}}}\n")
    (par / "spec" / "spec.md").write_text("# Par\n\nThe parent wraps the counter of ../cha.\n")
    return Engine(Project.open(par))


def test_fit_to_children_moves_requirements_and_keeps_the_top():
    from q3tui.flows.spec.parse.step import fit_to_children, modules_leaves_first

    def req(rid, mods):
        return {"req_id": rid, "rtl_modules": list(mods)}

    data = {"metadata": {"top_module": "m_cha_top"},
            "requirements": [req("REQ-001", ["m_cha_sub"]), req("REQ-002", ["m_cha_top", "m_par_top"]), req("REQ-003", ["m_par_top"])],
            "modules": [{"name": "m_par_top", "instances": ["m_wrap"], "req_ids": []},
                        {"name": "m_wrap", "instances": ["m_cha_top"], "req_ids": []},
                        {"name": "m_cha_top", "instances": ["m_cha_sub"], "req_ids": []},
                        {"name": "m_cha_sub", "instances": [], "req_ids": []}]}
    data["module_order"] = modules_leaves_first(data)
    warnings = fit_to_children(data, {"m_cha_top": "../cha", "m_cha_sub": "../cha"})
    assert data["metadata"]["top_module"] == "m_par_top"
    reqs = {r["req_id"]: r["rtl_modules"] for r in data["requirements"]}
    assert reqs == {"REQ-001": ["m_wrap"], "REQ-002": ["m_wrap", "m_par_top"], "REQ-003": ["m_par_top"]}
    assert "REQ-001 maps to m_cha_sub, a module of the child project ../cha: moved to m_wrap" in warnings
    assert "REQ-002 maps to m_cha_top, a module of the child project ../cha: moved to m_wrap" in warnings
    assert len(warnings) == 3  # (+ the top replaced)
    mods = {m["name"]: m for m in data["modules"]}
    assert mods["m_cha_top"]["provided_by"] == "../cha" and mods["m_cha_sub"]["provided_by"] == "../cha"
    assert "provided_by" not in mods["m_par_top"]
    assert mods["m_wrap"]["req_ids"] == ["REQ-001", "REQ-002"] and mods["m_par_top"]["req_ids"] == ["REQ-002", "REQ-003"]
    assert mods["m_cha_top"]["req_ids"] == [] and mods["m_cha_sub"]["req_ids"] == []
    assert data["module_order"] == modules_leaves_first(data)


def test_fit_to_children_without_own_modules_keeps_the_top_and_warns():
    from q3tui.flows.spec.parse.step import fit_to_children

    data = {"metadata": {"top_module": "m_cha_top"}, "requirements": [{"req_id": "REQ-001", "rtl_modules": ["m_cha_top"]}],
            "modules": [{"name": "m_cha_top", "instances": [], "req_ids": ["REQ-001"]}], "module_order": ["m_cha_top"]}
    warnings = fit_to_children(data, {"m_cha_top": "../cha"})
    assert data["metadata"]["top_module"] == "m_cha_top"
    assert any("no module of its own" in w for w in warnings)


def test_parse_with_child_projects_keeps_the_parents_top(tmp_path, parse_llm):
    """T4: an import record whose design has the child's top does not replace the parent's module map."""
    eng = _parse_project(tmp_path)
    eng.import_record_path("rtl").parent.mkdir(parents=True, exist_ok=True)
    eng.import_record_path("rtl").write_text(json.dumps({"source": "x", "files": {}, "design": {
        "top": "m_cha_top", "modules": {"m_cha_top": {"file": "m_cha_top.sv", "kind": "module", "ports": [], "instances": []}}}}))
    parse_llm.vf.parsed.append(_par_spec())
    seen = _events(eng)
    import anyio

    anyio.run(lambda: eng.run_step(eng.by_name["parse"]))
    data = artifacts.read(eng.project, "structured_spec.json")
    assert data["metadata"]["top_module"] == "m_par_top"
    assert [r["rtl_modules"] for r in data["requirements"]][0] == ["m_par_top"]
    assert any("REQ-001 maps to m_cha_top, a module of the child project ../cha: moved to m_par_top" in w for w in _said(seen, "warning"))
    assert {m["name"]: m.get("provided_by") for m in data["modules"]} == {"m_par_top": None, "m_cha_top": "../cha"}


def test_a_new_child_module_reruns_parse_without_the_llm(tmp_path, parse_llm):
    import anyio

    eng = _parse_project(tmp_path)
    parse_llm.vf.parsed.append(_par_spec())
    anyio.run(lambda: eng.run_step(eng.by_name["parse"]))
    assert len(calls(parse_llm, "parse_spec")) == 1
    cha = tmp_path / "cha" / "src" / "rtl"
    (cha / "m_cha_sub.sv").write_text("module m_cha_sub (input logic i_a, output logic o_a);\n  assign o_a = i_a;\nendmodule\n")
    (cha / "filelist.f").write_text("src/rtl/m_cha_top.sv\nsrc/rtl/m_cha_sub.sv\n")
    assert eng.evaluate(eng.by_name["parse"]).status == "stale"
    anyio.run(lambda: eng.run_step(eng.by_name["parse"]))
    assert len(calls(parse_llm, "parse_spec")) == 1
    data = artifacts.read(eng.project, "structured_spec.json")
    assert {m["name"] for m in data["modules"]} >= {"m_par_top", "m_cha_top"}


def test_a_child_added_later_moves_its_requirements_without_the_llm(tmp_path, parse_llm):
    """The option added after parse ran: the spec's signature is the same, so code alone refits the module map and rewrites the artifact."""
    import anyio

    eng = _parse_project(tmp_path)
    make_child(tmp_path, "chb", {"m_chb_top.sv": CHB})
    par = tmp_path / "par"
    (par / "q3tui.yaml").write_text("pipeline: {flow: spec, step_options: {rtl: {child_projects: [../cha]}}}\n")
    parse_llm.vf.parsed.append(_par_spec(
        requirements=[{"text": "The register shall hold the data.", "category": "functional", "rtl_modules": ["m_chb_top"]}],
        modules=[{"name": "m_par_top", "description": "top", "instances": ["m_cha_top", "m_chb_top"]},
                 {"name": "m_cha_top", "description": "counter"}, {"name": "m_chb_top", "description": "register"}]))
    anyio.run(lambda: eng.run_step(eng.by_name["parse"]))
    before = artifacts.read(eng.project, "structured_spec.json")
    assert before["requirements"][0]["rtl_modules"] == ["m_chb_top"]

    (par / "q3tui.yaml").write_text("pipeline: {flow: spec, step_options: {rtl: {child_projects: [../cha, ../chb]}}}\n")
    eng = Engine(Project.open(par))
    seen = _events(eng)
    assert eng.evaluate(eng.by_name["parse"]).status == "stale"
    anyio.run(lambda: eng.run_step(eng.by_name["parse"]))
    assert len(calls(parse_llm, "parse_spec")) == 1
    data = artifacts.read(eng.project, "structured_spec.json")
    assert data["requirements"][0]["rtl_modules"] == ["m_par_top"]
    assert {m["name"]: m.get("provided_by") for m in data["modules"]}["m_chb_top"] == "../chb"
    assert any("maps to m_chb_top, a module of the child project ../chb: moved to m_par_top" in w for w in _said(seen, "warning"))
    assert data != before


def test_format_and_style_lint_skip_child_files(tmp_path, tools_dir, llm):
    """T5(c) (R3, R4): format and style_lint see only this project's own file: the child copies are the owner's to format and lint."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    assert sorted(p.stem for p in eng.project.rtl_dir.glob("*.sv")) == ["m_cha_top", "m_chb_top", "m_par_top"]
    for name in ("format", "style_lint"):
        assert [f.stem for f in eng.by_name[name].rtl_files(eng)] == ["m_par_top"]
    assert eng.by_name["style_lint"]._provided(eng) == {"m_cha_top": "../cha", "m_chb_top": "../chb"}


def test_a_damaged_child_registry_does_not_crash_rtl_files_and_stops_the_steps(tmp_path, tools_dir, llm):
    """Ruling 5: rtl_files runs on every TUI refresh (never raises); format / style_lint / smoke wait with the registry's problem."""
    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    reg = children.registry_file(eng.project.rtl_dir)
    reg.write_text("{not json")
    for name in ("format", "style_lint", "smoke"):
        assert reg.name in eng.by_name[name].missing_input(eng) and "damaged" in eng.by_name[name].missing_input(eng)
    assert {f.stem for f in eng.by_name["format"].rtl_files(eng)} == {"m_cha_top", "m_chb_top", "m_par_top"}  # (falls back to the csr set)


def test_format_now_refuses_with_a_damaged_child_registry(tmp_path, tools_dir, llm):
    """A user-triggered format must not reformat the child copies when the registry that names them cannot be read."""
    from q3tui.pipeline.engine import EngineError

    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    rtl = eng.project.rtl_dir
    children.registry_file(rtl).write_text("{not json")
    before = {f.name: f.read_bytes() for f in rtl.glob("*.sv")}
    with pytest.raises(EngineError, match="provided.json"):
        Ops(eng).run_action("format_now")
    assert {f.name: f.read_bytes() for f in rtl.glob("*.sv")} == before


def test_downstream_flows_build_from_the_parent_filelist(tmp_path, tools_dir, llm):
    """R5 / T9: the dv, sanity, fpga and asic steps read the parent's `src/rtl/filelist.f`, which lists the child copies before the parent's
    module, so their commands (built here, no tool run) use the assembled IP."""
    from q3tui import flows
    from q3tui.flows.asic.lib import dc
    from q3tui.flows.dv.plain import gen as dv_gen
    from q3tui.flows.fpga.lib import vivado
    from q3tui.flows.sanity.lib import script as sanity_script

    _cha_chb(tmp_path)
    eng = make_parent(tmp_path, tools_dir, ["../cha", "../chb"])
    llm.vlsit_modules = {"m_par_top": {"reqs": ["REQ-001"], "children": ["m_cha_top", "m_chb_top"]}}
    run_step(eng, "rtl")
    project = eng.project
    root, rtl = project.root, project.rtl_dir
    want = [(rtl / n).resolve() for n in ("m_cha_top.sv", "m_chb_top.sv", "m_par_top.sv")]
    # dv: the filelist order, children first
    tb, work = root / "src" / "tb", root / "build" / "sim"
    tb.mkdir(parents=True, exist_ok=True)
    (tb / "tb_top.sv").write_text("module tb_top; endmodule\n")
    _, absf, files = dv_gen.filelists(root, tb, rtl, work, [])
    assert files[:3] == want and absf.read_text().split()[:3] == [str(p) for p in want]
    # sanity: the absolute filelist keeps the order
    abs_fl = sanity_script.absolute_filelist(rtl / "filelist.f", root, root / "build" / "sanity")
    assert [ln for ln in abs_fl.read_text().split() if not ln.startswith(("//", "#"))][:3] == [str(p) for p in want]
    # fpga / asic: every *.sv of the RTL folder (sorted by name), so the three files are there and the script names them
    for name, step_name in (("fpga", "fpga_synth"), ("asic", "asic_synth")):
        e = Engine(project, flow=flows.load_flow(name, root))
        got = e.by_name[step_name].rtl_files(e)
        assert {p.resolve() for p in want} <= {p.resolve() for p in got}
        if name == "fpga":
            text = vivado.script_text(got, "m_par_top", project.sdc_dir, root / "build" / "syn" / "fpga", {})
        else:
            text = dc.script_text(got, "m_par_top", ["lib.db"], None, root / "build" / "syn" / "asic", root / "build" / "syn" / "asic" / "n.v", {})
        for n in ("m_cha_top.sv", "m_chb_top.sv", "m_par_top.sv"):
            assert n in text
