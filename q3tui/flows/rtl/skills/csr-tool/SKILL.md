---
name: csr-tool
description: What the CSR generator (CSR-Generator, formerly APB-CSR-Generator) makes — an APB4, AXI4-Lite or AHB-Lite register block, field types, ports, models — its limits, and how the generated module connects to the rest of the design.
---

# The CSR generator

Python tool (https://github.com/nguyenquanicd/CSR-Generator; named APB-CSR-Generator before version 3.0). Input: an Excel workbook (a configuration
block and register rows; Q3TUI writes it from `src/rtl/csr/csr_map.json`). Output: one SystemVerilog register block on one bus, plus the tool's own
models. The csr step records the version it ran (`csr_report.json` `generator`) and the bus (`protocol`).

| Version | Buses | Models it writes next to the block |
|---|---|---|
| 1.x | APB4 | `m_vlsi_synch` (asynchronous only) |
| 2.x | APB4 | `m_csr_sync` (instantiated when asynchronous; from 2.2 inside the `PARA_ASYNCHRONOUS` generate branch) |
| 3.x | APB4, AXI4-Lite, AHB-Lite (the map's `protocol`) | `m_csr_sync`; AXI4-Lite / AHB-Lite also `m_csr_sync_fifo` and `m_csr_async_fifo` |

From 2.2 one RTL module holds both clock architectures, chosen by its parameter `PARA_ASYNCHRONOUS` (the workbook's value is the default): the models
of the other architecture are in the file list but not elaborated.

## What the module does
- **APB4 completer:** SETUP / ACCESS, PREADY the cycle after (one wait state), PSLVERR when `i_slverr_en` = 1 on a misaligned address
  (`PADDR[1:0] != 0`), on a partial `PSTRB` write and on `PPROT[1]` with `i_protect_en` = 1. With `i_slverr_en` = 0 (3.x) `PSTRB` selects the bytes
  that update. An aligned address that is no register completes without error: reads 0, a write is ignored.
- **AXI4-Lite subordinate (3.x):** the five channels, no ID or USER signals; AW and W are captured independently and paired once; B and R hold until
  their handshake. Responses: OKAY; **DECERR for an unmapped address**; SLVERR for a misaligned address, and for a write whose `WSTRB` is not all ones
  when `strobe_mode` is REJECT (HONOR updates only the strobed bytes; a write with no strobe changes nothing and answers OKAY). Error reads return 0.
  `AWPROT` / `ARPROT` are ports but are not evaluated. Requests accepted before their response: one in all (`outstanding` SINGLE), one per direction
  (READ_WRITE) or `rd_depth` / `wr_depth` (QUEUED, 1..8); one register engine serves both directions round-robin (read first after reset), each
  direction answers in order. The request / response queues add latency: a response is never in the cycle after the request.
- **AHB-Lite subordinate (3.x):** NONSEQ / SEQ beats with `HSEL` and `HREADY` high; every burst beat is a separate access; byte / halfword / word
  (`HSIZE` 0..2, naturally aligned); subword writes per `strobe_mode` (REJECT answers the two-cycle ERROR); `ahb_endian` maps the byte lanes.
  No `HPROT`.
- **Fields** (per field of the map): `rw` (software write, reads back), `ro` (read passes a hardware input straight through: no flop), `rwi` (software
  write wins over a same-cycle hardware write), `w1c` (writing 1 clears; a hardware load wins), from 2.2 `w0c` (writing 0 clears), `w2c` (any write to
  the register clears the field), `r2c` (a read returns the value and clears it), from 3.1 `wo` (write-only: stored, seen by hardware, reads 0).
  `reserved` reads 0, writes ignored, no ports.
- **Asynchronous = 1:** registers flop on the register clock; APB4 crosses with a request / acknowledge bridge, AXI4-Lite / AHB-Lite with the
  dual-clock queues. **Asynchronous = 0:** one clock; tie the bus clock and the register clock ports to the same clock (and the resets to the same
  reset).

## Ports of the generated module
The csr step lists the real ports (`csr_report.json` `ports`, `q3tui act csr_show`): use those names, never these tables' guesses.

| | APB4 (2.x / 3.x) | AXI4-Lite (3.x) | AHB-Lite (3.x) | APB4 (1.x) |
|---|---|---|---|---|
| bus clock / reset | `i_clk_apb i_rst_n_apb` | `i_clk_axi i_rst_n_axi` | `i_clk_ahb i_rst_n_ahb` | `i_bus_clk i_bus_rstn` |
| register clock / reset | `i_clk_reg i_rst_n_reg` | same | same | `i_reg_clk i_reg_rstn` |
| bus inputs | `i_apb_psel penable pwrite paddr pwdata pstrb pprot` | `i_axi_awvalid awaddr awprot wvalid wdata wstrb bready arvalid araddr arprot rready` | `i_ahb_hsel hready htrans haddr hwrite hsize hburst hwdata` | `i_psel …` |
| bus outputs | `o_apb_pready pslverr prdata` | `o_axi_awready wready bvalid bresp arready rvalid rdata rresp` | `o_ahb_hreadyout hresp hrdata` | `o_pready …` |
| access checks | `i_protect_en i_slverr_en` | — | — | same as 2.x |

(`i_apb_psel penable …` stands for `i_apb_psel i_apb_penable …`.) Per field, named `<reg>_<field>`: `ro` -> `i_<reg>_<field>` (hardware drives it);
`rw`, `wo` -> `o_<reg>_<field>`; `rwi`, `w1c`, `w0c`, `w2c`, `r2c` -> `o_<reg>_<field>` plus `i_hw_wdata_<reg>_<field>` and `i_hw_we_<reg>_<field>`
(one enable bit for the whole field); `wo` also `i_hw_clr_<reg>_<field>` (3.2: clears the field, wins over a software write).

## Connecting it (the design's side)
- Bus ports connect to the design's own bus ports of that protocol (the top, or the module the spec names); the responses go back to the bus. An
  integrator's port of another name connects by name in the instance: the generated names do not change.
- APB4: `i_protect_en` / `i_slverr_en` come from the design's ports or are tied as the spec says (constant after reset).
- Clocks and resets: the bus clock and reset ports to the bus clock and reset; the register clock and reset ports to the register clock domain (the
  same clock and reset when Asynchronous = 0).
- Per-field ports go to the logic that produces (`ro`, hardware loads, `wo` clear) or consumes (the outputs) the value. A hardware input port left tied
  to 0 is a decision the spec must support; say so in a comment.

## Limits you must respect
- 32-bit data (Q3TUI's maps; AXI4-Lite and AHB-Lite only have 32). One bus per register block.
- AXI4-Lite: no protection filtering (`AxPROT` ignored), no ID; the error codes above are fixed by the tool (an unmapped address is DECERR, not
  SLVERR or OKAY). APB4: an unmapped aligned address is not an error.
- The tool does **not** validate the map (duplicate offsets, overlapping bits, bad widths give broken RTL silently): Q3TUI checks before it runs.
- The synchronizer and the queues are the tool's generic behavioural models: `m_csr_sync` is a placeholder for a verified library synchronizer
  (tell the integrator when the design is asynchronous); the queues are small flip-flop FIFOs.
- The generated files are never edited by hand or rewritten by an LLM: change the register map (or the spec) and generate again.
