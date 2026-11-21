"""FR-03 of the FX1B project: CDC waivers VC Static applies for violations with nested destinations, an identity that does not depend on the
order of the report's lists, one waiver per selector (never one that waives a violation the review did not waive), virtual clocks in the
clock groups Q3TUI adds, and the waivers that matched nothing named by the check.

The report text is FR-03's Appendix A (VC Static X-2025.06 on FX1B's `crg`) and blocks of the same tool's reports, trimmed; the waiver
semantics of the fake tool below are the ones measured on that tool with `waive_cdc -preview` (FR-03 reply, "VC Static's matching")."""

import json
import re
import sys
from pathlib import Path

from q3tui.core.ops import Ops
from q3tui.core.project import Project
from q3tui.flows.sanity.lib import check, script, vcstatic, waivers
from q3tui.pipeline.engine import Engine
from tests.test_sanity import ReviewLLM, no_live_llm, proj  # noqa: F401  (fixtures)

APPENDIX_A = """\
  -----------------------------------------------------------------------------
  CDC_COHERENCY_MULTI_SYNC  (2 errors/0 waived)
  -----------------------------------------------------------------------------
  Tag                                     : CDC_COHERENCY_MULTI_SYNC
  Description                             : Multiple  synchronization of same source [SrcInfo]
  Violation                               : CDC:141
  SrcInfo
    SrcObject                             : u_crg_rstctl/reg_grp_ip_n/Q[9]
    SrcClockInfoList
      SrcClockInfo
        ClockName                         : i_clk_ref
        ClockObject                       : i_clk_ref
        ClockDomainId                     : 7
  NoConvNoSrcControlPathList
    NoConvControlPathInfo
      Type                                : Control Reset
      SyncOutput                          : u_crg_rst_sync_37/reg_rst_n_sync/Q
      DestInfoList
        DestInfo
          DestObject                      : u_crg_rst_sync_37/reg_rst_n_meta/Q
          DestClockInfoList
            DestClockInfo
              ClockName                   : i_clk_sys
              ClockObject                 : i_clk_sys
              ClockDomainId               : 9
    NoConvControlPathInfo
      Type                                : Control Reset
      SyncOutput                          : u_crg_rst_sync_35/reg_rst_n_sync/Q
      DestInfoList
        DestInfo
          DestObject                      : u_crg_rst_sync_35/reg_rst_n_meta/Q
          DestClockInfoList
            DestClockInfo
              ClockName                   : i_clk_sys
              ClockObject                 : i_clk_sys
              ClockDomainId               : 9
  ContainerInstance                       : m_crg_top
  Module                                  : m_crg_top

  -----------------------------------------------------------------------------
  Tag                                     : CDC_COHERENCY_MULTI_SYNC
  Description                             : Multiple  synchronization of same source [SrcInfo]
  Violation                               : CDC:146
  SrcInfo
    SrcObject                             : u_crg_rstctl/reg_grp_ip_n/Q[9]
    SrcClockInfoList
      SrcClockInfo
        ClockName                         : i_clk_ref
        ClockObject                       : i_clk_ref
        ClockDomainId                     : 7
  NoConvNoSrcControlPathList
    NoConvControlPathInfo
      Type                                : Control Reset
      SyncOutput                          : u_crg_rst_sync_38/reg_rst_n_sync/Q
      DestInfoList
        DestInfo
          DestObject                      : u_crg_rst_sync_38/reg_rst_n_meta/Q
          DestClockInfoList
            DestClockInfo
              ClockName                   : i_clk_ddrc
              ClockObject                 : i_clk_ddrc
              ClockDomainId               : 3
    NoConvControlPathInfo
      Type                                : Control Reset
      SyncOutput                          : u_crg_rst_sync_36/reg_rst_n_sync/Q
      DestInfoList
        DestInfo
          DestObject                      : u_crg_rst_sync_36/reg_rst_n_meta/Q
          DestClockInfoList
            DestClockInfo
              ClockName                   : i_clk_ddrc
              ClockObject                 : i_clk_ddrc
              ClockDomainId               : 3
  ContainerInstance                       : m_crg_top
  Module                                  : m_crg_top
"""
CLOCK_GROUP_MISSING = """\
  SETUP_CLOCK_GROUP_MISSING  (1 warning/0 waived)
  -----------------------------------------------------------------------------
  Tag          : SETUP_CLOCK_GROUP_MISSING
  Description  : [ClkType] [SdcClkName] is not specified in any user-defined clock group.
  Violation    : CDC:65
  ClkType      : Primary Clock
  SdcClkName   : vclk_async
  SrcFileInfo
    FileName   : <project>/constraints/sdc/extra.tcl
    LineNumber : 33
"""
# a crossing whose objects are fields of the block (the error severity only so that the reader keeps it: the tool reports it as an info)
SYNC_CTRL = """\
  CDC_SYNC_CTRL  (1 error/0 waived)
  -----------------------------------------------------------------------------
  Tag                       : CDC_SYNC_CTRL
  Description               : Control synchronization scheme found for CDC path from [SrcObject] to [DestObject], crossing from [SrcClockInfoList] to [DestClockInfoList]
  Violation                 : CDC:64
  SrcObject                 : u_crg_cmon/reg_gray_ls/Q[15:0]
  DestObject                : u_crg_cmon/u_crg_gray_sync_3/reg_meta/Q[15:0]
  SrcClockInfoList
    SrcClockInfo
      ClockName             : i_clk_ls
      ClockObject           : i_clk_ls
      ClockDomainId         : 4
  DestClockInfoList
    DestClockInfo
      ClockName             : i_clk_ref
      ClockObject           : i_clk_ref
      ClockDomainId         : 7
    DestClockInfo
      ClockName             : i_clk_sys
      ClockObject           : i_clk_sys
      ClockDomainId         : 9
"""
# two bus checks of crg (paths trimmed): the first source is a set of bit ranges, the third source is the one of the second check
BUS_NOCONV = """\
  CDC_COHERENCY_BUS_NOCONV  (2 warnings/0 waived)
  -----------------------------------------------------------------------------
  Tag          : CDC_COHERENCY_BUS_NOCONV
  Description  : Control synchronized bits of same destination vector signal not checked for presence of gray encoder
  Violation    : CDC:143
  DestInfo
    DestObject : u_crg_sync2_2/reg_meta
  NoConvControlPathList
    NoConvControlPathInfo
      Type     : Control
      DestInfoList
        DestInfo
          DestObject : u_crg_sync2_2/reg_meta/Q[12:0]
          SrcInfoList
            SrcInfo
              SrcObject : u_crg_rstctl/reg_held/Q{ [13:10] ,[8:0] }
    NoConvControlPathInfo
      Type     : Control
      DestInfoList
        DestInfo
          DestObject : u_crg_sync2_2/reg_meta/Q[18:16]
          SrcInfoList
            SrcInfo
              SrcObject : u_crg_rstctl/u_crg_sync2/reg_sync/Q[2:0]
    NoConvControlPathInfo
      Type     : Control
      DestInfoList
        DestInfo
          DestObject : u_crg_sync2_2/reg_meta/Q[29]
          SrcInfoList
            SrcInfo
              SrcObject : u_crg_cmon/reg_done/Q
  ContainerInstance : m_crg_top

  -----------------------------------------------------------------------------
  Tag          : CDC_COHERENCY_BUS_NOCONV
  Description  : Control synchronized bits of same destination vector signal not checked for presence of gray encoder
  Violation    : CDC:144
  DestInfo
    DestObject : u_crg_sync2_3/reg_meta
  NoConvControlPathList
    NoConvControlPathInfo
      Type     : Control
      DestInfoList
        DestInfo
          DestObject : u_crg_sync2_3/reg_meta/Q[1:0]
          SrcInfoList
            SrcInfo
              SrcObject : u_crg_cmon/reg_done/Q
"""
MSG = "Multiple  synchronization of same source u_crg_rstctl/reg_grp_ip_n/Q[9]"
MSG_CMD = "-msg {Multiple  synchronization of same source u_crg_rstctl/reg_grp_ip_n/Q\\[9\\]}"


