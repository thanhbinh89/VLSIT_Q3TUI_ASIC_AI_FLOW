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
from q3tui.pipeline.engine import Engine
from tests.fakes import use_test_flow

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
    assert str(e.value) == ("`m_cha_top` of the child project ../cha (../cha/src/rtl/m_cha_top.sv) has the name of this project's own module "
                            "(src/rtl/m_cha_top.sv): rename one of them")
    rtl.mkdir(parents=True)
    mine = "module m_cha_top (input logic i_clk);\nendmodule\n"
    (rtl / "m_cha_top.sv").write_text(mine)  # (this project's own file, not a copy)
    with pytest.raises(children.ChildError, match=r"\(\.\./cha/src/rtl/m_cha_top\.sv\) has the name of this project's own module \(src/rtl/m_cha_top\.sv\)"):
        children.copy(project, rtl, kids, set())
    assert (rtl / "m_cha_top.sv").read_text() == mine and not children.registry_file(rtl).exists()
    (rtl / "m_cha_top.sv").write_text(CHA)  # (the child's very bytes: adopted)
    res = children.copy(project, rtl, kids, set())
    assert list(res.registry) == ["m_cha_top"] and not (res.copied or res.restored)


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
