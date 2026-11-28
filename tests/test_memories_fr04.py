"""FR-04 of the FX1B project: memory modules in the fpga and asic flows — target bodies (R2), black boxes and their timing model (R3), the
logic and the memories apart (R4), the same in the LEC (R5), the stop before a tool builds a big array from registers (R6), a tool that
could not run is no design problem (R7), and nothing changes without the options (R8).

FakeLLM-free (the steps here make no LLM call) and with fake tools; the report text is trimmed from the runs of Vivado 2025.1, Design Compiler
X-2025.06-SP5 and Formality X-2025.06-SP4 on the fixture below (FR-04 reply, "Live checks")."""

import json
import sys
from pathlib import Path

import pytest

from q3tui.core.project import Project
from q3tui.flows.asic.lib import dc, fm
from q3tui.flows.fpga.lib import memories as memmod
from q3tui.flows.fpga.lib import vivado
from q3tui.hdl import memories as hdlmem
from q3tui.pipeline.engine import Engine

MEM = """module m_t_sram_dp #(
  parameter int PARA_DEPTH     = 64,
  parameter int PARA_DATA_W    = 32,
  parameter int PARA_MASK_GRAN = 1,
  localparam int LPARA_ADDR_W  = $clog2(PARA_DEPTH)
) (
  input  logic                                  i_clk_mem,
  input  logic                                  i_wr_en,
  input  logic [LPARA_ADDR_W-1:0]               i_wr_addr,
  input  logic [PARA_DATA_W-1:0]                i_wr_data,
  input  logic [PARA_DATA_W/PARA_MASK_GRAN-1:0] i_wr_mask,
  input  logic                                  i_rd_en,
  input  logic [LPARA_ADDR_W-1:0]               i_rd_addr,
  output logic [PARA_DATA_W-1:0]                o_rd_data
);
  logic [PARA_DATA_W-1:0] reg_mem [0:PARA_DEPTH-1];
  always_ff @(posedge i_clk_mem) begin : p_ff_mem
    for (int idx = 0; idx < PARA_DATA_W; idx++)
      if (i_wr_en && i_wr_mask[idx / PARA_MASK_GRAN]) reg_mem[i_wr_addr][idx] <= i_wr_data[idx];
  end
  always_ff @(posedge i_clk_mem) if (i_rd_en) o_rd_data <= reg_mem[i_rd_addr];
endmodule
"""
TOP = """module m_t_top (input logic i_clk, input logic i_rst_n, input logic i_wr_en, input logic [31:0] i_wr_data, input logic [3:0] i_wr_mask,
                output logic [31:0] o_rd_a, output logic [15:0] o_rd_b);
  logic [5:0] reg_wptr, reg_rptr;
  logic [31:0] w_rd_a;
  logic [15:0] w_rd_b;
  always_ff @(posedge i_clk or negedge i_rst_n)
    if (!i_rst_n) begin reg_wptr <= '0; reg_rptr <= '0; end
    else begin if (i_wr_en) reg_wptr <= reg_wptr + 6'd1; reg_rptr <= reg_rptr + 6'd3; end
  m_t_sram_dp #(.PARA_DEPTH(DEPTH_A), .PARA_DATA_W(32), .PARA_MASK_GRAN(8)) u_mem_a (.i_clk_mem(i_clk), .i_wr_en(i_wr_en), .i_wr_addr(reg_wptr),
    .i_wr_data(i_wr_data), .i_wr_mask(i_wr_mask), .i_rd_en(1'b1), .i_rd_addr(reg_rptr), .o_rd_data(w_rd_a));
  for (genvar g = 0; g < 1; g++) begin : gen_b
    m_t_sram_dp #(.PARA_DEPTH(DEPTH_B), .PARA_DATA_W(16), .PARA_MASK_GRAN(1)) u_mem_b (.i_clk_mem(i_clk), .i_wr_en(i_wr_en), .i_wr_addr(reg_rptr),
      .i_wr_data(i_wr_data[15:0]), .i_wr_mask(i_wr_data[31:16]), .i_rd_en(i_wr_mask[0]), .i_rd_addr(reg_wptr), .o_rd_data(w_rd_b));
  end
  always_ff @(posedge i_clk) begin o_rd_a <= w_rd_a ^ {w_rd_b, w_rd_b}; o_rd_b <= w_rd_b + w_rd_a[15:0]; end
endmodule
"""
BODY = MEM.replace("""  logic [PARA_DATA_W-1:0] reg_mem [0:PARA_DEPTH-1];
  always_ff @(posedge i_clk_mem) begin : p_ff_mem
    for (int idx = 0; idx < PARA_DATA_W; idx++)
      if (i_wr_en && i_wr_mask[idx / PARA_MASK_GRAN]) reg_mem[i_wr_addr][idx] <= i_wr_data[idx];
  end
  always_ff @(posedge i_clk_mem) if (i_rd_en) o_rd_data <= reg_mem[i_rd_addr];
""", """  for (genvar g = 0; g < PARA_DATA_W / PARA_MASK_GRAN; g++) begin : gen_col  // one RAM column per mask group
    logic [PARA_MASK_GRAN-1:0] reg_col [0:PARA_DEPTH-1];
    always_ff @(posedge i_clk_mem) if (i_wr_en && i_wr_mask[g]) reg_col[i_wr_addr] <= i_wr_data[g*PARA_MASK_GRAN +: PARA_MASK_GRAN];
    always_ff @(posedge i_clk_mem) if (i_rd_en) o_rd_data[g*PARA_MASK_GRAN +: PARA_MASK_GRAN] <= reg_col[i_rd_addr];
  end
""")
# Vivado 2025.1 on the fixture with the FPGA body (the rows the readers use)
UTIL = """| Slice LUTs*                |  157 |     0 |          0 |    203800 |  0.08 |
|   LUT as Logic             |   77 |     0 |          0 |    203800 |  0.04 |
|   LUT as Memory            |   80 |     0 |          0 |     64000 |  0.13 |
| Slice Registers            |  108 |     0 |          0 |    407600 |  0.03 |
| Block RAM Tile |    0 |     0 |          0 |       445 |  0.00 |
"""
TIMING = """| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------
      2.710        0.000                      0                  588        0.096        0.000                      0                  588

All user specified timing constraints are met.
"""
HIER = """1. Utilization by Hierarchy
---------------------------

+------------------+-----------------------------+------------+------------+---------+------+-----+--------+--------+------------+
|     Instance     |            Module           | Total LUTs | Logic LUTs | LUTRAMs | SRLs | FFs | RAMB36 | RAMB18 | DSP Blocks |
+------------------+-----------------------------+------------+------------+---------+------+-----+--------+--------+------------+
| m_t_top          |                       (top) |        157 |         77 |      80 |    0 | 108 |      0 |      0 |          0 |
|   (m_t_top)      |                       (top) |          9 |          9 |       0 |    0 |  60 |      0 |      0 |          0 |
|   u_mem_a        |                 m_t_sram_dp |         52 |          4 |      48 |    0 |  32 |      0 |      0 |          0 |
|   gen_b[0].u_mem_b | m_t_sram_dp__parameterized0 |       96 |         64 |      32 |    0 |  16 |      0 |      0 |          0 |
+------------------+-----------------------------+------------+------------+---------+------+-----+--------+--------+------------+
"""
# Design Compiler X-2025.06-SP5 on the fixture with the body as the ASIC body (`set_ungroup false` on the memories)
AREA = """Number of ports:                           14
Number of cells:                        12573
Number of sequential cells:              3141
Combinational area:               7672.148700
Noncombinational area:           11365.936000
Total cell area:                 19038.084724

Hierarchical area distribution
------------------------------

                                  Global cell area            Local cell area
                                  -------------------  -----------------------------
Hierarchical cell                 Absolute    Percent  Combi-     Noncombi-   Black-
                                  Total       Total    national   national    boxes   Design
--------------------------------  ----------  -------  ---------  ----------  ------  ------------------------------------------------------
m_t_top                           19038.0847    100.0   126.5241    218.3696  0.0000  m_t_top
u_mem_a                           12287.7546     64.5  4856.0436   7431.7110  0.0000  m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W32_PARA_MASK_GRAN8
gen_b_0__u_mem_b
                                   6405.4365     33.6  2689.5810   3715.8555  0.0000  m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W16_PARA_MASK_GRAN1
--------------------------------  ----------  -------  ---------  ----------  ------  ------------------------------------------------------
Total                                                  7672.1487  11365.9360  0.0000
"""
FAKE_VIVADO = r'''import json, re, sys, time
from pathlib import Path
script, work = Path(sys.argv[1]), Path(sys.argv[2])
cfg = json.loads(Path(%r).read_text())
with (Path(%r)).open("a") as f:
    f.write(script.read_text() + "\n=====\n")
log = work / "vivado.log"
log.write_text("Vivado v.2025.1 (lin64)\n")
if cfg.get("licence"):
    print("ERROR: [Common 17-345] A valid license was not found for feature 'Synthesis' and/or device 'xc7k325t'.")
    sys.exit(1)
if cfg.get("dissolve"):
    with log.open("a") as f:
        f.write("Starting Synthesize : Time (s): cpu = 00:00:02\nWARNING: [Synth 8-4767] Trying to implement RAM 'reg_mem_reg' in registers. Block RAM or DRAM implementation is not possible; see log for reasons.\n")
    time.sleep(60)
for n, text in cfg["reports"].items():
    (work / n).write_text(text)
print("synthesis done")
'''
FAKE_DC = r'''import re, sys
from pathlib import Path
script, work = Path(sys.argv[1]), Path(sys.argv[2])
text = script.read_text()
Path(%r).write_text(text)
area = Path(%r).read_text()
for n in ("timing", "qor", "power", "constraints", "check_design", "check_timing"):
    (work / (n + ".rpt")).write_text("slack (MET)    1.10\n" if n == "timing" else "")
(work / "area.rpt").write_text(area)
net = Path(re.search(r'write -format verilog -hierarchy -output "([^"]+)"', text).group(1))
net.parent.mkdir(parents=True, exist_ok=True)
stubs = "m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W32_PARA_MASK_GRAN8 m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W16_PARA_MASK_GRAN1".split() if "q3tui_bb_" in text else []
net.write_text("".join(f"module {s} ( i_clk_mem );\n  input i_clk_mem;\nendmodule\n" for s in stubs) + "module m_t_top ( i_clk );\nendmodule\n")
Path(re.search(r'set_svf "([^"]+)"', text).group(1)).write_text("guidance\n")
print("Design Compiler Version: X-2025.06-SP5")
'''
FAKE_FM = r'''import sys
from pathlib import Path
Path(%r).write_text(Path(sys.argv[1]).read_text())
Path(sys.argv[2], "lec.rpt").write_text("matched\n")
print("Verification SUCCEEDED")
print("  205 Passing compare points")
'''