def _swapped(text: str) -> str:
    """Appendix A as the `cdc_check` run of crg gave it: the two paths of CDC:146 in the other order."""
    i = text.index("CDC:146")
    rest = text[i:]
    a = rest.index("    NoConvControlPathInfo\n")
    b = rest.index("    NoConvControlPathInfo\n", a + 1)
    c = rest.index("  ContainerInstance", b)
    return text[:i] + rest[:a] + rest[b:c] + rest[a:b] + rest[c:]


def _read(text: str) -> list[dict]:
    found, _ = vcstatic.parse_report(text)
    return [{"id": f"V{i}", **v} for i, v in enumerate(found, 1)]


# -- the report, the message, the identity (T1, T4) ---------------------------------------------------------------------------------------------------


def test_t1_the_message_is_the_tools_and_the_nested_destinations_are_known():
    vs = _read(APPENDIX_A)
    assert [(v["tool_id"], v["tag"], v["severity"]) for v in vs] == [("CDC:141", "CDC_COHERENCY_MULTI_SYNC", "error"), ("CDC:146", "CDC_COHERENCY_MULTI_SYNC", "error")]
    assert [v["message"] for v in vs] == [MSG, MSG]  # (`report_violations -list` prints this line for both)
    assert vs[0]["nested"] == {"DestObject": ["u_crg_rst_sync_35/reg_rst_n_meta/Q", "u_crg_rst_sync_37/reg_rst_n_meta/Q"]}
    assert vs[1]["nested"] == {"DestObject": ["u_crg_rst_sync_36/reg_rst_n_meta/Q", "u_crg_rst_sync_38/reg_rst_n_meta/Q"]}
    assert vs[0]["top"] == [] and vs[0]["fields"]["SrcObject"] == "u_crg_rstctl/reg_grp_ip_n/Q[9]"  # (the source is in SrcInfo: no field of the block)
    assert vs[0]["fields"]["DestObject"] == "u_crg_rst_sync_37/reg_rst_n_meta/Q"  # (fields: the first value of each name, as before)


