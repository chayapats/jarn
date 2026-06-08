"""Self-verification helpers.

Detects how a project builds/tests/lints so the agent (via the system prompt and
hooks) can verify its own changes. Detection is best-effort and based on common
project markers; results are advisory hints, not commands run automatically
(running them is gated by the permission engine like any other shell command).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ProjectCapabilities:
    test: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.test or self.build or self.lint)

    def as_prompt_block(self) -> str:
        if not self.any:
            return ""
        lines = ["# Verification commands (detected)"]
        for label, cmds in (("test", self.test), ("build", self.build), ("lint", self.lint)):
            for cmd in cmds:
                lines.append(f"- {label}: `{cmd}`")
        lines.append("\nRun the relevant command(s) to verify changes before reporting done.")
        return "\n".join(lines)


def detect_capabilities(project_root: Path) -> ProjectCapabilities:
    caps = ProjectCapabilities()
    if not project_root or not project_root.is_dir():
        return caps

    _detect_node(project_root, caps)
    _detect_python(project_root, caps)
    _detect_make(project_root, caps)
    _detect_rust_go(project_root, caps)
    return caps


def _detect_node(root: Path, caps: ProjectCapabilities) -> None:
    pkg = root / "package.json"
    if not pkg.is_file():
        return
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    scripts = data.get("scripts", {}) or {}
    runner = "npm run"
    if (root / "pnpm-lock.yaml").is_file():
        runner = "pnpm"
    elif (root / "yarn.lock").is_file():
        runner = "yarn"
    elif (root / "bun.lockb").is_file():
        runner = "bun run"
    for name, bucket in (("test", caps.test), ("build", caps.build), ("lint", caps.lint)):
        if name in scripts:
            bucket.append(f"{runner} {name}")


def _detect_python(root: Path, caps: ProjectCapabilities) -> None:
    has_py_project = (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file()
    if has_py_project and ((root / "tests").is_dir() or (root / "pyproject.toml").is_file()):
        caps.test.append("pytest -q")
    if (root / "ruff.toml").is_file() or (root / "pyproject.toml").is_file():
        caps.lint.append("ruff check .")


def _detect_make(root: Path, caps: ProjectCapabilities) -> None:
    mk = root / "Makefile"
    if not mk.is_file():
        return
    try:
        text = mk.read_text(encoding="utf-8")
    except OSError:
        return
    for target, bucket in (("test", caps.test), ("build", caps.build), ("lint", caps.lint)):
        if f"\n{target}:" in f"\n{text}":
            bucket.append(f"make {target}")


def _detect_rust_go(root: Path, caps: ProjectCapabilities) -> None:
    if (root / "Cargo.toml").is_file():
        caps.test.append("cargo test")
        caps.build.append("cargo build")
    if (root / "go.mod").is_file():
        caps.test.append("go test ./...")
        caps.build.append("go build ./...")
