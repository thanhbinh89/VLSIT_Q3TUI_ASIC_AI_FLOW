"""Shared pieces of the FPGA flow steps: where the RTL, the SDC and the results are, the tool environment."""

from __future__ import annotations

from pathlib import Path

from q3tui.core.project import read_json, write_json
from q3tui.pipeline.base import StepDef
from q3tui.flows.fpga.lib import sdc as sdcmod
from q3tui.flows.fpga.lib import templates

SYNTH_JSON = "fpga_synth.json"  # schemas/: what the synthesis run produced (tool, part, status, reports)
PPA_JSON = "ppa_report.json"  # schemas/: utilization + timing + verdict against the targets


class FpgaStep(StepDef):
    """Base of the FPGA flow's steps. The RTL is the VLSIT flow's result (`src/rtl/`, read only); the constraints are `constraints/sdc/`."""

    uses_memory_modules = False  # (the step reads `project.memory_modules`: it is part of its staleness when set)

    def memory_modules(self, engine) -> list[str]:
        """`project.memory_modules` as written (names or patterns)."""
        return [str(x) for x in getattr(engine.project.cfg.project, "memory_modules", None) or [] if str(x).strip()]

    uses_min_mem_words = False  # (the step proposes memory modules: asic_port's `min_mem_words` is part of its staleness when set)

    def min_mem_words(self, engine) -> int:
        """asic_port's `min_mem_words` (16 unless set), in every flow: an unpacked array this deep makes its module a memory module (the
        `memory_array` rule)."""
        from q3tui.flows.fpga.lib import memories as memmod

        port = next((s for s in engine.steps if s.kind == "asic.asic_port"), None)
        raw = (port.options if port is not None else (engine.project.cfg.pipeline.step_options or {}).get("asic_port") or {}).get("min_mem_words")
        try:
            return max(1, int(raw or memmod.MIN_WORDS))
        except (TypeError, ValueError):
            return memmod.MIN_WORDS

    def extra_hash(self, engine) -> dict | None:
        from q3tui.flows.fpga.lib import memories as memmod

        out: dict = {}
        mm = self.memory_modules(engine) if self.uses_memory_modules else []
        if mm:
            out["memory_modules"] = mm
        words = self.min_mem_words(engine) if self.uses_min_mem_words else memmod.MIN_WORDS
        if words != memmod.MIN_WORDS:
            out["min_mem_words"] = words
        return out or None

    def body_files(self, engine, options: dict | None = None) -> list[Path]:
        """The target bodies of `options.bodies` (this step's, or the `options` given): inputs of the step (lib/memories.py)."""
        from q3tui.flows.fpga.lib import memories as memmod

        try:
            raw = memmod.option_bodies((self.options if options is None else options).get("bodies"))
        except memmod.PlanError:
            return []
        return [memmod._path(engine.project, v) for v in raw.values()]

    def rtl_dir(self, engine) -> Path:
        return engine.project.rtl_dir

    def filelist(self, engine) -> Path:
        return engine.project.root / (self.options.get("filelist") or str(self.rtl_dir(engine).relative_to(engine.project.root) / "filelist.f"))

    def rtl_files(self, engine) -> list[Path]:
        d = self.rtl_dir(engine)
        return sorted(d.glob("*.sv")) + sorted(d.glob("*.v")) if d.is_dir() else []

    def top(self, engine) -> str | None:
        if self.options.get("top"):
            return str(self.options["top"])
        spec = read_json(engine.project.schemas_dir / "structured_spec.json", {}) or {}
        return (spec.get("metadata") or {}).get("top_module")

    def missing_rtl(self, engine) -> str | None:
        if not self.filelist(engine).is_file():
            return f"the RTL is needed first ({engine.project.rel(self.filelist(engine))}; run the vlsit flow's rtl step)"
        if not self.top(engine):
            return "no top module: set `options.top` (or run the vlsit flow's parse step: schemas/structured_spec.json)"
        return None

    def template(self) -> templates.Template:
        """The FPGA family of the step's part (lib/templates.py `resolve`: the part decides it); a part no family here synthesizes fails the step."""
        t, _device, note = templates.resolve(self.options)
        if t is None:
            from q3tui.pipeline.base import StepFailed

            raise StepFailed(note)
        return t

    def part(self) -> tuple[str, str]:
        """(device, note): the device of `options.part` in its family's form, or the default part (said in the note) when none is chosen."""
        _t, device, note = templates.resolve(self.options)
        return device, note

    def workdir(self, engine) -> Path:
        d = engine.project.state_dir / "fpga" / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reports_dir(self, engine) -> Path:
        return engine.project.fpga_dir / "reports"

    def sdc_inputs(self, engine) -> list[Path]:
        d = sdcmod.sdc_dir(engine.project)
        return [d / n for n in sdcmod.FILES if (d / n).is_file()]

    def tools(self, engine):
        from q3tui.eda import tools as toolmod

        return toolmod.load_tools(engine.project.cfg, engine.project.root)

    def artifact(self, engine, name: str) -> Path:
        return engine.project.schemas_dir / name

    def read(self, engine, name: str, default=None):
        return read_json(self.artifact(engine, name), default)

    def write(self, engine, name: str, data: dict) -> Path:
        return write_json(self.artifact(engine, name), data)