def test_t1_placeholders_are_filled_the_way_the_tool_prints_them():
    (v,) = _read(SYNC_CTRL)
    # one clock: `name:domain` (as `-list` prints it, 166 of 166 crg messages compared); several clocks: left as they are
    assert v["message"] == ("Control synchronization scheme found for CDC path from u_crg_cmon/reg_gray_ls/Q[15:0] to "
                            "u_crg_cmon/u_crg_gray_sync_3/reg_meta/Q[15:0], crossing from i_clk_ls:4 to [DestClockInfoList]")
    assert v["top"] == ["SrcObject", "DestObject"] and v["nested"] == {}
    (g,) = _read(CLOCK_GROUP_MISSING)
    assert g["message"] == "Primary Clock vclk_async is not specified in any user-defined clock group." and g["nested"] == {} and g["top"] == []
    rdc, _ = vcstatic.parse_report("  RDC_X  (1 error/0 waived)\n  1.  reset a [b] c\n")
    assert rdc[0]["message"] == "reset a [b] c" and "nested" not in rdc[0]  # (the list format: nothing nested, the identity as before)


def test_t4_the_identity_does_not_depend_on_the_order_of_the_paths():
    before, after = _read(APPENDIX_A), _read(_swapped(APPENDIX_A))
    assert after[1]["fields"]["DestObject"] == "u_crg_rst_sync_36/reg_rst_n_meta/Q"  # (the order did change)
    assert [waivers.signature(v) for v in before] == [waivers.signature(v) for v in after]
    assert [waivers.sig_id("cdc", v) for v in before] == [waivers.sig_id("cdc", v) for v in after]
    assert waivers.sig_id("cdc", before[0]) != waivers.sig_id("cdc", before[1])
    assert waivers.legacy_sig_id("cdc", before[1]) != waivers.legacy_sig_id("cdc", after[1])  # (what 101c379 did: the first destination)
    assert waivers.signature(before[1]) == ("CDC_COHERENCY_MULTI_SYNC", "u_crg_rstctl/reg_grp_ip_n/Q[9]",
                                            "u_crg_rst_sync_36/reg_rst_n_meta/Q | u_crg_rst_sync_38/reg_rst_n_meta/Q")


# -- the waiver commands (T2, T3) ------------------------------------------------------------------------------------------------------------------


