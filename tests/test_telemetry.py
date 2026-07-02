"""Opt-in telemetry tests — default OFF, local-only, anonymized."""

from __future__ import annotations

import json

from jarn.observability.telemetry import Telemetry


def test_disabled_is_noop(tmp_path):
    t = Telemetry(enabled=False, sink_path=tmp_path / "t.jsonl")
    t.record("turn", when=1.0, tokens=100)
    t.flush()
    assert not (tmp_path / "t.jsonl").exists()


def test_enabled_writes_local(tmp_path):
    t = Telemetry(enabled=True, sink_path=tmp_path / "t.jsonl", install_id="abc")
    t.record("turn", when=1.5, tokens=100, cost_cents=2.0)
    t.flush()
    rows = [json.loads(line) for line in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert rows[0]["event"] == "turn"
    assert rows[0]["tokens"] == 100
    assert rows[0]["install"] == "abc"


def test_non_numeric_props_dropped(tmp_path):
    t = Telemetry(enabled=True, sink_path=tmp_path / "t.jsonl", install_id="x")
    t.record("turn", when=1.0, tokens=5, prompt="secret text", path="/home/u/file")
    t.flush()
    row = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert "prompt" not in row and "path" not in row
    assert row["tokens"] == 5


def test_from_config_default_off(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    assert Telemetry.from_config(False).enabled is False
    assert Telemetry.from_config(True).enabled is True


def test_status_command(tmp_path, monkeypatch, base_config):
    from jarn.controller.commands.diagnostics import cmd_telemetry
    from jarn.controller.core import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    sink = tmp_path / "telemetry.jsonl"
    ctrl = Controller(base_config, tmp_path / "proj")
    ctrl.telemetry = Telemetry(enabled=True, sink_path=sink, install_id="abc123")
    ctrl.telemetry.record("turn", when=1.0, tokens=10, cost_cents=1.5)
    ctrl.telemetry.flush()

    out = cmd_telemetry(ctrl, "status").text.lower()
    assert "enabled" in out
    assert "telemetry.jsonl" in out
    assert "events on disk: 1" in out
    assert "install id: present" in out
    ctrl.close()
