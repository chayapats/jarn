"""Modal-hosted SWE-bench-lite harness-prompt A/B for J.A.R.N.

Compares J.A.R.N.'s full harness prompt ("jarn-full") against a bare one-line
prompt ("minimal") on real SWE-bench-lite instances. Tools, model, agent loop,
repo and grading are identical between arms — only the system prompt differs, so
any gap is the harness prompt's contribution.

Why Modal: the official per-instance SWE-bench Docker images (repo + deps already
installed, x86_64) are the reliable runnable environment. Modal runs them in the
cloud (free compute), parallelising across (instance x arm) and sidestepping the
arm64 Mac mismatch.

Two interpreters per container:
* Modal function body = the image's conda Python 3.11 (also where swebench is
  installed, for grading).
* J.A.R.N. needs 3.12+, so it's installed into the add_python 3.12 at /usr/local
  and run as a SUBPROCESS. The agent's shell tools use the repo's conda `testbed`
  env (flask/pytest live there) via a PATH prepend.

LLM tokens (DeepSeek-V4 via OpenRouter) are billed at OpenRouter, not Modal. The
key comes from the Modal Secret ``jarn-openrouter``.

Usage:
    modal run scripts/swebench_modal.py::check_main   # verify the secret
    modal run scripts/swebench_modal.py::ab_main      # the A/B
"""

from __future__ import annotations

import json
import urllib.request

import modal

# Candidate instances — light, pytest-based repos likely within a strong model's
# reach (so the prompt's discipline can change the outcome rather than hit a
# floor/ceiling). Each has its own swebench image.
INSTANCES = [
    "pallets__flask-4992",
    "pylint-dev__pylint-5859",
    "django__django-13447",
    "django__django-12983",
    "django__django-13230",
]

MODEL_REF = "openrouter/deepseek/deepseek-v4-pro"
JARN_PYTHON = "/usr/local/bin/python3"
_WHEEL = "dist/jarn-0.3.0-py3-none-any.whl"

HARNESS_ARMS: list[tuple[str, str | None]] = [
    ("jarn-full", None),
    (
        "minimal",
        "You are a coding assistant working in a terminal. "
        "Use the available tools to complete the task.",
    ),
]

app = modal.App("jarn-swebench-ab")
OPENROUTER_SECRET = modal.Secret.from_name("jarn-openrouter")


def _swebench_image(instance_id: str) -> str:
    # Published images sanitize the instance_id's "__" to "_1776_".
    tag = instance_id.replace("__", "_1776_")
    return f"swebench/sweb.eval.x86_64.{tag}:latest"


def _agent_image(instance_id: str) -> modal.Image:
    """Per-instance image: swebench base + grading lib + J.A.R.N. on Py3.12."""
    return (
        modal.Image.from_registry(_swebench_image(instance_id), add_python="3.12")
        .pip_install("swebench==4.1.0")
        .add_local_file(_WHEEL, "/root/jarn-0.3.0-py3-none-any.whl", copy=True)
        .run_commands(
            "/usr/local/bin/python3 -m pip install /root/jarn-0.3.0-py3-none-any.whl"
        )
    )


# Stand-alone runner executed by JARN_PYTHON (3.12): reads the prompt on stdin,
# runs one headless turn against /testbed with the given prompt-override, prints
# JSON stats on the last stdout line.
JARN_RUNNER = '''
import os, sys, json, asyncio
from pathlib import Path
from jarn.config.loader import load_config
from jarn.headless import _run_headless

prompt = sys.stdin.read()
override = None if os.environ.get("ARM_OVERRIDE_NONE") == "1" else os.environ.get("ARM_OVERRIDE", "")
cfg = load_config(project_root=None, project_trusted=True)
try:
    res = asyncio.run(_run_headless(
        prompt, cfg, Path("/testbed"),
        project_trusted=True, system_prompt_override=override,
    ))
    print(json.dumps({"tool_calls": res.tool_calls, "cost": res.cost, "error": None}))
except Exception as exc:
    print(json.dumps({"tool_calls": 0, "cost": 0.0, "error": f"{type(exc).__name__}: {exc}"}))
'''


