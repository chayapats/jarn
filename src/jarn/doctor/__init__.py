"""Doctor diagnostics: collection and rendering."""

from jarn.doctor.collect import collect_doctor
from jarn.doctor.extensions import MarkdownExtensionRow, collect_extensions
from jarn.doctor.inventory import collect_host_inventory, collect_provider_reachability
from jarn.doctor.render import (
    append_extension_lines,
    doctor_lines,
    doctor_to_json,
    render_doctor_console,
)
from jarn.doctor.repair import (
    RepairAction,
    RepairPlan,
    RepairResult,
    apply_repair_plan,
    build_repair_plan,
)
from jarn.doctor.report import (
    SUPPORT_REPORT_VERSION,
    build_support_report,
    scan_support_report,
    support_report_json,
    write_support_report,
)
from jarn.doctor.service import (
    DoctorServiceResult,
    plan_doctor_repairs,
    run_doctor_service,
)
from jarn.doctor.telegram_extra import TELEGRAM_EXTRA_MISSING, telegram_extra_warnings

__all__ = [
    "MarkdownExtensionRow",
    "DoctorServiceResult",
    "RepairAction",
    "RepairPlan",
    "RepairResult",
    "SUPPORT_REPORT_VERSION",
    "TELEGRAM_EXTRA_MISSING",
    "apply_repair_plan",
    "append_extension_lines",
    "build_repair_plan",
    "build_support_report",
    "collect_doctor",
    "collect_extensions",
    "collect_host_inventory",
    "collect_provider_reachability",
    "doctor_lines",
    "doctor_to_json",
    "plan_doctor_repairs",
    "render_doctor_console",
    "run_doctor_service",
    "scan_support_report",
    "support_report_json",
    "telegram_extra_warnings",
    "write_support_report",
]
