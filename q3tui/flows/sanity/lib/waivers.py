"""The waiver review's data: the decision of each violation, the VC Static waiver commands, the knowledge the review applies, and how a
fix is handed to the flow that owns it.

VC Static numbers its violations per run (`CDC:48` is another violation the next time), so a violation is identified by what it is
(`signature`: the tag and the objects, or the message) and a waiver names it that way (`selector`). A waiver applies to every violation of
its tag its selector matches, so one is written per selector, and only when the review waived every violation it matches (`waiver_plan`)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from q3tui.flows.sanity.lib.vcstatic import PLACEHOLDER

ACTIONS = ("waive", "fix_rtl", "spec", "sdc", "ask")
OPEN_ASKING = ("sdc", "ask")  # the decisions that wait for you


class Decision(BaseModel):
    id: str = Field(description="The violation id from the report (V1, V2, …)")
    action: Literal["waive", "fix_rtl", "spec", "sdc", "ask"] = Field(
        description="waive: safe, under a knowledge rule whose conditions you verified, or by a design reason with its evidence; "
                    "fix_rtl: change one RTL module; spec: the spec owner has to decide; "
                    "sdc: a constraint is missing or wrong in the SDC (a plain SDC command in sdc_request is added to constraints/sdc/extra.tcl on hand-off; the user decides otherwise); "
                    "ask: you cannot verify a condition and nothing else settles it: ask the user")
    module: str = Field("", description="action fix_rtl: the module (file name without extension); empty otherwise")
    reason: str = Field(description="waive: why it is safe, citing the evidence (file:line, signal) for each condition; "
                                    "fix_rtl / spec / sdc: what is wrong or undecided; ask: what you checked and what is missing")
    change: str = Field("", description="fix_rtl: the structural change, concretely; spec: the question for the spec; empty otherwise")
    rule: str = Field("", description="waive: the knowledge case it relies on (K1, U2, …); empty for a plain design reason")
    sdc_request: str = Field("", description="action sdc: the constraint to add or change, as one Tcl command with the real object names "
                                             "(e.g. set_max_delay 15.0 -from {…} -to {…}), and where; empty otherwise")
    question: str = Field("", description="action ask / sdc: ONE clear question for the user; empty for the others")
    options: list[str] = Field(default_factory=list, description="action ask: two to four suggested answers; empty otherwise")


class Review(BaseModel):
    decisions: list[Decision] = Field(description="One decision for every violation id of the report, none missing, none invented")
    summary: str = Field("", description="Two or three sentences: what was waived, what needs RTL, spec, SDC work or the user")


def _glob(name: str) -> str:
    return name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")  # (waive_cdc patterns are globs: `[7:0]` is not a range)


def _objects(v: dict, name: str) -> str:
    """An object of the violation as part of what it is: its one value outside the report's lists, else all its values inside them, sorted."""
    nested = v.get("nested") or {}
    if name in nested:
        return " | ".join(nested[name])
    return (v.get("fields") or {}).get(name) or ""


def legacy_signature(v: dict) -> tuple:
    """The signature of Q3TUI before nested objects were read (the first value of each object): what a report written then, and the
    decisions and answers stored with it, are keyed by."""
    f = v.get("fields") or {}
    return (v["tag"], f.get("SrcObject") or f.get("ConvergencePoint") or v.get("message", ""), f.get("DestObject") or "")


def signature(v: dict) -> tuple:
    """What a violation is, the same in every run of the same RTL and SDC: the tag; the convergence point, else the source, else the message;
    the destination. An object the report gives only inside lists counts with all its values, sorted: VC Static lists them in any order (the
    `cdc` and `cdc_check` runs of one RTL differ). A violation read before that (no `nested`): `legacy_signature`, as it was then."""
    if "nested" not in v:
        return legacy_signature(v)
    f = v.get("fields") or {}
    return (v["tag"], f.get("ConvergencePoint") or _objects(v, "SrcObject") or v.get("message", ""), _objects(v, "DestObject"))


def _sig_id(role: str, sig: tuple) -> str:
    return f"{role.upper()}-" + hashlib.sha256("\0".join(sig).encode()).hexdigest()[:6]


def sig_id(role: str, v: dict) -> str:
    """A stable id of a violation across runs (the `V` numbers are not): the id of its question."""
    return _sig_id(role, signature(v))


def legacy_sig_id(role: str, v: dict) -> str:
    """The id `sig_id` gave before nested objects were read (decisions and answers stored then are found by it)."""
    return _sig_id(role, legacy_signature(v))


def _comment(d: dict) -> str:
    why = " ".join(f"{d.get('rule') or 'design'}: {d.get('reason', '')}".split())[:300]
    return why.replace("{", "(").replace("}", ")").replace("\\", "/")


def _plain(name: str) -> bool:
    return bool(name) and not set(name) & set("{}")  # (VC Static writes a set of bit ranges as `Q{ [13:10] ,[8:0] }`: no waiver matches it)