def _project(root: Path, depth_a: int = 64, depth_b: int = 64, yaml_extra: str = "", flow: str = "fpga") -> Project:
    (root / "src" / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "src" / "rtl" / "m_t_sram_dp.sv").write_text(MEM)
    (root / "src" / "rtl" / "m_t_top.sv").write_text(TOP.replace("DEPTH_A", str(depth_a)).replace("DEPTH_B", str(depth_b)))
    (root / "src" / "rtl" / "filelist.f").write_text("src/rtl/m_t_sram_dp.sv\nsrc/rtl/m_t_top.sv\n")
    (root / "src" / "fpga").mkdir(parents=True, exist_ok=True)
    (root / "src" / "fpga" / "m_t_sram_dp.sv").write_text(BODY)
    (root / "schemas").mkdir(exist_ok=True)
    (root / "schemas" / "structured_spec.json").write_text(json.dumps({"metadata": {"top_module": "m_t_top"}}))
    (root / "lib.db").write_text("fake library\n")
    (root / "vivado.json").write_text(json.dumps({"reports": {"utilization.rpt": UTIL, "timing.rpt": TIMING, "utilization_hier.rpt": HIER,
                                                              "check_timing.rpt": "", "paths.rpt": ""}}))
    (root / "fake_vivado.py").write_text(FAKE_VIVADO % (str(root / "vivado.json"), str(root / "vivado_scripts.txt")))
    (root / "area.rpt").write_text(AREA)
    (root / "fake_dc.py").write_text(FAKE_DC % (str(root / "dc_script.tcl"), str(root / "area.rpt")))
    (root / "fake_fm.py").write_text(FAKE_FM % str(root / "fm_script.tcl"))
    py = sys.executable
    (root / "q3tui.yaml").write_text(
        f"pipeline:\n  flow: {flow}\n  history: false\n  step_options:\n    timing_util_report: {{judge: 'off'}}\n    asic_synth:\n      library: {root / 'lib.db'}\n"
        + yaml_extra + "tools:\n  roles:\n"
        f"    fpga_synth: {{cmd: \"{py} {root / 'fake_vivado.py'} {{script}} {{workdir}}\", parse: vivado}}\n"
        f"    asic_synth: {{cmd: \"{py} {root / 'fake_dc.py'} {{script}} {{workdir}}\", parse: dc}}\n"
        f"    asic_lec: {{cmd: \"{py} {root / 'fake_fm.py'} {{script}} {{workdir}}\", parse: generic}}\n")
    return Project.open(root)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """The asic_port porter finds nothing (an empty plan); no other stage may call an LLM here."""
    from q3tui.flows.asic.lib.porter import PortPlan
    from q3tui.llm import runtime
    from q3tui.llm.runtime import StageResult

    async def fake(stage, cfg, emit):
        if stage.name.startswith("asic_port"):
            return StageResult("ok", PortPlan(), 0.0, 1, "s")
        raise AssertionError(f"unexpected LLM stage {stage.name}")

    monkeypatch.setattr(runtime, "run_stage", fake)


