"""Child projects (FR-01; docs/spec/rtl-flow.md "Child projects (provided RTL)"): an integration project uses the RTL of other Q3TUI projects
exactly as their own flows verified it.

`pipeline.step_options.rtl.child_projects` lists the children (project roots, relative to this project or absolute). The files each child's
`filelist.f` lists are copied byte for byte into this project's RTL folder and recorded in `<rtl>/child/provided.json`: beside the csr step's
`csr/provided.json` but a file of its own, because the csr step rewrites its registry and drops the entries it did not make. A copy is read-only
here in every flow: a problem in it is reported for the project that owns it, fixed there, and this project is run again. A child that is itself
a parent passes on what it got from its own children with their owner (its registry says who), so the owner of every file stays known however
deep it comes from. All of it is code: what is copied, the clashes and the owners are computed, never asked of an LLM."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from q3tui.core.config import CONFIG_FILENAME, Config
from q3tui.core.project import Project, sha256_file, write_json

OPTION = "child_projects"  # the rtl step's option: pipeline.step_options.rtl.child_projects
REGISTRY = "child/provided.json"  # in the RTL folder: {unit: {kind: "child", owner, child, source, sha256, file, unit, instances, top?}}
_DEFAULT_RTL = "src/rtl"  # (ProjectConfig.rtl_dir's default: where a child whose q3tui.yaml cannot be read is looked for)
_HDL = (".sv", ".v")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_IMPORT = re.compile(r"\s*import\s+[^;]*;")  # a package import in a module header: `module m import p::*; #(…) (…);`


class ChildError(ValueError):
    """A child project cannot be used as it is: the step stops with this message (it names the child, and what to do)."""


@dataclass(frozen=True)
class Child:
    entry: str  # as the option lists it
    root: Path  # resolved: one child per folder
    label: str  # the root relative to this project (`../cha`): how every message and the registry name it
    rtl_dir: Path  # its RTL folder, where its own rtl step keeps it (its q3tui.yaml)
    filelist: Path
    config: Path  # its q3tui.yaml


@dataclass
class CopyResult:
    registry: dict[str, dict]  # what is recorded now (the registry file's content)
    copied: list[str] = field(default_factory=list)  # units written: new here, or their child's file changed
    restored: list[str] = field(default_factory=list)  # units whose copy was edited or deleted here: the child's file again
    removed: list[str] = field(default_factory=list)  # units no child provides any more: their copy is gone (backed up)
    warnings: list[str] = field(default_factory=list)


# -- which children ---------------------------------------------------------------------------------------------------

def configured(engine) -> list[str]:
    """The option as this run has it: the rtl step's (flow file + q3tui.yaml) when the flow has that step, else q3tui.yaml's (the dv, sanity,
    fpga … flows run on the RTL the rtl flow assembled and must know which of it is a child's)."""
    step = engine.by_name.get("rtl")
    opts = getattr(step, "options", None) if step is not None else engine.project.cfg.pipeline.step_options.get("rtl")
    return _entries((opts or {}).get(OPTION))


def _entries(value) -> list[str]:
    return [value] if isinstance(value, str) else [str(v) for v in value] if isinstance(value, list) else []


def resolve(project_root: Path, entries: list[str]) -> list[Child]:
    """The children the option names, in its order: `~` and `$VARS` expanded, a relative entry taken from this project's root, one child per
    folder (`../cha`, `../cha/` and its absolute path are one: the first is kept). Never raises: what is wrong with a child is `problems()`."""
    base = _real(project_root)
    out: dict[Path, Child] = {}
    for raw in entries:
        entry = str(raw).strip()
        root = _root(base, entry) if entry else None
        if root is not None and root not in out:
            cfg, _ = _config(root / CONFIG_FILENAME)
            rtl = root / _rtl_rel(cfg)
            out[root] = Child(entry, root, _label(base, root), rtl, rtl / "filelist.f", root / CONFIG_FILENAME)
    return list(out.values())


def _root(base: Path, entry: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(entry)))
    return _real(p if p.is_absolute() else base / p)


def _rtl_rel(cfg: Config | None) -> str:
    """The child's RTL folder as its own rtl step has it: the step's option `rtl_dir`, else `project.rtl_dir`."""
    if cfg is None:
        return _DEFAULT_RTL
    return str((cfg.pipeline.step_options.get("rtl") or {}).get("rtl_dir") or cfg.project.rtl_dir or _DEFAULT_RTL)


_CONFIGS: dict[tuple[str, int, int], tuple[Config | None, str]] = {}


