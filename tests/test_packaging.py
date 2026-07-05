"""Wheel/sdist build smoke tests — packaging gate for releases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SDIST_FRAGMENTS = (
    ".jarn/checkpoints.sqlite",
    ".jarn/state.sqlite",
    "/.venv/",
    "__pycache__",
)


def _run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=True, env=env
    )


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory) -> dict[str, Path]:
    """Build sdist + wheel once per module into a temp dist directory."""
    out = tmp_path_factory.mktemp("dist")
    _run(["uv", "build", "--out-dir", str(out)], cwd=ROOT)
    wheels = sorted(out.glob("*.whl"))
    sdists = sorted(out.glob("*.tar.gz"))
    assert wheels and sdists, f"uv build produced no artifacts in {out}"
    return {"wheel": wheels[-1], "sdist": sdists[-1], "dist_dir": out}


def test_sdist_excludes_runtime_artifacts(built_artifacts):
    sdist = built_artifacts["sdist"]
    with tarfile.open(sdist, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("src/jarn/cli.py") for n in names)
    for name in names:
        for frag in FORBIDDEN_SDIST_FRAGMENTS:
            assert frag not in name, f"sdist must not ship {frag!r}; found {name!r}"


def test_wheel_contains_repl_entrypoints(built_artifacts):
    wheel = built_artifacts["wheel"]
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    assert any(n.endswith("jarn/repl/__init__.py") for n in names)
    assert any(n.endswith("jarn/cli.py") for n in names)
    assert not any(".jarn" in n for n in names)


def test_wheel_install_smoke(built_artifacts, tmp_path):
    """Install the built wheel in a clean venv and run CLI smoke commands."""
    venv = tmp_path / "venv"
    _run(["uv", "venv", str(venv)], cwd=ROOT)
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    jarn = venv / ("Scripts/jarn.exe" if sys.platform == "win32" else "bin/jarn")
    wheel = built_artifacts["wheel"]
    jarn_home = tmp_path / "jarn-home"
    jarn_home.mkdir()
    (jarn_home / "config.yaml").write_text(
        "providers:\n  openrouter:\n    type: openrouter\n    api_key: sk-test\n"
        "    base_url: https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )
    isolated = os.environ.copy()
    isolated["JARN_HOME"] = str(jarn_home)
    _run(
        ["uv", "pip", "install", "--python", str(py), str(wheel)],
        cwd=ROOT,
        env=isolated,
    )
    version = _run([str(jarn), "--version"], cwd=ROOT, env=isolated)
    from jarn.version import __version__

    assert __version__ in version.stdout
    doctor = subprocess.run(
        [str(jarn), "doctor", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=isolated,
        check=False,
    )
    data = json.loads(doctor.stdout)
    assert data["global_config_present"] is True
    assert "extensions" in data
    assert data["extensions"]["counts"]["skills"] >= 0


def test_npm_launcher_includes_license(tmp_path):
    """npm/build-packages.mjs must copy LICENSE into all assembled packages."""
    import json
    import os
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    out = tmp_path / "npm-out"
    bins = tmp_path / "bins"
    bins.mkdir()
    for target in ("linux-x64", "linux-arm64", "darwin-arm64"):
        tdir = bins / f"binary-{target}"
        tdir.mkdir()
        fake = tdir / "jarn"
        fake.write_bytes(b"#!/bin/sh\necho fake\n")
        os.chmod(fake, 0o755)
    result = subprocess.run(
        ["node", "npm/build-packages.mjs",
         "--version", "0.0.0-test",
         "--binaries", str(bins),
         "--out", str(out),
         "--allow-missing"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    main_license = out / "jarn-cli" / "LICENSE"
    assert main_license.is_file(), "jarn-cli/LICENSE must exist in the assembled launcher package"
    for target in ("linux-x64", "linux-arm64", "darwin-arm64"):
        plat_license = out / f"jarn-cli-{target}" / "LICENSE"
        assert plat_license.is_file(), f"jarn-cli-{target}/LICENSE must exist"
    pkg = json.loads((out / "jarn-cli" / "package.json").read_text())
    assert "LICENSE" in pkg.get("files", []), "LICENSE must be in main package's files"
