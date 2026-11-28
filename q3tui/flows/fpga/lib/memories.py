"""Memory modules in the fpga and asic flows (docs/spec/fpga-flow.md "Memories"; FX1B's FR-04).

A design whose memories are wrapper modules keeps only a behavioural body of each in `src/rtl/` (the reference of rtl, dv and sanity); the
real bodies depend on the target. In `fpga_synth` and `asic_synth` (and `asic_lec`, which takes asic_synth's) a memory module can be:

- the RTL's own body, as before (nothing set);
- a **target body**: `options.bodies: {m_x_sram_dp: src/fpga/m_x_sram_dp.sv}` — that flow reads the file instead of the RTL file that
  defines the module (`src/rtl/` is not changed);
- a **black box**: `options.blackbox: [m_x_sram_dp]` (names or patterns; `true`: every memory module) — a port-only stub of the module
  (its header as written) is read instead, so synthesis, timing and utilization cover the logic only.

The memory modules are `project.memory_modules` (names or patterns), else the modules holding an unpacked array of 16 words or more (the
`memory_array` rule of asic_port: what Q3TUI proposes). They are what the reports give apart from the logic. Before a tool builds more
than `options.max_register_kbit` (default 64) kbit of arrays from registers, the step stops and says which arrays and what to set:
Design Compiler always builds an array from flip-flops (checked before it runs), Vivado says so when it cannot map one to RAM
(`Synth 8-4767`, watched while it runs)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from q3tui.hdl import memories as hdlmem

MIN_WORDS = 16  # (asic_port's `min_mem_words` default: an array this deep is a memory)
DEFAULT_MAX_REGISTER_KBIT = 64
VIVADO_BLACK_BOX = "(* black_box *)"
YOSYS_BLACK_BOX = "(* blackbox *)"


class PlanError(Exception):
    """A memory setting that cannot be applied (said before the tool runs)."""


def option_bodies(raw) -> dict[str, str]:
    """`options.bodies` as {module: file}: a mapping, or `module=file` lines (the Settings dialog), or one string of them."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}
    items = re.split(r"[\s,]+", raw) if isinstance(raw, str) else [str(x) for x in raw]
    out = {}
    for it in items:
        k, sep, v = str(it).partition("=")
        if not sep or not k.strip() or not v.strip():
            raise PlanError(f"bodies: '{it}' is not `module=file`")
        out[k.strip()] = v.strip()
    return out


def option_blackbox(raw) -> list[str] | bool:
    """`options.blackbox`: `true` (every memory module), or names / patterns (a list, or one string of them)."""
    every = ("true", "yes", "all", "memory_modules")
    if raw is True or (isinstance(raw, str) and raw.strip().lower() in every):
        return True
    if not raw:
        return []
    items = [x for x in (re.split(r"[\s,]+", raw) if isinstance(raw, str) else [str(x) for x in raw]) if x.strip()]
    return True if [x.lower() for x in items] in ([w] for w in every) else items  # (the Settings dialog's one line `true`)


def max_register_bits(options: dict) -> int:
    """`options.max_register_kbit` (1 kbit = 1024 bits) as bits; 0: no limit."""
    raw = (options or {}).get("max_register_kbit")
    try:
        kbit = float(DEFAULT_MAX_REGISTER_KBIT if raw in (None, "") else raw)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"max_register_kbit: '{raw}' is not a number") from exc
    return int(kbit * 1024)


