"""The PPA plan: what the optimizer proposes (`schemas/ppa_plan.json`) and how a fix is handed back to the flow that owns it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PLAN_JSON = "ppa_plan.json"


class Fix(BaseModel):
    target: Literal["rtl", "spec"] = Field(description="rtl: change one RTL module (hand-off: change request for the RTL step); "
                                                       "spec: the target cannot be met by RTL changes alone, the requirement or the constraint has to change")
    module: str = Field("", description="target rtl: the module (file name without extension) to change; empty for spec")
    change: str = Field(description="What to change, concretely (the structure to change and how), in one to four sentences. "
                                    "Behaviour, ports and requirements stay the same for an rtl fix.")
    why: str = Field(description="The evidence in the report: the path, the resource or the number it addresses")
    expected: str = Field("", description="The expected effect, e.g. 'removes 1-2 logic levels on the worst path'")
    risk: str = Field("", description="What could go wrong (area up, latency changes, …)")


class PpaPlan(BaseModel):
    analysis: str = Field(description="Where the target is missed and why, from the report only (2-6 sentences)")
    fixes: list[Fix] = Field(default_factory=list, description="Ordered by expected gain. Empty when nothing in the RTL or the spec can help")
    no_fix_reason: str = Field("", description="Why there is no fix, when `fixes` is empty")


def handoff_text(fix: dict, part: str, goal: str = "") -> tuple[str, str]:
    """(step of the owning flow, change-request text) for one fix of the plan."""
    why = f" Evidence: {fix['why']}." if fix.get("why") else ""
    risk = f" Watch: {fix['risk']}." if fix.get("risk") else ""
    if fix["target"] == "rtl":
        return "rtl", (f"[rtl:{fix['module']}] PPA ({part}): {fix['change']}.{why}{risk} "
                       "Keep the behaviour, the ports and every requirement tag unchanged; lint stays clean.")
    return "spec", (f"PPA ({part}): the target cannot be met by RTL changes alone. {fix['change']}.{why}"
                    f"{' Goal: ' + goal + '.' if goal else ''} Decide whether to relax or clarify the requirement / the clock target.")


def owner_of(entry: dict) -> str:
    """The child project a registry record belongs to, as the messages name it (`../cha`)."""
    return str(entry.get("owner") or entry.get("child") or "?")


def split_fixes(fixes: list, modules: list[str], registry: dict[str, dict]) -> tuple[list[tuple[int, object]], list[str]]:
    """([(number, fix)] kept, [why dropped]) for the optimizer's proposals (R3: a child project's module is fixed in that project, so a
    fix aimed at it is dropped and reported, never handed off). The number is the fix's place in the proposal (1-based), the id's base."""
    kept, dropped = [], []
    for i, f in enumerate(fixes, 1):
        if f.target == "rtl" and f.module in registry:
            dropped.append(f"fix {i}: `{f.module}` is a module of the child project {owner_of(registry[f.module])} (read-only here)")
        elif f.target == "rtl" and f.module not in modules:
            dropped.append(f"fix {i}: unknown module '{f.module}'")
        else:
            kept.append((i, f))
    return kept, dropped


def handoff_label(item: dict) -> tuple[str, str]:
    """(text, colour) of the hand-off column for a fix / finding: where it went, `refused → <owner>` (a child's module), or not yet."""
    sent = item.get("handed_off")
    if sent:
        return f"→ {sent['flow']}:{sent['step']}", "green"
    if item.get("refused"):
        return f"refused → {item['refused'].get('owner') or '?'}", "red"
    return "not yet", "yellow"