def _facts(root: Path) -> hdlmem.Facts:
    return hdlmem.scan([root / "src" / "rtl" / "m_t_sram_dp.sv", root / "src" / "rtl" / "m_t_top.sv"], "m_t_top")


# -- the facts and the plan -------------------------------------------------------------------------------------------------------------------


def test_the_memories_of_each_instance_are_read_with_their_parameters(tmp_path):
    p = _project(tmp_path)
    f = _facts(p.root)
    assert f.error == "" and f.memory_modules() == ["m_t_sram_dp"]  # (what Q3TUI proposes: an array of 16 words or more)
    mem = {i.path: (i.module, [(a.name, a.words, a.width) for a in i.arrays]) for i in f.instances if i.arrays}
    assert mem == {"u_mem_a": ("m_t_sram_dp", [("reg_mem", 64, 32)]), "gen_b[0].u_mem_b": ("m_t_sram_dp", [("reg_mem", 64, 16)])}
    assert f.clocks["m_t_sram_dp"] == ["i_clk_mem"] and f.match(["m_t_*_dp"]) == ["m_t_sram_dp"]
    assert f.headers["m_t_sram_dp"].startswith("module m_t_sram_dp #(") and f.headers["m_t_sram_dp"].endswith(");")
    assert hdlmem.norm("gen_b[0].u_mem_b") == hdlmem.norm("gen_b_0__u_mem_b")  # (Design Compiler's name of the same instance)
    assert hdlmem.module_of("m_t_sram_dp__parameterized0", f.modules) == "m_t_sram_dp" and hdlmem.module_of("m_x_ctrl_0", ["m_x", "m_x_ctrl"]) == "m_x_ctrl"


