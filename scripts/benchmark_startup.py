#!/usr/bin/env python3
"""Measure J.A.R.N. cold-process command latency and emit one JSON report.

The harness intentionally starts a new process for every sample. Warmups prime
the filesystem cache; measured samples therefore represent the GA specification's
"warm filesystem cache" while retaining real interpreter/binary startup cost.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

SCHEMA_VERSION = 1


class BenchmarkError(RuntimeError):
    """A command failed its bounded benchmark contract."""


def _percentile_nearest_rank(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("at least one sample is required")
    ordered = sorted(samples)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _benchmark_config(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        """\
config_version: 3
default_profile: benchmark
default_model: benchmark/local-model
providers:
  benchmark:
    type: openai_compatible
    api_key: ${JARN_BENCHMARK_API_KEY}
    base_url: http://127.0.0.1:9/v1
routing:
  main: benchmark/local-model
updates:
  check: false
ui:
  splash: "off"
  theme: dark
  locale: en
  terminal_title: false
observability:
  transcript: false
""",
        encoding="utf-8",
    )


def _measure_once(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
    allowed_exit_codes: set[int],
    require_json: bool,
) -> float:
    started = time.perf_counter_ns()
    try:
        result = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(
            f"{' '.join(argv[1:])} exceeded the {timeout_seconds:g}s harness timeout"
        ) from exc
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if result.returncode not in allowed_exit_codes:
        raise BenchmarkError(
            f"{' '.join(argv[1:])} returned unexpected exit code {result.returncode}"
        )
    if require_json:
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BenchmarkError("doctor --json did not emit valid JSON") from exc
        if not isinstance(decoded, dict) or "ok" not in decoded:
            raise BenchmarkError("doctor --json omitted its required status object")
    return elapsed_ms


def _run_case(
    name: str,
    argv: list[str],
    *,
    env: dict[str, str],
    warmups: int,
    runs: int,
    threshold_ms: float,
    timeout_seconds: float,
    allowed_exit_codes: set[int],
    require_json: bool = False,
) -> dict[str, Any]:
    for _ in range(warmups):
        _measure_once(
            argv,
            env=env,
            timeout_seconds=timeout_seconds,
            allowed_exit_codes=allowed_exit_codes,
            require_json=require_json,
        )
    samples = [
        _measure_once(
            argv,
            env=env,
            timeout_seconds=timeout_seconds,
            allowed_exit_codes=allowed_exit_codes,
            require_json=require_json,
        )
        for _ in range(runs)
    ]
    p95 = _percentile_nearest_rank(samples, 0.95)
    return {
        "name": name,
        "arguments": argv[1:],
        "samples_ms": [round(sample, 3) for sample in samples],
        "min_ms": round(min(samples), 3),
        "median_ms": round(median(samples), 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(samples), 3),
        "threshold_ms": threshold_ms,
        "passed": p95 <= threshold_ms,
    }


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop and reap one benchmark child without leaving a PTY process behind."""

    if process.poll() is not None:
        process.wait()
        return
    if os.name == "posix":
        import signal

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Some macOS/sandbox profiles permit signalling the direct child
            # but deny killpg despite start_new_session=True.
            process.terminate()
    else:  # pragma: no cover - interactive evidence runs on the POSIX CI job
        process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            import signal

            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.kill()
        else:  # pragma: no cover - see above
            process.kill()
        process.wait(timeout=1.0)


def _read_pty_until(fd: int, needle: bytes, *, deadline: float) -> bytes:
    """Read bounded PTY output through *needle* or raise a controlled error."""

    import select

    captured = bytearray()
    while time.perf_counter() < deadline:
        remaining = max(0.0, deadline - time.perf_counter())
        ready, _, _ = select.select([fd], [], [], min(0.05, remaining))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError as exc:
            raise BenchmarkError("interactive J.A.R.N. closed before rendering input") from exc
        if not chunk:
            break
        captured.extend(chunk)
        if needle in captured:
            return bytes(captured)
        if len(captured) > 262_144:
            raise BenchmarkError("interactive startup output exceeded 256 KiB")
    tail = bytes(captured[-2_048:]).decode("utf-8", "replace")
    tail = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", tail)
    tail = "".join(char if char.isprintable() or char in "\n\t" else " " for char in tail)
    raise BenchmarkError(
        f"interactive J.A.R.N. did not render {needle!r} before the bounded timeout; "
        f"bounded terminal tail={tail[-500:]!r}"
    )