def test_t2_a_multi_sync_is_waived_by_the_tools_message():
    vs = _read(APPENDIX_A)
    for v in vs:
        cmd = waivers.waive_command(v, {"id": v["id"], "action": "waive", "rule": "", "reason": "one synchronizer per reset output"}, "cdc")
        assert cmd == f"waive_cdc -add W_{v['id']} -tag CDC_COHERENCY_MULTI_SYNC {MSG_CMD} -comment {{design: one synchronizer per reset output}}"
    old = {k: v for k, v in vs[0].items() if k not in ("nested", "top")}  # (a report read before: its command as then)
    assert waivers.selector(old, "cdc").startswith("-from {u_crg_rstctl/reg_grp_ip_n/Q\\[9\\]} -to {u_crg_rst_sync_37")


def test_t3_one_waiver_for_the_violations_it_selects_and_only_when_all_are_waived():
    vs = _read(APPENDIX_A)
    waive = {"action": "waive", "rule": "", "reason": "one synchronizer per reset output"}
    assert waivers.shared(vs, "cdc") == {"V1": ["V1", "V2"], "V2": ["V2", "V1"]}
    tcl, skipped = waivers.waive_tcl(vs, [{"id": "V1", **waive}, {"id": "V2", **waive}], "r", "cdc")
    assert tcl.count("waive_cdc") == 1 and f"waive_cdc -add W_V1 -tag CDC_COHERENCY_MULTI_SYNC {MSG_CMD}" in tcl and "# W_V1 selects V1, V2" in tcl and skipped == []
    tcl, skipped = waivers.waive_tcl(vs, [{"id": "V1", **waive}, {"id": "V2", "action": "fix_rtl"}], "r", "cdc")
    assert "waive_cdc" not in tcl and skipped == ["V1"]  # (never one that waives V2 too)


def test_a_set_of_bit_ranges_or_a_shared_source_is_not_what_from_names():
    """crg: `-from` never matches `Q{ [13:10] ,[8:0] }`, and selects every bus check that has the source (VC Static ignores `-to` there)."""
    v1, v2 = vs = _read(BUS_NOCONV)
    assert v1["nested"] == {"SrcObject": ["u_crg_cmon/reg_done/Q", "u_crg_rstctl/reg_held/Q{ [13:10] ,[8:0] }", "u_crg_rstctl/u_crg_sync2/reg_sync/Q[2:0]"]}
    assert v1["fields"]["SrcObject"] == "u_crg_rstctl/reg_held/Q{ [13:10] ,[8:0] }" and v1["fields"]["DestObject"] == "u_crg_sync2_2/reg_meta"
    assert waivers.selector(v1, "cdc", vs) == "-from {u_crg_rstctl/u_crg_sync2/reg_sync/Q\\[2:0\\]} -to {u_crg_sync2_2/reg_meta}"  # (it alone)
    assert waivers.selector(v2, "cdc", vs) == "-from {u_crg_cmon/reg_done/Q} -to {u_crg_sync2_3/reg_meta}"
    assert waivers.shared(vs, "cdc") == {"V2": ["V2", "V1"]}  # (V2's only source is one of V1's too)
    plan, skipped = waivers.waiver_plan(vs, [{"id": "V1", "action": "fix_rtl"}, {"id": "V2", "action": "waive", "reason": "x"}], "cdc")
    assert plan == [] and skipped == ["V2"]
    plan, _ = waivers.waiver_plan(vs, [{"id": "V1", "action": "waive", "reason": "x"}, {"id": "V2", "action": "waive", "reason": "x"}], "cdc")
    assert [(name, ids) for name, ids, _cmd in plan] == [("W_V1", ["V1"]), ("W_V2", ["V2", "V1"])]
    (sync,) = _read(SYNC_CTRL)  # (objects that are fields of the block: -from and -to as before)
    assert waivers.selector(sync, "cdc") == "-from {u_crg_cmon/reg_gray_ls/Q\\[15:0\\]} -to {u_crg_cmon/u_crg_gray_sync_3/reg_meta/Q\\[15:0\\]}"


