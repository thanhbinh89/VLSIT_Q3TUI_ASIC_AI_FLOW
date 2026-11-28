"""The memories of a design, read with pyslang (code, ground truth): every instance with the unpacked arrays it holds — at the size its own
parameters give them —, the file that defines each module, and what a port-only stub of a module needs (its header as written).

The fpga and asic flows use it for memory modules (docs/spec/fpga-flow.md "Memories"): a target body in place of the behavioural one, a
black box, the logic and the memories reported apart, and the stop before a tool builds a big array from registers.

Instance paths are written the way the tools write them: instances separated by `/`, a generate scope before its instance with a `.`
(`u_wr_ctx_0/u_sram_dp`, `gen_rd_ctx[0].u_rd_ctx/u_sram_tp`); the top itself has the path ""."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Array:
    name: str  # the variable (`reg_mem`)
    words: int  # all its unpacked dimensions together
    width: int  # bits of one word

    @property
    def bits(self) -> int:
        return self.words * self.width


@dataclass
class Instance:
    path: str  # from the top, as the tools write it ("" for the top)
    module: str
    arrays: list[Array] = field(default_factory=list)

    def bits(self, min_words: int = 1) -> int:
        return sum(a.bits for a in self.arrays if a.words >= min_words)


@dataclass
class Facts:
    top: str
    instances: list[Instance] = field(default_factory=list)  # every instance of the design, the top first
    files: dict[str, str] = field(default_factory=dict)  # module → the file that defines it
    file_modules: dict[str, list[str]] = field(default_factory=dict)  # file → every module, interface, package it defines
    headers: dict[str, str] = field(default_factory=dict)  # module → its header (`module … );`) and the port / parameter declarations of a non-ANSI one
    ports: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # module → [(direction, name)]
    clocks: dict[str, list[str]] = field(default_factory=dict)  # module → its clock and reset ports (its edges, reset-named ports)
    error: str = ""  # why the design could not be read here (the rest is then empty)

    @property
    def modules(self) -> list[str]:
        return sorted(self.files)

    def instances_of(self, module: str) -> list[Instance]:
        return [i for i in self.instances if i.module == module]

    def memory_modules(self, min_words: int = 16) -> list[str]:
        """The modules that hold an unpacked array of `min_words` words or more (in some instance): what Q3TUI proposes as the memory
        modules when the project names none (`project.memory_modules`)."""
        return sorted({i.module for i in self.instances if i.path and any(a.words >= min_words for a in i.arrays)})

    def match(self, names) -> list[str]:
        """The modules of the design that `names` (names or shell patterns, `m_*_sram_*`) stand for, in design order, once each."""
        pats = [str(n).strip() for n in names or [] if str(n).strip()]
        return [m for m in self.modules if any(fnmatch.fnmatchcase(m, p) for p in pats)]


def _scope_name(sym) -> str:
    return str(sym.hierarchicalPath).rsplit(".", 1)[-1]


def _array(sym) -> Array | None:
    t = sym.type
    if not getattr(t, "isUnpackedArray", False):
        return None
    words = 1
    while getattr(t, "isUnpackedArray", False):
        if not getattr(t, "hasFixedRange", False):
            return None  # (dynamic, associative, queue: no fixed size)
        words *= int(t.fixedRange.width)
        t = t.elementType
    width = int(getattr(t, "bitWidth", 0) or 0)
    return Array(sym.name, words, width) if width else None


def header_text(syn, sm=None, cache: dict | None = None) -> str:
    """A module's header as written (`module … );`, without the comments and directives before it), with the port and parameter
    declarations of its body when its ports are not ANSI: everything a port-only stub needs. The header is the file's own text between
    its first and last token (`sm`: the source manager that read it); without one, or for a header no file holds as it is (a macro), the
    header's syntax with its comments removed."""
    from pyslang import syntax

    h = syn.header
    first = h.getFirstToken().rawText
    text = _file_text(h, sm, cache) if sm is not None else ""
    if not text.startswith(first):
        text = _from_first(str(h), first)
    K = syntax.SyntaxKind
    extra = [str(m).strip() for m in syn.members if m.kind in (K.PortDeclaration, K.ParameterDeclarationStatement)] if (
        h.ports is not None and h.ports.kind == K.NonAnsiPortList) else []
    return "\n".join([text.rstrip(), *extra])


def _file_text(node, sm, cache: dict | None) -> str:
    """The text of the file between the node's first and last token (its source range: byte offsets), "" when no one file holds it.
    (Not the syntax's text: the trivia before a header carry its file's banner inside directives such as `timescale, whose raw text is
    empty, and the syntax's text drops blank lines.)"""
    try:
        r = node.sourceRange
        if r.start.buffer != r.end.buffer:
            return ""
        path = str(sm.getFullPath(r.start.buffer))
        data = cache.get(path) if cache is not None else None
        if data is None:
            data = Path(path).read_bytes()
            if cache is not None:
                cache[path] = data
        return data[r.start.offset:r.end.offset].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a header from a macro or an include: the syntax's text then
        return ""


_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _from_first(text: str, first: str) -> str:
    """`text` from its first token (`first`, the keyword) on, its comments removed: what is before the keyword is only trivia."""
    clean = _COMMENT_RE.sub(" ", text)
    m = re.search(r"(?<![\w`$])" + re.escape(first) + r"\b", clean)
    return clean[m.start():] if m else clean.strip()