def _from_points(v: dict) -> list[str]:
    """The sources `waive_cdc -from` matches for a violation (VC Static X-2025.06, checked with `waive_cdc -preview` on FX1B's crg): the
    source that is a field of the block itself, and the sources of its paths (inside lists). Not one given in a container: the `SrcInfo` of a
    CDC_COHERENCY_MULTI_SYNC is no from point (`CDC_WAIVE_OPTION_INVALID: … Option 'from' doesn't exist`). A violation read before `top`
    was recorded: its first source, as Q3TUI assumed then."""
    f = v.get("fields") or {}
    if "top" not in v:
        return [f["SrcObject"]] if f.get("SrcObject") else []
    own = [f["SrcObject"]] if "SrcObject" in v["top"] and f.get("SrcObject") else []
    return own + [s for s in (v.get("nested") or {}).get("SrcObject", []) if s not in own]


def _to_points(v: dict) -> list[str]:
    """The destinations `waive_cdc -to` matches: only one that is a field of the block itself. A destination in a container (the `DestInfo`
    of a CDC_COHERENCY_BUS_NOCONV) is none, and VC Static then ignores `-to`: the waiver selects by `-from` alone."""
    f = v.get("fields") or {}
    if "top" not in v:
        return [f["DestObject"]] if f.get("DestObject") else []
    return [f["DestObject"]] if "DestObject" in v["top"] and f.get("DestObject") else []


def _selects(sel: tuple, w: dict) -> bool:
    """Whether a waiver selects violation `w` of its tag: the model of VC Static's matching the grouping relies on."""
    if sel[0] == "filter":
        return (w.get("fields") or {}).get("ConvergencePoint") == sel[1]
    if sel[0] == "from":
        to = _to_points(w)
        return sel[1] in _from_points(w) and (not to or sel[2] in to)
    return w.get("message", "") == sel[1]  # ("msg": a message with no wildcard: the whole message)


def _pick(v: dict, role: str, violations: list[dict] | None = None) -> tuple | None:
    """The selector of a violation as data: ("filter", point) | ("from", source, destination) | ("msg", message) | None (see `selector`)."""
    msg = v.get("message", "")
    if role == "rdc":
        return ("msg", msg) if msg else None
    f = v.get("fields") or {}
    if v["tag"].startswith("CDC_COHERENCY") and f.get("ConvergencePoint"):
        return ("filter", f["ConvergencePoint"])
    dst = f.get("DestObject") or ""
    sources = [s for s in _from_points(v) if _plain(s)]
    if sources and _plain(dst) and "DestObject" not in (v.get("nested") or {}):
        first = f.get("SrcObject") if f.get("SrcObject") in sources else sources[0]
        others = [w for w in violations or [] if w["tag"] == v["tag"] and w["id"] != v.get("id")]
        alone = [s for s in [first, *sorted(sources)] if not any(_selects(("from", s, dst), w) for w in others)]
        return ("from", (alone or [first])[0], dst)  # (a source no other violation of the tag has: the waiver selects this one only)
    if "nested" in v and (f.get("SrcObject") or dst) and msg and not PLACEHOLDER.search(msg) and not set(msg) & set("{}*?"):
        return ("msg", msg)
    return None


def selector(v: dict, role: str, violations: list[dict] | None = None) -> str | None:
    """What a waiver of this violation names after its `-tag`: VC Static applies the waiver to every violation of the tag it matches.

    RDC: the message (`report_rdc -list` gives nothing else). CDC: the convergence point (`-filter`); the source and the destination when the
    report gives the destination outside its lists (`-from` / `-to`); else, when the report names objects, the message (`-msg`): VC Static has
    no from / to points for a violation whose destinations are paths of a list (CDC_COHERENCY_MULTI_SYNC: `CDC_WAIVE_OPTION_INVALID`), and
    matches the message `report_violations -list` prints. Never a message with a placeholder Q3TUI could not fill, or with `{ } * ?` (a
    glob that would match more). None: no selector (the decision becomes a question).

    `-from` names one source of the violation: the first, unless another violation of the tag in `violations` (the report) has it too (a
    CDC_COHERENCY_BUS_NOCONV is selected by `-from` alone), or VC Static wrote it as a set of bit ranges (`Q{ [13:10] ,[8:0] }`, which
    `-from` never matches): then another of its sources that selects it alone, if it has one."""
    sel = _pick(v, role, violations)
    if sel is None:
        return None
    if sel[0] == "filter":
        return f'-filter {{ConvergencePoint == "{sel[1]}"}}'
    if sel[0] == "from":
        return f"-from {{{_glob(sel[1])}}} -to {{{_glob(sel[2])}}}"
    return f"-msg {{{_glob(sel[1].replace('{', '*').replace('}', '*'))}}}"