def fetch_instance(instance_id: str) -> dict:
    """Pull one SWE-bench_Lite row from the HF datasets-server REST API."""
    for off in (0, 100, 200):
        u = (
            "https://datasets-server.huggingface.co/rows?"
            "dataset=princeton-nlp/SWE-bench_Lite&config=default&split=test"
            f"&offset={off}&length=100"
        )
        for r in json.load(urllib.request.urlopen(u))["rows"]:
            if r["row"]["instance_id"] == instance_id:
                return r["row"]
    raise RuntimeError(f"instance {instance_id} not found in SWE-bench_Lite")


def _write_jarn_config() -> None:
    import os
    from pathlib import Path

    home = Path("/root/.jarn")
    home.mkdir(parents=True, exist_ok=True)
    os.environ["JARN_HOME"] = str(home)
    (home / "config.yaml").write_text(
        "default_profile: openrouter\n"
        f"default_model: {MODEL_REF}\n"
        "permission_mode: yolo\n"
        "providers:\n"
        "  openrouter:\n"
        "    type: openrouter\n"
        "    api_key: ${OPENROUTER_API_KEY}\n"
        "    base_url: https://openrouter.ai/api/v1\n"
    )


def _grade(instance: dict) -> dict:
    """Run swebench's eval-script and report per-test status (official grading)."""
    import subprocess
    from pathlib import Path

    from swebench.harness.grading import get_eval_tests_report, get_logs_eval, get_resolution_status
    from swebench.harness.test_spec.test_spec import make_test_spec

    spec = make_test_spec(instance)
    Path("/tmp/eval.sh").write_text(spec.eval_script)
    proc = subprocess.run(
        ["bash", "/tmp/eval.sh"], capture_output=True, text=True, timeout=900
    )
    Path("/tmp/eval.log").write_text(proc.stdout + "\n" + proc.stderr)
    status_map, found = get_logs_eval(spec, "/tmp/eval.log")
    report = get_eval_tests_report(
        status_map,
        gold_results={
            "FAIL_TO_PASS": json.loads(instance["FAIL_TO_PASS"])
            if isinstance(instance["FAIL_TO_PASS"], str) else instance["FAIL_TO_PASS"],
            "PASS_TO_PASS": json.loads(instance["PASS_TO_PASS"])
            if isinstance(instance["PASS_TO_PASS"], str) else instance["PASS_TO_PASS"],
        },
    )
    return {"found": found, "resolved": get_resolution_status(report), "report": report}


def _reset_testbed() -> None:
    import subprocess
    subprocess.run(["git", "-C", "/testbed", "checkout", "--", "."],
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", "/testbed", "clean", "-fdq"],
                   capture_output=True, text=True)