def _config(path: Path) -> tuple[Config | None, str]:
    """A child's config from its own q3tui.yaml only (not ~/.q3tui/q3tui.yaml nor $Q3TUI_CONFIG: they are the settings of whoever runs this
    project, not the child's layout), and why it cannot be used ('' when it can, or when there is no file). Cached by mtime: the steps'
    inputs() read it on every TUI refresh."""
    try:
        st = path.stat()
    except OSError:
        return None, ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _CONFIGS:
        if len(_CONFIGS) > 256:
            _CONFIGS.clear()
        _CONFIGS[key] = _read_config(path)
    return _CONFIGS[key]


def _read_config(path: Path) -> tuple[Config | None, str]:
    import yaml
    from pydantic import ValidationError

    from q3tui.core.config import _expand, _legacy

    try:
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            return None, "its top level is not a mapping"
        return Config.model_validate(_expand(_legacy(data))), ""
    except ValidationError as exc:
        return None, "; ".join(f"{'.'.join(map(str, e['loc'])) or 'top level'}: {e['msg']}" for e in exc.errors()[:3])
    except Exception as exc:  # noqa: BLE001 - YAML or file errors: problems() reports them, the child keeps the default layout meanwhile
        return None, " ".join(str(exc).split())[:300]


def problems(project_root: Path, children: list[Child]) -> list[str]:
    """What stops the copy, one line per child that cannot be used (the rtl step's missing input: nothing is copied while one is left)."""
    base = _real(project_root)
    out = []
    for c in children:
        cfg, err = _config(c.config)
        if c.root == base:
            out.append(f"child project {c.label} is this project itself")
        elif not c.root.is_dir():
            out.append(f"child project {c.label}: no folder {c.root}")
        elif not c.config.is_file():
            out.append(f"child project {c.label}: no {CONFIG_FILENAME} (not a Q3TUI project)")
        elif cfg is None:
            out.append(f"child project {c.label}: its {CONFIG_FILENAME} cannot be used ({err})")
        elif any(g.root == base for g in resolve(c.root, _entries((cfg.pipeline.step_options.get("rtl") or {}).get(OPTION)))):
            out.append(f"child project {c.label} lists this project among its own child projects: a cycle")
        elif not c.filelist.is_file():
            out.append(_no_filelist(c))
    return out


def _no_filelist(child: Child) -> str:
    return f"child project {child.label}: no {_inside(child, child.filelist)} (run its rtl flow first)"


# -- what a child lists -----------------------------------------------------------------------------------------------

def listed(child: Child) -> list[Path]:
    """The files the child's filelist.f lists, in its order (Q3TUI writes one per line, relative to the project root; blank lines and `//` /
    `#` comments are skipped as in Q3TUI's own filelists). Raises ChildError for what cannot be copied as it is: an option (`+incdir+`, `-f` …:
    the copy is flat, an option would point elsewhere), a file that is not SystemVerilog / Verilog, a file that is not there."""
    if not child.filelist.is_file():
        raise ChildError(_no_filelist(child))
    out: list[Path] = []
    for line in _lines(child):
        if line.startswith(("+", "-")):
            raise ChildError(f"{child.label}: its filelist.f has the option `{line}`")
        f = _path(child, line)
        if f.suffix.lower() not in _HDL:
            raise ChildError(f"{child.label}: its filelist.f lists {line}, not a .sv / .v file")
        if not f.is_file():
            raise ChildError(f"{child.label}: its filelist.f lists {line}, which is not there" + ("" if line == str(f) else f" ({f})"))
        out.append(f)
    return list(dict.fromkeys(out))


def input_files(children: list[Child]) -> list[Path]:
    """What the copy is made from, for the rtl step's inputs (a change to any makes it stale, R7): each child's q3tui.yaml (its layout), its
    filelist.f and the files it lists, those not there yet too (the step goes stale when one appears). Never raises."""
    out: list[Path] = []
    for c in children:
        out += [c.config, c.filelist, *(_path(c, s) for s in _lines(c) if not s.startswith(("+", "-")))]
    return list(dict.fromkeys(out))


def _lines(child: Child) -> list[str]:
    """The filelist's lines that say something, comments taken out as Q3TUI's filelist parser does; [] when it cannot be read."""
    from q3tui.hdl.filelist import _strip_comments

    try:
        text = child.filelist.read_text(errors="replace")
    except OSError:
        return []
    return [s for s in (ln.strip() for ln in _strip_comments(text).splitlines()) if s]


