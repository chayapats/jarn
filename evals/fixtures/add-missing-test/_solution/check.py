"""Checker for add-missing-test — grades the agent's test_parser.py by MUTATION.

Structural checks alone are gameable: a test with no assertions, or one that
only exercises the happy path, would pass. So we grade behaviourally:

  1. test_parser.py must exist and PASS against the correct parser.py.
  2. It must FAIL against a MUTANT parser whose parse_kv silently accepts an
     invalid line (never raises). A test that can't tell the correct parser
     from this mutant is not actually testing parse_kv's contract (ValueError
     on a line without '='), so it does not satisfy the task.

This is run by the harness as `python check.py` inside the work dir; check.py
and parser.py are restored from the seed before scoring, so the agent can only
influence the verdict through test_parser.py.
"""

import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
TEST = HERE / "test_parser.py"

#: A broken parse_kv that NEVER raises on a missing '=' — a correct test of the
#: error-path contract must catch this.
_MUTANT = 'def parse_kv(line):\n    k, _, v = line.partition("=")\n    return k, v\n'


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def run_pytest(cwd: pathlib.Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_parser.py"],
        cwd=str(cwd),
        capture_output=True,
    ).returncode


if not TEST.exists():
    fail("test_parser.py does not exist")

# 1. Passes against the real parser.
if run_pytest(HERE) != 0:
    fail("test_parser.py does not pass against the correct parser.py")

# 2. Fails against a parser that doesn't enforce the error-path contract.
with tempfile.TemporaryDirectory() as tmp:
    tmp_dir = pathlib.Path(tmp)
    (tmp_dir / "test_parser.py").write_text(TEST.read_text())
    (tmp_dir / "parser.py").write_text(_MUTANT)
    if run_pytest(tmp_dir) == 0:
        fail(
            "test_parser.py passes even against a parser that never raises on an "
            "invalid line — it doesn't test the error path the task requires."
        )

print("PASS")
sys.exit(0)
