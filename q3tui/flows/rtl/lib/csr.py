"""The CSR generator (https://github.com/nguyenquanicd/CSR-Generator, named APB-CSR-Generator before version 3) for the RTL flow: validation of
a register map, the Excel workbook it reads, running it through the tool role `csr_gen`, reading what it generated.

The tool turns a workbook (`Table - Configuration` + `REGISTER` rows) into one register block on a bus with address decode, the bus's handshake and
error responses, per-field semantics (rw / ro / rwi / w1c; w0c / w2c / r2c from 2.2, wo from 3.1) and an optional clock-domain-crossing bridge.
The bus is APB4 up to version 2.x; from version 3.0 the workbook chooses APB4, AXI4-Lite or AHB-Lite (FR-02). It validates nothing (no overlap, no
duplicate offset check), so the checks are here, before it runs. The tool is not shipped with Q3TUI: it is found by `options.tool_dir`,
`$CSR_GEN_HOME`, `~/.q3tui/tools/CSR-Generator` (or `APB-CSR-Generator`, the clone's name before the rename) or `<project>/tools/` likewise
(`q3tui act csr_tool_install` clones it)."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from q3tui.flows.rtl.lib.models import CsrField, CsrMap

REPO = "https://github.com/nguyenquanicd/CSR-Generator.git"
FOLDERS = ("CSR-Generator", "APB-CSR-Generator")  # the clone's folder: the repository's name from version 3, then before
SCRIPT = "CSR_Generation.py"
PROTOCOL_SCRIPT = "CSR_Protocol_Generation.py"  # version 3.x: the AXI4-Lite / AHB-Lite emitters (the tool has no models/ folder from 2.4)
SYNC_MODELS = ("m_csr_sync", "m_vlsi_synch")  # the CDC synchronizer the generated module instantiates when Asynchronous=1 (tool v2.x/v3.x, v1.x)
FIFO_MODELS = ("m_csr_sync_fifo", "m_csr_async_fifo")  # version 3.x, AXI4-Lite / AHB-Lite: the request / response queues (one clock, two clocks)
MODELS = SYNC_MODELS + FIFO_MODELS  # the generator's own modules, named by the tool (the same name in every project that uses it)
PROTOCOLS = ("APB4", "AXI4-Lite", "AHB-Lite")
SHEET = "CSR"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VOLATILE = re.compile(r"^(//\s*(Generator|Date and time):.*|\s*\*\s*(Author|Date)\s*:.*)$", re.M)  # (v1.x: `// Generator:`, v2.x/v3.x: ` * Author :`)
_VERSION = re.compile(r'^VERSION\s*=\s*["\'](\d+)\.(\d+)', re.M)
_TYPES_FROM = {"w0c": (2, 2), "w2c": (2, 2), "r2c": (2, 2), "wo": (3, 1)}  # field types newer than the tool's first version


# -- where the tool is ------------------------------------------------------------------------------------------------

def tool_dirs(project_root: Path, option: str | None = None, role_env: str | None = None) -> list[Path]:
    """Where the generator may be, first wins: `options.tool_dir`, `env.CSR_GEN_HOME` of the `csr_gen` tool role (tools.json), the
    process's `$CSR_GEN_HOME`, `~/.q3tui/tools/`, `<project>/tools/` (each under the name of version 3, then the older name)."""
    home = Path(os.environ.get("Q3TUI_HOME") or "~/.q3tui").expanduser()
    cands = [option, role_env, os.environ.get("CSR_GEN_HOME"), *(home / "tools" / f for f in FOLDERS), *(project_root / "tools" / f for f in FOLDERS)]
    out = []
    for c in cands:
        if c:
            p = Path(os.path.expandvars(str(c))).expanduser()
            out.append(p if p.is_absolute() else project_root / p)
    return out


def missing(tool_dir: Path) -> list[str]:
    """What a generator folder lacks (empty: it is the generator): the script, and the protocol emitters of version 3.x or a synchronizer model of
    version 1.x / 2.x (which kept the models in the tool's folder)."""
    out = [] if (tool_dir / SCRIPT).is_file() else [SCRIPT]
    if not (tool_dir / PROTOCOL_SCRIPT).is_file() and sync_model(tool_dir) is None:
        out.append(" or ".join([PROTOCOL_SCRIPT, *(f"models/{s}.sv" for s in SYNC_MODELS)]))
    return out


def find_tool(project_root: Path, option: str | None = None, role_env: str | None = None) -> Path | None:
    return next((d for d in tool_dirs(project_root, option, role_env) if not missing(d)), None)