def _path(child: Child, line: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(line)))
    return p if p.is_absolute() else child.root / p


# -- the registry -----------------------------------------------------------------------------------------------------

def registry_file(rtl_dir: Path) -> Path:
    return rtl_dir / REGISTRY


def read_registry(rtl_dir: Path) -> dict[str, dict]:
    """{unit: its record} of the copies here; {} when there is none (no child project). A registry that cannot be read (a merge conflict, a
    hand edit) raises ChildError rather than count as none: without it the copies would be writable by every stage and never removed once
    no child provides them."""
    records, damage = _registry(rtl_dir)
    if damage:
        raise ChildError(f"{registry_file(rtl_dir)} is damaged ({damage}): delete it, then run again")
    return records


def _registry(rtl_dir: Path) -> tuple[dict[str, dict], str]:
    """(the records, what is wrong with the file: '' when nothing, or when there is no file)."""
    f = registry_file(rtl_dir)
    if not f.exists():
        return {}, ""
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError) as exc:
        return {}, " ".join(str(exc).split())[:200]
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        return {}, "not a JSON object of records"
    return data, ""


def read_all_provided(rtl_dir: Path) -> dict[str, dict]:
    """Every module the rtl step does not write: the csr step's and the child projects' (one reader wherever provided modules count)."""
    from q3tui.flows.rtl.lib import csr

    return csr.read_provided(rtl_dir) | read_registry(rtl_dir)


# -- the copy ---------------------------------------------------------------------------------------------------------

def copy(project, rtl_dir: Path, children: list[Child], own: set[str]) -> CopyResult:
    """Copy the children's listed files into `rtl_dir` byte for byte and record them (R2); `own`: units this project makes itself (the csr
    step's), which no child may provide. Everything is checked before anything is written: on a ChildError the folder is as it was."""
    base = _real(project.root)
    found = problems(base, children)
    if found:
        raise ChildError("; ".join(found))
    old = read_registry(rtl_dir)
    res, sources = CopyResult({}), {}
    for child in children:
        _add_units(base, child, res, sources)
    for e in res.registry.values():
        e["instances"] = [i for i in e["instances"] if i in res.registry]
    for unit, e in res.registry.items():
        _check_here(project, rtl_dir, unit, e, old, own)
    _write(project, rtl_dir, res, sources, old)
    return res


def _add_units(base: Path, child: Child, res: CopyResult, sources: dict[str, Path]) -> None:
    """Record what one child lists: one unit per file, named after it (the copy is flat: the name is all a file has), once however many
    children have it (R8), with its owner (R9); its top (the unit its parent instantiates) is marked."""
    from q3tui.core.importer import design_facts

    files = listed(child)
    facts = design_facts(files)
    mods = facts["modules"]
    declared: dict[str, list[str]] = {}
    for name, m in mods.items():
        declared.setdefault(m.get("file", ""), []).append(name)
    owners = _owners(child)
    for f in files:
        unit, names = f.stem, sorted(declared.get(f.name, []))
        if names != [unit]:
            raise ChildError(f"{child.label}: {_inside(child, f)} declares {', '.join(f'`{n}`' for n in names) or 'nothing'}: "
                             "a child's file must hold the unit it is named after" + (", and only it" if unit in names else ""))
        sha, source = sha256_file(f), _label(base, _real(f))  # (`source`: the file copied, the one `sha256` is of)
        owner = _owner_of(base, child, f, sha, owners, res.warnings)
        if unit in res.registry:
            if res.registry[unit]["sha256"] != sha:
                raise ChildError(f"`{unit}` comes from two different files: {res.registry[unit]['source']} and {source} "
                                 f"(one design holds one `{unit}`: make them the same, or rename one in its own project)")
            continue
        res.registry[unit] = {"kind": "child", "owner": owner, "child": child.label, "source": source, "sha256": sha, "file": f.name,
                              "unit": mods[unit].get("kind") or "module", "instances": list(mods[unit].get("instances") or [])}
        sources[unit] = f
    if facts.get("top") in res.registry:
        res.registry[facts["top"]]["top"] = True


def _owners(child: Child) -> dict[str, tuple[Path, Path | None]]:
    """unit -> (its owner's root, the file the child copied it from) of what the child got from its own children: its own registry, resolved
    against its root. Raises ChildError when that registry is damaged: the owners would not be known."""
    records, damage = _registry(child.rtl_dir)
    if damage:
        raise ChildError(f"child project {child.label}: its {REGISTRY} is damaged ({damage}): delete it, run {child.label} again, "
                         "then this project")
    out = {}
    for unit, e in records.items():
        owner, src = e.get("owner"), e.get("source")
        if isinstance(owner, str) and owner.strip():
            out[unit] = (_real(child.root / owner), _real(child.root / src) if isinstance(src, str) and src.strip() else None)
    return out


