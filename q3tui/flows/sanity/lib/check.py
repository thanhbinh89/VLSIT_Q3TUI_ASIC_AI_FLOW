"""The check after a waiver review (steps `cdc_check`, `rdc_check`): the review's waivers become `waive_cdc` / `waive_rdc` commands, the tool
runs again with them sourced after the check, and what is left open must be exactly the violations the review did not waive — the count and which
ones. 111 violations with 110 waived must end with 1 open; 0 open when all are waived. Anything else fails the step with what differs.
Code only, no LLM."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

import anyio

from q3tui.eda import tools as toolmod
from q3tui.eda.base import ToolUnavailable
from q3tui.flows.sanity.lib import table, vcstatic, waivers
from q3tui.flows.sanity.lib.analysis import AnalysisStep
from q3tui.flows.sanity.lib.base import check_json, report_json, waivers_json
from q3tui.pipeline.base import StepContext, StepFailed


def summary(r: dict) -> str:
    text = f"{r['role'].upper()}: {r['waived']}/{r['total']} waived, {r['open']} open (expected {r['expected_open']})"
    unused = "; ".join(f"{w['waiver']} ({', '.join(w.get('ids') or []) or 'not Q3TUI'}" + (f": {w['why']}" if w.get("why") else "") + ")"
                       for w in r.get("not_applied") or [])
    unused = f"; waivers that matched no violation: {unused}" if unused else ""
    if r["ok"]:
        return text + unused
    old = ("; the report was read by a Q3TUI before nested objects were read: run step " + r["role"] + " again") if r.get("old_report") else ""
    return text + "; MISMATCH — " + "; ".join(x for x in (
        f"waived but still reported: {', '.join(r['still_open_but_waived'])}" if r["still_open_but_waived"] else "",
        f"open but not expected: {', '.join(r['waived_but_not_applied'])}" if r["waived_but_not_applied"] else "",
        f"no waive command for: {', '.join(r['not_expressed'])}" if r["not_expressed"] else "",
        f"count {r['open']} ≠ {r['expected_open']}" if r["open"] != r["expected_open"] else "") if x) + unused + old


def not_applied(work: Path, role: str, waive: Path, log: str, plan: list[tuple[str, list[str], str]]) -> list[dict]:
    """The waivers of the run that matched no violation (`<role>_not_applied.rpt` the script writes, vcstatic.parse_not_applied), each with
    the violations it was written for and what the tool said about it in the log (`CDC_WAIVE_OPTION_INVALID: …`)."""
    rpt = work / f"{role}_not_applied.rpt"
    if not rpt.is_file():
        return []
    ids = {name: list(i) for name, i, _cmd in plan}
    said = vcstatic.waive_warnings(log, waive.name)
    return [{"waiver": w["waiver"], "ids": ids.get(w["waiver"], []), "why": said.get(w["line"], ""), "select": w["select"]}
            for w in vcstatic.parse_not_applied(rpt.read_text(errors="replace"))]


class CheckStep(AnalysisStep):
    hashed_options = ("extra_tcl", "resets", "blackbox")
    tool_fix_options: dict[str, str] = {}  # (the check runs the analysis step's script: its options are set there)

    def inputs(self, engine) -> list[Path]:
        return [self.artifact(engine, report_json(self.role)), self.artifact(engine, waivers_json(self.role)), self.filelist(engine),
                *self.rtl_files(engine), *self.sdc_files(engine)]

    def outputs(self, engine) -> list[Path]:
        return [p for p in (self.artifact(engine, check_json(self.role)), self.waive_script(engine)) if p.is_file()]

    def reset_files(self, engine) -> list[Path]:
        return self.outputs(engine)

    def missing_input(self, engine) -> str | None:
        if not self.artifact(engine, waivers_json(self.role)).is_file():
            return f"the waiver review is needed first (step {self.role}_waivers)"
        return None

    def apply(self, engine) -> dict:
        """Run the tool with the review's waivers and compare. Raises StepFailed when the tool cannot run; the verdict is `ok` in the result."""
        project, role = engine.project, self.role
        report, plan = self.read(engine, report_json(role), {}) or {}, self.read(engine, waivers_json(role), {}) or {}
        if not report or not plan:
            raise StepFailed(f"run {role} and {role}_waivers first: there is nothing to apply")
        base = {"role": role, "waivers": project.rel(self.waive_script(engine)), "rtl": plan.get("rtl", ""), "runtime_s": 0, "total": len(report.get("violations", []))}
        if not report.get("violations"):  # (a clean analysis: nothing to waive, nothing may be left)
            out = {**base, "waived": 0, "open": 0, "expected_open": 0, "not_expressed": [], "still_open_but_waived": [], "waived_but_not_applied": [], "ok": True}
            self.write(engine, check_json(role), out)
            return out
        tcl, skipped = waivers.waive_tcl(report["violations"], plan["items"], plan.get("rtl", ""), role)
        waive = self.waive_script(engine)
        waive.parent.mkdir(parents=True, exist_ok=True)
        waive.write_text(tcl)
        try:
            tools = self.tools(engine)
            if not toolmod.available(tools, role):
                raise ToolUnavailable(f"the `{role}` tool was not found (VC Static: `vc_static_shell` on PATH, or the role's cmd in config/tools.json; `q3tui tools check`, docs/user_guide.md#setting-up-the-eda-tools-for-your-site)")
        except ToolUnavailable as exc:
            raise StepFailed(f"{role.upper()} not run: {exc}") from exc
        work = self.workdir(engine)
        for old in work.glob("*.rpt"):
            old.unlink()
        script = work / f"{role}_waive_setup.tcl"  # (the analysis script with the waivers sourced after the check, the reports in this step's folder)
        script.write_text(self.script_text(engine, report.get("top") or self.top(engine), work, waive))
        if stale := vcstatic.clear_stale_lock(work):
            engine.bus.emit("log", self.name, message=f"{role.upper()}: {stale}")
        t0 = time.monotonic()
        res = toolmod.run_role(tools, role, work, {"script": script, "top": report.get("top"), "filelist": self.filelist(engine),
                                                   "files": self.rtl_files(engine), "workdir": work})
        toolmod.check_broken(res, role)
        rpt = work / f"{role}_violations.rpt"
        if not res.ok or not rpt.is_file():
            raise StepFailed(f"the {role.upper()} tool did not produce the report with the waivers applied (log: {res.log_path})")
        left, _ = vcstatic.parse_report(rpt.read_text(errors="replace"))
        by_id = {i["id"]: i for i in plan["items"]}
        # (a report read before nested objects were read keeps the identity it was reviewed with, on both sides)
        old = any("nested" not in v for v in report["violations"]) and any("nested" in v for v in left)
        sig = waivers.legacy_signature if old else waivers.signature
        want = Counter(sig(v) for v in report["violations"] if by_id.get(v["id"], {}).get("action") != "waive")
        got = Counter(sig(v) for v in left)
        ids: dict[tuple, list[str]] = {}
        for v in report["violations"]:
            ids.setdefault(sig(v), []).append(v["id"])
        total = len(report["violations"])
        log = Path(res.log_path).read_text(errors="replace") if res.log_path and Path(res.log_path).is_file() else ""
        out = {**base, "runtime_s": round(time.monotonic() - t0, 1), "waived": total - sum(want.values()), "open": len(left), "expected_open": sum(want.values()),
               "not_expressed": skipped,
               "still_open_but_waived": sorted(i for s, n in (got - want).items() for i in ids.get(s, [])[:n]),
               "waived_but_not_applied": sorted(i for s, n in (want - got).items() for i in ids.get(s, [])[:n]),
               "not_applied": not_applied(work, role, waive, log, waivers.waiver_plan(report["violations"], plan["items"], role)[0])}
        if old:
            out["old_report"] = True
        out["ok"] = out["open"] == out["expected_open"] and not (skipped or out["still_open_but_waived"] or out["waived_but_not_applied"])
        self.write(engine, check_json(role), out)
        table.write(self.table_files(engine), role, report, plan)
        return out

    async def run(self, ctx: StepContext) -> None:
        ctx.emit("log", message=f"{self.role.upper()} again with the waivers of the review…")
        r = await anyio.to_thread.run_sync(lambda: self.apply(ctx.engine), abandon_on_cancel=True)
        ctx.emit("log", message=summary(r))
        if not r["ok"]:
            raise StepFailed(summary(r) + f" (schemas/{check_json(self.role)})")