def version(tool_dir: Path | None) -> tuple[int, int] | None:
    """The generator's version (major, minor): `VERSION = "3.5"` in its script (3.x); else what its files say (3.0 with the protocol emitters, 2.0
    with `models/m_csr_sync.sv`, 1.0 with `models/m_vlsi_synch.sv`). None: not a generator folder."""
    if tool_dir is None or missing(tool_dir):
        return None
    try:
        m = _VERSION.search((tool_dir / SCRIPT).read_text(errors="replace"))
    except OSError:
        m = None
    if m:
        return int(m.group(1)), int(m.group(2))
    if (tool_dir / PROTOCOL_SCRIPT).is_file():
        return 3, 0
    return (2, 0) if sync_model(tool_dir) == SYNC_MODELS[0] else (1, 0)


def protocols(ver: tuple[int, int] | None) -> tuple[str, ...]:
    """The buses a generator of version `ver` makes (unknown: all of them, the tool decides)."""
    return PROTOCOLS if ver is None or ver >= (3, 0) else ("APB4",)


def sync_model(folder: Path) -> str | None:
    """The synchronizer model in `folder`/models (the tool's own folder up to 2.3, or the RTL/ it generated): `m_csr_sync` from v2.0, `m_vlsi_synch`
    before."""
    return next((s for s in SYNC_MODELS if (folder / "models" / f"{s}.sv").is_file()), None)


def generated_files(rtl: Path, module: str, asynchronous: bool) -> dict[str, Path]:
    """The files of one generation, by module name (the register block first), in the `RTL/` folder the tool wrote: the ones its `filelist.f` lists
    (version 3.x writes one: the block, its synchronizer and, for AXI4-Lite / AHB-Lite, its queues; all of them, as the tool compiles them together,
    also those a generate branch of the chosen parameters leaves out); without a filelist (1.x / 2.x) the block and its synchronizer when it is
    asynchronous or names it (2.2 to 2.4: both clock architectures in one module, chosen by `PARA_ASYNCHRONOUS`)."""
    out = {module: rtl / f"{module}.sv"}
    fl = rtl / "filelist.f"
    if fl.is_file():
        for line in fl.read_text(errors="replace").splitlines():
            line = line.split("//", 1)[0].strip()
            if line.endswith((".sv", ".v")) and not line.startswith(("+", "-")):
                f = rtl / line
                out.setdefault(f.stem, f)
        return out
    sync = sync_model(rtl)
    try:
        named = bool(sync) and re.search(rf"\b{sync}\b", out[module].read_text(errors="replace")) is not None
    except OSError:
        named = False
    if sync and (asynchronous or named):
        out[sync] = rtl / "models" / f"{sync}.sv"
    return out


# -- the register map -------------------------------------------------------------------------------------------------

def complete(m: CsrMap) -> CsrMap:
    """The map with every bit of every register covered: a gap becomes a `reserved` ro field (the tool wants the whole register described; a
    `reserved` field has no ports). Fields are ordered high to low."""
    for r in m.registers:
        r.fields.sort(key=lambda f: -f.msb)
        have = [(f.msb, f.lsb) for f in r.fields]
        top = m.data_width - 1
        gaps = []
        for b in range(top, -1, -1):
            if not any(lo <= b <= hi for hi, lo in have):
                if gaps and gaps[-1][1] == b + 1:
                    gaps[-1][1] = b
                else:
                    gaps.append([b, b])
        for hi, lo in gaps:
            r.fields.append(CsrField(name="reserved", msb=hi, lsb=lo, type="ro", description="Reserved. Read-as-zero, write ignored."))
        r.fields.sort(key=lambda f: -f.msb)
    return m


