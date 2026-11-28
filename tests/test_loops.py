"""Combinational loops found statically (hdl/loops.py) and reported by the RTL check."""

from pathlib import Path

from q3tui.hdl.filelist import filelist_from_files
from q3tui.hdl.parse import parse_design

CHILD = """
module busy_unit(input logic i_valid, input logic i_stall, output logic o_req, output logic o_busy);
  assign o_req = i_valid && !i_stall;
  assign o_busy = o_req;
endmodule
"""


def _loops(tmp_path: Path, top_src: str, top: str = "top") -> list[dict]:
    (tmp_path / "busy_unit.sv").write_text(CHILD)
    (tmp_path / "top.sv").write_text(top_src)
    parsed = parse_design(filelist_from_files([tmp_path / "busy_unit.sv", tmp_path / "top.sv"]), top)
    assert parsed.ok, parsed.diagnostics
    return parsed.comb_loops


def test_a_loop_through_an_instance_and_the_parent_is_found(tmp_path):
    loops = _loops(tmp_path, """
module top(input logic clk, input logic v, output logic req);
  logic busy, stall;
  assign stall = busy;
  busy_unit u(.i_valid(v), .i_stall(stall), .o_req(req), .o_busy(busy));
endmodule
""")
    assert len(loops) == 1
    sig = loops[0]["signals"]
    assert {"top.stall", "top.busy", "top.u.o_req"} <= set(sig)
    assert any(f.endswith("busy_unit.sv") for f, _ in loops[0]["where"])


def test_registers_loop_variables_and_bits_of_one_vector_are_not_loops(tmp_path):
    loops = _loops(tmp_path, """
module top(input logic clk, input logic v, input logic [3:0] g, output logic req, output logic [3:0] b);
  logic busy, busy_q;
  always_ff @(posedge clk) busy_q <= busy;
  busy_unit u(.i_valid(v), .i_stall(busy_q), .o_req(req), .o_busy(busy));
  integer i;
  always_comb begin
    b[3] = g[3];
    for (i = 2; i >= 0; i = i - 1) b[i] = g[i] ^ b[i + 1];
  end
endmodule
""")
    assert loops == []


# -- FR-04 R9: what an assignment writes is its left side's variables, not the expressions that select where -----------------------------------

FR04_WR_CTX = """
module top(input logic clk, input logic rst_n, input logic i_aw_rd_valid, input logic i_pch_pop_stop, output logic o_ready);
  logic [3:0] reg_ctx_valid, w_ctx_valid_nxt;
  logic [1:0] reg_ctx_alloc_ptr, w_ctx_alloc_ptr_nxt;
  logic w_aw_ready, w_aw_take;
  always_comb begin
    w_aw_ready = ~reg_ctx_valid[reg_ctx_alloc_ptr] & ~i_pch_pop_stop;
    w_aw_take  = i_aw_rd_valid & w_aw_ready;
  end
  always_comb begin
    w_ctx_valid_nxt = reg_ctx_valid;
    w_ctx_alloc_ptr_nxt = reg_ctx_alloc_ptr;
    if (w_aw_take) begin
      w_ctx_valid_nxt[reg_ctx_alloc_ptr] = 1'b1;
      w_ctx_alloc_ptr_nxt = reg_ctx_alloc_ptr + 2'd1;
    end
  end
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin reg_ctx_alloc_ptr <= '0; reg_ctx_valid <= '0; end
    else begin reg_ctx_alloc_ptr <= w_ctx_alloc_ptr_nxt; reg_ctx_valid <= w_ctx_valid_nxt; end
  assign o_ready = w_aw_ready;
endmodule
"""


def test_a_register_used_as_the_index_of_a_write_closes_no_loop(tmp_path):
    """FX1B ddrc_fe (FR-04 §2.4): `w_ctx_valid_nxt[reg_ctx_alloc_ptr] = 1'b1` under `if (w_aw_take)` made reg_ctx_alloc_ptr a written signal,
    which closed `w_aw_take → reg_ctx_alloc_ptr → w_aw_ready → w_aw_take`; the pointer is a register."""
    assert _loops(tmp_path, FR04_WR_CTX) == []


def test_an_index_computed_from_the_vector_it_writes_into_is_still_a_loop(tmp_path):
    loops = _loops(tmp_path, """
module top(input logic [3:0] a, input logic x, output logic [3:0] o);
  logic [3:0] v;
  logic [1:0] idx;
  always_comb idx = v[1:0];
  always_comb begin v = a; v[idx] = x; end
  assign o = v;
endmodule
""")
    assert len(loops) == 1 and set(loops[0]["signals"]) == {"top.v", "top.idx"}


def test_selectors_inside_a_member_access_and_a_concatenation_are_sources_not_targets(tmp_path):
    assert _loops(tmp_path, """
module top(input logic clk, input logic t, output logic [7:0] o);
  typedef struct packed { logic [3:0] v; logic [3:0] w; } s_t;
  s_t s, s_nxt;
  logic [1:0] reg_p;
  logic [3:0] u, u_nxt;
  logic g;
  always_comb g = s.v[reg_p] | u[reg_p] | t;
  always_comb begin
    s_nxt = s; u_nxt = u;
    if (g) begin s_nxt.v[reg_p] = 1'b1; {u_nxt[reg_p], s_nxt.w[reg_p +: 1]} = 2'b11; end
  end
  always_ff @(posedge clk) begin s <= s_nxt; u <= u_nxt; reg_p <= reg_p + 2'd1; end
  assign o = {s.v, u};
endmodule
""") == []
