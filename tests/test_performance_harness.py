"""Machine-readable GA startup benchmark harness tests."""

from __future__ import annotations

import json
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_startup.py"
_SPEC = spec_from_file_location("jarn_benchmark_startup", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
benchmark_startup = module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark_startup)


def test_nearest_rank_percentile_is_deterministic():
    assert benchmark_startup._percentile_nearest_rank([4.0, 1.0, 3.0, 2.0], 0.95) == 4.0
    assert benchmark_startup._percentile_nearest_rank([4.0, 1.0, 3.0, 2.0], 0.50) == 2.0
    with pytest.raises(ValueError):
        benchmark_startup._percentile_nearest_rank([], 0.95)


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_benchmark_report_runs_real_processes_and_validates_doctor_json(tmp_path: Path):
    executable = tmp_path / "jarn"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, tty\n"
        "if sys.argv[1:] == ['--ignore-project-config']:\n"
        "    tty.setraw(sys.stdin.fileno())\n"
        "    print('Ask jarn fixture\\n› ', end='', flush=True)\n"
        "    print(sys.stdin.read(1), end='', flush=True)\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:] == ['doctor', '--json']:\n"
        "    print(json.dumps({'ok': True}))\n"
        "else:\n"
        "    print('jarn fixture')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    report = benchmark_startup.run_benchmarks(
        executable,
        runs=2,
        warmups=1,
        startup_threshold_ms=10_000,
        doctor_threshold_ms=10_000,
        timeout_seconds=2,
    )

    assert report["schema_version"] == 1
    assert report["passed"] is True
    assert [case["name"] for case in report["cases"]] == [
        "version",
        "help",
        "interactive_prompt_ready",
        "keystroke_render",
        "doctor_offline",
    ]
    assert all(len(case["samples_ms"]) == 2 for case in report["cases"])
    json.dumps(report)


def test_harness_failure_is_json_and_nonzero(capsys: pytest.CaptureFixture[str], tmp_path: Path):
    code = benchmark_startup.main(["--executable", str(tmp_path / "missing")])

    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["passed"] is False
    assert "does not exist" in output["error"]


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_harness_can_write_machine_readable_evidence(tmp_path: Path):
    executable = tmp_path / "jarn"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, tty\n"
        "if sys.argv[1:] == ['--ignore-project-config']:\n"
        "    tty.setraw(sys.stdin.fileno())\n"
        "    print('Ask jarn fixture\\n› ', end='', flush=True)\n"
        "    print(sys.stdin.read(1), end='', flush=True)\n"
        "elif 'doctor' in sys.argv:\n"
        "    print(json.dumps({'ok': True}))\n"
        "else:\n"
        "    print('ok')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    output = tmp_path / "performance.json"

    code = benchmark_startup.main([
        "--executable",
        str(executable),
        "--runs",
        "1",
        "--warmups",
        "0",
        "--startup-threshold-ms",
        "10000",
        "--doctor-threshold-ms",
        "10000",
        "--output",
        str(output),
    ])

    assert code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["passed"] is True
    assert written["cases"][2]["name"] == "interactive_prompt_ready"
    assert written["cases"][3]["name"] == "keystroke_render"
    assert written["cases"][4]["name"] == "doctor_offline"