@dataclass
class Plan:
    files: list[Path]  # what the tool reads: the RTL files, the mapped modules' files swapped for their bodies / stubs
    facts: hdlmem.Facts | None = None  # the reference RTL's (src/rtl)
    modules: list[str] = field(default_factory=list)  # the memory modules: the project's, else the ones proposed
    proposed: bool = True  # `modules` were found by Q3TUI (project.memory_modules not set)
    bodies: dict[str, Path] = field(default_factory=dict)
    blackbox: list[str] = field(default_factory=list)
    stubs: dict[str, Path] = field(default_factory=dict)
    timing: dict[str, dict] = field(default_factory=dict)  # black box → {clock, access_pct, setup_pct} (options.blackbox_timing)
    notes: list[str] = field(default_factory=list)
    min_words: int = MIN_WORDS  # an unpacked array this deep is a memory (asic_port's `min_mem_words`)

    @property
    def active(self) -> bool:
        return bool(self.bodies or self.blackbox)

    def mode(self, module: str) -> str:
        return "blackbox" if module in self.blackbox else ("body" if module in self.bodies else "rtl")

    def instances(self) -> list[hdlmem.Instance]:
        """The instances of the memory modules (not those nested in another memory instance)."""
        if self.facts is None:
            return []
        mem = [i for i in self.facts.instances if i.path and i.module in self.modules]
        return [i for i in mem if not any(i.path.startswith(o.path + "/") for o in mem if o is not i)]

    def summary(self, project=None) -> dict:
        """What `schemas/<step>.json` records (`memories`): the memory modules and how each was synthesized, their instances and bits."""
        rel = (lambda p: project.rel(p)) if project is not None else str
        return {"modules": self.modules, "proposed": self.proposed, "bodies": {m: rel(p) for m, p in self.bodies.items()},
                "blackbox": self.blackbox, "stubs": {m: rel(p) for m, p in self.stubs.items()},
                "instances": [{"instance": i.path, "module": i.module, "mode": self.mode(i.module), "bits": i.bits(self.min_words),
                               "arrays": [{"name": a.name, "words": a.words, "width": a.width} for a in i.arrays if a.words >= self.min_words]}
                              for i in self.instances()],
                **({"notes": self.notes} if self.notes else {})}


def _path(project, raw: str) -> Path:
    p = Path(os.path.expandvars(raw)).expanduser()
    return p if p.is_absolute() else project.root / p


def _defined(path: Path) -> list[str]:
    """The modules a file defines (its syntax only)."""
    from pyslang import syntax

    try:
        tree = syntax.SyntaxTree.fromFile(str(path))
    except Exception:  # noqa: BLE001
        return []
    return [m.header.name.valueText for m in tree.root.members if getattr(getattr(m, "header", None), "name", None) is not None]