def _owner_of(base: Path, child: Child, f: Path, sha: str, owners: dict, warnings: list[str]) -> str:
    """The owner of a file the child lists: the child itself, or, for what it got from its own children, the owner its registry names
    (relabelled for this project). A middle copy that no longer matches the file the middle project copied it from is still the one used (the
    middle project lists it), with a warning: the middle project has not run since that file changed."""
    unit = f.stem
    if unit not in owners:
        return child.label
    owner_root, copied_from = owners[unit]
    if owner_root == base:
        raise ChildError(f"child project {child.label} has `{unit}` from this project: a cycle (a project cannot be its own child)")
    owner = _label(base, owner_root)
    if copied_from is not None and (not copied_from.is_file() or sha256_file(copied_from) != sha):
        src = _label(base, copied_from)
        why = f"its copy no longer matches {src}" if copied_from.is_file() else f"{src}, which its copy came from, is not there any more"
        warnings.append(f"`{unit}` (owner {owner}) comes through the child project {child.label}: {why}; run {child.label} again")
    return owner


def _check_here(project, rtl_dir: Path, unit: str, entry: dict, old: dict[str, dict], own: set[str]) -> None:
    """R8 against this project's own modules: a unit it makes itself, or a file of the unit's name here that is not a copy, stops the copy,
    naming both. A file here with the child's very bytes is no clash: it becomes the copy."""
    copy_here = (old.get(unit) or {}).get("file")
    clash = [rtl_dir / f"{unit}.sv"] if unit in own else [
        f for f in dict.fromkeys((rtl_dir / entry["file"], rtl_dir / f"{unit}.sv"))
        if f.is_file() and f.name != copy_here and not (f.name == entry["file"] and sha256_file(f) == entry["sha256"])]
    if clash:
        raise ChildError(f"`{unit}` of the child project {entry['owner']} ({entry['source']}) has the name of this project's own module "
                         f"({project.rel(clash[0])}): rename one of them")


def _write(project, rtl_dir: Path, res: CopyResult, sources: dict[str, Path], old: dict[str, dict]) -> None:
    """Make the folder what the registry says: a copy written where its bytes differ from its child's file (backed up first), the copy of a unit
    no child provides any more deleted (backed up), the registry written, or deleted when no child is left (a project without children has
    none)."""
    for unit, e in res.registry.items():
        dest = rtl_dir / e["file"]
        if dest.is_file() and sha256_file(dest) == e["sha256"]:
            continue
        prior = old.get(unit) or {}
        project.backup([dest])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sources[unit], dest)
        edited_here = prior.get("sha256") == e["sha256"] and prior.get("file") == e["file"]  # (the child's file is as it was: the copy changed)
        (res.restored if edited_here else res.copied).append(unit)
    files = {e["file"] for e in res.registry.values()}
    for unit, e in old.items():
        if unit not in res.registry:
            res.removed.append(unit)
        f = e.get("file")
        if isinstance(f, str) and f == Path(f).name and f not in files and (rtl_dir / f).is_file():  # (a plain name: never outside the folder)
            project.backup([rtl_dir / f])
            (rtl_dir / f).unlink()
    reg = registry_file(rtl_dir)
    if res.registry:
        if list(old.items()) != list(res.registry.items()) or not reg.is_file():
            write_json(reg, res.registry)
    elif reg.is_file():
        project.backup([reg])
        reg.unlink()
        with contextlib.suppress(OSError):
            reg.parent.rmdir()


# -- what the other steps read ----------------------------------------------------------------------------------------

def header(text: str, name: str) -> str:
    """The interface of unit `name` in `text`: `module name` up to its first `;` (after the package imports of the header, `module m import
    p::*; #(…) (…);` as Q3TUI's RTL rules write it), comments stripped, whitespace collapsed; '' when the text does not declare it. An own module
    that instantiates a child module depends on this, not on the child's body: a fix inside the child is no reason to write it again."""
    t = _COMMENT.sub(" ", text)
    m = re.search(rf"\b(?:module|interface|package|program)\s+(?:(?:static|automatic)\s+)?{re.escape(name)}\b", t)
    if not m:
        return ""
    start = m.end()
    while (imp := _IMPORT.match(t, start)) is not None:
        start = imp.end()
    end = t.find(";", start)
    return " ".join(t[m.start(): end + 1 if end >= 0 else len(t)].split())