def test_t1_without_a_memory_option_the_files_and_the_script_are_as_before(tmp_path):
    """R8: no body, no black box — the tool reads the RTL files as before (the same list, the same order, the same script)."""
    p = _project(tmp_path)
    rtl = sorted((p.root / "src" / "rtl").glob("*.sv"))
    plan = memmod.make_plan(p, {}, rtl, "m_t_top", p.fpga_dir / "stubs", memmod.VIVADO_BLACK_BOX)
    assert plan.files == rtl and not plan.active and plan.modules == ["m_t_sram_dp"] and plan.proposed
    assert vivado.script_text(plan.files, "m_t_top", p.sdc_dir, tmp_path) == vivado.script_text(rtl, "m_t_top", p.sdc_dir, tmp_path)
    lib = ["/lib.db"]
    assert dc.script_text(plan.files, "m_t_top", lib, None, tmp_path, tmp_path / "n.v", {}, None) == dc.script_text(rtl, "m_t_top", lib, None, tmp_path, tmp_path / "n.v", {})
    assert fm.script_text(rtl, "m_t_top", lib, tmp_path / "n.v", None, tmp_path, {}, None) == fm.script_text(rtl, "m_t_top", lib, tmp_path / "n.v", None, tmp_path, {})


def test_t1_a_target_body_replaces_the_rtl_file_in_that_flow_only(tmp_path):
    p = _project(tmp_path)
    rtl = sorted((p.root / "src" / "rtl").glob("*.sv"))
    plan = memmod.make_plan(p, {"bodies": {"m_t_sram_dp": "src/fpga/m_t_sram_dp.sv"}}, rtl, "m_t_top", p.fpga_dir / "stubs", None)
    script = vivado.script_text(plan.files, "m_t_top", p.sdc_dir, tmp_path)
    assert str(p.root / "src" / "fpga" / "m_t_sram_dp.sv") in script and str(p.root / "src" / "rtl" / "m_t_sram_dp.sv") not in script
    assert str(p.root / "src" / "rtl" / "m_t_top.sv") in script and plan.mode("m_t_sram_dp") == "body"
    assert (p.root / "src" / "rtl" / "m_t_sram_dp.sv").read_text() == MEM  # (the RTL is not changed)
    assert memmod.option_bodies(["m_t_sram_dp=src/fpga/m_t_sram_dp.sv"]) == {"m_t_sram_dp": "src/fpga/m_t_sram_dp.sv"}  # (the Settings dialog's lines)


@pytest.mark.parametrize("options, says", [
    ({"bodies": {"m_nope": "src/fpga/m_t_sram_dp.sv"}}, "`m_nope` is not a module of the RTL"),
    ({"bodies": {"m_t_sram_dp": "src/fpga/missing.sv"}}, "is not there"),
    ({"bodies": {"m_t_sram_dp": "src/rtl/m_t_top.sv"}}, "does not define `m_t_sram_dp`"),
    ({"blackbox": ["m_t_top"]}, "the top `m_t_top` cannot be a black box"),
    ({"blackbox": ["m_t_sram_dp"], "bodies": {"m_t_sram_dp": "src/fpga/m_t_sram_dp.sv"}}, "a body and a black box at once"),
    ({"blackbox": ["m_t_sram_dp"], "blackbox_timing": {"*": {"access_pct": 40}}}, "give access_pct and setup_pct"),
    ({"max_register_kbit": "lots"}, "is not a number"),
])
def test_a_memory_setting_that_cannot_be_applied_is_said_before_the_tool(tmp_path, options, says):
    p = _project(tmp_path)
    with pytest.raises(memmod.PlanError, match=says.replace("`", ".").replace("(", ".").replace(")", ".")):
        memmod.max_register_bits(options)
        memmod.make_plan(p, options, sorted((p.root / "src" / "rtl").glob("*.sv")), "m_t_top", p.fpga_dir / "stubs", None)


def test_a_file_that_defines_another_module_cannot_be_left_out(tmp_path):
    p = _project(tmp_path)
    both = p.root / "src" / "rtl" / "m_t_sram_dp.sv"
    both.write_text(MEM + "module m_t_helper(input logic a, output logic b); assign b = a; endmodule\n")
    with pytest.raises(memmod.PlanError, match="defines m_t_helper besides `m_t_sram_dp`"):
        memmod.make_plan(p, {"blackbox": ["m_t_sram_dp"]}, sorted((p.root / "src" / "rtl").glob("*.sv")), "m_t_top", p.fpga_dir / "stubs", None)


# -- black boxes (T3) ---------------------------------------------------------------------------------------------------------------------------


