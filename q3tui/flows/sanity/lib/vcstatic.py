"""Reader of the VC Static violation reports: `report_violations -app CDC -verbose` (blocks of `Tag : …`, `Description : … [Field] …`,
`Violation : CDC:3` and the fields the check found) and `report_rdc -list` (numbered lines `1.  text`), both under a header
`TAG  (1 error/0 waived)`.

Only fatals, errors and warnings are violations; infos (what the tool inferred) are counted and dropped.

A verbose block nests: a line with a name and no `:` opens a container (`SrcInfo`, `DestInfoList`, `ConvControlPathList`, …) for the
lines indented under it. `fields` keeps the first value of each name, nested or not. `nested` keeps, for the objects (`OBJECTS`) the report
gives only inside lists (several paths: `…List`), all their values sorted (their order changes from run to run): a CDC_COHERENCY_MULTI_SYNC
names its source in `SrcInfo` and each destination in a path of `NoConvNoSrcControlPathList`. The message is the description with each
`[Field]` replaced the way `report_violations -list` prints it (`message_text`); a placeholder Q3TUI cannot fill stays as it is."""

from __future__ import annotations

import re

_SECTION = re.compile(r"^\s+(\w+)\s+\((\d+) (fatal|error|warning|info)s?/(\d+) waived(?:/\d+ compressed)?\)\s*$")
_FIELD = re.compile(r"^\s+(\w+)\s*:\s*(.*?)\s*$")
_CONTAINER = re.compile(r"^\s+(\w+)\s*$")  # (a name alone: the lines indented under it belong to it)
_ITEM = re.compile(r"^\s+\d+\.\s+(\S.*?)\s*$")
KEEP = ("fatal", "error", "warning")
_HEADER = re.compile(r"^\s+\w+\s+\((\d+) (fatal|error|warning)s?/")  # (any header that counts errors, whatever follows the waived count)
OBJECTS = ("SrcObject", "DestObject")  # the objects a waiver can name (`nested`: the ones the report gives only inside lists)
PLACEHOLDER = re.compile(r"\[([A-Z][A-Za-z0-9]*)\]")  # (a `[Field]` left in a message; an index like `Q[9]` is no placeholder)


def declared(text: str) -> int:
    """The fatals, errors and warnings the report's own headers count: what `parse_report` must find (a report it reads as clean is no pass)."""
    return sum(int(m.group(1)) for line in text.splitlines() if (m := _HEADER.match(line)))


def _in_list(path: tuple[str, ...]) -> bool:
    return any(p.endswith("List") for p in path)


def _container_text(name: str, nodes: list[tuple[tuple[str, ...], str, str]]) -> str | None:
    """What `report_violations -list` prints for a placeholder that names a top-level container (VC Static X-2025.06): the object directly
    inside it (`[SrcInfo]` → its `SrcObject`), or for a clock list with one clock `ClockName:ClockDomainId` (`[SrcClockInfoList]` →
    `i_clk_sys:9`). None for anything else (several clocks, other containers): the placeholder then stays."""
    inside = [(p, n, v) for p, n, v in nodes if p and p[0] == name]
    direct = [v for p, n, v in inside if len(p) == 1 and n.endswith("Object")]
    if direct:
        return direct[0]
    if name.endswith("ClockInfoList"):
        names = [v for _p, n, v in inside if n == "ClockName"]
        ids = [v for _p, n, v in inside if n == "ClockDomainId"]
        if len(names) == 1 and len(ids) == 1:
            return f"{names[0]}:{ids[0]}"
    return None


def message_text(desc: str, fields: dict[str, str], nodes: list[tuple[tuple[str, ...], str, str]] | None = None) -> str:
    """The description with each `[Field]` filled: a field's value, else what the tool prints for a top-level container (`_container_text`)."""
    top = {p[0] for p, _n, _v in nodes or [] if p}

    def fill(m: re.Match) -> str:
        name = m.group(1)
        if name in fields:
            return fields[name]
        if name in top:
            text = _container_text(name, nodes or [])
            if text is not None:
                return text
        return m.group(0)

    return re.sub(r"\[(\w+)\]", fill, desc)