def validate(m: CsrMap, ver: tuple[int, int] | None = None, protocol: str | None = None) -> list[str]:
    """What is wrong with a map (empty: fine), in words the author of the map can act on. `ver`: the generator's version, when known (what it
    can make); `protocol`: the bus the project fixes (the step option `protocol`), if any."""
    p: list[str] = []
    if not m.has_registers:
        return p
    if not _IDENT.match(m.module_name or ""):
        p.append(f"module_name `{m.module_name}` is not an identifier")
    if protocol and m.protocol != protocol:
        p.append(f"protocol is {m.protocol}: the project fixes the register block's bus to {protocol} (option protocol of the csr step)")
    if m.protocol not in protocols(ver):
        p.append(f"protocol is {m.protocol}: the generator here is version {ver[0]}.{ver[1]}, which makes APB4 only (AXI4-Lite and AHB-Lite need "
                 "CSR-Generator 3.0 or later)")
    if m.event_sideband and m.protocol != "APB4":
        p.append(f"event_sideband is an APB4 option of the generator (protocol is {m.protocol})")
    elif m.event_sideband and ver is not None and ver < (3, 0):
        p.append(f"event_sideband needs the generator 3.0 or later (here {ver[0]}.{ver[1]})")
    if m.data_width != 32:
        p.append(f"data_width is {m.data_width}: the tool supports 32 only")
    if not 4 <= m.addr_width <= 32:
        p.append(f"addr_width {m.addr_width} is outside 4..32")
    if m.protocol == "AXI4-Lite" and m.outstanding == "QUEUED" and not (1 <= m.rd_depth <= 8 and 1 <= m.wr_depth <= 8):
        p.append(f"rd_depth {m.rd_depth} / wr_depth {m.wr_depth}: the tool takes 1..8 outstanding reads and writes")
    if not m.registers:
        p.append("the map has no register")
    names, offsets = set(), {}
    for r in m.registers:
        if not _IDENT.match(r.name):
            p.append(f"register name `{r.name}` is not an identifier")
        if r.name.lower() in names:
            p.append(f"register `{r.name}` appears twice (names are compared without case: the tool builds ports from them)")
        names.add(r.name.lower())
        if r.offset % 4:
            p.append(f"register `{r.name}` offset {hex(r.offset)} is not a multiple of 4 (the tool answers SLVERR to a misaligned address)")
        if r.offset < 0 or r.offset >= (1 << m.addr_width):
            p.append(f"register `{r.name}` offset {hex(r.offset)} does not fit in {m.addr_width} address bits")
        if r.offset in offsets:
            p.append(f"registers `{offsets[r.offset]}` and `{r.name}` share offset {hex(r.offset)}")
        offsets[r.offset] = r.name
        covered = [False] * m.data_width
        fnames = set()
        for f in r.fields:
            where = f"register `{r.name}` field `{f.name}`"
            if not _IDENT.match(f.name):
                p.append(f"{where}: not an identifier")
            if f.name != "reserved" and f.name in fnames:
                p.append(f"register `{r.name}` has two fields named `{f.name}`")
            fnames.add(f.name)
            if not (0 <= f.lsb <= f.msb < m.data_width):
                p.append(f"{where}: bits [{f.msb}:{f.lsb}] are not inside [{m.data_width - 1}:0]")
                continue
            if f.name == "reserved" and f.type != "ro":
                p.append(f"{where}: a `reserved` field is `ro` (it has no ports and reads 0)")
            need = _TYPES_FROM.get(f.type)
            if need and ver is not None and ver < need:
                p.append(f"{where}: type `{f.type}` needs the generator {need[0]}.{need[1]} or later (here {ver[0]}.{ver[1]})")
            if f.reset >= (1 << (f.msb - f.lsb + 1)):
                p.append(f"{where}: reset value {hex(f.reset)} does not fit in {f.msb - f.lsb + 1} bit(s)")
            for b in range(f.lsb, f.msb + 1):
                if covered[b]:
                    p.append(f"{where}: bit {b} is also in another field of `{r.name}`")
                    break
                covered[b] = True
    return p


def reset_value(r) -> int:
    return sum(f.reset << f.lsb for f in r.fields)


def config_rows(m: CsrMap) -> list[tuple[str, object]]:
    """The configuration block: the keys every version reads (`Protocol` APB for APB4, the value 1.x / 2.x know), then the bus options of
    version 3.x for AXI4-Lite (strobe mode, outstanding mode and depths) and AHB-Lite (strobe mode, endian)."""
    rows: list[tuple[str, object]] = [("Module Name", m.module_name), ("Protocol", "APB" if m.protocol == "APB4" else m.protocol),
                                      ("Data Width", m.data_width), ("Address Width", m.addr_width), ("Write Strobe", 4),
                                      ("Asynchronous", 1 if m.asynchronous else 0)]
    if m.protocol == "AXI4-Lite":
        rows += [("Strobe Mode", m.strobe_mode), ("AXI Outstanding Mode", m.outstanding), ("AXI Read Outstanding Depth", m.rd_depth),
                 ("AXI Write Outstanding Depth", m.wr_depth)]
    elif m.protocol == "AHB-Lite":
        rows += [("Strobe Mode", m.strobe_mode), ("AHB Endian", m.ahb_endian)]
    if m.protocol == "APB4" and m.event_sideband:  # only when asked: the workbooks of existing APB4 maps stay byte for byte the same
        rows.append(("Register Event Sideband", 1))
    return rows