def test_t3_a_black_box_is_a_stub_of_the_modules_ports_and_parameters(tmp_path):
    p = _project(tmp_path)
    rtl = sorted((p.root / "src" / "rtl").glob("*.sv"))
    plan = memmod.make_plan(p, {"blackbox": True, "blackbox_timing": {"m_t_sram_dp": {"clock": "i_clk", "access_pct": 40, "setup_pct": 25}}},
                            rtl, "m_t_top", p.fpga_dir / "stubs", memmod.VIVADO_BLACK_BOX)
    stub = (p.fpga_dir / "stubs" / "m_t_sram_dp.sv").read_text()
    assert plan.blackbox == ["m_t_sram_dp"] and plan.files == [p.root / "src" / "rtl" / "m_t_top.sv", p.fpga_dir / "stubs" / "m_t_sram_dp.sv"]
    assert "(* black_box *)\nmodule m_t_sram_dp #(" in stub and "parameter int PARA_DATA_W    = 32" in stub and "LPARA_ADDR_W  = $clog2(PARA_DEPTH)" in stub
    assert "output logic [PARA_DATA_W-1:0]                o_rd_data\n);\nendmodule" in stub and "reg_mem" not in stub  # (ports, no body)
    from pyslang import syntax  # (the stub elaborates for both instances: its parameters size its ports)

    assert hdlmem.scan([p.root / "src" / "rtl" / "m_t_top.sv", p.fpga_dir / "stubs" / "m_t_sram_dp.sv"], "m_t_top").error == "" and syntax
    pre, post = memmod.dc_lines(plan, keep_memories=True)
    assert 'set q3tui_bb_m_t_sram_dp [get_cells -hierarchical -filter "(ref_name == m_t_sram_dp || ref_name =~ m_t_sram_dp_*)"]' in pre
    assert any("set_dont_touch $q3tui_bb_m_t_sram_dp; set_boundary_optimization $q3tui_bb_m_t_sram_dp false" in x for x in pre)
    assert any(x.strip().startswith("set_output_delay -clock [get_clocks i_clk] [expr {[get_attribute [get_clocks i_clk] period] * 0.25}]")
               and '-filter "full_name =~ */i_clk_mem"' in x for x in post)  # (the clock pin is no data pin)
    assert any(x.strip().startswith("set_input_delay -clock [get_clocks i_clk] [expr {[get_attribute [get_clocks i_clk] period] * 0.4}]") for x in post)
    s = dc.script_text(plan.files, "m_t_top", ["/lib.db"], tmp_path / "sdc.tcl", tmp_path, tmp_path / "n.v", {}, (pre, post))
    assert s.index("uniquify") < s.index("q3tui_bb_m_t_sram_dp") < s.index('source -echo') < s.index("set_input_delay") < s.index("compile_ultra")
    assert memmod.dc_filter("m_x", ["m_x", "m_x_ctrl"]) == "(ref_name == m_x || ref_name =~ m_x_*) && ref_name !~ m_x_ctrl*"


def test_t3_the_lec_reads_the_black_boxes_as_interfaces_on_both_sides(tmp_path):
    net = "module m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W32_PARA_MASK_GRAN8 ( a );\nendmodule\nmodule m_t_top ( b );\nendmodule\n"
    assert memmod.netlist_modules(net, ["m_t_sram_dp", "m_t_top"], ["m_t_sram_dp"]) == ["m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W32_PARA_MASK_GRAN8"]
    s = fm.script_text([tmp_path / "m_t_top.sv", tmp_path / "stub.sv"], "m_t_top", ["/lib.db"], tmp_path / "n.v", None, tmp_path, {},
                       (["m_t_sram_dp"], ["m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W32_PARA_MASK_GRAN8"]))
    r, i = s.index('set_app_var hdlin_interface_only "m_t_sram_dp"'), s.index('set_app_var hdlin_interface_only "m_t_sram_dp_PARA_DEPTH64')
    assert r < s.index("read_sverilog -container r") < s.index("set_top r:") < i < s.index("read_verilog -container i") < s.index("match")


# -- the steps with fake tools: fpga ------------------------------------------------------------------------------------------------------------


async def test_t1_t4_fpga_synth_reads_the_body_and_the_report_gives_the_memories_apart(tmp_path):
    p = _project(tmp_path, yaml_extra="    fpga_synth:\n      bodies: {m_t_sram_dp: src/fpga/m_t_sram_dp.sv}\n")
    eng = Engine(p)
    await eng.run(yes=True, stop="timing_util_report")
    st = {v.name: v.status for v in eng.status()}
    assert st["fpga_synth"] == "done" and st["timing_util_report"] == "done", eng.status()
    script = (p.fpga_dir / "synth.tcl").read_text()
    assert str(p.root / "src" / "fpga" / "m_t_sram_dp.sv") in script and str(p.root / "src" / "rtl" / "m_t_sram_dp.sv") not in script
    synth = json.loads((p.schemas_dir / "fpga_synth.json").read_text())
    assert synth["memories"]["bodies"] == {"m_t_sram_dp": "src/fpga/m_t_sram_dp.sv"} and synth["memories"]["files"][-1] == "src/fpga/m_t_sram_dp.sv"
    ppa = json.loads((p.schemas_dir / "ppa_report.json").read_text())
    m = ppa["memory"]
    assert m["bits"] == 3072 and (m["lut"], m["lut_ram"], m["ff"]) == (148, 80, 48) and ppa["logic"] == {"lut": 9, "ff": 60}
    assert [(i["instance"], i["mode"], i["bits"], i["reported"]) for i in m["instances"]] == [("u_mem_a", "body", 2048, True), ("gen_b[0].u_mem_b", "body", 1024, True)]
    assert ppa["utilization"]["lut"]["used"] == 157  # (the totals stay as they were)
    # R8: the option goes, the synthesis is stale and reads the RTL again
    p.save_config_values({"pipeline": {"step_options": {"fpga_synth": {"bodies": None}}}})
    eng = Engine(Project.open(p.root))
    assert {v.name: v.status for v in eng.status()}["fpga_synth"] == "stale"