def make_plan(project, options: dict, rtl_files: list[Path], top: str, stub_dir: Path, attribute: str | None,
              facts: hdlmem.Facts | None = None, min_words: int = MIN_WORDS) -> Plan:
    """The memory plan of a synthesis step (`options`: the step's). With no body and no black box the files are the RTL's as they are
    (the script does not change); the facts are still read (the memory modules for the reports, the register limit) when given or
    readable. Raises PlanError for a setting that cannot be applied."""
    bodies_raw, bb_raw = option_bodies((options or {}).get("bodies")), option_blackbox((options or {}).get("blackbox"))
    if facts is None:
        facts = hdlmem.scan(rtl_files, top)
    plan = Plan(list(rtl_files), facts, min_words=min_words)
    named = list(getattr(getattr(project.cfg, "project", None), "memory_modules", None) or [])
    if facts.error:
        if bodies_raw or bb_raw:
            raise PlanError(f"memory bodies / black boxes need the design read by Q3TUI: {facts.error}")
        plan.facts = None
        return plan
    plan.modules = [m for m in facts.match(named) if m != top] if named else facts.memory_modules(min_words)  # (never the top itself)
    plan.proposed = not named
    if named:
        unknown = [n for n in named if not facts.match([n])]
        if unknown:
            plan.notes.append(f"project.memory_modules: {', '.join(unknown)} match no module of the RTL")
    known = set(facts.modules)
    hint = f" (modules: {', '.join(facts.modules[:12])}{', …' if len(facts.modules) > 12 else ''})"
    for m in bodies_raw:
        if m not in known:
            raise PlanError(f"bodies: `{m}` is not a module of the RTL{hint}")
    if bb_raw is True:
        plan.blackbox = list(plan.modules)
        if not plan.blackbox:
            raise PlanError("blackbox: true, but the design has no memory module (project.memory_modules, or an unpacked array of "
                            f"{min_words} words or more)")
    else:
        for n in bb_raw:
            if not facts.match([n]):
                raise PlanError(f"blackbox: `{n}` is not a module of the RTL{hint}")
        plan.blackbox = facts.match(bb_raw)
    both = sorted(set(plan.blackbox) & set(bodies_raw))
    if both:
        raise PlanError(f"{', '.join(both)}: a body and a black box at once (options.bodies, options.blackbox): keep one")
    if top in plan.blackbox or top in bodies_raw:
        raise PlanError(f"the top `{top}` cannot be a black box or get another body")
    covered = set(plan.blackbox) | set(bodies_raw)
    rtl = {str(Path(f).resolve()): Path(f) for f in rtl_files}
    drop: set[str] = set()
    for m in sorted(covered):
        f = str(Path(facts.files.get(m) or "").resolve())
        if f not in rtl:
            raise PlanError(f"`{m}` is not defined in a file of the RTL folder (defined in {facts.files.get(m) or '?'})")
        others = sorted(set(facts.file_modules.get(f) or []) - covered)
        if others:
            raise PlanError(f"{rtl[f].name} defines {', '.join(others)} besides `{m}`: it cannot be left out for `{m}`'s "
                            f"{'black box' if m in plan.blackbox else 'body'} (one module per file)")
        drop.add(f)
    for m, raw in bodies_raw.items():
        p = _path(project, raw)
        if not p.is_file():
            raise PlanError(f"bodies: the body of `{m}`, {raw}, is not there")
        defs = _defined(p)
        if m not in defs:
            raise PlanError(f"bodies: {raw} does not define `{m}`" + (f" (it defines {', '.join(defs)})" if defs else ""))
        clash = sorted((set(defs) - {m} - covered) & known)
        if clash:
            raise PlanError(f"bodies: {raw} also defines {', '.join(clash)}, a module of the RTL (two definitions)")
        plan.bodies[m] = p
    for old in sorted(stub_dir.glob("*.sv")) if stub_dir.is_dir() else []:  # (stubs of an earlier run: only this run's black boxes are read)
        if old.stem not in plan.blackbox:
            old.unlink()
    if plan.blackbox:
        stub_dir.mkdir(parents=True, exist_ok=True)
        for m in plan.blackbox:
            head = facts.headers.get(m)
            if not head:
                raise PlanError(f"blackbox: no header of `{m}` could be read for its stub")
            stub = stub_dir / f"{m}.sv"
            stub.write_text(f"// generated by Q3TUI: the black box of {m} (its ports, no body); {project.rel(Path(facts.files[m]))} is not read\n"
                            + (f"{attribute}\n" if attribute else "") + head + "\nendmodule\n")
            plan.stubs[m] = stub
        # the stubs must stand for their modules: the RTL with them, and not the files they replace, elaborates (else nothing is run: a stub
        # the tool cannot read would fail the synthesis and look like the design's problem)
        gone = {str(Path(facts.files[m]).resolve()) for m in plan.blackbox}
        bad = hdlmem.scan([f for f in rtl_files if str(Path(f).resolve()) not in gone] + list(plan.stubs.values()), top).error
        if bad:
            raise PlanError(f"the black-box stub(s) {', '.join(project.rel(s) for s in plan.stubs.values())} do not stand for their modules "
                            f"({bad}): nothing was run — a problem of Q3TUI's stubs, not of your design (send the stub and its RTL file)")
        tm = (options or {}).get("blackbox_timing") or {}
        if not isinstance(tm, dict):
            raise PlanError("blackbox_timing: a mapping {module or '*': {clock, access_pct, setup_pct}}")
        for m in plan.blackbox:
            entry = tm.get(m) or tm.get("*")
            if entry:
                if not isinstance(entry, dict) or "access_pct" not in entry or "setup_pct" not in entry:
                    raise PlanError(f"blackbox_timing.{m if m in tm else '*'}: give access_pct and setup_pct (and the clock)")
                plan.timing[m] = {"clock": str(entry.get("clock") or ""), "access_pct": float(entry["access_pct"]), "setup_pct": float(entry["setup_pct"])}
    plan.files = [f for f in rtl_files if str(Path(f).resolve()) not in drop] + list(plan.bodies.values()) + list(plan.stubs.values())
    return plan


