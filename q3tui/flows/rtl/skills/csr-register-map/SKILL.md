---
name: csr-register-map
description: Read the registers of a spec (bus, offsets, fields, access types, resets) into the structured map the CSR generator takes, without inventing anything.
---

# From the spec to a register map

The registers are generated, so the map must say exactly what the spec says. A wrong field type or offset becomes wrong RTL that no one wrote.

## Where to look
The spec's register / memory-map / CSR / SFR sections (tables of offset, name, bits, access, reset), the interface section (the bus: APB4, AXI4-Lite or
AHB-Lite, its options, the address width), the clocking section (is there a register clock of its own, asynchronous to the bus clock?), and the requirements that mention registers
(`req_ids`). Take names from the spec as written, lowercased where the table is in capitals; keep a spec's field names.

## The map
- `module_name`: the module of the design's module map that is the register block, if one is (use its exact name); else `<top>_csr`.
- `parent`: the module that instantiates it: the one the module map says, else the top.
- `protocol`: the register block's bus as the spec's interface section gives it: `APB4`, `AXI4-Lite` or `AHB-Lite` (the prompt says when the project
  fixes it, and which buses the installed generator makes). Its options, from the spec, else the defaults: AXI4-Lite / AHB-Lite `strobe_mode`
  (`HONOR`: a narrow write updates only its bytes; `REJECT`: it answers SLVERR / ERROR and changes nothing); AXI4-Lite `outstanding` (`SINGLE`,
  `READ_WRITE`, `QUEUED` with `rd_depth` / `wr_depth` 1..8); AHB-Lite `ahb_endian` (`LITTLE`, `BE8`, `BE32`). The error codes and latencies the
  tool gives are fixed (skill `csr-tool`): if the spec requires others, ask.
- `addr_width`: the address bits the bus carries to the block (the spec's address width; 16 when it says nothing). `data_width` is 32 (the tool's only width).
- `asynchronous`: true only when the spec says the registers are clocked by a clock asynchronous to the bus clock (the tool then adds a handshake bridge).
- `event_sideband`: true only when the spec asks for the generator's APB4 "Register Event Sideband", or states that reading or writing a
  register itself has an effect the field types cannot give (a read pops a FIFO, a write pushes into one, a read latches another register):
  the block then reports each access (skill `csr-tool`). APB4 only; false otherwise.
- Each register: `name`, `offset` (bytes, a multiple of 4, relative to the block's base), the fields with `msb`/`lsb`, `type`, `reset`, a one-line description. List
  the fields the spec gives; bits it leaves out are filled as `reserved` by code, do not list them.
- A register wider than 32 bits is two registers (low word first); say so in `summary`.

## Field types: pick by what the *hardware* does, not only the software
| Spec says | type |
|---|---|
| software reads and writes; the value only configures hardware | `rw` (hardware sees it on an output port) |
| read-only status / counter / version, driven by hardware | `ro` (hardware drives an input port) |
| software writes it, hardware may also load or update it (a start bit that hardware clears, a value hardware overwrites) | `rwi` |
| a status/interrupt flag that hardware sets and software clears by writing 1 | `w1c` |
| a flag software clears by writing 0 / by any write to its register (generator 2.2+) | `w0c` / `w2c` |
| a status or counter a read returns and clears (generator 2.2+) | `r2c` |
| a write-only value (a key, a command word) that reads as 0, hardware may clear (generator 3.1+) | `wo` |
| reserved / unused bits | not listed |
Do not use `w1c` for a plain status bit software never clears, nor `rw` for a bit the hardware must change. A *self-clearing pulse* (a start bit) is
`rwi` with hardware clearing the bit, or `wo` with its hardware clear; say which in `summary`. A type newer than the installed generator fails
`csr_check`: use the closest older one and say so, or ask.

## Questions, not guesses
Ask (kind `spec_gap`, with a default) when: two registers share an offset, a field's access type is missing or contradicts the behaviour described, a reset value
is not given, the register clock relationship is unclear, the bus or a bus option is not given (or is not the one the project fixes). A reasonable default is fine; an invented register is not.

## Check it
Call `csr_check` with your map: it lists what is wrong (overlap, duplicate offset, misaligned, a reset too wide) and, when the map is sound, the ports the
generated module will have. Fix problems and check again before you answer.