async def test_t3_fpga_black_boxes_cover_the_logic_only_and_say_their_pins_are_not_timed(tmp_path):
    p = _project(tmp_path, yaml_extra="    fpga_synth:\n      blackbox: [m_t_sram_dp]\n")
    json_path = p.root / "vivado.json"
    cfg = json.loads(json_path.read_text())
    cfg["reports"]["utilization_hier.rpt"] = HIER.split("|   u_mem_a")[0] + "+------------------+\n"  # (Vivado lists no black box)
    json_path.write_text(json.dumps(cfg))
    eng = Engine(p)
    await eng.run(yes=True, stop="timing_util_report")
    script = (p.fpga_dir / "synth.tcl").read_text()
    assert str(p.fpga_dir / "stubs" / "m_t_sram_dp.sv") in script and str(p.root / "src" / "rtl" / "m_t_sram_dp.sv") not in script
    assert (p.fpga_dir / "stubs" / "m_t_sram_dp.sv").read_text().splitlines()[1] == "(* black_box *)"
    m = json.loads((p.schemas_dir / "ppa_report.json").read_text())["memory"]
    assert m["black_boxes"]["instances"] == 2 and m["black_boxes"]["timed"] is False and "Constraints 18-472" in m["black_boxes"]["note"]
    assert {i["mode"] for i in m["instances"]} == {"blackbox"} and m["lut"] == 0


async def test_t5_vivado_building_a_big_array_from_registers_is_stopped(tmp_path):
    """ddrc_fe: Vivado dissolved 284 kbit of wrapper arrays into registers and ran out of the machine's memory; it is stopped at the message."""
    import subprocess

    p = _project(tmp_path, depth_a=1024, depth_b=1024)  # (1024 x 32 + 1024 x 16 = 48 kbit; the limit below is 32)
    p.save_config_values({"pipeline": {"step_options": {"fpga_synth": {"max_register_kbit": 32}}}})
    cfg = json.loads((p.root / "vivado.json").read_text())
    (p.root / "vivado.json").write_text(json.dumps({**cfg, "dissolve": True}))
    eng = Engine(Project.open(p.root))
    await eng.run(yes=True, stop="fpga_synth")
    st = {v.name: v for v in eng.status()}["fpga_synth"]
    assert st.status == "failed" and "Vivado would build 48.0 kbit of memory from registers (over max_register_kbit = 32)" in st.detail
    assert "u_mem_a.reg_mem (m_t_sram_dp, 1024 × 32 = 32.0 kbit)" in st.detail and "options.blackbox: m_t_sram_dp" in st.detail
    assert not eng.state.feedback.get("fpga_port")  # (a setting of yours: nothing for fpga_port)
    left = subprocess.run(["pgrep", "-f", str(p.fpga_dir / "synth.tcl")], capture_output=True, text=True).stdout.split()
    assert left == [], "the stopped tool is still running"


def test_t5_the_vivado_guard_counts_the_arrays_the_message_names():
    facts = hdlmem.Facts("top", instances=[hdlmem.Instance("u_a", "m_a", [hdlmem.Array("reg_mem", 1024, 64)]),
                                           hdlmem.Instance("u_b", "m_b", [hdlmem.Array("reg_big", 4096, 64)])])
    plan = memmod.Plan([], facts, modules=["m_a", "m_b"])
    g = memmod.RegisterGuard(facts, set(), 64 * 1024, plan)
    assert g.line("INFO: something") is None
    assert g.line("WARNING: [Synth 8-4767] Trying to implement RAM 'u_a/reg_mem_reg' in registers.") is None  # (64 kbit: not over 64)
    assert g.line("WARNING: [Synth 8-4767] Trying to implement RAM 'reg_other_reg' in registers.") is None  # (none of the design's arrays)
    msg = g.line("WARNING: [Synth 8-4767] Trying to implement RAM ' ' in registers.")  # (no name: every array outside the black boxes)
    assert msg and "320.0 kbit" in msg and msg.startswith("Vivado would build")
    assert memmod.RegisterGuard(facts, {"m_b"}, 64 * 1024, plan).line("[Synth 8-4767] Trying to implement RAM ' ' in registers") is None  # (m_b a black box)
    assert memmod.RegisterGuard(facts, set(), 0, plan).line("[Synth 8-4767] Trying to implement RAM ' ' in registers") is None  # (0: no limit)


async def test_t6_a_tool_without_its_licence_fails_the_step_and_sends_nothing_to_fpga_port(tmp_path):
    p = _project(tmp_path)
    cfg = json.loads((p.root / "vivado.json").read_text())
    (p.root / "vivado.json").write_text(json.dumps({**cfg, "licence": True}))
    eng = Engine(p)
    await eng.run(yes=True, stop="fpga_synth")
    st = {v.name: v for v in eng.status()}["fpga_synth"]
    assert st.status == "failed" and "Vivado got no licence" in st.detail and "Common 17-345" in st.detail
    assert not eng.state.feedback.get("fpga_port") and not (p.schemas_dir / "fpga_synth.json").exists()
    runs = (p.root / "vivado_scripts.txt").read_text().count("=====")
    (p.root / "vivado.json").write_text(json.dumps(cfg))  # (the licence is back: the next run synthesizes again, no --regenerate)
    eng = Engine(Project.open(p.root))
    await eng.run(yes=True, stop="fpga_synth")
    assert {v.name: v.status for v in eng.status()}["fpga_synth"] == "done" and (p.root / "vivado_scripts.txt").read_text().count("=====") == runs + 1
    assert vivado.license_error("ERROR: [Common 17-348] Failed to get the license for feature 'Synthesis'") and not vivado.license_error("ERROR: [Synth 8-439]")


# -- the steps with fake tools: asic ------------------------------------------------------------------------------------------------------------