# -- the register limit (R6) -------------------------------------------------------------------------------------------------------


def register_arrays(facts: hdlmem.Facts | None, covered: set[str], min_words: int = MIN_WORDS, name: str = "") -> list[tuple[hdlmem.Instance, hdlmem.Array]]:
    """The arrays of `facts` (the design as the tool reads it) outside the black boxes, largest first; `name`: only the arrays called so."""
    if facts is None:
        return []
    out = [(i, a) for i in facts.instances if i.module not in covered for a in i.arrays if a.words >= min_words and (not name or a.name == name)]
    return sorted(out, key=lambda x: -x[1].bits)


def over_limit(arrays: list[tuple[hdlmem.Instance, hdlmem.Array]], limit_bits: int, tool: str, plan: Plan, flow: str) -> str | None:
    """The message of the stop when the arrays a tool builds from registers hold more than `limit_bits` (0: no limit); None below it."""
    total = sum(a.bits for _, a in arrays)
    if not limit_bits or total <= limit_bits:
        return None
    shown = ", ".join(f"{(i.path or '(top)')}.{a.name} ({i.module}, {a.words} × {a.width} = {a.bits / 1024:.1f} kbit)" for i, a in arrays[:6])
    more = f" and {len(arrays) - 6} more" if len(arrays) > 6 else ""
    mods = sorted({i.module for i, _ in arrays})
    mm = plan.modules or mods
    body_dir = "src/fpga" if flow == "fpga" else "src/asic"
    return (f"{tool} would build {total / 1024:.1f} kbit of memory from registers (over max_register_kbit = {limit_bits / 1024:g}): {shown}{more}. "
            f"That takes hours and tens of GB, and its result is no memory. Give the memory modules a target body "
            f"(options.bodies: {{{mods[0]}: {body_dir}/{mods[0]}.sv}}) or make them black boxes (options.blackbox: {' '.join(mods)}); "
            f"project.memory_modules names them (" + ("Q3TUI proposes " if plan.proposed else "now ") + f"{', '.join(mm) or 'none'}). "
            "To synthesize them from registers all the same, raise max_register_kbit (0: no limit).")


_RAM_IN_REGISTERS = re.compile(r"\[Synth 8-4767\] Trying to implement RAM '([^']*)' in registers")


class RegisterGuard:
    """Watches Vivado's log (`fpga_synth`): when it says it builds a RAM from registers (`Synth 8-4767`) and the arrays it may mean are over
    the limit, `reason` is the message to stop with. A RAM it names counts with the arrays of that name; one it does not name, with all."""

    def __init__(self, facts: hdlmem.Facts | None, covered: set[str], limit_bits: int, plan: Plan):
        self.facts, self.covered, self.limit, self.plan = facts, covered, limit_bits, plan
        self.reason: str | None = None

    def line(self, text: str) -> str | None:
        if self.reason or not self.limit:
            return self.reason
        m = _RAM_IN_REGISTERS.search(text)
        if not m:
            return None
        name = re.sub(r"(_reg)?(_\d+)*$", "", m.group(1).strip().rsplit("/", 1)[-1])
        if name:  # (a RAM it names: the arrays of that name; one that is none of them is not a memory of this design's arrays)
            arrays = register_arrays(self.facts, self.covered, self.plan.min_words, name=name)
            if not arrays:
                return None
        else:
            arrays = register_arrays(self.facts, self.covered, self.plan.min_words)
        msg = over_limit(arrays, self.limit, "Vivado", self.plan, "fpga")
        if msg:
            self.reason = f"{msg} (Vivado: {m.group(0)} …; stopped)"
        return self.reason