def _run_one(instance_id: str, arm: str, override: str | None) -> dict:
    """Run ONE arm against ONE instance and grade it, with transient-error retry.

    Each call is a fresh container so /testbed starts pristine. The agent runs as
    a 3.12 subprocess; a network/stream flake is retried (repo reset between tries)
    so one blip doesn't waste the cell.
    """
    import os
    import subprocess
    import time
    from pathlib import Path

    _write_jarn_config()
    instance = fetch_instance(instance_id)
    prompt = instance["problem_statement"]
    Path("/tmp/jarn_runner.py").write_text(JARN_RUNNER)

    sub_env = dict(os.environ)
    sub_env["PATH"] = "/opt/miniconda3/envs/testbed/bin:" + sub_env.get("PATH", "")
    sub_env["JARN_HOME"] = "/root/.jarn"
    if override is None:
        sub_env["ARM_OVERRIDE_NONE"] = "1"
    else:
        sub_env["ARM_OVERRIDE"] = override

    stats = {"tool_calls": 0, "cost": 0.0, "error": "not run"}
    attempts = 0
    t0 = time.monotonic()
    while attempts < 3:
        attempts += 1
        _reset_testbed()
        proc = subprocess.run(
            [JARN_PYTHON, "/tmp/jarn_runner.py"],
            input=prompt, capture_output=True, text=True, env=sub_env, timeout=2400,
        )
        try:
            stats = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            stats = {"tool_calls": 0, "cost": 0.0,
                     "error": f"runner: {proc.stdout[-200:]} || {proc.stderr[-200:]}"}
        # Retry on ANY error (network drops surface as an empty RuntimeError, so
        # keyword-matching transients misses them); a clean run with resolved=NO
        # carries no error and is kept as-is.
        if not stats.get("error"):
            break
    elapsed = round(time.monotonic() - t0, 1)

    diff = subprocess.run(["git", "-C", "/testbed", "diff"],
                          capture_output=True, text=True).stdout
    files_changed = diff.count("\ndiff --git ") + (1 if diff.startswith("diff --git ") else 0)

    grade = _grade(instance)
    return {
        "instance": instance_id,
        "arm": arm,
        "resolved": str(grade["resolved"]),
        "tool_calls": stats.get("tool_calls", 0),
        "files_changed": files_changed,
        "attempts": attempts,
        "elapsed_s": elapsed,
        "agent_error": stats.get("error"),
    }


# One instance per `modal run`, selected by env var (so the Modal function stays
# GLOBAL-scope — source-shipped, no serialized/Python-version coupling). Multiple
# instances are orchestrated in parallel from the shell, each with its own
# JARN_INSTANCE. The two arms run in parallel inside one invocation.
import os  # noqa: E402

INSTANCE = os.environ.get("JARN_INSTANCE", INSTANCES[0])
agent_image = _agent_image(INSTANCE)


@app.function(image=agent_image, secrets=[OPENROUTER_SECRET], timeout=5400)
def run_arm(instance_id: str, arm: str, override: str | None) -> dict:
    # instance_id is passed explicitly (remote containers don't inherit the local
    # JARN_INSTANCE env var); the image is still selected per-run via that env var
    # at local import, so image and instance always match within one invocation.
    return _run_one(instance_id, arm, override)


@app.function(image=agent_image, secrets=[OPENROUTER_SECRET])
def check_secret() -> dict:
    key = os.getenv("OPENROUTER_API_KEY") or ""
    return {"present": bool(key), "len": len(key), "looks_valid": key.startswith("sk-or-")}


@app.local_entrypoint()
def check_main():
    print(json.dumps(check_secret.remote(), indent=2, default=str))


@app.local_entrypoint()
def ab_main():
    """Run both arms for INSTANCE in parallel; print a table + JSON."""
    calls = [(arm, run_arm.spawn(INSTANCE, arm, ov)) for arm, ov in HARNESS_ARMS]
    results = []
    for arm, fc in calls:
        try:
            results.append(fc.get())
        except Exception as exc:  # noqa: BLE001
            results.append({"instance": INSTANCE, "arm": arm, "resolved": "ERROR",
                            "tool_calls": 0, "files_changed": 0, "attempts": 0,
                            "elapsed_s": 0.0, "agent_error": f"{type(exc).__name__}: {exc}"})

    print(f"\n{'INSTANCE':<26} {'ARM':<10} {'RESOLVED':<13} {'TOOLS':>6} "
          f"{'EDITS':>6} {'TRY':>4} {'TIME':>7}  ERROR")
    print("-" * 92)
    for r in sorted(results, key=lambda x: x["arm"]):
        print(f"{r['instance']:<26} {r['arm']:<10} {r['resolved']:<13} "
              f"{r['tool_calls']:>6} {r['files_changed']:>6} {r['attempts']:>4} "
              f"{r['elapsed_s']:>7.1f}  {(r['agent_error'] or '')[:40]}")
    print("\nJSON_RESULTS " + json.dumps(results, default=str))