def test_a_message_with_a_placeholder_is_never_a_selector():
    (v,) = _read(SYNC_CTRL.replace("  SrcObject                 : u_crg_cmon/reg_gray_ls/Q[15:0]\n", ""))  # (no source: no -from)
    assert v["message"].endswith("[DestClockInfoList]") and waivers.selector(v, "cdc") is None
    assert "[SrcObject]" in waivers.no_selector_reason(v) or "[DestClockInfoList]" in waivers.no_selector_reason(v)
    (g,) = _read(CLOCK_GROUP_MISSING)
    assert waivers.selector(g, "cdc") is None and "does not say which objects" in waivers.no_selector_reason(g)  # (as before: a question)


# -- virtual clocks (T5) ----------------------------------------------------------------------------------------------------------------------------------


def test_t5_a_virtual_clock_of_extra_tcl_gets_its_own_group(tmp_path):
    sdcj = {"clocks": [{"name": "A"}, {"name": "B"}],
            "extra_tcl": ["# create_clock -name old_one -period 5", "create_clock -name vclk_async -period 41.666",
                          "set_input_delay -clock [get_clocks vclk_async] 0 [get_ports {i_por_n i_pll_cpu_lock}]"]}
    t = script.text("cdc", "blk", tmp_path, tmp_path / "f.f", tmp_path / "w", tmp_path / "s.tcl", sdcj)
    assert "set_clock_groups -asynchronous -group {A} -group {B} -group {vclk_async}\n" in t and "old_one" not in t
    grouped = {**sdcj, "clock_groups": [{"kind": "asynchronous", "groups": [["A"], ["B"]]}]}
    assert "set_clock_groups" not in script.text("rdc", "blk", tmp_path, tmp_path / "f.f", tmp_path / "w", tmp_path / "s.tcl", grouped)  # (yours: as it is)
    assert script.group_lines({"clocks": [{"name": "A"}], "extra_tcl": ["create_clock -name v -period 10"]}) == "set_clock_groups -asynchronous -group {A} -group {v}\n"
    assert script.group_lines({"extra_tcl": ["create_clock -name v -period 10"]}) == ""  # (no sdc.json clocks: a hand-written SDC is trusted)
    assert script.created_clocks("create_clock -period 10 -name {c1} [get_ports x]\n  # create_clock -name c2\ncreate_clock -name c1") == ["c1"]


# -- the not-applied waivers (T6) -------------------------------------------------------------------------------------------------------------------------

NOT_APPLIED = """
  -----------------------------------------------------------------------------
  Waiver Summary
  -----------------------------------------------------------------------------
  Waiver         : W_V32
  Comment        : design: FR-03 trial
  Filename       : /p/build/sanity/cdc_waive.tcl
  Line           : 27
  Violations     : 0
  Select
    Tag          : CDC_COHERENCY_BUS_NOCONV
    ToPoints     : u_crg_sync2_2/reg_meta
    FromPoints   : u_crg_rstctl/reg_held/Q{ [13:10] ,[8:0] }
  -----------------------------------------------------------------------------
"""


def test_t6_the_tools_list_of_waivers_that_matched_nothing_is_read(tmp_path):
    (w,) = vcstatic.parse_not_applied(NOT_APPLIED)
    assert w == {"waiver": "W_V32", "file": "/p/build/sanity/cdc_waive.tcl", "line": 27, "violations": 0,
                 "select": {"Tag": "CDC_COHERENCY_BUS_NOCONV", "ToPoints": "u_crg_sync2_2/reg_meta", "FromPoints": "u_crg_rstctl/reg_held/Q{ [13:10] ,[8:0] }"}}
    log = "/p/build/sanity/cdc_waive.tcl:27: [Warning] CDC_WAIVE_OPTION_INVALID: waive_cdc Option 'from' doesn't exist in Violations\n"
    assert vcstatic.waive_warnings(log, "cdc_waive.tcl") == {27: "CDC_WAIVE_OPTION_INVALID: waive_cdc Option 'from' doesn't exist in Violations"}
    (tmp_path / "cdc_not_applied.rpt").write_text(NOT_APPLIED)
    got = check.not_applied(tmp_path, "cdc", Path("/p/build/sanity/cdc_waive.tcl"), log, [("W_V32", ["V32"], "")])
    assert got[0]["waiver"] == "W_V32" and got[0]["ids"] == ["V32"] and got[0]["why"].startswith("CDC_WAIVE_OPTION_INVALID")
    t = script.text("cdc", "blk", tmp_path, tmp_path / "f.f", tmp_path / "w", tmp_path / "s.tcl", {}, waive=tmp_path / "cdc_waive.tcl")
    assert t.index(f"source {tmp_path / 'cdc_waive.tcl'}") < t.index(f"catch {{redirect -file {tmp_path / 'w'}/cdc_not_applied.rpt {{waive_cdc -not_applied}}}}") < t.index("report_violations")
    assert "not_applied" not in script.text("cdc", "blk", tmp_path, tmp_path / "f.f", tmp_path / "w", tmp_path / "s.tcl", {})  # (the analysis run: no waivers)