def _measure_interactive_once(
    executable: Path,
    *,
    env: dict[str, str],
    timeout_seconds: float,
) -> tuple[float, float]:
    """Measure process-to-prompt and one real PTY keystroke-to-render latency."""

    if os.name != "posix":  # pragma: no cover - CI performance job is Ubuntu
        raise BenchmarkError("interactive PTY benchmarks require a POSIX host")

    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    # Pin the GA compatibility size instead of inheriting the runner's terminal.
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    started = time.perf_counter()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [str(executable), "--ignore-project-config"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            # Stay outside the source checkout (and any project-local session
            # index) so an interrupted developer session cannot insert a resume
            # prompt into the cold-start measurement.
            cwd=str(Path(env["JARN_HOME"]).parent),
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        slave = -1
        deadline = started + timeout_seconds
        # The composer placeholder is rendered by the same prompt-toolkit
        # Application after the focused input Buffer has been attached.
        # Quiet toolbar no longer prints ASCII ``cwd ``; the English first-turn
        # invitation is ASCII and survives encoding/style sequences. Locale is
        # pinned to ``en`` in the benchmark config so this needle stays stable.
        _read_pty_until(master, b"Ask jarn", deadline=deadline)
        prompt_ms = (time.perf_counter() - started) * 1_000

        # Prompt-toolkit has entered raw mode by the time its input marker is
        # rendered. A literal tilde is therefore app input, not kernel echo.
        key_started = time.perf_counter()
        os.write(master, b"~")
        _read_pty_until(master, b"~", deadline=deadline)
        key_ms = (time.perf_counter() - key_started) * 1_000
        return prompt_ms, key_ms
    finally:
        if process is not None:
            # Clear the benchmark character, then exercise the normal double
            # Ctrl+C exit before falling back to bounded process-tree cleanup.
            with contextlib.suppress(OSError):
                os.write(master, b"\x15\x03\x03")
            _stop_process_tree(process)
        with contextlib.suppress(OSError):
            os.close(master)
        if slave >= 0:
            with contextlib.suppress(OSError):
                os.close(slave)


def _run_interactive_cases(
    executable: Path,
    *,
    env: dict[str, str],
    warmups: int,
    runs: int,
    prompt_threshold_ms: float,
    keystroke_threshold_ms: float,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    for _ in range(warmups):
        _measure_interactive_once(
            executable, env=env, timeout_seconds=timeout_seconds
        )
    measurements = [
        _measure_interactive_once(
            executable, env=env, timeout_seconds=timeout_seconds
        )
        for _ in range(runs)
    ]
    cases: list[dict[str, Any]] = []
    for name, index, threshold in (
        ("interactive_prompt_ready", 0, prompt_threshold_ms),
        ("keystroke_render", 1, keystroke_threshold_ms),
    ):
        samples = [measurement[index] for measurement in measurements]
        p95 = _percentile_nearest_rank(samples, 0.95)
        cases.append({
            "name": name,
            "arguments": ["--ignore-project-config"],
            "samples_ms": [round(sample, 3) for sample in samples],
            "min_ms": round(min(samples), 3),
            "median_ms": round(median(samples), 3),
            "p95_ms": round(p95, 3),
            "max_ms": round(max(samples), 3),
            "threshold_ms": threshold,
            "passed": p95 <= threshold,
            "terminal": "80x24 PTY",
        })
    return cases


def run_benchmarks(
    executable: Path,
    *,
    runs: int = 10,
    warmups: int = 2,
    startup_threshold_ms: float = 500.0,
    interactive_threshold_ms: float = 2_000.0,
    keystroke_threshold_ms: float = 50.0,
    doctor_threshold_ms: float = 10_000.0,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Run all GA startup cases and return a machine-readable report."""
    executable = Path(executable).expanduser().resolve()
    if not executable.is_file():
        raise BenchmarkError(f"benchmark executable does not exist: {executable}")
    if os.name != "nt" and not stat.S_IXUSR & executable.stat().st_mode:
        raise BenchmarkError(f"benchmark executable is not executable: {executable}")
    if runs < 1 or warmups < 0:
        raise BenchmarkError("runs must be >= 1 and warmups must be >= 0")
    if (
        startup_threshold_ms <= 0
        or interactive_threshold_ms <= 0
        or keystroke_threshold_ms <= 0
        or doctor_threshold_ms <= 0
        or timeout_seconds <= 0
    ):
        raise BenchmarkError("thresholds and timeout must be positive")

    with tempfile.TemporaryDirectory(prefix="jarn-benchmark-") as raw_tmp:
        temp = Path(raw_tmp)
        home = temp / "home"
        state = temp / "state"
        _benchmark_config(home)
        env = os.environ.copy()
        env.update({
            "JARN_HOME": str(home),
            "JARN_STATE_DIR": str(state),
            "JARN_BENCHMARK": "1",
            "JARN_BENCHMARK_API_KEY": "benchmark-local-only",
            "NO_COLOR": "1",
            "TERM": "xterm-256color",
            "LANG": "C",
            "LC_ALL": "C",
            "LC_MESSAGES": "C",
        })
        cases = [
            _run_case(
                "version",
                [str(executable), "--version"],
                env=env,
                warmups=warmups,
                runs=runs,
                threshold_ms=startup_threshold_ms,
                timeout_seconds=timeout_seconds,
                allowed_exit_codes={0},
            ),
            _run_case(
                "help",
                [str(executable), "--help"],
                env=env,
                warmups=warmups,
                runs=runs,
                threshold_ms=startup_threshold_ms,
                timeout_seconds=timeout_seconds,
                allowed_exit_codes={0},
            ),
        ]
        cases.extend(
            _run_interactive_cases(
                executable,
                env=env,
                warmups=warmups,
                runs=runs,
                prompt_threshold_ms=interactive_threshold_ms,
                keystroke_threshold_ms=keystroke_threshold_ms,
                timeout_seconds=timeout_seconds,
            )
        )
        cases.append(
            _run_case(
                "doctor_offline",
                [str(executable), "doctor", "--json"],
                env=env,
                warmups=warmups,
                runs=runs,
                threshold_ms=doctor_threshold_ms,
                timeout_seconds=timeout_seconds,
                allowed_exit_codes={0, 1},
                require_json=True,
            )
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
        },
        "executable": str(executable),
        "runs": runs,
        "warmups": warmups,
        "cases": cases,
        "passed": all(case["passed"] for case in cases),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    default_executable = os.environ.get("JARN_BENCH_EXECUTABLE") or shutil.which("jarn")
    parser.add_argument(
        "--executable",
        type=Path,
        default=Path(default_executable) if default_executable else None,
        help="J.A.R.N. executable to benchmark (default: command resolved from PATH)",
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--startup-threshold-ms", type=float, default=500.0)
    parser.add_argument("--interactive-threshold-ms", type=float, default=2_000.0)
    parser.add_argument("--keystroke-threshold-ms", type=float, default=50.0)
    parser.add_argument("--doctor-threshold-ms", type=float, default=10_000.0)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--output", type=Path, help="Also write the JSON report here")
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always exit zero after a successful measurement, even above thresholds",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.executable is None:
        error = {"schema_version": SCHEMA_VERSION, "passed": False, "error": "jarn not found"}
        print(json.dumps(error, sort_keys=True))
        return 2
    try:
        report = run_benchmarks(
            args.executable,
            runs=args.runs,
            warmups=args.warmups,
            startup_threshold_ms=args.startup_threshold_ms,
            interactive_threshold_ms=args.interactive_threshold_ms,
            keystroke_threshold_ms=args.keystroke_threshold_ms,
            doctor_threshold_ms=args.doctor_threshold_ms,
            timeout_seconds=args.timeout_seconds,
        )
    except BenchmarkError as exc:
        error = {"schema_version": SCHEMA_VERSION, "passed": False, "error": str(exc)}
        print(json.dumps(error, sort_keys=True))
        return 2
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(output, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    return 0 if report["passed"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
