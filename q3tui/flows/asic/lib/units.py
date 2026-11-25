"""Library units of the ASIC flow (FR-04).

The shared SDC (`constraints/sdc/`, also the FPGA flow's) is written in ns and pF. Design Compiler reads the numbers of an SDC in the
units of its main library and does not convert them: `set_units` only checks that they match (IFS-001), and its commands cannot be
wrapped. A library characterized in ps / fF (most commercial kits) would read a 2.5 ns clock as 2.5 ps. The options `lib_time_unit`
(ns | ps) and `lib_cap_unit` (pF | fF) of `asic_synth` say the library's units: the ASIC copy of the SDC (`build/syn/asic/sdc/`) is then
scaled when it is rendered, and the times read back from the reports are scaled to ns. The defaults (ns, pF) change nothing."""

from __future__ import annotations

import re

TIME = {"ns": 1.0, "ps": 1000.0}  # library time units per ns
CAP = {"pf": 1.0, "ff": 1000.0}  # library capacitance units per pF

# commands whose numeric arguments are times / capacitances (every bare number of the command; -waveform's list too)
TIME_CMDS = frozenset({"create_clock", "set_clock_uncertainty", "set_clock_latency", "set_clock_transition", "set_input_delay",
                       "set_output_delay", "set_input_transition", "set_max_transition", "set_max_delay", "set_min_delay",
                       "set_max_time_borrow"})
CAP_CMDS = frozenset({"set_load", "set_max_capacitance", "set_min_capacitance"})
_NUM = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")


def scales(options: dict | None) -> tuple[float, float]:
    """(time scale, capacitance scale) of the asic_synth options: library units per ns and per pF."""
    o = options or {}
    t = str(o.get("lib_time_unit") or "ns").strip().lower()
    c = str(o.get("lib_cap_unit") or "pF").strip().lower()
    if t not in TIME:
        raise ValueError(f"lib_time_unit must be one of {', '.join(TIME)}, not {t!r}")
    if c not in CAP:
        raise ValueError(f"lib_cap_unit must be pF or fF, not {c!r}")
    return TIME[t], CAP[c]


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _split(code: str) -> list[str]:
    """Tcl words of one command at depth 0 ([…], {…} and "…" kept whole)."""
    words, cur, depth, quote = [], "", 0, False
    for ch in code:
        if ch == '"' and depth == 0:
            quote = not quote
        if ch in "[{" and not quote:
            depth += 1
        elif ch in "]}" and not quote:
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0 and not quote:
            if cur:
                words.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        words.append(cur)
    return words


def scale_line(line: str, t: float, c: float) -> str:
    """One SDC line with its times multiplied by `t` and its capacitances by `c` (comments and other commands unchanged)."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return line
    code, sep, comment = line.partition(";#")
    words = _split(code)
    if not words:
        return line
    f = t if words[0] in TIME_CMDS else c if words[0] in CAP_CMDS else 1.0
    if f == 1.0:
        return line
    out = []
    for i, w in enumerate(words):
        if i and _NUM.match(w):
            w = _fmt(float(w) * f)
        elif i and words[i - 1] == "-waveform" and w.startswith("{"):
            w = "{" + " ".join(_fmt(float(x) * f) if _NUM.match(x) else x for x in w.strip("{}").split()) + "}"
        out.append(w)
    lead = line[: len(line) - len(stripped)]
    return lead + " ".join(out) + (("  " + sep + comment) if sep else "")


def scale_sdc(text: str, t: float, c: float) -> str:
    """An SDC file (Tcl) with every time × `t` and every capacitance × `c`."""
    if t == 1.0 and c == 1.0:
        return text
    return "\n".join(scale_line(ln, t, c) for ln in text.split("\n"))