# -- the steps, with a fake VC Static that matches waivers the way the tool does ------------------------------------------------------------------------

FAKE_NESTED = r'''
import json, re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
if "check_" not in text:  # (the setup's dry run)
    sys.exit(0)
out = re.search(r"-file (\S+_violations\.rpt)", text).group(1)
if "check_rdc" in text:
    Path(out).write_text("")
    sys.exit(0)
blocks = json.loads(Path(%r, "nested.json").read_text())
un = lambda s: s.replace("\\[", "[").replace("\\]", "]")
waived, summary = set(), []
for m in re.finditer(r"^source (\S+)$", text, re.M):
    wf = Path(m.group(1))
    for n, cmd in enumerate(wf.read_text().splitlines(), 1):
        if not cmd.startswith("waive_cdc"):
            continue
        tag = re.search(r"-tag (\S+)", cmd).group(1)
        sel = {k: un(x) for k, x in re.findall(r"-(msg|from|to) \{(.*?)\}(?= -|$)", cmd)}
        cp = re.search(r'ConvergencePoint == "(.*?)"', cmd)
        hit = []
        for i, b in enumerate(blocks):
            if b["tag"] != tag:
                continue
            if cp:
                ok = b.get("cp") == cp.group(1)
            elif "msg" in sel:
                ok = b["msg"] == sel["msg"]
            else:  # (-from: one of the violation's from points; -to only where it has to points)
                ok = sel.get("from") in b.get("from", []) and (not b.get("to") or sel.get("to") in b["to"])
            if ok:
                hit.append(i)
        waived.update(hit)
        if not hit:
            summary.append((re.search(r"-add (\S+)", cmd).group(1), wf, n, tag))
            if "from" in sel:
                print(f"{wf}:{n}: [Warning] CDC_WAIVE_OPTION_INVALID: waive_cdc Option 'from' doesn't exist in Violations")
na = re.search(r"redirect -file (\S+) \{waive_cdc -not_applied\}", text)
if na:
    Path(na.group(1)).write_text("  Waiver Summary\n" + "".join(f"  Waiver : {w}\n  Filename : {f}\n  Line : {n}\n  Violations : 0\n  Select\n    Tag : {t}\n"
                                                         for w, f, n, t in summary))
lines = []
for i, b in enumerate(blocks):
    if i not in waived:
        body = b.get("check_text") if "source " in text and b.get("check_text") else b["text"]
        lines += [f"  {b['tag']}  (1 {b['severity']}/0 waived)", "  ----", body.rstrip("\n"), ""]
Path(out).write_text("\n".join(lines) + "\n")
print("VC Static ran")
'''


def _blocks(text: str) -> list[str]:
    return ["  Tag" + p.split("\n\n")[0].rstrip() + "\n" for p in re.split(r"(?m)^  Tag(?=\s+:)", text)[1:]]


def _multi_sync(swap_in_check: bool = False) -> list[dict]:
    """The two MULTI_SYNC of Appendix A for the fake tool: no from / to points, the message of `-list`; the second one's paths swapped in the
    check run when asked (as on crg)."""
    out = []
    for a, b in zip(_blocks(APPENDIX_A), _blocks(_swapped(APPENDIX_A))):
        out.append({"tag": "CDC_COHERENCY_MULTI_SYNC", "severity": "error", "text": a, "msg": MSG, "from": [], "to": [],
                    **({"check_text": b} if swap_in_check else {})})
    return out