async def test_t2_t4_asic_synth_and_asic_lec_read_the_same_body_and_the_area_is_given_apart(tmp_path):
    p = _project(tmp_path, flow="asic", yaml_extra="      bodies: {m_t_sram_dp: src/fpga/m_t_sram_dp.sv}\n")
    (p.config_dir).mkdir(exist_ok=True)
    (p.config_dir / "pdks.json").write_text(json.dumps({"pdks": [{"id": "t40", "title": "T40", "node_nm": 40, "globs": [str(p.root / "lib.db")],
                                                                  "sram_um2_per_bit": 0.3, "nand2_um2": 0.6305}]}))
    eng = Engine(p)
    await eng.run(yes=True)
    st = {v.name: v.status for v in eng.status()}
    assert st["asic_synth"] == "done" and st["asic_lec"] == "done", eng.status()
    dc_script, fm_script = (p.root / "dc_script.tcl").read_text(), (p.root / "fm_script.tcl").read_text()
    for s in (dc_script, fm_script):
        assert str(p.root / "src" / "fpga" / "m_t_sram_dp.sv") in s and str(p.root / "src" / "rtl" / "m_t_sram_dp.sv") not in s
    assert "set_ungroup $q3tui_mem false" in dc_script and "hdlin_interface_only" not in fm_script  # (a body: no black box)
    a = json.loads((p.schemas_dir / "asic_synth.json").read_text())
    assert a["area"]["total_um2"] == 19038.084724 and a["memory"]["measured_um2"] == round(12287.7546 + 6405.4365, 3)
    assert a["logic"] == {"area_um2": round(19038.084724 - 12287.7546 - 6405.4365, 3), "nand2_eq": round((19038.084724 - 12287.7546 - 6405.4365) / 0.6305)}
    assert [(i["instance"], i["mode"], i["reported"]) for i in a["memory"]["instances"]] == [("u_mem_a", "body", True), ("gen_b[0].u_mem_b", "body", True)]


async def test_t3_t4_asic_black_boxes_are_kept_timed_estimated_and_black_boxes_in_the_lec(tmp_path):
    extra = "      blackbox: [m_t_sram_dp]\n      blackbox_timing: {'*': {access_pct: 40, setup_pct: 25}}\n"
    p = _project(tmp_path, flow="asic", yaml_extra=extra)
    (p.config_dir).mkdir(exist_ok=True)
    (p.config_dir / "pdks.json").write_text(json.dumps({"pdks": [{"id": "t40", "title": "T40", "node_nm": 40, "globs": [str(p.root / "lib.db")],
                                                                  "sram_um2_per_bit": 0.3}]}))
    (p.root / "area.rpt").write_text(AREA.replace("19038.084724", "344.473277").split("u_mem_a ")[0] + "--------\nTotal\n")
    eng = Engine(p)
    await eng.run(yes=True)
    st = {v.name: v.status for v in eng.status()}
    assert st["asic_synth"] == "done" and st["asic_lec"] == "done", eng.status()
    dc_script, fm_script = (p.root / "dc_script.tcl").read_text(), (p.root / "fm_script.tcl").read_text()
    assert str(p.asic_dir / "stubs" / "m_t_sram_dp.sv") in dc_script and "set_dont_touch $q3tui_bb_m_t_sram_dp" in dc_script and "set_input_delay" in dc_script
    assert str(p.asic_dir / "stubs" / "m_t_sram_dp.sv") in fm_script and 'hdlin_interface_only "m_t_sram_dp"' in fm_script
    assert 'hdlin_interface_only "m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W32_PARA_MASK_GRAN8 m_t_sram_dp_PARA_DEPTH64_PARA_DATA_W16_PARA_MASK_GRAN1"' in fm_script
    m = json.loads((p.schemas_dir / "asic_synth.json").read_text())
    assert m["memory"]["estimated_um2"] == round(3072 * 0.3, 3) and all(i["estimate"] for i in m["memory"]["instances"])
    assert m["memory"]["black_boxes"]["timed"] is True and m["area_with_memory_um2"] == round(344.473277 + 3072 * 0.3, 3) and m["area"]["total_um2"] == 344.473277


async def test_t5_design_compiler_is_not_run_on_a_big_array_of_registers(tmp_path):
    p = _project(tmp_path, flow="asic", depth_a=2048, depth_b=1024)  # (2048 x 32 + 1024 x 16 = 80 kbit, over the default 64)
    eng = Engine(p)
    await eng.run(yes=True)
    st = {v.name: v for v in eng.status()}["asic_synth"]
    assert st.status == "failed" and "ASIC synthesis not run: Design Compiler would build 80.0 kbit of memory from registers (over max_register_kbit = 64)" in st.detail
    assert "options.bodies: {m_t_sram_dp: src/asic/m_t_sram_dp.sv}" in st.detail and not (p.root / "dc_script.tcl").exists()


# -- the project's memory modules ---------------------------------------------------------------------------------------------------------------


def test_project_memory_modules_are_part_of_the_staleness_only_when_set(tmp_path):
    p = _project(tmp_path)
    eng = Engine(p)
    synth, sdc = eng.by_name["fpga_synth"], eng.by_name["sdc"]
    assert synth.extra_hash(eng) is None and sdc.extra_hash(eng) is None  # (R8: nothing added to any hash)
    before = eng.inputs_hash(synth)
    p.save_config_values({"project": {"memory_modules": ["m_t_*"]}})
    eng = Engine(Project.open(p.root))
    assert eng.by_name["fpga_synth"].extra_hash(eng) == {"memory_modules": ["m_t_*"]} and eng.inputs_hash(eng.by_name["fpga_synth"]) != before
    assert eng.by_name["sdc"].extra_hash(eng) is None  # (a step that does not read them)
    plan = memmod.make_plan(eng.project, {}, sorted((p.root / "src" / "rtl").glob("*.sv")), "m_t_top", p.fpga_dir / "stubs", None)
    assert plan.modules == ["m_t_sram_dp"] and not plan.proposed


