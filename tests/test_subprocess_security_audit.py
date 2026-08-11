"""Static regression gate for subprocess and shell construction."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_subprocesses.py"
SPEC = importlib.util.spec_from_file_location("audit_subprocesses", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


def test_current_source_has_no_unreviewed_shell_surface() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "jarn"
    assert audit.audit_tree(root) == []


def test_audit_rejects_shell_true_and_forbidden_apis(tmp_path: Path) -> None:
    root = tmp_path / "src" / "jarn"
    root.mkdir(parents=True)
    (root / "bad.py").write_text(
        "import asyncio, os, subprocess\n"
        "subprocess.run('user text', shell=True)\n"
        "os.system('whoami')\n"
        "asyncio.create_subprocess_shell('id')\n",
        encoding="utf-8",
    )

    findings = audit.audit_tree(root)

    assert {finding.code for finding in findings} == {
        "unreviewed-shell-true",
        "forbidden-shell-api",
    }


def test_review_marker_must_use_a_known_security_boundary(tmp_path: Path) -> None:
    root = tmp_path / "src" / "jarn"
    root.mkdir(parents=True)
    (root / "reviewed.py").write_text(
        "import subprocess\n"
        "subprocess.run('x', shell=True)  # security: reviewed-shell=permission-engine\n",
        encoding="utf-8",
    )
    (root / "fake.py").write_text(
        "import subprocess\n"
        "subprocess.run('x', shell=True)  # security: reviewed-shell=looks-safe\n",
        encoding="utf-8",
    )

    findings = audit.audit_tree(root)

    assert len(findings) == 1
    assert findings[0].path.endswith("fake.py")