def scan(files: list[Path], top: str, incdirs: list[Path] | None = None, defines: dict | None = None) -> Facts:
    """The memories of `top` elaborated from `files`; `Facts.error` says why when the design cannot be read here (never an exception)."""
    try:
        return _scan(files, top, incdirs or [], defines or {})
    except Exception as exc:  # noqa: BLE001 - an AST shape this reader does not know: no facts, said so
        return Facts(top, error=f"the design could not be read here ({type(exc).__name__}: {str(exc)[:200]})")


def _scan(files: list[Path], top: str, incdirs: list[Path], defines: dict) -> Facts:
    import pyslang as ps
    from pyslang import ast, parsing, syntax

    from q3tui.hdl.parse import _EDGE_RE, _EDGE_SIG_RE, _RESET_NAME_RE

    sm = ps.SourceManager()
    pp = parsing.PreprocessorOptions()
    pp.additionalIncludePaths = [str(d) for d in incdirs]
    pp.predefines = [f"{k}={v}" if v is not None else k for k, v in defines.items()]
    co = ast.CompilationOptions()
    co.topModules = {top}
    bag = ps.Bag([pp, co])
    comp = ast.Compilation(bag)
    sources = [str(f) for f in files if Path(f).is_file()]
    if not sources:
        return Facts(top, error="no RTL file")
    tree = syntax.SyntaxTree.fromFiles(sources, sm, bag)
    comp.addSyntaxTree(tree)
    errors = [d for d in comp.getAllDiagnostics() if d.isError()]
    tops = list(comp.getRoot().topInstances)
    if errors or not tops or tops[0].name != top:
        first = ps.DiagnosticEngine.reportAll(sm, errors[:1]).strip().splitlines()[0] if errors else f"top module '{top}' not found"
        return Facts(top, error=f"the design does not elaborate here: {first[:300]}")
    facts = Facts(top)
    texts: dict = {}  # (the files' bytes, read once: the modules' headers)

    def resolved(buffer) -> str:
        try:
            f = str(sm.getFullPath(buffer))
        except Exception:  # noqa: BLE001
            return ""
        return str(Path(f).resolve()) if f else ""

    def path_of(sym) -> str:
        return resolved(sym.location.buffer)

    for m in tree.root.members:  # (what every file defines: a file whose module gets another body must define nothing else)
        name = getattr(getattr(getattr(m, "header", None), "name", None), "valueText", None)
        f = resolved(m.sourceRange.start.buffer) if name else ""
        if f:
            facts.file_modules.setdefault(f, []).append(name)

    def module(inst) -> str:
        d = inst.definition
        name = d.name
        if name not in facts.files:
            facts.files[name] = path_of(d)
            if d.syntax is not None:
                facts.headers[name] = header_text(d.syntax, sm, texts)
                text = str(d.syntax)
                ports = [(str(p.direction).split(".")[-1].lower(), p.name) for p in inst.body.portList if hasattr(p, "direction")]
                facts.ports[name] = [("input" if d_ == "in" else "output" if d_ == "out" else d_, n) for d_, n in ports]
                names = {n for _, n in ports}
                edge = {n for ev in _EDGE_RE.findall(text) for _, n in _EDGE_SIG_RE.findall(ev)}
                facts.clocks[name] = sorted(n for n in names if n in edge or _RESET_NAME_RE.search(n))
        return name

    def walk(scope, owner: Instance, prefix: str, depth: int = 0) -> None:
        for m in scope:
            k = m.kind
            if k == ast.SymbolKind.Variable:
                a = _array(m)
                if a is not None:
                    owner.arrays.append(a)
            elif k == ast.SymbolKind.Instance and depth < 64:
                child = Instance(prefix + m.name, module(m))
                facts.instances.append(child)
                walk(m.body, child, child.path + "/", depth + 1)
            elif k == ast.SymbolKind.GenerateBlockArray:
                for blk in m:
                    if blk.kind == ast.SymbolKind.GenerateBlock and not getattr(blk, "isUninstantiated", False):
                        walk(blk, owner, prefix + _scope_name(blk) + ".", depth + 1)
            elif k == ast.SymbolKind.GenerateBlock and not getattr(m, "isUninstantiated", False):
                walk(m, owner, prefix + _scope_name(m) + ".", depth + 1)

    root = Instance("", module(tops[0]))
    facts.instances.append(root)
    walk(tops[0].body, root, "")
    return facts


_NORM = re.compile(r"[^A-Za-z0-9]")


def norm(path: str) -> str:
    """A path as a name the tools agree on: `gen_rd_ctx[0].u_rd_ctx/u_sram` and Design Compiler's `gen_rd_ctx_0__u_rd_ctx_u_sram` are the same."""
    return _NORM.sub("_", path)


def module_of(design: str, modules: list[str]) -> str | None:
    """The RTL module a design name of a tool report stands for: itself, or itself with the tool's suffixes (Vivado `m__parameterized0`,
    `m_0`; Design Compiler `m_PARA_DEPTH64_…`, `m_00000080_…_3`): the longest module name that fits, so `m_x_ctrl_0` is `m_x_ctrl`, not `m_x`."""
    fits = [m for m in modules if design == m or design.startswith(m + "_")]
    return max(fits, key=len) if fits else None