def test_the_sanity_analysis_black_boxes_the_projects_memory_modules(tmp_path):
    from q3tui import flows

    p = _project(tmp_path, flow="sanity")
    eng = Engine(p, flow=flows.load_flow("sanity", p.root))
    cdc = eng.by_name["cdc"]
    assert cdc.memory_blackboxes(eng) == []
    p.save_config_values({"project": {"memory_modules": ["m_t_*"]}})  # (a pattern that the top matches too: never the top)
    eng = Engine(Project.open(p.root), flow=flows.load_flow("sanity", p.root))
    assert eng.by_name["cdc"].memory_blackboxes(eng) == ["m_t_sram_dp"] and eng.by_name["cdc"].extra_hash(eng) == {"memory_modules": ["m_t_*"]}
    (p.root / "constraints" / "sdc").mkdir(parents=True, exist_ok=True)
    (p.root / "constraints" / "sdc" / "sdc.tcl").write_text("create_clock -name c -period 10 [get_ports i_clk]\n")
    text = eng.by_name["cdc"].script_text(eng, "m_t_top", tmp_path / "w")
    assert "set_blackbox -designs {m_t_sram_dp}\nanalyze" in text


def test_the_memory_modules_proposed_follow_asic_ports_min_mem_words(tmp_path):
    """R1: the proposal is asic_port's `memory_array` rule — an unpacked array of `min_mem_words` words or more (16 unless set), in every flow."""
    p = _project(tmp_path, flow="asic")
    rtl = sorted((p.root / "src" / "rtl").glob("*.sv"))
    eng = Engine(p)
    assert eng.by_name["asic_synth"].min_mem_words(eng) == 16 and eng.by_name["asic_synth"].extra_hash(eng) is None
    p.save_config_values({"pipeline": {"step_options": {"asic_port": {"min_mem_words": 128}}}})
    for flow in ("asic", "fpga"):  # (the asic flow reads its asic_port step, the fpga flow the project's setting)
        p.save_config_values({"pipeline": {"flow": flow}})
        eng = Engine(Project.open(p.root))
        step = eng.by_name["asic_synth" if flow == "asic" else "fpga_synth"]
        assert step.min_mem_words(eng) == 128 and step.extra_hash(eng) == {"min_mem_words": 128}
    assert eng.by_name["sdc"].extra_hash(eng) is None
    plan = memmod.make_plan(eng.project, {}, rtl, "m_t_top", p.fpga_dir / "stubs", None, min_words=128)
    assert plan.modules == [] and plan.summary()["instances"] == []  # (the fixture's arrays have 64 words)
    with pytest.raises(memmod.PlanError, match="128 words or more"):
        memmod.make_plan(eng.project, {"blackbox": True}, rtl, "m_t_top", p.fpga_dir / "stubs", None, min_words=128)


BANNER = """/*
 * Project      : t — bộ nhớ (© 2026; the module m_t_sram_dp is named here too)
 * Description  : a behavioural memory wrapper
 */
`timescale 1ns / 1ps
`default_nettype none

"""


def test_t3_the_stub_is_the_header_as_written_after_a_banner_and_directives(tmp_path):
    """FX1B's files open with a banner comment, `timescale and `default_nettype (pyslang keeps the banner inside the directives' trivia,
    whose raw text is empty): the stub is the header from `module` on, cut by its byte offsets (the banner has non-ASCII text)."""
    p = _project(tmp_path)
    (p.root / "src" / "rtl" / "m_t_sram_dp.sv").write_text(BANNER + MEM.replace("  input  logic                                  i_wr_en,",
                                                                                "  input  logic                                  i_wr_en,  // ghi (write)\n")
                                                           + "`default_nettype wire\n")
    rtl = sorted((p.root / "src" / "rtl").glob("*.sv"))
    plan = memmod.make_plan(p, {"blackbox": ["m_t_sram_dp"]}, rtl, "m_t_top", p.fpga_dir / "stubs", memmod.VIVADO_BLACK_BOX)
    stub = plan.stubs["m_t_sram_dp"].read_text()
    lines = stub.splitlines()
    assert lines[1] == "(* black_box *)" and lines[2] == "module m_t_sram_dp #(" and lines[-1] == "endmodule"
    assert "Project" not in stub and "`timescale" not in stub and "// ghi (write)\n\n" in stub  # (the header's own comment and blank line kept)
    # a stub that does not stand for its module is never given to the tool (it would fail the synthesis as if the design had)
    facts = _facts(p.root)
    facts.headers["m_t_sram_dp"] = "module m_t_sram_dp #(parameter int PARA_DEPTH = 64) (input logic i_clk_mem"
    with pytest.raises(memmod.PlanError, match="do not stand for their modules .*not of your design"):
        memmod.make_plan(p, {"blackbox": ["m_t_sram_dp"]}, rtl, "m_t_top", p.fpga_dir / "stubs", memmod.VIVADO_BLACK_BOX, facts=facts)