# -- logic and memory apart (R4) ---------------------------------------------------------------------------------------------------

_NUM = r"-?[\d.]+"


def dc_rows(area_rpt: str) -> list[dict]:
    """[{cell, total_um2, blackbox_um2, design}] of the "Hierarchical area distribution" of `report_area -hierarchy` (a long cell name is
    on a line of its own, its numbers on the next)."""
    i = area_rpt.find("Hierarchical area distribution")
    if i < 0:
        return []
    lines = area_rpt[i:].splitlines()
    head = next((n for n, ln in enumerate(lines) if ln.startswith("Hierarchical cell")), None)
    if head is None:
        return []
    row = re.compile(rf"^(\S+)?\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+(\S+)\s*$")
    out, pending, inside = [], None, False
    for ln in lines[head + 1:]:
        if ln.startswith("-----"):
            if inside:
                break  # (the line under the rows: Total follows)
            inside = True  # (the line under the column names)
            continue
        if not inside or not ln.strip():
            continue
        m = row.match(ln)
        if m:
            out.append({"cell": m.group(1) or pending or "", "total_um2": float(m.group(2)), "blackbox_um2": float(m.group(6)), "design": m.group(7)})
            pending = None
        elif re.match(r"^\S+\s*$", ln):
            pending = ln.strip()
    return out


def vivado_rows(hier_rpt: str) -> list[dict]:
    """[{path, module, lut, logic_lut, lut_ram, srl, ff, ramb36, ramb18, dsp}] of `report_utilization -hierarchical`: the path from the top
    (the top row has ""), from the indentation of the instance column."""
    out, stack = [], []
    cols = ("lut", "logic_lut", "lut_ram", "srl", "ff", "ramb36", "ramb18", "dsp")
    for ln in hier_rpt.splitlines():
        if not ln.startswith("|") or "Instance" in ln:
            continue
        cells = ln.split("|")[1:-1]
        if len(cells) < 2 + len(cols):
            continue
        raw, module = cells[0], cells[1].strip()
        name = raw.strip()
        if not name or name.startswith("("):
            continue
        level = (len(raw) - len(raw.lstrip(" ")) - 1) // 2
        try:
            nums = [float(c.strip() or 0) for c in cells[2:2 + len(cols)]]
        except ValueError:
            continue
        del stack[level:]
        stack.append(name)
        out.append({"path": "/".join(stack[1:]), "module": module, **{k: (int(v) if v == int(v) else v) for k, v in zip(cols, nums)}})
    return out


def _match(plan: Plan, rows: list[dict], key: str) -> dict[str, dict]:
    """{instance path: row} for the memory instances a report lists (its path, written the tool's way)."""
    by = {hdlmem.norm(r[key]): r for r in rows if r.get(key)}
    return {i.path: by[hdlmem.norm(i.path)] for i in plan.instances() if hdlmem.norm(i.path) in by}


