"""Crash, privacy, and multi-process contracts for local telemetry."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import jarn.observability.telemetry as telemetry_module
from jarn.observability.telemetry import Telemetry


def _row(ts: float, **props: int | float | bool) -> dict[str, Any]:
    return {"event": "turn", "install": "test", "ts": ts, **props}


def _encoded(row: dict[str, Any]) -> bytes:
    return json.dumps(row, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _install_id_worker(home: str, start: Any, results: Any) -> None:
    os.environ["JARN_HOME"] = home
    start.wait()
    from jarn.observability.telemetry import _install_id

    results.put(_install_id())


def _writer_worker(sink: str, worker: int, count: int, start: Any) -> None:
    start.wait()
    telemetry = Telemetry(enabled=True, sink_path=Path(sink), install_id="shared")
    for sequence in range(count):
        telemetry.record(
            "turn",
            when=float(worker * count + sequence),
            worker=worker,
            sequence=sequence,
            ok=sequence % 2 == 0,
        )
        if sequence % 5 == 4:
            telemetry.flush()
    telemetry.flush()


def _start_and_join(processes: list[Any], start: Any) -> None:
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
    hanging = [process for process in processes if process.is_alive()]
    for process in hanging:
        process.terminate()
        process.join(timeout=5)
    assert not hanging, "telemetry workers deadlocked"
    assert [process.exitcode for process in processes] == [0] * len(processes)


def test_default_off_creates_no_files_or_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path))

    telemetry = Telemetry.from_config(False)
    telemetry.record("turn", when=1.0, tokens=1)
    telemetry.flush()
    summary = telemetry.status_summary()

    assert summary["enabled"] is False
    assert summary["event_count"] == 0
    assert not list(tmp_path.rglob("*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_install_id_is_locked_atomic_stable_and_mode_0600(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_install_id_worker, args=(str(tmp_path), start, results))
        for _ in range(8)
    ]

    _start_and_join(processes, start)

    ids = [results.get(timeout=5) for _ in processes]
    install_path = tmp_path / ".install_id"
    assert len(set(ids)) == 1
    assert ids[0] == install_path.read_text(encoding="utf-8")
    assert len(ids[0]) == 32
    assert stat.S_IMODE(install_path.stat().st_mode) == 0o600
    assert (tmp_path / ".install_id.lock").is_file()


def test_concurrent_process_flushes_keep_every_event(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    sink = tmp_path / "telemetry.jsonl"
    worker_count = 5
    events_per_worker = 25
    processes = [
        context.Process(
            target=_writer_worker,
            args=(str(sink), worker, events_per_worker, start),
        )
        for worker in range(worker_count)
    ]

    _start_and_join(processes, start)

    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    observed = {(row["worker"], row["sequence"]) for row in rows}
    expected = {
        (worker, sequence)
        for worker in range(worker_count)
        for sequence in range(events_per_worker)
    }
    assert len(rows) == worker_count * events_per_worker
    assert observed == expected
    assert Telemetry(enabled=True, sink_path=sink).status_summary()["event_count"] == len(
        expected
    )


def test_flush_repairs_only_malformed_final_record_and_reports_recovery(tmp_path: Path):
    sink = tmp_path / "telemetry.jsonl"
    original = _row(1.0, tokens=3)
    sink.write_bytes(_encoded(original) + b'\n{"event":"turn","ts":')
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id="test")

    before = telemetry.status_summary()
    assert before["valid_event_count"] == 1
    assert before["corrupt_record_count"] == 1
    assert before["repairable_final_record"] is True

    telemetry.record("turn", when=2.0, tokens=4)
    telemetry.flush()

    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert rows == [original, _row(2.0, tokens=4)]
    after = telemetry.status_summary()
    assert after["health"] == "recovered"
    assert after["valid_event_count"] == 2
    assert after["corrupt_record_count"] == 0
    assert after["recovery_performed"] is True
    assert "malformed final telemetry record" in after["recovery_message"]


def test_flush_preserves_interior_corruption_and_counts_only_valid_events(tmp_path: Path):
    sink = tmp_path / "telemetry.jsonl"
    first = _row(1.0, tokens=1)
    second = _row(2.0, tokens=2)
    corrupt = b'{"event": definitely-not-json}\n'
    original = _encoded(first) + b"\n" + corrupt + _encoded(second) + b"\n"
    sink.write_bytes(original)
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id="test")

    telemetry.record("turn", when=3.0, tokens=3)
    telemetry.flush()

    current = sink.read_bytes()
    assert current.startswith(original)
    assert corrupt in current
    status = telemetry.status_summary()
    assert status["health"] == "corrupt"
    assert status["valid_event_count"] == 3
    assert status["corrupt_record_count"] == 1
    assert status["corrupt_record_lines"] == [2]
    assert status["repairable_final_record"] is False
    assert status["recovery_performed"] is False
    assert "preserved 1 malformed non-final" in status["last_error"]


def test_valid_final_record_without_newline_is_preserved(tmp_path: Path):
    sink = tmp_path / "telemetry.jsonl"
    first = _row(1.0, tokens=1)
    sink.write_bytes(_encoded(first))
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id="test")

    telemetry.record("turn", when=2.0, tokens=2)
    telemetry.flush()

    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert rows == [first, _row(2.0, tokens=2)]
    status = telemetry.status_summary()
    assert status["health"] == "recovered"
    assert "missing newline" in status["recovery_message"]


def test_malformed_final_line_with_valid_event_prefix_is_not_truncated(tmp_path: Path):
    sink = tmp_path / "telemetry.jsonl"
    valid_prefix = _encoded(_row(1.0, tokens=1))
    original = valid_prefix + b"trailing-corruption"
    sink.write_bytes(original)
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id="test")

    telemetry.record("turn", when=2.0, tokens=2)
    telemetry.flush()

    assert sink.read_bytes().startswith(original + b"\n")
    status = telemetry.status_summary()
    assert status["health"] == "corrupt"
    assert status["corrupt_record_count"] == 1
    assert status["repairable_final_record"] is False
    assert status["recovery_performed"] is False


def test_sink_enforces_numeric_bool_only_and_central_secret_redaction(tmp_path: Path):
    sink = tmp_path / "telemetry.jsonl"
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id=secret)

    telemetry.record(
        secret,
        when=1.0,
        tokens=5,
        ok=True,
        prompt="private prompt",
        path="/private/worktree",
        not_a_number=[1, 2],
        nan=float("nan"),
        infinity=float("inf"),
        **{"api_key": 1234, "secret_metric": 9},
    )
    telemetry.flush()

    raw = sink.read_text(encoding="utf-8")
    row = json.loads(raw)
    assert secret not in raw
    assert "private prompt" not in raw
    assert "/private/worktree" not in raw
    assert row == {
        "event": "redacted_event",
        "install": "redacted",
        "ok": True,
        "tokens": 5,
        "ts": 1.0,
    }


def test_invalid_timestamp_never_creates_sink(tmp_path: Path):
    sink = tmp_path / "telemetry.jsonl"
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id="test")

    telemetry.record("turn", when=float("nan"), tokens=1)
    telemetry.record("turn", when=True, tokens=2)
    telemetry.flush()

    assert not sink.exists()


def test_flush_fsyncs_and_restricts_sink_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    sink = tmp_path / "telemetry.jsonl"
    fsynced: list[int] = []
    real_fsync = telemetry_module.os.fsync

    def tracked_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(telemetry_module.os, "fsync", tracked_fsync)
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id="test")
    telemetry.record("turn", when=1.0, tokens=1)
    telemetry.flush()

    assert fsynced
    if os.name != "nt":
        assert stat.S_IMODE(sink.stat().st_mode) == 0o600


def test_unavailable_lock_keeps_buffer_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from contextlib import contextmanager

    @contextmanager
    def unavailable(_path: Path):
        yield False

    sink = tmp_path / "telemetry.jsonl"
    monkeypatch.setattr(telemetry_module, "file_lock", unavailable)
    telemetry = Telemetry(enabled=True, sink_path=sink, install_id="test")
    telemetry.record("turn", when=1.0, tokens=1)

    telemetry.flush()

    assert not sink.exists()
    assert len(telemetry._buffer) == 1
    assert "write lock" in telemetry.status_summary()["last_error"]