def _nested_tools(root: Path, blocks: list[dict]) -> None:
    (root / "nested.json").write_text(json.dumps(blocks))
    (root / "fake_nested.py").write_text(FAKE_NESTED % str(root))
    cmd = f"{sys.executable} {root / 'fake_nested.py'} {{script}}"
    (root / "q3tui.yaml").write_text(f"pipeline:\n  flow: sanity\ntools:\n  roles:\n    cdc: {{cmd: \"{cmd}\", parse: generic}}\n    rdc: {{cmd: \"{cmd}\", parse: generic}}\n")


WAIVE = dict(action="waive", reason="spec 9.3: one synchronizer per reset output, each output goes to its own block (crg intent Table 5-2)")


async def test_t3_the_multi_syncs_of_crg_end_waived_by_one_waiver(proj, monkeypatch):
    from q3tui.llm import runtime

    _nested_tools(proj.root, _multi_sync(swap_in_check=True))
    llm = ReviewLLM(lambda i: WAIVE)
    monkeypatch.setattr(runtime, "run_stage", llm)
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)
    chk = json.loads((proj.schemas_dir / "cdc_check.json").read_text())
    assert (chk["total"], chk["waived"], chk["open"], chk["expected_open"], chk["ok"], chk["not_applied"]) == (2, 2, 0, 0, True, [])
    tcl = (proj.root / "build" / "sanity" / "cdc_waive.tcl").read_text()
    assert tcl.count("waive_cdc") == 1 and f"waive_cdc -add W_V1 -tag CDC_COHERENCY_MULTI_SYNC {MSG_CMD}" in tcl
    prompt = llm.stages[0].prompt
    assert MSG in prompt and "every path of the report: DestObject (2): u_crg_rst_sync_35/reg_rst_n_meta/Q, u_crg_rst_sync_37/reg_rst_n_meta/Q" in prompt
    assert {v.name: v.status for v in eng.status()}["rdc_check"] == "done"  # (the RDC lane runs: cdc_check passed)


async def test_t3_a_waive_that_would_waive_a_violation_not_waived_is_asked(proj, monkeypatch):
    from q3tui.llm import runtime

    _nested_tools(proj.root, _multi_sync())
    decisions = {"V1": WAIVE, "V2": dict(action="ask", reason="not sure the ddrc reset may be released late", question="Is a late release fine?",
                                         options=["Yes, waive it", "No, fix the RTL"])}
    llm = ReviewLLM(lambda i: decisions[i])
    monkeypatch.setattr(runtime, "run_stage", llm)
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="cdc_waivers")
    items = {i["id"]: i for i in json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())["items"]}
    v1 = items["V1"]
    assert v1["action"] == "ask" and v1["shared"]["with"] == ["V2"] and "V2" in v1["question"] and MSG_CMD in v1["reason"]
    assert v1["options"] == ["Yes, accept it as open", "No, fix the RTL"] and v1["shared"]["reason"] == WAIVE["reason"]
    assert len(eng.by_name["cdc_waivers"].question_list(eng)) == 2
    await eng.run(yes=True, only="cdc_check")
    chk = json.loads((proj.schemas_dir / "cdc_check.json").read_text())
    assert chk["ok"] and chk["open"] == 2 and chk["expected_open"] == 2  # (no waiver written: both stay open, as the review decided)
    assert "waive_cdc" not in (proj.root / "build" / "sanity" / "cdc_waive.tcl").read_text()
    # your answer settles V2: the review decides it again (waive); V1 is not decided again: its own waive comes back, one waiver for both
    decisions["V2"] = WAIVE
    Ops(eng).answer(items["V2"]["sig"], "Yes, waive it")
    n = len(llm.stages)
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True)
    assert len(llm.stages) == n + 1 and "**V1**" not in llm.stages[-1].prompt and "**V2**" in llm.stages[-1].prompt
    items = {i["id"]: i for i in json.loads((proj.schemas_dir / "cdc_waivers.json").read_text())["items"]}
    assert items["V1"]["action"] == "waive" and "shared" not in items["V1"] and items["V2"]["action"] == "waive"
    chk = json.loads((proj.schemas_dir / "cdc_check.json").read_text())
    assert chk["ok"] and chk["waived"] == 2 and chk["open"] == 0