def write_workbook(m: CsrMap, path: Path, sheet: str = SHEET) -> Path:
    """The workbook in the layout CSR_Generation.py reads (see its CSR_GOLDEN_FORMAT.xlsx): configuration block, then a header row, then a row per
    register (`REGISTER`, `OFFSET`, reset) followed by one row per field (`BIT NAME`, `FIELD WIDTH` `[msb:lsb]`, `BIT TYPE`, reset, description)."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    def row(n: int, *values) -> None:
        for c, v in enumerate(values, 1):
            if v is not None:
                ws.cell(row=n, column=c, value=v)

    row(1, "Table - Configuration")
    row(2, "Config", "Value", "Description")
    for n, (k, v) in enumerate(config_rows(m), 3):  # (rows 3..12: the register table starts at row 14)
        row(n, k, v)
    row(14, "Table - Register Definition")
    row(15, "REGISTER", "OFFSET", "BIT NAME", "FIELD WIDTH", "BIT TYPE", "RESET VALUE", "DESCRIPTION")
    n = 16
    for r in m.registers:
        row(n, r.name, f"0x{r.offset:04X}", None, None, None, hex(reset_value(r)), r.description or None)
        n += 1
        for f in r.fields:
            row(n, None, None, f.name, f"[{f.msb}]" if f.msb == f.lsb else f"[{f.msb}:{f.lsb}]", f.type, hex(f.reset), f.description or None)
            n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def run_tool(ts, tool: Path, workbook: Path, workdir: Path, sheet: str = SHEET):
    """Run the generator through the role `csr_gen` in `workdir` (it writes `RTL/` and `Debug/` into its working directory)."""
    from q3tui.eda.tools import run_role

    shutil.rmtree(workdir / "RTL", ignore_errors=True)
    shutil.rmtree(workdir / "Debug", ignore_errors=True)
    return run_role(ts, "csr_gen", workdir, {"python": sys.executable, "tool": tool, "workbook": workbook, "sheet": sheet,
                                             "workdir": workdir, "project": workdir}, log_name="csr_gen.log")


def stable(text: str) -> str:
    """The generated text without the lines that change on every run (user name, date): what decides whether the module changed."""
    return _VOLATILE.sub("", text)


def volatile(line: str) -> bool:
    """A line of the tool's header that changes on every run (user name, date)."""
    return bool(_VOLATILE.match(line))


_PORT = re.compile(r"^\s*(input|output|inout)\b\s*(?:(?:wire|logic|reg|var)\b\s*)?(\[[^\]]+\])?\s*(\w+)\s*,?\s*(?://.*)?$", re.M)
_LPARA = re.compile(r"\blocalparam\b[^=;,]*?\b(\w+)\s*=\s*(?:\d*'[dD])?(\d+)\s*[,)\n]")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_MODULE = re.compile(r"\bmodule\s+\w+\s*")
# the bus, clock / reset and access-check ports: v2.x / v3.x names (APB4; AXI4-Lite and AHB-Lite from 3.0), then v1.x
BUS_PORTS = {"i_clk_apb", "i_rst_n_apb", "i_clk_reg", "i_rst_n_reg", *(f"i_apb_{s}" for s in ("psel", "penable", "pwrite", "paddr", "pwdata", "pstrb", "pprot")),
             "o_apb_pready", "o_apb_pslverr", "o_apb_prdata",
             "i_clk_axi", "i_rst_n_axi", *(f"i_axi_{s}" for s in ("awvalid", "awaddr", "awprot", "wvalid", "wdata", "wstrb", "bready", "arvalid", "araddr",
                                                                   "arprot", "rready")),
             *(f"o_axi_{s}" for s in ("awready", "wready", "bvalid", "bresp", "arready", "rvalid", "rdata", "rresp")),
             "i_clk_ahb", "i_rst_n_ahb", *(f"i_ahb_{s}" for s in ("hsel", "hready", "htrans", "haddr", "hwrite", "hsize", "hburst", "hwdata")),
             "o_ahb_hreadyout", "o_ahb_hresp", "o_ahb_hrdata",
             "i_bus_clk", "i_bus_rstn", "i_reg_clk", "i_reg_rstn", "i_psel", "i_penable", "i_pwrite", "i_paddr", "i_pwdata", "i_pstrb", "i_pprot",
             "o_pready", "o_pslverr", "o_prdata", "i_protect_en", "i_slverr_en"}