def fpga_split(plan_data: dict, rows: list[dict], util: dict) -> dict:
    """{memory, logic} of ppa_report.json from fpga_synth's `memories` (Plan.summary) and the hierarchical utilization."""
    insts = plan_data.get("instances") or []
    by = {hdlmem.norm(r["path"]): r for r in rows if r.get("path")}
    keys = ("lut", "lut_ram", "ff", "ramb36", "ramb18")
    tot = dict.fromkeys(keys, 0)
    out_insts = []
    for i in insts:
        r = by.get(hdlmem.norm(i["instance"]))
        entry = {**{k: i[k] for k in ("instance", "module", "mode", "bits")}, "reported": r is not None}
        if r is not None:
            entry.update({k: r.get(k, 0) for k in keys})
            for k in keys:
                tot[k] += r.get(k, 0) or 0
        elif i["mode"] == "blackbox":
            entry.update(dict.fromkeys(keys, 0))
        out_insts.append(entry)
    bb = [i for i in insts if i["mode"] == "blackbox"]
    memory = {"modules": plan_data.get("modules") or [], "proposed": bool(plan_data.get("proposed")), "instances": out_insts,
              "bits": sum(i["bits"] for i in insts), **tot,
              "design_lut_ram": (util.get("lut_ram") or {}).get("used"), "design_bram_tile": (util.get("bram_tile") or {}).get("used")}
    if bb:
        memory["black_boxes"] = {"instances": len(bb), "timed": False,
                                 "note": "Vivado times no pin of a black box (Constraints 18-472): paths into and out of them are not timed"}
    unrep = [e["instance"] for e in out_insts if not e["reported"] and e["mode"] != "blackbox"]
    if unrep:
        memory["not_reported"] = unrep[:20]
        memory["note"] = ("instances the tool's per-instance report does not list (Vivado: merged into their parent when it flattened them): "
                          "their resources count in the logic")
    lut, ff = (util.get("lut") or {}).get("used"), (util.get("ff") or {}).get("used")
    logic = {"lut": lut - tot["lut"] if isinstance(lut, (int, float)) else None, "ff": ff - tot["ff"] if isinstance(ff, (int, float)) else None}
    return {"memory": memory, "logic": logic}


def asic_split(plan: Plan, rows: list[dict], total_um2: float | None, um2_per_bit: float | None, nand2_um2: float | None) -> dict:
    """{memory, logic, area_with_memory_um2} of asic_synth.json: the memory instances' area from the hierarchical area report, a black box's
    from the PDK's `sram_um2_per_bit` (an estimate), the logic the rest of the total."""
    found = _match(plan, rows, "cell")
    insts, measured, estimated = [], 0.0, 0.0
    for i in plan.instances():
        bits = i.bits(plan.min_words)
        e = {"instance": i.path, "module": i.module, "mode": plan.mode(i.module), "bits": bits, "words": max((a.words for a in i.arrays), default=0),
             "width": max((a.width for a in i.arrays), default=0)}
        r = found.get(i.path)
        if e["mode"] == "blackbox":
            area = round(bits * um2_per_bit, 3) if um2_per_bit else None
            e.update({"area_um2": area, "estimate": True, "reported": False})
            estimated += area or 0.0
        elif r is not None:
            e.update({"area_um2": r["total_um2"], "estimate": False, "reported": True})
            measured += r["total_um2"]
        else:
            e.update({"area_um2": None, "estimate": False, "reported": False})
        insts.append(e)
    memory = {"modules": plan.modules, "proposed": plan.proposed, "instances": insts, "bits": sum(e["bits"] for e in insts),
              "measured_um2": round(measured, 3), "estimated_um2": round(estimated, 3), "area_um2": round(measured + estimated, 3),
              "um2_per_bit": um2_per_bit}
    if plan.blackbox:
        memory["black_boxes"] = {"instances": sum(e["mode"] == "blackbox" for e in insts), "timed": bool(plan.timing), "timing": plan.timing,
                                 **({} if um2_per_bit else {"note": "no sram_um2_per_bit in the PDK entry (pdks.json): the black boxes' area is not estimated"})}
    unrep = [e["instance"] for e in insts if not e["reported"] and e["mode"] != "blackbox"]
    if unrep:
        memory["not_reported"] = unrep[:20]
        memory["note"] = "instances Design Compiler ungrouped into their parent: their area counts in the logic"
    logic_um2 = round(total_um2 - measured, 3) if total_um2 is not None else None
    logic = {"area_um2": logic_um2}
    if nand2_um2 and logic_um2 is not None:
        logic["nand2_eq"] = round(logic_um2 / nand2_um2)
        memory["nand2_eq"] = round((measured + estimated) / nand2_um2)
    return {"memory": memory, "logic": logic,
            "area_with_memory_um2": round(total_um2 + estimated, 3) if total_um2 is not None else None}