async def test_t6_the_check_names_the_waiver_that_matched_nothing(proj, monkeypatch):
    from q3tui.llm import runtime

    (sync,) = _blocks(SYNC_CTRL)
    _nested_tools(proj.root, [{"tag": "CDC_SYNC_CTRL", "severity": "error", "text": sync, "msg": "m", "from": ["u_other/Q"], "to": []}])
    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(lambda i: WAIVE))
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="cdc_check")
    st = {v.name: v for v in eng.status()}["cdc_check"]
    chk = json.loads((proj.schemas_dir / "cdc_check.json").read_text())
    assert st.status == "failed" and "waived but still reported: V1" in st.detail
    assert "waivers that matched no violation: W_V1 (V1: CDC_WAIVE_OPTION_INVALID: waive_cdc Option 'from' doesn't exist in Violations)" in st.detail
    assert chk["not_applied"][0]["waiver"] == "W_V1" and chk["not_applied"][0]["ids"] == ["V1"] and chk["not_applied"][0]["select"] == {"Tag": "CDC_SYNC_CTRL"}


# -- what was decided before the identity changed -------------------------------------------------------------------------------------------------------


async def test_decisions_and_answers_stored_with_the_old_identity_are_kept(proj, monkeypatch):
    """A review stored by 101c379 (items and answers keyed by the first destination) is found again: nothing goes to the LLM again."""
    from q3tui.llm import runtime

    _nested_tools(proj.root, _multi_sync())
    decisions = {"V1": dict(action="ask", reason="x", question="q1?", options=["a", "b"]), "V2": dict(action="ask", reason="y", question="q2?", options=["a", "b"])}
    llm = ReviewLLM(lambda i: decisions[i])
    monkeypatch.setattr(runtime, "run_stage", llm)
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="cdc_waivers")
    report = json.loads((proj.schemas_dir / "cdc_report.json").read_text())
    f = proj.schemas_dir / "cdc_waivers.json"
    plan = json.loads(f.read_text())
    old = {v["id"]: waivers.legacy_sig_id("cdc", v) for v in report["violations"]}
    assert all(old[i["id"]] != i["sig"] for i in plan["items"])  # (the identity did change for these two)
    for i in plan["items"]:  # (the plan and the answer as 101c379 wrote them)
        i["sig"], i["answer"] = old[i["id"]], ("a" if i["id"] == "V1" else "")
    f.write_text(json.dumps(plan))
    (proj.root / "reviews").mkdir(exist_ok=True)
    (proj.root / "reviews" / "cdc_answers.json").write_text(json.dumps({old["V1"]: "a"}))
    n = len(llm.stages)
    await Engine(Project.open(proj.root)).run(yes=True, only="cdc_waivers")
    assert len(llm.stages) == n  # (both kept: same RTL, SDC and knowledge, the answer unchanged)
    items = {i["id"]: i for i in json.loads(f.read_text())["items"]}
    assert items["V1"]["sig"] == waivers.sig_id("cdc", report["violations"][0]) and items["V1"]["answer"] == "a" and items["V2"]["answer"] == ""


async def test_a_report_read_before_keeps_its_identity_in_the_check(proj, monkeypatch):
    """A cdc_report.json written by 101c379 (no nested objects): the check compares by the identity it was reviewed with, and when that
    fails it says to run the analysis again."""
    from q3tui.llm import runtime

    _nested_tools(proj.root, _multi_sync(swap_in_check=True))
    monkeypatch.setattr(runtime, "run_stage", ReviewLLM(lambda i: dict(action="fix_rtl", module="blk", change="x", reason="y")))
    eng = Engine(Project.open(proj.root))
    await eng.run(yes=True, stop="cdc_waivers")
    f = proj.schemas_dir / "cdc_report.json"
    report = json.loads(f.read_text())
    for v in report["violations"]:
        v.pop("nested"), v.pop("top")
    f.write_text(json.dumps(report))
    await eng.run(yes=True, only="cdc_check")
    st = {v.name: v for v in eng.status()}["cdc_check"]
    chk = json.loads((proj.schemas_dir / "cdc_check.json").read_text())
    assert chk["old_report"] is True and st.status == "failed" and "run step cdc again" in st.detail  # (the first destination moved: 101c379's identity)