def _balanced(code: str, i: int) -> int:
    """The index just after the `)` that closes the `(` at `code[i]`."""
    depth = 0
    for k in range(i, len(code)):
        if code[k] == "(":
            depth += 1
        elif code[k] == ")":
            depth -= 1
            if depth == 0:
                return k + 1
    return len(code)


def _header(sv: str) -> tuple[str, str]:
    """(parameter list, port list) of the first module of a generated file, comments removed: a comment may hold a `);` (version 3.x writes
    `0 (synchronous) or 1 (asynchronous); default …` in its parameter comments), so neither list ends at the first `);` of the text."""
    code = _COMMENT.sub("", sv)
    m = _MODULE.search(code)
    if m is None:
        return "", code.split(");", 1)[0]
    i, params = m.end(), ""
    if code.startswith("#", i):
        j = code.find("(", i)
        if j < 0:
            return "", code[i:]
        end = _balanced(code, j)
        params, i = code[j:end], end
    j = code.find("(", i)
    if j < 0:
        return params, ""
    return params, code[j + 1:_balanced(code, j) - 1]


def ports(sv: str) -> list[tuple[str, str, str]]:
    """(direction, width, name) of the ports of a generated module, in order; a width written with the module's localparams (v2.x / v3.x:
    `[LPARA_ADDR_W-1:0]`) is given in numbers (`[15:0]`)."""
    params_text, body = _header(sv)
    params = dict(_LPARA.findall(params_text + "\n" + body))
    return [(d, _width(w or "", params), n) for d, w, n in _PORT.findall(body)]


def _width(w: str, params: dict[str, str]) -> str:
    w = w.replace(" ", "")
    if params and re.search(r"[A-Za-z_]", w):
        w = re.sub(r"\b[A-Za-z_]\w*\b", lambda m: params.get(m.group(0), m.group(0)), w)
        w = re.sub(r"\b(\d+)-1\b", lambda m: str(int(m.group(1)) - 1), w)
    return w


EVENT_PORTS = ("o_reg_rd_en", "o_reg_rd_addr", "o_reg_wr_en", "o_reg_wr_addr")  # APB4 Register Event Sideband (map `event_sideband`)


def port_summary(sv: str, module: str) -> str:
    """The generated module's ports as the integrator reads them: APB bus, clocks/resets, the event sideband (APB4, when generated), then the
    register ports grouped by register."""
    ps = ports(sv)
    bus = [n for _, _, n in ps if n in BUS_PORTS]
    events = [(d, w, n) for d, w, n in ps if n in EVENT_PORTS]
    rest = [(d, w, n) for d, w, n in ps if n not in bus and n not in EVENT_PORTS]
    lines = [f"module {module}", "bus / clocks: " + ", ".join(f"{n}" for n in bus)]
    if events:
        lines.append("event sideband (one-cycle pulse in the first ACCESS cycle of each read / accepted write, with the register offset): "
                     + ", ".join(f"{d} {w + ' ' if w else ''}{n}" for d, w, n in events))
    lines.append("register ports:")
    lines += [f"  {d}{' ' + w if w else ''} {n}" for d, w, n in rest]
    return "\n".join(lines)


def instantiation_hint(sv: str, module: str, inst: str | None = None) -> str:
    ps = ports(sv)
    inst = inst or "u_" + re.sub(r"^m_", "", module)
    body = ",\n".join(f"  .{n}()" for _, _, n in ps)
    return f"{module} {inst} (\n{body}\n);"


# -- modules the tool provides (the `rtl` step does not write, rule-check or fix them) ------------------------------------

PROVIDED = "csr/provided.json"  # in the RTL folder: {module: {kind, parent, instances, req_ids}}


def provided_file(rtl_dir: Path) -> Path:
    return rtl_dir / PROVIDED


def read_provided(rtl_dir: Path) -> dict[str, dict]:
    from q3tui.core.project import read_json

    data = read_json(provided_file(rtl_dir), {}) or {}
    return data if isinstance(data, dict) else {}


_MENTIONS = re.compile(r"\b(registers?|CSRs?|SFRs?|register map|memory[- ]mapped|address map)\b", re.I)


def mentions_registers(text: str) -> bool:
    """A cheap first look: a spec that never says register / CSR / SFR / memory-mapped has no register map to generate (no LLM needed)."""
    return bool(_MENTIONS.search(text))
