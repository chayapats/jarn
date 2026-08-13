"""Frozen-runtime environment boundaries for external executables."""

from __future__ import annotations

from jarn.util.process_env import external_command_env


def test_external_command_env_restores_original_loader_path() -> None:
    source = {
        "PATH": "/usr/bin:/bin",
        "LD_LIBRARY_PATH": "/tmp/_MEI-runtime",
        "LD_LIBRARY_PATH_ORIG": "/system/one:/system/two",
    }

    clean = external_command_env(source, frozen=True, platform_name="linux")

    assert clean["PATH"] == source["PATH"]
    assert clean["LD_LIBRARY_PATH"] == "/system/one:/system/two"
    assert "LD_LIBRARY_PATH_ORIG" not in clean
    assert clean["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert source["LD_LIBRARY_PATH"] == "/tmp/_MEI-runtime"


def test_external_command_env_removes_synthetic_loader_path_without_original() -> None:
    clean = external_command_env(
        {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/tmp/_MEI-runtime"},
        frozen=True,
        platform_name="linux",
    )

    assert "LD_LIBRARY_PATH" not in clean
    assert "LD_LIBRARY_PATH_ORIG" not in clean
    assert clean["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_source_install_preserves_operator_loader_path() -> None:
    clean = external_command_env(
        {"LD_LIBRARY_PATH": "/operator/libs", "PATH": "/usr/bin"},
        frozen=False,
        platform_name="linux",
    )

    assert clean["LD_LIBRARY_PATH"] == "/operator/libs"
    assert clean["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_external_command_env_preserves_macos_loader_settings_when_frozen() -> None:
    clean = external_command_env(
        {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/operator/libs"},
        frozen=True,
        platform_name="darwin",
    )

    assert clean["LD_LIBRARY_PATH"] == "/operator/libs"
    assert clean["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
