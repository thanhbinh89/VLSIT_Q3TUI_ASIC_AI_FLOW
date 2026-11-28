"""ASIC step `asic_lec`: Formality proves the netlist equals the RTL (tool role `asic_lec`). `schemas/asic_lec.json` holds the result,
`build/syn/asic/reports/lec.rpt` the matched / failing points. A tool that is not there, or a run without a verdict, is a failed step.

The reference is the RTL as asic_synth read it: a memory module's target body (`bodies` of asic_synth) is the reference too, and a black box
(`blackbox` of asic_synth) is a black box on both sides (its stub and its netlist design read as interfaces), matched by its pins."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import anyio

from q3tui.eda import tools as toolmod
from q3tui.eda.base import ToolUnavailable
from q3tui.flows.asic.lib import fm
from q3tui.flows.asic.lib.base import LEC_JSON, SYNTH_JSON, AsicStep
from q3tui.pipeline.base import StepContext, StepFailed


class Step(AsicStep):
    name = "asic_lec"
    title = "Logic equivalence check"
    deps = ("asic_synth",)
    tool_roles = ("asic_lec",)
    hashed_options = ("message_filter", "pre_tcl", "extra_tcl", "defines")  # (staleness: these change the result)

    def inputs(self, engine) -> list[Path]:
        net = self.netlist(engine)
        return [self.artifact(engine, SYNTH_JSON), *self.rtl_files(engine), *([net] if net.is_file() else []), *self.body_files(engine, self.synth_options(engine))]

    def synth_options(self, engine) -> dict:
        """asic_synth's options (its bodies and black boxes are the reference's too)."""
        synth = next((s for s in engine.steps if s.kind == "asic.asic_synth"), None)
        return dict(synth.options) if synth else {}

    def black_boxes(self, engine, top: str) -> tuple[list[Path], tuple[list[str], list[str]] | None]:
        """(the reference's files, (its black boxes, their designs in the netlist)) from asic_synth's memory plan; (the RTL, None) without one."""
        from q3tui.flows.fpga.lib import memories as memmod

        opts = self.synth_options(engine)
        if not (opts.get("bodies") or opts.get("blackbox")):
            return self.rtl_files(engine), None
        plan = memmod.make_plan(engine.project, opts, self.rtl_files(engine), top, self.asic_dir(engine) / "stubs", None,
                                min_words=self.min_mem_words(engine))  # (as asic_synth: `blackbox: true` names the same modules)
        if not plan.blackbox:
            return plan.files, None
        net = self.netlist(engine)
        impl = memmod.netlist_modules(net.read_text(errors="replace") if net.is_file() else "", plan.facts.modules, plan.blackbox)
        return plan.files, (plan.blackbox, impl)

    def outputs(self, engine) -> list[Path]:
        p, rep = self.artifact(engine, LEC_JSON), self.reports_dir(engine) / "lec.rpt"
        return [f for f in (p, rep) if f.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        if not self.artifact(engine, SYNTH_JSON).is_file() or not self.netlist(engine).is_file():
            return "the netlist is needed first (step asic_synth)"
        return None

    async def run(self, ctx: StepContext) -> None:
        engine, project = ctx.engine, ctx.project
        top = self.top(engine)
        libs = self.libraries(engine)
        try:
            tools = self.tools(engine)
            if not await anyio.to_thread.run_sync(lambda: toolmod.available(tools, "asic_lec")):
                raise ToolUnavailable("the `asic_lec` tool was not found (module synopsys/formality; see config/tools.json / `q3tui tools check`)")
        except ToolUnavailable as exc:
            raise StepFailed(f"equivalence check not run: {exc}") from exc
        work = self.workdir(engine)
        for n in ("lec.rpt", "fm.log"):
            (work / n).unlink(missing_ok=True)
        svf = self.asic_dir(engine) / "netlist" / f"{top}.svf"
        script = self.asic_dir(engine) / "lec.tcl"
        project.backup([script])
        from q3tui.flows.fpga.lib import memories as memmod

        try:
            files, bb = await anyio.to_thread.run_sync(lambda: self.black_boxes(engine, top))
        except memmod.PlanError as exc:
            raise StepFailed(f"equivalence check not run: {exc}") from exc
        if bb and not bb[1]:
            raise StepFailed(f"equivalence check not run: the netlist has no design of the black box(es) {', '.join(bb[0])} (run asic_synth again)")
        script.write_text(fm.script_text(files, top, libs, self.netlist(engine), svf, work, self.options, bb))
        ctx.emit("log", message=f"checking {top}: RTL against {project.rel(self.netlist(engine))}…")
        t0 = time.monotonic()
        res = await anyio.to_thread.run_sync(lambda: toolmod.run_role(
            tools, "asic_lec", work, {"script": script, "top": top, "workdir": work}, log_name="fm.log"), abandon_on_cancel=True)
        log = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
        result = fm.parse_result(log)
        rep = self.reports_dir(engine)
        rep.mkdir(parents=True, exist_ok=True)
        if (work / "lec.rpt").is_file():
            shutil.copy2(work / "lec.rpt", rep / "lec.rpt")
        self.write(engine, LEC_JSON, {**result, "top": top, "netlist": project.rel(self.netlist(engine)), "svf": project.rel(svf) if svf.is_file() else None,
                                      "runtime_s": round(time.monotonic() - t0, 1), "log": project.rel(Path(res.log_path)) if res.log_path else None})
        if result.get("unsuppressed"):  # not hidden for you: an index out of bounds etc. is the RTL's to look at, or yours to accept
            codes = ", ".join(result["unsuppressed"])
            raise StepFailed(f"Formality stopped reading the RTL on {codes} (see {project.rel(Path(res.log_path))}). Fix the RTL, or accept the message: "
                             f"options.message_filter: [{result['unsuppressed'][0]}] in the flow file / q3tui.yaml (step_options.asic_lec).")
        if result["status"] == "unknown":
            toolmod.check_broken(res, "asic_lec")
            raise StepFailed(f"Formality gave no verdict (log: {res.log_path})")
        if result["status"] != "equivalent":
            raise StepFailed(f"the netlist is {result['status'].replace('_', ' ')} to the RTL ({result.get('failing', '?')} failing point(s)): see build/syn/asic/reports/lec.rpt")
        if result.get("rtl_messages"):
            ctx.emit("warning", message="Formality: RTL interpretation messages were produced (accepted by options.message_filter): the result can differ from a simulator's")
        ctx.emit("log", message=f"equivalent: {result.get('passing', '?')} compare points pass ({time.monotonic() - t0:.0f} s)")