def no_selector_reason(v: dict) -> str:
    """Why a violation has no selector (the reason of the question its `waive` becomes)."""
    f = v.get("fields") or {}
    if not (f.get("SrcObject") or f.get("DestObject") or f.get("ConvergencePoint")):
        return "the tool's report does not say which objects it is about"
    left = PLACEHOLDER.search(v.get("message", ""))
    if left:
        return f"no waiver can name its objects and Q3TUI cannot fill the message's [{left.group(1)}]"
    return ("no waiver can name its objects (given only inside the report's lists, or as a set of bit ranges) and its message is no plain "
            "text to match")


def waive_command(v: dict, d: dict, role: str, violations: list[dict] | None = None) -> str | None:
    """The `waive_cdc` / `waive_rdc` of one violation the review waived; None when there is no `selector` for it (`violations`: the report,
    for the choice of its `-from` source)."""
    sel = selector(v, role, violations)
    return f"waive_{role} -add W_{d['id']} -tag {v['tag']} {sel} -comment {{{_comment(d)}}}" if sel else None


def shared(violations: list[dict], role: str) -> dict[str, list[str]]:
    """{id: the ids of the report its waiver selects, itself first}, for the violations whose waiver selects others too: the same convergence
    point, the same message, or a `-from` source another violation of the tag has as well (`_selects`)."""
    out: dict[str, list[str]] = {}
    for v in violations:
        sel = _pick(v, role, violations)
        if sel is None:
            continue
        also = [w["id"] for w in violations if w["tag"] == v["tag"] and w["id"] != v["id"] and _selects(sel, w)]
        if also:
            out[v["id"]] = [v["id"], *also]
    return out


def waiver_plan(violations: list[dict], items: list[dict], role: str) -> tuple[list[tuple[str, list[str], str]], list[str]]:
    """([(waiver name, the ids it waives, the command)], the waived ids no command is written for). A command per selector, named after its
    first violation, written only when the review waived every violation of the report it selects (never one it did not waive): the waived
    ids whose waiver would select a violation not waived, and those with no selector, are not written. Violations one command already
    selects get no other."""
    by_id = {v["id"]: v for v in violations}
    action = {d["id"]: d["action"] for d in items}
    same = shared(violations, role)
    out, skipped, done = [], [], set()
    for d in items:
        if d["action"] != "waive" or d["id"] in done:
            continue
        v = by_id.get(d["id"])
        cmd = waive_command(v, d, role, violations) if v else None
        ids = same.get(d["id"], [d["id"]])
        if cmd is None or any(action.get(i) != "waive" for i in ids):
            skipped.append(d["id"])
            continue
        out.append((f"W_{d['id']}", ids, cmd))
        done.update(ids)
    return out, skipped


def waive_tcl(violations: list[dict], items: list[dict], rtl: str, role: str) -> tuple[str, list[str]]:
    """(the script, the ids it cannot express). Only `waive` decisions: a fix, a constraint or a question stays an error."""
    lines = [f"# {role.upper()} waivers of the review (Q3TUI {role}_waivers), RTL {rtl}: sourced after check_{role}"]
    plan, skipped = waiver_plan(violations, items, role)
    for name, ids, cmd in plan:
        if len(ids) > 1:
            lines.append(f"# {name} selects {', '.join(ids)} (one selector)")
        lines.append(cmd)
    return "\n".join(lines) + "\n", skipped


def knowledge(builtin: Path, config_dir: Path, role: str) -> str:
    """The cases the review applies: the flow's (`<step>/md/knowledge.md`, K1…) then the project's (`config/sanity/<role>_knowledge.md`, U1…: they win)."""
    parts = []
    for label, f in (("Built-in knowledge", builtin), ("Project knowledge (wins over the built-in cases)", config_dir / "sanity" / f"{role}_knowledge.md")):
        if f.is_file() and f.read_text(errors="replace").strip():
            parts.append(f"## {label}\n\n{f.read_text(errors='replace').strip()}")
    return "\n\n".join(parts) or f"(no {role.upper()} knowledge: waive only what you can fully justify from the RTL, the SDC and the spec; ask otherwise)"


def handoff_text(role: str, d: dict, v: dict) -> tuple[str, str]:
    """(step of the owning flow, change-request text) for one `fix_rtl` / `spec` decision."""
    what = f"{role.upper()} {v['tag']}: {v['message'][:160]}"
    if d["action"] == "fix_rtl":
        return "rtl", (f"[rtl:{d['module']}] {what} — {d['change']}. Evidence: {d['reason']} "
                       "Keep the behaviour, the ports and every requirement tag unchanged; lint stays clean.")
    return "spec", (f"{what} — the spec must say: {d['change']}. Context: {d['reason']} Change only the requirements this affects (the RTL is updated "
                    "from those requirements module by module: do not reword or restructure the rest of the spec).")
