from __future__ import annotations

import json
import os

import pytest

from jarn.install_state import InstallStateError, update_setup_status


def _manifest(path, active):
    active.parent.mkdir(parents=True, exist_ok=True)
    (active.parent / ".jarn-install-method").write_text("python 0.11.0\n", encoding="utf-8")
    value = {
        "schema_version": 1,
        "version": "0.11.0",
        "method": "portable-python",
        "channel": "stable",
        "active_path": str(active),
        "state_dir": str(path.parent),
        "setup_status": "required",
        "future_installer_field": {"preserve": True},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def test_setup_status_update_is_atomic_private_and_preserves_unknown_fields(tmp_path):
    manifest = tmp_path / "install.json"
    active = tmp_path / "bin" / "jarn"
    active.parent.mkdir()
    active.write_text("stub", encoding="utf-8")
    _manifest(manifest, active)

    assert update_setup_status("complete", path=manifest, updated_at="2026-08-09T00:00:00Z")

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    assert raw["setup_status"] == "complete"
    assert raw["setup_updated_at"] == "2026-08-09T00:00:00Z"
    assert raw["future_installer_field"] == {"preserve": True}
    if os.name != "nt":
        assert os.stat(manifest).st_mode & 0o777 == 0o600


def test_setup_status_missing_unmanaged_manifest_is_noop(tmp_path):
    assert update_setup_status("in_progress", path=tmp_path / "missing.json") is False


def test_setup_status_rejects_unknown_status_before_writing(tmp_path):
    manifest = tmp_path / "install.json"
    before = _manifest(manifest, tmp_path / "jarn")
    with pytest.raises(ValueError, match="invalid setup status"):
        update_setup_status("done-ish", path=manifest)
    assert json.loads(manifest.read_text(encoding="utf-8")) == before


def test_setup_status_refuses_symlink(tmp_path):
    real = tmp_path / "real.json"
    _manifest(real, tmp_path / "jarn")
    link = tmp_path / "install.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(InstallStateError, match="symbolic link"):
        update_setup_status("complete", path=link)

    assert json.loads(real.read_text(encoding="utf-8"))["setup_status"] == "required"