def refusal(unit: str, entry: dict) -> str:
    """Why a change to a child's unit is not made here (R3): it names the project to make it in."""
    owner = entry.get("owner") or entry.get("child") or "?"
    return f"`{unit}` belongs to the child project {owner} (read-only here): fix it in {owner}, then run this project again"


def status_warnings(child: Child, files: list[Path]) -> list[str]:
    """R11: a child used before its own rtl flow passed, or with files changed since it passed (`files` and its filelist), is used anyway, with
    a warning. Its history is read with its own layout (its q3tui.yaml), never written."""
    from q3tui.core import history

    cfg, _ = _config(child.config)
    project = Project(child.root, cfg or Config())
    paths = list(dict.fromkeys(p for p in (_under(child.root, f) for f in [*files, child.filelist]) if p))
    try:
        rev = history.mark(project, "rtl")
        if rev is None:
            return [f"child project {child.label} has not passed its own rtl flow (no history mark passed/rtl)"]
        changed = history.changed_since(project, rev, paths)
    except history.HistoryError as exc:
        return [f"child project {child.label}: its history cannot be read ({exc})"]
    return [f"child project {child.label}: {', '.join(changed)} changed since it passed its rtl flow (passed/rtl {rev[:8]})"] if changed else []


def rtl_dir(engine) -> Path:
    """This project's RTL folder, where the copies are: the rtl step's (its option `rtl_dir`) when the flow has that step, else the project's."""
    lay = getattr(engine.by_name.get("rtl"), "lay", None)  # (a step named rtl of another kind, as in the test flow, has no layout)
    return lay(engine).rtl if callable(lay) else engine.project.rtl_dir


def protected(engine) -> dict[Path, str]:
    """Every file no LLM stage may write here (R3), with why: the copies (naming the project that owns each) and the registry. {} without
    child projects; a damaged registry: only itself (which files are copies is not known then, and the copy stops on it)."""
    rtl = rtl_dir(engine)
    records, damage = _registry(rtl)
    if damage:  # (it runs before every LLM stage: it never fails one; the copy stops on the damage)
        return {registry_file(rtl): f"Q3TUI's record of the files copied from the child projects, damaged ({damage}): delete it, then run "
                                    "this project again"}
    out: dict[Path, str] = {}
    for e in records.values():
        f = e.get("file")
        if isinstance(f, str) and f and f == Path(f).name:
            owner = e.get("owner") or e.get("child") or "?"
            out[rtl / f] = f"a file of the child project {owner}, copied here unchanged (fix it in {owner}, then run this project again)"
    if registry_file(rtl).is_file():
        out[registry_file(rtl)] = "Q3TUI's record of the files copied from the child projects (code writes it)"
    return out


def units_by_owner(project_root: Path, entries: list[str]) -> dict[str, str]:
    """unit -> the project that owns it, before any copy or without one (parse, a change request): a child's units are named by its filelist (a
    file holds the unit it is named after), the owner of what it got from its own children by its registry. Never raises."""
    base = _real(project_root)
    out: dict[str, str] = {}
    for child in resolve(base, entries):
        if child.root == base:
            continue
        try:
            owners = _owners(child)
        except ChildError:  # (its damaged registry: the copy stops on it; its own files are still its own)
            owners = {}
        for line in _lines(child):
            f = _path(child, line)
            if line.startswith(("+", "-")) or f.suffix.lower() not in _HDL:
                continue
            owner_root = owners.get(f.stem, (child.root, None))[0]
            if owner_root != base:  # (a cycle: the copy stops on it)
                out.setdefault(f.stem, _label(base, owner_root))
    return out


# -- paths ------------------------------------------------------------------------------------------------------------

def _label(base: Path, path: Path) -> str:
    """A path as messages and the registry give it: relative to this project (`../cha`, `../cha/src/rtl/m_cha_top.sv`)."""
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # (another drive)
        return path.as_posix()


def _inside(child: Child, path: Path) -> str:
    """A path of a child as its own project names it (relative to its root), else as it is."""
    return _under(child.root, path) or str(path)


def _under(root: Path, path: Path) -> str | None:
    try:
        return _real(path).relative_to(root).as_posix()
    except ValueError:
        return None


def _real(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):  # (a symlink loop)
        return Path(os.path.abspath(path))