def parse_report(text: str) -> tuple[list[dict], int]:
    """(violations, number of infos): each `{tool_id, tag, severity, message, fields}` in report order, plus for a verbose block `nested`
    (`{object name: sorted values}`, empty when every object is given once outside a list) and `top` (the objects that are fields of the
    block itself, not of a container: what VC Static's `waive_cdc -from` / `-to` match, waivers.py)."""
    blocks: list[dict] = []
    severity, section, cur = "info", "", None
    stack: list[tuple[int, str]] = []  # (indent, container name) of the lines above the current one
    for line in text.splitlines():
        sec = _SECTION.match(line)
        if sec:
            section, severity, cur = sec.group(1), sec.group(3), None
            continue
        item = _ITEM.match(line)
        if item and section:  # the list format: one numbered line per violation
            blocks.append({"tag": section, "severity": severity, "fields": {"Description": item.group(1)}})
            continue
        f = _FIELD.match(line)
        if f and f.group(1) == "Tag":
            cur, stack = {"tag": f.group(2), "severity": severity, "fields": {}, "nodes": []}, []
            blocks.append(cur)
            continue
        c = None if f else _CONTAINER.match(line)
        if cur is None or not (f or c):
            continue
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if c:
            stack.append((indent, c.group(1)))
        elif f.group(2):
            cur["nodes"].append((tuple(n for _i, n in stack), f.group(1), f.group(2)))
            cur["fields"].setdefault(f.group(1), f.group(2))  # (the first value of a name: nested lists repeat names)
    out, infos = [], 0
    for b in blocks:
        if b["severity"] not in KEEP:
            infos += 1
            continue
        fields = b["fields"]
        desc = fields.pop("Description", b["tag"])
        v = {"tool_id": fields.pop("Violation", ""), "tag": b["tag"], "severity": b["severity"],
             "message": message_text(desc, fields, b.get("nodes")), "fields": fields}
        fields.pop("Tag", None)
        if "nodes" in b:  # (a verbose block; the list format has no objects)
            nodes = b["nodes"]
            outside = {n for p, n, _v in nodes if not _in_list(p)}
            v["nested"] = {name: sorted({x for p, n, x in nodes if n == name and _in_list(p)})
                           for name in OBJECTS if name not in outside and any(n == name for _p, n, _v in nodes)}
            v["top"] = [name for name in OBJECTS if any(not p and n == name for p, n, _v in nodes)]  # (a field of the block itself)
        out.append(v)
    return out, infos


def clear_stale_lock(work: Path) -> str | None:
    """VC Static refuses to start while `vcst_rtdb/session.lock` names a session ("Another session is already running…"): a run that crashed leaves it
    (rv32iv_sm: the RDC check after a segfault). When the process it names is not running on this host, the lock is stale: it is removed and the
    message says so. A live session, another host's, or a lock that cannot be read: left as it is (VC Static's own error then says it). None: nothing removed."""
    import os
    import re
    import socket

    lock = work / "vcst_rtdb" / "session.lock"
    if not lock.is_file():
        return None
    text = lock.read_text(errors="replace")
    pid = re.search(r"ProcessId\s*:\s*\[(\d+)\]", text)
    host = re.search(r"HostMachine\s*:\s*\[([^\]]*)\]", text)
    if not pid:
        return None
    here = {socket.gethostname(), socket.gethostname().split(".")[0]}
    if host and host.group(1) and host.group(1) not in here and host.group(1).split(".")[0] not in here:
        return None
    try:
        os.kill(int(pid.group(1)), 0)
        return None  # (alive: a session that is really running)
    except ProcessLookupError:
        pass
    except PermissionError:
        return None  # (another user's live process)
    lock.unlink()
    return f"removed a stale VC Static lock ({lock.name}: session {pid.group(1)} is not running here; a run that crashed left it)"
