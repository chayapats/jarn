"""CLI-independent orchestration for doctor, repair, and support reports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarn.doctor.collect import collect_doctor
from jarn.doctor.repair import (
    RepairPlan,
    RepairResult,
    apply_repair_plan,
    build_repair_plan,
)
from jarn.doctor.report import write_support_report


@dataclass(frozen=True, slots=True)
class DoctorServiceResult:
    """Complete machine-readable outcome for a doctor invocation."""

    exit_code: int
    diagnostics: dict[str, Any]
    repair_plan: RepairPlan
    repair_result: RepairResult | None = None
    report_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and (self.repair_result is None or self.repair_result.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "diagnostics": self.diagnostics,
            "repair_plan": self.repair_plan.to_dict(),
            "repair_result": (
                self.repair_result.to_dict() if self.repair_result is not None else None
            ),
            "report_path": str(self.report_path) if self.report_path else None,
        }


def plan_doctor_repairs(
    diagnostics: dict[str, Any],
    *,
    global_home: Path | None = None,
) -> RepairPlan:
    """Build a safe repair plan from previously collected diagnostics."""
    if global_home is None:
        from jarn.config.paths import global_home as configured_global_home

        global_home = configured_global_home()
    return build_repair_plan(diagnostics, global_home=global_home)


def run_doctor_service(
    *,
    config: Any = None,
    project_root: Path | None = None,
    project_trusted: bool | None = None,
    extra_roots: Any = None,
    prompt_modules: dict[str, Any] | None = None,
    network: bool = False,
    fix: bool = False,
    dry_run: bool = True,
    report_path: Path | None = None,
    known_secrets: set[str] | None = None,
    global_home: Path | None = None,
) -> DoctorServiceResult:
    """Collect diagnostics and optionally preview/apply repairs and write a report.

    The default is offline and non-mutating.  ``fix=True`` opts into the
    allowlisted repair executor; it still previews only unless ``dry_run=False``
    is explicit.  This conservative API lets both CLI and TUI share one policy.
    """
    diagnostics: dict[str, Any] = {}
    exit_code = collect_doctor(
        diagnostics,
        config=config,
        project_root=project_root,
        project_trusted=project_trusted,
        extra_roots=extra_roots,
        prompt_modules=prompt_modules,
        network=network,
    )
    plan = plan_doctor_repairs(diagnostics, global_home=global_home)
    repair_result: RepairResult | None = None
    if fix:
        if global_home is None:
            from jarn.config.paths import global_home as configured_global_home

            repair_home = configured_global_home()
        else:
            repair_home = global_home
        repair_result = apply_repair_plan(plan, global_home=repair_home, dry_run=dry_run)
        if not repair_result.ok:
            exit_code = 1

    written_report = None
    if report_path is not None:
        written_report = write_support_report(
            diagnostics,
            report_path,
            known_secrets=known_secrets,
        )
    return DoctorServiceResult(
        exit_code=exit_code,
        diagnostics=diagnostics,
        repair_plan=plan,
        repair_result=repair_result,
        report_path=written_report,
    )


__all__ = ["DoctorServiceResult", "plan_doctor_repairs", "run_doctor_service"]
