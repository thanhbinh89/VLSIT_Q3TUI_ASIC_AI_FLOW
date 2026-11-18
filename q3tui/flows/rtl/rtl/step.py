"""Step `rtl` of the RTL flow: the VLSIT `rtl` step (RTL per module, rule check, lint, synthesis, update-not-re-run) writing to
`src/rtl/`, with the project's own rules `config/rtl_rules.md` in front of Q3TUI's default ones.

The rule stack is shared with `style_lint` (flows/rtl/lib/rules.py): the generator writes to the rules the style checker
turns into Verible flags. The user's file is an input: editing it updates the modules (`describe_changes`: "the RTL rules changed").

Modules the `csr` step provides (the APB-CSR-Generator's slave and its CDC synchronizer: `src/rtl/csr/provided.json`) are part of the design: they
are in the plan (the parent instantiates them, reads their ports, is updated when they change), in the filelist, the design lint and the synthesis,
but this step never writes or rule-checks them (KI-1 of docs/spec/rtl-flow.md). Their lint findings are fixed in place by a small session that keeps the
module's name and ports (option `keep_generated: true`: reported only, never edited); the csr step keeps the fixed file while the generator makes the same
module. A change request scoped to a generated module (`[rtl:m_soc_csr] …`: the dv flow's simulation triage, or yours) is applied in place the same way.
Every change to a generated module (these, an edit by hand) is recorded as a patch with its why (`src/rtl/csr/patches/<module>.patch` / `.md`,
flows/rtl/lib/patches.py): when the register map changes, the csr step makes the module again and applies the patch to it.

The modules of child projects (FR-01, option `child_projects`; docs/spec/rtl-flow.md "Child projects (provided RTL)", flows/rtl/lib/children.py)
are provided as well, but read-only whatever `keep_generated` says: they passed the rule check, REQ tags and lint of their own project, and a
problem in one is fixed there. Every run first copies the files each child's `filelist.f` lists, byte for byte (registry
`src/rtl/child/provided.json`), so that the plan, the checks and the synthesis read the children as they are now. The copies are in the plan
with the instances their own project gave them (the own modules the spec names instantiate them, else the top does), first in the filelist,
in the design lint and the synthesis; this step never writes, rule-checks, REQ-tags or fixes them. The children's files, the registry and the
copies are inputs: a child's change makes the step stale and the next run copies it again, a copy edited here is restored. An own module that
instantiates a child module follows its interface (`children.header`), not its body: a fix inside the child writes nothing here; the children's
packages and interfaces it may take types from are read with it (its lint) and by their text (a change updates it). A lint finding or a
synthesis error located in a child's unit is reported for the project that owns it, never sent to a fix round: the lint finding as a warning
(counted in the report), the synthesis error by failing the step. A change request scoped to a child's unit is refused, naming that project
(`refuse_change`)."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import sha256_file, sha256_json

from q3tui.flows.rtl.lib import children, csr, patches, rules
from q3tui.flows.rtl.rtl.plan import PlanModule, RtlPlan, _names, dependencies
from q3tui.flows.rtl.rtl.gen import RtlGenStep
from q3tui.pipeline.base import StepContext, StepFailed
from q3tui.steps.common import split_feedback


class Step(RtlGenStep):
    title = "RTL (rules, lint, synthesis)"

    def rule_files(self) -> list[str]:
        return rules.stack(getattr(self, "_project", None), super().rule_files())

    def _provided(self, engine) -> dict[str, dict]:
        """Every module this step does not write: the csr step's and the child projects' copies. A damaged child registry counts as none here:
        inputs and outputs read this on every TUI refresh and must not fail it, and `missing_input` reports the damage (the step never runs on it)."""
        rtl = self.lay(engine).rtl
        try:
            return children.read_all_provided(rtl)
        except children.ChildError:
            return csr.read_provided(rtl)

    def inputs(self, engine) -> list[Path]:
        self._project = engine.project
        user = rules.user_file(self._project)
        rtl = self.lay(engine).rtl
        prov = csr.provided_file(rtl)
        return [*super().inputs(engine), *([user] if user.is_file() else []), *([prov] if prov.is_file() else []),
                *[rtl / f"{n}.sv" for n in csr.read_provided(rtl) if (rtl / f"{n}.sv").is_file()], *self._child_inputs(engine)]

    def _child_inputs(self, engine) -> list[Path]:
        """What the copies are made from, and the copies (R7): each child's q3tui.yaml, filelist.f and listed files (changed: stale, the run
        copies again), the registry and the copies here (edited by hand: stale, the run restores them). None without the option and without
        an earlier copy (R10)."""
        rtl = self.lay(engine).rtl
        reg = children.registry_file(rtl)
        entries = children.configured(engine)
        if not entries and not reg.exists():
            return []
        try:
            records = children.read_registry(rtl)
        except children.ChildError:  # (damaged: the file itself is still an input, and missing_input says what is wrong)
            records = {}
        return [*children.input_files(children.resolve(engine.project.root, entries)), *([reg] if reg.is_file() else []),
                *_copies(rtl, records).values()]

    def outputs(self, engine) -> list[Path]:
        own = self._provided(engine)  # (the csr step's files are its own outputs, the child projects' copies are inputs: not this step's)
        return [f for f in super().outputs(engine) if not (f.suffix == ".sv" and f.stem in own)]

    def missing_input(self, engine) -> str | None:
        found = super().missing_input(engine)
        if found is not None:
            return found
        entries = children.configured(engine)
        problems = children.problems(engine.project.root, children.resolve(engine.project.root, entries)) if entries else []
        if problems:
            return "; ".join(problems)
        try:
            children.read_registry(self.lay(engine).rtl)
        except children.ChildError as exc:  # (which files are copies is not known: the step waits rather than take them for its own)
            return str(exc)
        return None

    async def run(self, ctx: StepContext) -> None:
        self._project = ctx.project
        rtl = self.lay(ctx.engine).rtl
        self._generated_now = set(csr.read_provided(rtl))  # (the csr step's modules: generated, fixed in place; the children's: `_child_registry`)
        self._children_now, self._copied_now = self._copy_children(ctx, rtl, self._generated_now)  # (first: everything after reads the copies)
        ctx.feedback = self._not_refused(ctx)
        await super().run(ctx)

    @property
    def _child_registry(self) -> dict[str, dict]:
        """The child projects' units of this run: unit -> its registry record, as the copy left it ({} before a run, and without children)."""
        return getattr(self, "_children_now", {})

    def refuse_change(self, engine, text: str) -> str | None:
        """R3: a change request scoped to a child project's unit (`[rtl:m_cha_top] …`: yours, or a flow's hand-off) is not this project's to
        make: refused, naming the project to make it in. The units are the registry's; before the first copy (or with a damaged registry)
        the children's filelists name them."""
        by_unit, _ = split_feedback([text.strip()], self.name)
        unit = next(iter(by_unit), None)
        if unit is None:
            return None
        try:
            kids = children.read_registry(self.lay(engine).rtl)
        except children.ChildError:
            kids = {}
        if not kids:
            kids = {u: {"owner": owner} for u, owner in children.units_by_owner(engine.project.root, children.configured(engine)).items()}
        return children.refusal(unit, kids[unit]) if unit in kids else None

    def _not_refused(self, ctx: StepContext) -> list[str]:
        """The change requests this run acts on: one scoped to a child's unit is dropped with a warning saying where to make it. It can be
        queued without `request_change` (written into pipeline.json, or queued before the unit was a child's). A new list: the engine empties
        its queue by the list it gave the run, so a refused request leaves the queue too."""
        kept = []
        for f in ctx.feedback:
            why = self.refuse_change(ctx.engine, f)
            if why:
                ctx.emit("warning", message=f"change request refused: {why}")
            else:
                kept.append(f)
        return kept

    def _copy_children(self, ctx: StepContext, rtl: Path, own: set[str]) -> tuple[dict[str, dict], set[str]]:
        """The child projects' files, copied into the RTL folder (R2): (the registry, the units whose copy is new this run). No work at all
        without the option and without an earlier copy (R10); with the option removed, the earlier copies and the registry go (backed up)."""
        entries = children.configured(ctx.engine)
        if not entries and not children.registry_file(rtl).exists():
            return {}, set()
        kids = children.resolve(ctx.project.root, entries)
        try:
            res = children.copy(ctx.project, rtl, kids, own)
            status = [w for c in kids for w in children.status_warnings(c, children.listed(c))]
        except children.ChildError as exc:
            raise StepFailed(str(exc)) from exc
        if res.copied:
            ctx.emit("log", message=f"child projects: copied {', '.join(res.copied)}")
        for unit in res.restored:
            e = res.registry[unit]
            ctx.emit("warning", message=f"{unit}: its copy here was changed — restored from {e['source']} (a file of the child project "
                                        f"{e['owner']} is read-only here: fix it in {e['owner']}, then run this project again)")
        if res.removed:
            ctx.emit("log", message=f"child projects: {', '.join(res.removed)} removed (no child project provides them any more; backed up)")
        for w in [*res.warnings, *status]:
            ctx.emit("warning", message=w)
        built = ctx.engine.state.step_meta.setdefault(self.name, {}).setdefault("modules", {})
        for unit in res.registry:  # (a module this step wrote before a child provided it: its build record is not the copy's)
            built.pop(unit, None)
        return res.registry, set(res.copied)

    async def _plan(self, ctx: StepContext, spec: dict, reqs: dict[str, str], spec_files: list[str]) -> RtlPlan:
        """The plan, with the provided modules in it: the child projects' (`_with_children`); the parent instantiates the CSR (and the CSR its
        synchronizer)."""
        plan = await super()._plan(ctx, spec, reqs, spec_files)
        kids = self._child_registry
        if kids:
            plan = self._with_children(ctx, plan, spec, kids)
        prov = csr.read_provided(self.lay(ctx.engine).rtl)
        if not prov:
            return plan
        mods = {m.name: m for m in plan.modules}
        for name, info in prov.items():
            pm = mods.get(name) or PlanModule(name=name, description="generated by the APB-CSR-Generator (provided: not written by this step)")
            pm.instances = list(dict.fromkeys([*pm.instances, *info.get("instances", [])]))
            pm.req_ids = list(dict.fromkeys([*pm.req_ids, *[r for r in info.get("req_ids", []) if r in reqs]]))
            mods[name] = pm
        for name, info in prov.items():
            if info.get("kind") != "csr":
                continue
            parent = info.get("parent") if info.get("parent") in mods else plan.top
            if name != parent:
                mods[parent].instances = list(dict.fromkeys([*mods[parent].instances, name]))
        return RtlPlan(top=plan.top, modules=list(mods.values()))

    def _with_children(self, ctx: StepContext, plan: RtlPlan, spec: dict, kids: dict[str, dict]) -> RtlPlan:
        """The child projects' units in the plan (R5): read-only, with the instances their own project gave them. The own modules instantiate
        them as the spec's module map says (plan_from_spec dropped those names: the map has no entry for them); a child's top that no module
        instantiates is the top's. A requirement the plan gave a child unit moves to the own module that instantiates it, else to the top:
        REQ tags go only into this project's files."""
        if plan.top in kids:
            raise StepFailed(f"the top module {plan.top} is a module of the child project {kids[plan.top]['owner']}: this project's top must be "
                             "its own module, the one that instantiates the child projects' tops (top_module of the spec: step parse)")
        mods = {m.name: m for m in plan.modules}
        own = [m for m in plan.modules if m.name not in kids]
        raw = _spec_instances(spec)
        for m in own:
            m.instances = list(dict.fromkeys([*m.instances, *[i for i in raw.get(m.name, []) if i in kids]]))
        moved = {u: mods[u].req_ids for u in kids if u in mods and mods[u].req_ids}
        for unit, e in kids.items():
            mods[unit] = PlanModule(name=unit, description=f"provided by the child project {e['owner']} (read-only)",
                                    instances=list(e.get("instances") or []))
        used = {i for m in mods.values() for i in m.instances}
        for unit, e in kids.items():
            if e.get("top") and unit not in used:
                mods[plan.top].instances.append(unit)
                ctx.emit("log", message=f"the spec names no module that instantiates {unit}: the top {plan.top} does")
        for unit, rids in moved.items():
            target = next((m.name for m in own if unit in m.instances), plan.top)
            mods[target].req_ids = list(dict.fromkeys([*mods[target].req_ids, *rids]))
            for rid in rids:
                ctx.emit("warning", message=f"{rid} is on {unit}, a module of the child project {kids[unit]['owner']}: moved to {target}")
        return RtlPlan(top=plan.top, modules=list(mods.values()))

    # -- the child projects' units in the design (hooks of RtlGenStep) ------------------------------------------------------

    def _unit_files(self, engine) -> dict[str, Path]:
        return _copies(self.lay(engine).rtl, self._child_registry)

    def _read_only_units(self, run) -> dict[str, str]:
        return {u: f"a module of the child project {e['owner']}: fix it in {e['owner']}, then run this project again"
                for u, e in self._child_registry.items()}

    def _order(self, run) -> list[str]:
        """The child projects' files first, in the order their own filelists give (a child's packages before what uses them), then this
        project's (R5: a module after the modules it instantiates)."""
        kids = list(self._child_registry)
        return [*kids, *[n for n in super()._order(run) if n not in kids]]

    def _child_types(self, deps: list[str]) -> list[str]:
        """The child projects' packages and interfaces (registry `unit` other than module), in the registry's order, for a module whose
        dependencies `deps` hold a child unit: a child module's ports may be of their types (its header imports them), and a package not
        named *_pkg is no dependency of the plan. [] for a module that uses no child unit."""
        kids = self._child_registry
        if not any(d in kids for d in deps):
            return []
        return [u for u, e in kids.items() if e.get("unit", "module") != "module"]

    def _lint_units(self, run, module: str) -> list[str]:
        """The child projects' packages and interfaces first (a child module's header needs them), then the rest of what `module` depends on
        (a package named *_pkg is in both: listed once)."""
        deps = super()._lint_units(run, module)
        types = self._child_types(deps)
        return [*types, *[d for d in deps if d not in types]]

    def _child_ref(self, run, name: str) -> str:
        e = self._child_registry.get(name)
        ref = super()._child_ref(run, name)
        return f"{ref} (dự án con {e['owner']}: chỉ đọc, không sửa)" if e else ref

    def _report_extra(self, run) -> dict:
        """R12: the child projects' modules, apart from this project's own (`modules`), each with its owner, the file it is a copy of and the
        design lint's findings in it (`_full_lint`: reported, not fixed here)."""
        kids = self._child_registry
        if not kids:
            return {}
        rel = run.ctx.project.rel
        return {"child_modules": {u: {"file": rel(run.file(u)), "owner": e["owner"], "child": e["child"], "source": e["source"],
                                      "sha256": e["sha256"], "lint_findings": (run.lint_state.get(u) or {}).get("lint_findings", 0)}
                                  for u, e in kids.items()}}

    def _keep_generated(self) -> bool:
        return bool(self.options.get("keep_generated"))

    def _snapshot(self, run, pm: PlanModule) -> dict:
        snap = super()._snapshot(run, pm)
        for c in set(snap["children"]) & getattr(self, "_generated_now", set()):  # a generated child: its ports, not its text
            f = run.file(c)  # (a lint fix inside the CSR module is no change for the parent that instantiates it)
            snap["children"][c] = sha256_json(csr.ports(f.read_text())) if f.is_file() else ""
        kids = self._child_registry
        for c in set(snap["children"]) & set(kids):  # a child project's module: its interface, not its body (a fix inside it is no change here)
            f = run.file(c)
            if kids[c].get("unit", "module") == "module" and f.is_file():  # (a package, an interface: by text; header() ends at their first `;`)
                snap["children"][c] = sha256_json(children.header(f.read_text(), c))
        for c in self._child_types(dependencies(run.plan, pm.name)):  # the children's packages and interfaces, by text: a type it may use
            if c not in snap["children"]:  # (a package named *_pkg is counted already)
                f = run.file(c)
                snap["children"][c] = sha256_json(f.read_text()) if f.is_file() else ""
        return snap

    def _check_module(self, run, pm: PlanModule) -> list[str]:
        if pm.name in self._child_registry:  # (checked in its own project, never here: R4)
            run.lint_state[pm.name] = {"status": "provided"}
            return []
        if pm.name not in getattr(self, "_generated_now", set()):
            return super()._check_module(run, pm)
        # generated: lint only (the RTL rules and REQ tags are not the generator's, KI-1 of docs/spec/rtl-flow.md)
        problems, warnings, ran = self._lint(run, pm.name)
        run.lint_state[pm.name] = {"status": "not run" if not ran else ("fail" if problems else "pass"), "warnings": warnings}
        return problems

    async def _work(self, run, pm: PlanModule, todo: bool) -> list[str]:
        if pm.name in self._child_registry:  # a child project's module: copied as it is, never written, checked or fixed here (R3, R4)
            run.lint_state[pm.name] = {"status": "provided"}
            if pm.name in getattr(self, "_copied_now", set()):
                run.wrote.add(pm.name)  # (a new copy is a new design: synthesis runs again, the result is not the one you approved)
            return []
        if pm.name not in getattr(self, "_generated_now", set()):
            return await super()._work(run, pm, todo)
        ctx, path = run.ctx, run.file(pm.name)
        if not path.is_file():
            raise StepFailed(f"{pm.name} is provided by the csr step but {ctx.project.rel(path)} does not exist: run the csr step")
        asked = run.feedback_unit.get(pm.name, [])
        if self._keep_generated():
            if asked:
                ctx.emit("warning", message=f"{pm.name} (generated): {len(asked)} change request(s) not applied — Settings → rtl \"Never edit generated RTL\" "
                                            f"is on: fix the generator or the spec, or turn it off: " + "; ".join(a[:120] for a in asked))
            ctx.emit("log", message=f"{pm.name}: provided by the csr step (generated: not written or checked here)")
            return []
        if patches.unrecorded(ctx.project, run.lay.rtl, pm.name):
            patches.record(ctx.project, run.lay.rtl, pm.name, ["changed outside the steps (by hand)"])
        await self._change_generated(run, pm, asked)
        return await self._lint_generated(run, pm)

    async def _change_generated(self, run, pm: PlanModule, asked: list[str]) -> None:
        """The change requests for a generated module, applied in place (name, ports and registers kept) and recorded in its patch."""
        from q3tui.flows.rtl.rtl import task as rtl_prompts

        if not asked:
            return
        ctx, path = run.ctx, run.file(pm.name)
        ctx.emit("log", message=f"{pm.name} (generated): applying {len(asked)} change request(s) in place")
        patches.record(ctx.project, run.lay.rtl, pm.name, [])  # (the base, for a module generated before patches were kept)
        await ctx.llm(self._stage(run, pm, rtl_prompts.change_generated_prompt(module=pm.name, path=ctx.project.rel(path), requests=asked,
                                                                               code=path.read_text())))
        run.wrote.add(pm.name)
        self._own_edit(ctx, path)
        patches.record(ctx.project, run.lay.rtl, pm.name, [f"change request: {a}" for a in asked])

    async def _lint_generated(self, run, pm: PlanModule) -> list[str]:
        import anyio

        ctx = run.ctx
        problems = await anyio.to_thread.run_sync(lambda: self._check_module(run, pm))
        if not problems:
            ctx.emit("log", message=f"{pm.name}: provided by the csr step, lint clean")
            return []
        # never written from the spec here (the csr step makes it); its lint findings are fixed in place, like any module's
        for attempt in range(ctx.cfg.rtl.max_fix_attempts):
            ctx.emit("warning", message=f"{pm.name} (generated): {len(problems)} lint problem(s); fixing …")
            await self._fix(run, pm, problems)
            problems = await anyio.to_thread.run_sync(lambda: self._check_module(run, pm))
            if not problems:
                ctx.emit("log", message=f"{pm.name}: clean")
                break
        else:
            ctx.emit("warning", message=f"{pm.name} (generated): still {len(problems)} problem(s) after {ctx.cfg.rtl.max_fix_attempts} fix round(s)")
        return problems

    async def _fix(self, run, pm: PlanModule, problems: list[str]) -> None:
        if pm.name in self._child_registry:  # (read-only here: a child's problem is fixed in its own project)
            return
        if pm.name not in getattr(self, "_generated_now", set()):
            await super()._fix(run, pm, problems)
            return
        if self._keep_generated():
            return
        from q3tui.flows.rtl.rtl import task as rtl_prompts

        ctx, path = run.ctx, run.file(pm.name)
        patches.record(ctx.project, run.lay.rtl, pm.name, [])  # (the base, for a module generated before patches were kept)
        await ctx.llm(self._stage(run, pm, rtl_prompts.fix_prompt(
            module=pm.name, path=ctx.project.rel(path), problems=rtl_prompts.diagnostics_text(problems),
            code=path.read_text() if path.is_file() else "", reqs=[], generated=True)))
        run.wrote.add(pm.name)
        self._own_edit(ctx, path)
        patches.record(ctx.project, run.lay.rtl, pm.name, ["lint fix: " + "; ".join(str(p)[:160] for p in problems[:4]) + (f" (+{len(problems) - 4} more)" if len(problems) > 4 else "")])

    @staticmethod
    def _own_edit(ctx, path: Path) -> None:
        """The csr step's output, changed here: not your edit (the csr step keeps it while the tool makes the same module)."""
        rel = ctx.project.rel(path)
        for rec in ctx.engine.state.steps.values():
            if rel in rec.outputs and path.is_file():
                rec.outputs[rel] = sha256_file(path)

    def _full_lint(self, run) -> dict[str, list[str]]:
        per = self._child_findings(run, super()._full_lint(run))
        if not self._keep_generated():
            return per  # (findings in the generated modules go to their fix like any module's)
        for name in getattr(self, "_generated_now", set()) & set(per):
            run.ctx.emit("warning", message=f"{name} (generated by the APB-CSR-Generator): {len(per[name])} lint finding(s), not fixed here — "
                                            + "; ".join(per.pop(name)[:2]))
        return per

    def _child_findings(self, run, per: dict[str, list[str]]) -> dict[str, list[str]]:
        """R4: the design lint's findings located in a child project's unit (`per` is keyed by the file they are in, without its suffix) are
        reported for the project that owns it, one warning per unit, counted in the report's `child_modules`, and taken out: they are fixed
        there, never in a fix round here. With only those, the design lint passes: what this project wrote is clean."""
        kids = self._child_registry
        if not kids:
            return per
        for u, e in kids.items():
            found = per.pop(run.file(u).stem, [])
            run.lint_state.setdefault(u, {"status": "provided"})["lint_findings"] = len(found)
            if found:
                owner = e["owner"]
                run.ctx.emit("warning", message=f"{u} (child project {owner}): {len(found)} lint finding(s), not fixed here — fix it in {owner}, "
                                                f"then run this project again: " + "; ".join(found[:2])
                                                + (f" (+{len(found) - 2} more)" if len(found) > 2 else ""))
        if not per:  # (the base failed the design lint on the children's findings alone, or passed it already)
            run.lint_state["__design__"]["status"] = "pass"
        return per


def _copies(rtl: Path, records: dict[str, dict]) -> dict[str, Path]:
    """unit -> its copy in the RTL folder, by the name the registry records (a plain file name: never a path out of the folder)."""
    return {u: rtl / f for u, e in records.items() if isinstance(f := e.get("file"), str) and f and f == Path(f).name}


def _spec_instances(spec: dict) -> dict[str, list[str]]:
    """module -> the instances the spec's module map gives it, read as plan_from_spec reads them but before it drops the names that are not
    modules of the map (the child projects' units are not)."""
    return {name: _names(m.get("instances") or m.get("children")) for m in spec.get("modules") or []
            if isinstance(m, dict) and (name := m.get("name") or m.get("module"))}