# -- the tools' commands for black boxes and memory instances ----------------------------------------------------------------------


def dc_filter(module: str, modules: list[str]) -> str:
    """A `get_cells -filter` expression for the cells of `module` after elaboration (`m`, `m_PARA…`, `m_…_3`), not those of a longer
    module name that starts like it."""
    longer = [m for m in modules if m != module and m.startswith(module + "_")]
    return f'(ref_name == {module} || ref_name =~ {module}_*)' + "".join(f" && ref_name !~ {m}*" for m in longer)


def dc_lines(plan: Plan, keep_memories: bool) -> tuple[list[str], list[str]]:
    """(lines after `uniquify`, lines after the SDC) of the Design Compiler script: a black box is kept as it is (dont_touch, no boundary
    optimization) and its data pins get the timing model of `options.blackbox_timing`; with `keep_memories` the memory instances are not
    ungrouped (their area stays theirs in the hierarchical report)."""
    if plan.facts is None:
        return [], []
    mods = plan.facts.modules
    pre, post = [], []
    for m in plan.blackbox:
        pre += [f"# black box {m} (Q3TUI options.blackbox): its stub is kept, its pins are the boundary",
                f'set q3tui_bb_{m} [get_cells -hierarchical -filter "{dc_filter(m, mods)}"]',
                f"if {{[sizeof_collection $q3tui_bb_{m}]}} {{ set_dont_touch $q3tui_bb_{m}; set_boundary_optimization $q3tui_bb_{m} false }}"]
        t = plan.timing.get(m)
        if t:
            clk = f"[get_clocks {t['clock']}]" if t["clock"] else "[index_collection [get_clocks] 0]"
            not_data = " || ".join(f"full_name =~ */{p}" for p in plan.facts.clocks.get(m, [])) or "full_name == q3tui_none"
            post += [f"# black box {m}: inputs needed {t['setup_pct']:g} % of the period before the edge, outputs valid {t['access_pct']:g} % after it",
                     f"if {{[sizeof_collection $q3tui_bb_{m}]}} {{",
                     f'  set_output_delay -clock {clk} [expr {{[get_attribute {clk} period] * {t["setup_pct"] / 100:g}}}] '
                     f'[remove_from_collection [get_pins -of_objects $q3tui_bb_{m} -filter "pin_direction == in"] [get_pins -of_objects $q3tui_bb_{m} -filter "{not_data}"]]',
                     f'  set_input_delay -clock {clk} [expr {{[get_attribute {clk} period] * {t["access_pct"] / 100:g}}}] [get_pins -of_objects $q3tui_bb_{m} -filter "pin_direction == out"]',
                     "}"]
    if keep_memories:
        for m in [m for m in plan.modules if m not in plan.blackbox]:
            pre += [f'set q3tui_mem [get_cells -hierarchical -filter "{dc_filter(m, mods)}"]',
                    f"if {{[sizeof_collection $q3tui_mem]}} {{ set_ungroup $q3tui_mem false }}  ;# memory {m}: its area stays its own"]
    return pre, post


def netlist_modules(netlist_text: str, modules: list[str], wanted: list[str]) -> list[str]:
    """The module names of a netlist that are designs of `wanted` (the black boxes, uniquified by Design Compiler)."""
    names = re.findall(r"^\s*module\s+(\w+)", netlist_text, re.M)
    return [n for n in names if hdlmem.module_of(n, modules) in wanted]
