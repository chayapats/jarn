"""CI/release workflow contract tests — YAML gates must stay wired."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ACTION_YML = Path(__file__).resolve().parent.parent / "action" / "action.yml"
PR_REVIEW_YML = (
    Path(__file__).resolve().parent.parent / "examples" / "github" / "pr-review.yml"
)
ISSUE_FIX_YML = (
    Path(__file__).resolve().parent.parent / "examples" / "github" / "issue-fix.yml"
)

REPO = Path(__file__).resolve().parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
RELEASE_YML = REPO / ".github" / "workflows" / "release.yml"
NIGHTLY_YML = REPO / ".github" / "workflows" / "nightly.yml"
DEPENDABOT_YML = REPO / ".github" / "dependabot.yml"
PYPROJECT = REPO / "pyproject.toml"
SUPPORTED_PLATFORMS = REPO / "docs" / "SUPPORTED_PLATFORMS.md"
RELEASE_LIFECYCLE = REPO / "scripts" / "release_lifecycle_canary.sh"


def _run_lines(workflow_path: Path, job: str) -> list[str]:
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = workflow["jobs"][job]["steps"]
    return [s["run"] for s in steps if isinstance(s, dict) and "run" in s]


def _uses_names(workflow_path: Path, job: str) -> list[str]:
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = workflow["jobs"][job]["steps"]
    return [s["uses"] for s in steps if isinstance(s, dict) and "uses" in s]


def _all_uses(value: object) -> list[str]:
    if isinstance(value, dict):
        found = [str(value["uses"])] if "uses" in value else []
        return found + [item for child in value.values() for item in _all_uses(child)]
    if isinstance(value, list):
        return [item for child in value for item in _all_uses(child)]
    return []


def _needs(workflow: dict, job_name: str) -> set[str]:
    value = workflow["jobs"][job_name].get("needs", [])
    return {value} if isinstance(value, str) else set(value)


def _dependency_closure(workflow: dict, job_name: str) -> set[str]:
    found: set[str] = set()
    pending = list(_needs(workflow, job_name))
    while pending:
        dependency = pending.pop()
        if dependency in found:
            continue
        found.add(dependency)
        pending.extend(_needs(workflow, dependency))
    return found


def test_ci_has_mypy_step() -> None:
    run_lines = _run_lines(CI_YML, "test")
    assert any("mypy src/" in line for line in run_lines), (
        "ci.yml must invoke 'mypy src/' to gate type errors"
    )


def test_ci_mypy_runs_after_lint() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    steps = workflow["jobs"]["test"]["steps"]
    runs = [s.get("run", "") for s in steps if isinstance(s, dict)]
    lint_idx = next(i for i, r in enumerate(runs) if "ruff check" in r)
    mypy_idx = next(i for i, r in enumerate(runs) if "mypy src/" in r)
    assert mypy_idx > lint_idx, "mypy step must come after the ruff Lint step"


def test_ci_lints_scripts() -> None:
    run_lines = _run_lines(CI_YML, "test")
    assert any("ruff check src tests scripts" in line for line in run_lines), (
        "ci.yml must lint scripts/ alongside src and tests"
    )


def test_ci_has_coverage_gate() -> None:
    run_lines = _run_lines(CI_YML, "test")
    test_cmd = next(line for line in run_lines if "pytest" in line)
    assert "--cov=src/jarn" in test_cmd, "ci.yml must run pytest with --cov=src/jarn"
    assert "--cov-fail-under=" in test_cmd, "ci.yml must enforce a coverage floor"


def test_ci_has_windows_matrix() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    os_list = workflow["jobs"]["test"]["strategy"]["matrix"]["os"]
    assert "windows-latest" in os_list, "ci.yml must include windows-latest in the test matrix"


def test_ci_has_security_job() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    assert "security" in workflow["jobs"], "ci.yml must define a security job"
    run_lines = _run_lines(CI_YML, "security")
    assert any("pip-audit" in line for line in run_lines), (
        "security job must run pip-audit"
    )
    uses = _uses_names(CI_YML, "security")
    assert any("gitleaks" in name for name in uses), "security job must run gitleaks"


def test_every_external_github_action_is_pinned_to_full_commit_sha() -> None:
    workflow_files = [
        *sorted((REPO / ".github" / "workflows").glob("*.yml")),
        *sorted((REPO / ".github" / "workflows").glob("*.yaml")),
        ACTION_YML,
        PR_REVIEW_YML,
        ISSUE_FIX_YML,
    ]
    violations: list[str] = []
    for workflow_path in workflow_files:
        parsed = yaml.safe_load(workflow_path.read_text())
        for use in _all_uses(parsed):
            if use.startswith("./"):
                continue
            if re.fullmatch(r"[^@\s]+@[0-9a-fA-F]{40}", use) is None:
                violations.append(f"{workflow_path.relative_to(REPO)}: {use}")
    assert not violations, "external actions must use immutable 40-char SHAs:\n" + "\n".join(
        violations
    )


def test_ci_actionlint_fail_closed_covers_release_workflow() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    assert "actionlint" in workflow["jobs"]
    joined = "\n".join(_run_lines(CI_YML, "actionlint"))
    assert "actionlint_1.7.12" in joined
    assert "sha256sum -c" in joined
    assert "./actionlint" in joined
    assert ".github/workflows/release.yml" in joined
    assert ".github/workflows/ci.yml" in joined


def test_ci_publishes_machine_readable_performance_evidence() -> None:
    workflow = yaml.safe_load(CI_YML.read_text())
    assert "performance" in workflow["jobs"]
    joined = "\n".join(_run_lines(CI_YML, "performance"))
    assert "scripts/benchmark_startup.py" in joined
    assert "--output performance.json" in joined
    uses = _uses_names(CI_YML, "performance")
    assert any("actions/upload-artifact" in use for use in uses)


def test_dependabot_configured() -> None:
    config = yaml.safe_load(DEPENDABOT_YML.read_text())
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert "pip" in ecosystems, "dependabot must watch pip/uv.lock"
    assert "npm" in ecosystems, "dependabot must watch npm/"


def test_release_has_preflight_job() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    assert "preflight" in workflow["jobs"], "release.yml must define a preflight job"


def test_release_publish_jobs_need_preflight() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    for job in ("pypi", "npm"):
        closure = _dependency_closure(workflow, job)
        assert {
            "preflight",
            "security",
            "contracts",
            "supply_chain",
            "draft_canary",
            "tier1_linux_lifecycle",
            "tier1_macos_lifecycle",
            "upgrade_canary",
            "release_gate",
            "promote_release",
            "public_release_canary",
        }.issubset(closure), f"{job} bypasses a required release gate"


def test_release_preflight_runs_ci_gates() -> None:
    run_lines = _run_lines(RELEASE_YML, "preflight")
    joined = "\n".join(run_lines)
    assert 'os.environ["GITHUB_REF_NAME"]' in joined
    assert 'Path("pyproject.toml")' in joined
    assert 'Path("src/jarn/version.py")' in joined
    assert "release version mismatch" in joined
    assert "ruff check src tests scripts" in joined
    assert "mypy src/" in joined
    assert "pytest -q" in joined
    assert "test_packaging.py" in joined


def test_release_security_is_an_early_build_gate() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    security = workflow["jobs"]["security"]
    assert "needs" not in security, "security must run as early as preflight"
    joined = "\n".join(_run_lines(RELEASE_YML, "security"))
    assert "pip-audit==" in joined
    assert "test_permission_bypass_matrix.py" in joined
    assert "test_approval_contract.py" in joined
    assert any("gitleaks" in use for use in _uses_names(RELEASE_YML, "security"))
    for build_job in ("python_dist", "binaries"):
        closure = _dependency_closure(workflow, build_job)
        assert {"preflight", "security", "contracts"}.issubset(closure)


def test_release_executes_local_transaction_contracts_on_glibc_235() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    assert workflow["jobs"]["contracts"]["runs-on"] == "ubuntu-22.04"
    joined = "\n".join(_run_lines(RELEASE_YML, "contracts"))
    assert 'getconf GNU_LIBC_VERSION)" = "glibc 2.35"' in joined
    for selector in (
        "test_exact_npm_shadow_plus_glibc_regression_is_not_false_success",
        "test_upgrade_retains_prior_binary_as_rollback_candidate",
        "test_post_activation_smoke_failure_automatically_restores_prior",
        "tests/test_update.py",
        "tests/test_uninstall_ga.py",
        "tests/test_ga_config_migrations.py",
    ):
        assert selector in joined


def test_release_binaries_smoke_after_build() -> None:
    run_lines = _run_lines(RELEASE_YML, "binaries")
    joined = "\n".join(run_lines)
    assert "./dist/jarn --version" in joined
    assert "./dist/jarn doctor --json" in joined


def test_release_binary_runs_on_oldest_supported_glibc() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    run_lines = _run_lines(RELEASE_YML, "binaries")
    joined = "\n".join(run_lines)
    assert joined.count("docker run --rm") >= 2
    assert "ubuntu:20.04" in joined
    assert "Build Linux binary on the oldest supported glibc" in str(
        workflow["jobs"]["binaries"]["steps"]
    )
    build_rows = workflow["jobs"]["binaries"]["strategy"]["matrix"]["include"]
    mac_row = next(row for row in build_rows if row["target"] == "darwin-arm64")
    assert mac_row["os"] == "macos-15"
    assert "/jarn --version" in joined


def test_release_publishes_binary_checksums() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    checksums = workflow["jobs"]["checksums"]
    assert {"python_dist", "binaries", "npm_build"}.issubset(set(checksums["needs"]))
    joined = "\n".join(_run_lines(RELEASE_YML, "checksums"))
    assert 'sha256sum "$artifact"' in joined
    assert 'done < "$artifact_list" > checksums.txt' in joined
    assert "sha256sum -c checksums.txt" in joined
    assert "install -m 0755 install.sh release-subjects/install.sh" in joined
    assert "install.sh\" {n++}" in joined
    assert any("upload-artifact" in name for name in _uses_names(RELEASE_YML, "checksums"))

    release_assets = workflow["jobs"]["release_assets"]
    assert {"checksums", "supply_chain"}.issubset(set(release_assets["needs"]))
    attach = next(
        step
        for step in release_assets["steps"]
        if "action-gh-release" in step.get("uses", "")
    )
    files = attach["with"]["files"]
    assert "release-subjects/install.sh" in files
    assert "release-subjects/checksums.txt" in files
    assert attach["with"]["fail_on_unmatched_files"] is True
    assert attach["with"]["draft"] is True
    assert attach["with"]["make_latest"] is False


def test_release_checksum_and_provenance_steps_execute_on_local_fixtures(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    checksum_script = next(
        step["run"]
        for step in workflow["jobs"]["checksums"]["steps"]
        if step.get("name") == "Assemble and verify canonical subjects"
    )
    for target in ("linux-x64", "linux-arm64", "darwin-arm64"):
        binary = tmp_path / "binaries" / f"binary-{target}" / "jarn"
        binary.parent.mkdir(parents=True)
        binary.write_text(f"#!/bin/sh\necho {target}\n", encoding="utf-8")
        binary.chmod(0o755)
    (tmp_path / "python-dist").mkdir()
    (tmp_path / "python-dist" / "jarn-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "python-dist" / "jarn-1.0.0.tar.gz").write_bytes(b"sdist")
    (tmp_path / "npm-tarballs").mkdir()
    for index in range(4):
        (tmp_path / "npm-tarballs" / f"jarn-cli-fixture-{index}.tgz").write_bytes(
            f"npm-{index}".encode()
        )
    (tmp_path / "install.sh").write_bytes((REPO / "install.sh").read_bytes())

    result = subprocess.run(
        ["bash", "-c", checksum_script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    checksum_lines = (tmp_path / "release-subjects" / "checksums.txt").read_text().splitlines()
    names = [line.split(maxsplit=1)[1].lstrip("* ") for line in checksum_lines]
    assert len(names) == 10
    assert names.count("install.sh") == 1
    assert names == sorted(names)

    provenance_script = next(
        step["run"]
        for step in workflow["jobs"]["supply_chain"]["steps"]
        if step.get("name") == "Write deterministic build-provenance statement"
    )
    env = os.environ.copy()
    env.update(
        {
            "PROVENANCE_REPOSITORY": "example/jarn",
            "PROVENANCE_REF": "refs/tags/v1.0.0",
            "PROVENANCE_SHA": "a" * 40,
            "PROVENANCE_RUN_ID": "123",
            "PROVENANCE_RUN_ATTEMPT": "1",
        }
    )
    result = subprocess.run(
        ["bash", "-c", provenance_script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    statement = json.loads(
        (tmp_path / "supply-chain" / "provenance.intoto.json").read_text()
    )
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == "https://slsa.dev/provenance/v1"
    assert {subject["name"] for subject in statement["subject"]} == set(names)


def test_release_generates_and_publishes_dual_format_sboms() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    job = workflow["jobs"]["supply_chain"]
    assert _needs(workflow, "supply_chain") == {"checksums"}

    uses = _uses_names(RELEASE_YML, "supply_chain")
    assert sum("anchore/sbom-action" in use for use in uses) == 2
    assert any("actions/upload-artifact" in use for use in uses)
    assert not any("softprops/action-gh-release" in use for use in uses)

    steps = job["steps"]
    formats = {
        step.get("with", {}).get("format")
        for step in steps
        if isinstance(step, dict)
    }
    assert {"spdx-json", "cyclonedx-json"}.issubset(formats)
    outputs = {
        step.get("with", {}).get("output-file", "")
        for step in steps
        if isinstance(step, dict)
    }
    assert any(path.endswith(".spdx.json") for path in outputs)
    assert any(path.endswith(".cdx.json") for path in outputs)
    assert all(
        step.get("with", {}).get("upload-release-assets") is False
        for step in steps
        if "anchore/sbom-action" in step.get("uses", "")
    )
    joined = "\n".join(_run_lines(RELEASE_YML, "supply_chain"))
    assert "provenance.intoto.json" in joined
    assert "sha256sum -c checksums.txt" in joined


def test_release_signs_provenance_and_sboms_and_verifies_them() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    job = workflow["jobs"]["supply_chain"]
    permissions = job["permissions"]
    assert permissions["id-token"] == "write"
    assert permissions["attestations"] == "write"
    assert permissions["artifact-metadata"] == "write"

    steps = job["steps"]
    attest_steps = [
        step
        for step in steps
        if isinstance(step, dict) and "actions/attest" in step.get("uses", "")
    ]
    assert len(attest_steps) == 3
    assert sum(step.get("name") == "Attest SPDX SBOM" for step in steps) == 1
    assert all(step.get("uses") and step.get("with") for step in attest_steps)
    assert all(
        step.get("with", {}).get("subject-checksums")
        == "release-subjects/checksums.txt"
        for step in attest_steps
    )
    assert any("sbom-path" not in step.get("with", {}) for step in attest_steps)
    assert sum("sbom-path" in step.get("with", {}) for step in attest_steps) == 2
    joined = "\n".join(_run_lines(RELEASE_YML, "supply_chain"))
    assert "gh attestation verify" in joined
    assert "provenance.sigstore.json" in joined


def test_supply_chain_and_all_canaries_precede_registry_publish() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    assert {
        "security",
        "contracts",
        "supply_chain",
        "draft_canary",
        "tier1_linux_lifecycle",
        "tier1_macos_lifecycle",
        "upgrade_canary",
    }.issubset(_needs(workflow, "release_gate"))
    assert _needs(workflow, "promote_release") == {"release_gate"}
    assert _needs(workflow, "public_release_canary") == {"promote_release"}
    assert _needs(workflow, "pypi") == {"public_release_canary"}
    assert _needs(workflow, "npm") == {"public_release_canary"}
    assert "release_assets" in _dependency_closure(workflow, "draft_canary")
    assert "release_assets" in _dependency_closure(workflow, "upgrade_canary")

    publish_jobs = set()
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if "gh-action-pypi-publish" in step.get("uses", "") or "npm publish" in step.get(
                "run", ""
            ):
                publish_jobs.add(job_name)
    assert publish_jobs == {"pypi", "npm"}


def test_release_npm_smoke_before_publish() -> None:
    joined = "\n".join(_run_lines(RELEASE_YML, "npm_build"))
    assert "jarn-cli-linux-x64/bin/jarn --version" in joined
    assert "npm pack" in joined
    assert "npm publish" not in joined

    publish = "\n".join(_run_lines(RELEASE_YML, "npm"))
    assert 'npm publish "release-subjects/$filename"' in publish
    assert "--provenance" in publish


def test_release_canary_fetches_authenticated_draft_before_promotion() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    canary = workflow["jobs"]["draft_canary"]
    assert _needs(workflow, "draft_canary") == {"release_assets"}
    assert canary["runs-on"] == "ubuntu-22.04"
    assert canary["permissions"] == {"contents": "write"}
    checkout = next(step for step in canary["steps"] if "actions/checkout" in step.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False
    joined = "\n".join(_run_lines(RELEASE_YML, "draft_canary"))
    assert "gh release download" in joined
    assert joined.index("unset GH_TOKEN") < joined.index("canary/jarn-linux-x86_64 --version")
    for mutation in ("gh release create", "gh release edit", "gh release upload", "gh release delete"):
        assert mutation not in joined
    assert "--json isDraft" in joined
    assert 'test "$state" = true' in joined
    assert "file://$GITHUB_WORKSPACE/canary/draft-origin" in joined
    assert "raw.githubusercontent.com" not in joined
    assert "https://github.com/" not in joined
    assert "--method binary" in joined
    assert '"$rc" -eq 10' in joined
    assert "install.json" in joined
    assert 'getconf GNU_LIBC_VERSION)" = "glibc 2.35"' in joined
    assert "ubuntu:20.04" in joined
    assert 'npm_path=$(command -v npm)' in joined
    assert "install --global --prefix /usr/local" in joined
    assert "/usr/local/bin/jarn" in joined
    assert "GLIBC_2.38" in joined
    assert "JARN_INSTALLER_UNDER_TEST" in str(canary["steps"])


def test_release_tier1_lifecycle_matrix_matches_documented_platforms() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    linux = workflow["jobs"]["tier1_linux_lifecycle"]
    assert _needs(workflow, "tier1_linux_lifecycle") == {"release_assets"}
    rows = linux["strategy"]["matrix"]["include"]
    expected_platforms = {
        "ubuntu-20.04",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "debian-11",
        "debian-12",
    }
    assert {(row["platform"], row["arch"]) for row in rows} == {
        (distro, arch) for distro in expected_platforms for arch in ("x86_64", "arm64")
    }
    for row in rows:
        if row["arch"] == "arm64":
            assert str(row["runner"]).endswith("-arm")
            assert row["docker_arch"] == "arm64"
            assert row["asset"] == "jarn-linux-arm64"
        else:
            assert row["runner"] == "ubuntu-22.04"
            assert row["docker_arch"] == "amd64"
            assert row["asset"] == "jarn-linux-x86_64"
    linux_script = "\n".join(_run_lines(RELEASE_YML, "tier1_linux_lifecycle"))
    assert "scripts/release_lifecycle_canary.sh" in linux_script
    assert "docker run --rm --platform" in linux_script
    assert "apt-get install" in linux_script and "curl" in linux_script

    macos = workflow["jobs"]["tier1_macos_lifecycle"]
    assert _needs(workflow, "tier1_macos_lifecycle") == {"release_assets"}
    mac_rows = macos["strategy"]["matrix"]["include"]
    assert {
        (
            row["runner"],
            row["arch"],
            row["method"],
            row["expected_method"],
            row["asset"],
        )
        for row in mac_rows
    } == {
        ("macos-15", "arm64", "binary", "binary", "jarn-macos-arm64"),
        ("macos-15-intel", "x86_64", "auto", "python", ""),
        ("macos-26", "arm64", "binary", "binary", "jarn-macos-arm64"),
        ("macos-26-intel", "x86_64", "auto", "python", ""),
    }
    mac_script = "\n".join(_run_lines(RELEASE_YML, "tier1_macos_lifecycle"))
    assert "scripts/release_lifecycle_canary.sh" in mac_script
    assert "JARN_CANARY_UV_BIN" in mac_script

    support = SUPPORTED_PLATFORMS.read_text(encoding="utf-8")
    tier1, compatibility = support.split("## Compatibility tier", maxsplit=1)
    for label in ("Ubuntu 20.04, 22.04, 24.04", "Debian 11, 12", "macOS 15 and 26"):
        assert label in tier1
    assert "WSL2" not in tier1
    assert "WSL2" in compatibility
    assert "not Tier 1" in compatibility


@pytest.mark.skipif(os.name == "nt", reason="release lifecycle harness is POSIX-only")
def test_release_lifecycle_harness_executes_local_binary_fixture(tmp_path: Path) -> None:
    from jarn.version import __version__

    machine = platform.machine().lower()
    target = (platform.system(), machine)
    assets = {
        ("Linux", "x86_64"): "jarn-linux-x86_64",
        ("Linux", "amd64"): "jarn-linux-x86_64",
        ("Linux", "aarch64"): "jarn-linux-arm64",
        ("Linux", "arm64"): "jarn-linux-arm64",
        ("Darwin", "arm64"): "jarn-macos-arm64",
    }
    if target not in assets:
        pytest.skip(f"no binary installer contract for local {target!r}")

    subjects = tmp_path / "release-subjects"
    subjects.mkdir()
    shutil.copy2(REPO / "install.sh", subjects / "install.sh")
    candidate = subjects / assets[target]
    candidate.write_text(
        "#!/bin/sh\n" f"exec {shlex.quote(sys.executable)} -m jarn \"$@\"\n",
        encoding="utf-8",
    )
    candidate.chmod(0o755)
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    (subjects / "checksums.txt").write_text(
        f"{digest}  {candidate.name}\n", encoding="utf-8"
    )

    env = os.environ.copy()
    env.update(
        {
            "JARN_CANARY_SUBJECTS": str(subjects),
            "JARN_CANARY_VERSION": __version__,
            "JARN_CANARY_REPO": "example/jarn",
            "JARN_CANARY_METHOD": "binary",
            "JARN_CANARY_EXPECT_METHOD": "binary",
            "JARN_CANARY_ASSET": candidate.name,
            "JARN_MIN_DISK_KB": "1",
        }
    )
    result = subprocess.run(
        [str(RELEASE_LIFECYCLE)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "release lifecycle canary passed" in result.stdout


def test_release_promotion_is_a_fail_closed_post_canary_step() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    promote = workflow["jobs"]["promote_release"]
    assert _needs(workflow, "promote_release") == {"release_gate"}
    assert promote["if"] == "${{ vars.JARN_GA_PROMOTE_TAG == github.ref_name }}"
    assert promote["permissions"] == {"contents": "write"}
    joined = "\n".join(_run_lines(RELEASE_YML, "promote_release"))
    assert 'test "$before" = true' in joined
    assert 'gh release edit "$GITHUB_REF_NAME"' in joined
    assert "--draft=false" in joined
    assert 'test "$after" = false' in joined

    release_jobs = workflow["jobs"]
    public_mutators = [
        name
        for name, job in release_jobs.items()
        if any("gh release edit" in step.get("run", "") for step in job.get("steps", []))
    ]
    assert public_mutators == ["promote_release"]


def test_release_promotion_script_refuses_non_draft_fixture(tmp_path: Path) -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["promote_release"]["steps"]
        if "gh release edit" in step.get("run", "")
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$GH_LOG"
if [ "$1 $2" = "release view" ]; then
  count=$(cat "$GH_COUNT")
  if [ "$count" -eq 0 ]; then
    printf '%s\n' "$GH_FIRST_STATE"
  else
    printf '%s\n' "$GH_SECOND_STATE"
  fi
  printf '%s\n' "$((count + 1))" > "$GH_COUNT"
  exit 0
fi
if [ "$1 $2" = "release edit" ]; then
  : > "$GH_EDITED"
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    count = tmp_path / "count"
    log = tmp_path / "gh.log"
    edited = tmp_path / "edited"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "GH_TOKEN": "fixture-only",
            "GH_COUNT": str(count),
            "GH_LOG": str(log),
            "GH_EDITED": str(edited),
            "GITHUB_REF_NAME": "v1.2.3",
            "GITHUB_REPOSITORY": "example/jarn",
        }
    )

    count.write_text("0\n", encoding="utf-8")
    env.update({"GH_FIRST_STATE": "false", "GH_SECOND_STATE": "false"})
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert not edited.exists(), "a non-draft release must never be edited/published"
    assert "release edit" not in log.read_text(encoding="utf-8")

    count.write_text("0\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    env.update({"GH_FIRST_STATE": "true", "GH_SECOND_STATE": "false"})
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert edited.exists()
    assert "release edit v1.2.3" in log.read_text(encoding="utf-8")


def test_public_release_canary_blocks_registries_and_uses_anonymous_urls() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    assert _needs(workflow, "public_release_canary") == {"promote_release"}
    assert _needs(workflow, "pypi") == {"public_release_canary"}
    assert _needs(workflow, "npm") == {"public_release_canary"}
    joined = "\n".join(_run_lines(RELEASE_YML, "public_release_canary"))
    assert "unset GH_TOKEN GITHUB_TOKEN" in joined
    assert "https://raw.githubusercontent.com/$GITHUB_REPOSITORY/main/install.sh" in joined
    assert (
        "https://raw.githubusercontent.com/$GITHUB_REPOSITORY/$RELEASE_VERSION/install.sh"
        in joined
    )
    assert (
        "https://github.com/$GITHUB_REPOSITORY/releases/download/$RELEASE_VERSION"
        in joined
    )
    for asset in (
        "checksums.txt",
        "install.sh",
        "jarn-linux-x86_64",
        "jarn-linux-arm64",
        "jarn-macos-arm64",
    ):
        assert asset in joined
    assert "--retry-all-errors" in joined
    assert "JARN_GITHUB_BASE" not in joined
    assert 'sh "$canonical_installer"' in joined
    assert '"$install_rc" -ne 10' in joined
    assert "move the GitHub release back to draft" in joined
    assert "yank the exact PyPI release" in joined
    assert "deprecate the exact npm versions" in joined


@pytest.mark.parametrize("corrupt_asset", [False, True])
def test_public_release_canary_script_executes_and_fails_closed(
    tmp_path: Path,
    corrupt_asset: bool,
) -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["public_release_canary"]["steps"]
        if step.get("name") == "Fetch and execute credential-free public release"
    )
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    executed = tmp_path / "installer-executed"
    installer = fixture / "install.sh"
    installer.write_text(
        """#!/bin/sh
set -eu
version=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = --version ]; then version=$2; shift 2; else shift; fi
done
mkdir -p "$HOME/.local/bin" "$HOME/.local/state/jarn"
cat > "$HOME/.local/bin/jarn" <<'SH'
#!/bin/sh
case "${1:-}" in
  --version) printf '%s\n' 'jarn 1.2.3' ;;
  --help) printf '%s\\n' 'fixture help' ;;
  *) exit 0 ;;
esac
SH
chmod 755 "$HOME/.local/bin/jarn"
printf '{"version": "%s"}\\n' "$version" > "$HOME/.local/state/jarn/install.json"
: > "$PUBLIC_EXECUTED"
exit 10
""",
        encoding="utf-8",
    )
    installer.chmod(0o755)
    binary = fixture / "jarn-linux-x86_64"
    binary.write_text(
        """#!/bin/sh
case "${1:-}" in
  --version) printf '%s\n' 'jarn 1.2.3' ;;
  --help) printf '%s\n' 'fixture help' ;;
  *) exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    (fixture / "jarn-linux-arm64").write_bytes(b"arm64 fixture\n")
    (fixture / "jarn-macos-arm64").write_bytes(b"macos fixture\n")
    names = [
        "install.sh",
        "jarn-linux-x86_64",
        "jarn-linux-arm64",
        "jarn-macos-arm64",
    ]
    (fixture / "checksums.txt").write_text(
        "".join(
            f"{hashlib.sha256((fixture / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/bin/sh
set -eu
[ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ] || exit 97
output=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) output=$2; shift 2 ;;
    https://*) url=$1; shift ;;
    *) shift ;;
  esac
done
[ -n "$output" ] && [ -n "$url" ]
case "$url" in
  https://raw.githubusercontent.com/*/install.sh) source="$PUBLIC_FIXTURE/install.sh" ;;
  */releases/download/*/checksums.txt) source="$PUBLIC_FIXTURE/checksums.txt" ;;
  */releases/download/*/install.sh) source="$PUBLIC_FIXTURE/install.sh" ;;
  */releases/download/*/jarn-linux-x86_64) source="$PUBLIC_FIXTURE/jarn-linux-x86_64" ;;
  */releases/download/*/jarn-linux-arm64) source="$PUBLIC_FIXTURE/jarn-linux-arm64" ;;
  */releases/download/*/jarn-macos-arm64) source="$PUBLIC_FIXTURE/jarn-macos-arm64" ;;
  *) exit 98 ;;
esac
cp "$source" "$output"
if [ "${CORRUPT_PUBLIC_ASSET:-}" = 1 ] && echo "$url" | grep -q jarn-linux-arm64; then
  printf '%s\n' corrupt >> "$output"
fi
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    summary = tmp_path / "summary.md"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PUBLIC_FIXTURE": str(fixture),
            "PUBLIC_EXECUTED": str(executed),
            "GITHUB_REPOSITORY": "example/jarn",
            "RELEASE_VERSION": "v1.2.3",
            "GITHUB_STEP_SUMMARY": str(summary),
            "GH_TOKEN": "must-be-unset",
            "GITHUB_TOKEN": "must-also-be-unset",
            "CORRUPT_PUBLIC_ASSET": "1" if corrupt_asset else "0",
        }
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if corrupt_asset:
        assert result.returncode != 0
        assert not executed.exists(), "checksum failure must precede installer execution"
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert executed.exists()


def test_release_bootstraps_real_prior_upgrade_lifecycles_with_preserved_data() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    history = "\n".join(_run_lines(RELEASE_YML, "release_history"))
    assert "len(selected) == 2" in history
    assert "if not selected" in history
    assert '{"jarn-linux-x86_64", "checksums.txt"}.issubset(assets)' in history
    assert "at least one prior stable release" in history

    upgrade = workflow["jobs"]["upgrade_canary"]
    assert {"release_assets", "release_history"} == _needs(workflow, "upgrade_canary")
    assert upgrade["runs-on"] == "ubuntu-24.04"
    assert upgrade["permissions"] == {"contents": "write"}
    assert "fromJSON(needs.release_history.outputs.versions)" in str(
        upgrade["strategy"]["matrix"]["prior"]
    )
    joined = "\n".join(_run_lines(RELEASE_YML, "upgrade_canary"))
    assert 'getconf GNU_LIBC_VERSION)" = "glibc 2.39"' in joined
    assert 'download_assets "v$PRIOR_VERSION"' in joined
    assert 'cp "$prior_dir/jarn-linux-x86_64" "$active"' in joined
    assert '"schema_version": 1' in joined
    assert '"version": os.environ["PRIOR"]' in joined
    assert '"method": "binary"' in joined
    assert '"active_path": str(active)' in joined
    assert '"libc_version": "2.39"' in joined
    assert '"setup_status": "skipped"' in joined
    assert 'sh "$prior_dir/install.sh"' not in joined
    assert '"$current_dir/jarn-linux-x86_64" update --version "$current_number"' in joined
    assert "JARN_UPDATE_CANARY_MODE=1" in joined
    assert "http://127.0.0.1:" in joined
    assert joined.index("unset GH_TOKEN") < joined.index(
        '"$current_dir/jarn-linux-x86_64" update --version'
    )
    for mutation in ("gh release create", "gh release edit", "gh release upload", "gh release delete"):
        assert mutation not in joined
    assert 'sh "$current_dir/install.sh" --version "$current_number"' in joined
    assert joined.count("rollback --json") == 1
    assert joined.count('run_rollback_stage "') == 2
    assert 'run_rollback_stage "first rollback to $PRIOR_VERSION"' in joined
    assert 'run_rollback_stage "forward rollback to ${CURRENT_VERSION#v}"' in joined
    assert "upgrade canary: %s failed (exit %s)" in joined
    assert 'sed -n \'1,200p\' "$rollback_output" "$rollback_error"' in joined
    assert "uninstall --yes --executable --dependencies" in joined
    assert "Preserved: config, sessions, cache, credentials." in joined
    for sentinel in (
        "config.yaml",
        "sessions/canary",
        "memory/canary",
        "skills/canary",
        "cache/canary",
        "secrets/canary",
    ):
        assert sentinel in joined


def test_release_history_step_executes_and_fails_closed_with_fixtures(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["release_history"]["steps"]
        if step.get("id") == "prior"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text('#!/bin/sh\ncat "$FAKE_RELEASES"\n', encoding="utf-8")
    fake_gh.chmod(0o755)
    releases_path = tmp_path / "fixture-releases.json"
    output = tmp_path / "github-output"

    def release(tag: str, *, compatible: bool = True) -> dict[str, object]:
        names = (
            ["jarn-linux-x86_64", "checksums.txt"]
            if compatible
            else ["checksums.txt"]
        )
        return {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "assets": [{"name": name} for name in names],
        }

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_RELEASES": str(releases_path),
            "GH_TOKEN": "fixture-only",
            "GITHUB_REPOSITORY": "example/jarn",
            "GITHUB_REF_NAME": "v3.0.0",
            "GITHUB_OUTPUT": str(output),
        }
    )
    releases_path.write_text(
        json.dumps(
            [
                release("v3.0.0"),
                release("v2.5.0", compatible=False),
                release("v2.0.0"),
                release("v1.0.0"),
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == 'versions=["2.0.0","1.0.0"]\n'

    output.unlink()
    releases_path.write_text(json.dumps([release("v2.0.0")]), encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == 'versions=["2.0.0"]\n'

    output.unlink()
    releases_path.write_text(
        json.dumps([release("v2.0.0", compatible=False)]), encoding="utf-8"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "requires at least one prior stable release" in result.stderr
    assert not output.exists()


def test_release_run_blocks_are_locally_bash_syntax_checked() -> None:
    workflow = yaml.safe_load(RELEASE_YML.read_text())
    failures: list[str] = []
    for job_name, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps", [])):
            script = step.get("run")
            if not isinstance(script, str):
                continue
            # GitHub resolves expressions before invoking Bash. Replace them with
            # one inert word so local `bash -n` checks the exact remaining script.
            local_script = re.sub(r"\$\{\{[^{}]+\}\}", "github_value", script)
            result = subprocess.run(
                ["bash", "-n", "-c", local_script],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                failures.append(f"{job_name} step {index}: {result.stderr.strip()}")
    assert not failures, "release shell syntax failures:\n" + "\n".join(failures)


def test_pyproject_pins_pyinstaller_in_build_extra() -> None:
    text = PYPROJECT.read_text()
    assert "[project.optional-dependencies]" in text
    assert re.search(r'build\s*=\s*\[\s*"pyinstaller==', text), (
        "pyproject.toml must pin pyinstaller in the build extra"
    )


def test_nightly_eval_workflow_exists() -> None:
    workflow = yaml.safe_load(NIGHTLY_YML.read_text())
    job = workflow["jobs"]["eval"]
    assert job.get("continue-on-error") is True
    steps = job["steps"]
    gate = next(s for s in steps if s.get("id") == "gate")
    assert "secrets.NIGHTLY_EVAL_ENABLED" in gate["env"]["NIGHTLY_EVAL_ENABLED"]
    run_lines = _run_lines(NIGHTLY_YML, "eval")
    assert any("scripts/eval.py" in line for line in run_lines)


# ---------------------------------------------------------------------------
# T-4-7: GitHub Action + PR bot
# ---------------------------------------------------------------------------


def test_action_yaml_valid() -> None:
    """action/action.yml parses as valid YAML, has required inputs, and pins
    jarn-cli at the correct major.minor matching version.py."""
    from jarn.version import __version__

    assert ACTION_YML.exists(), "action/action.yml must exist"
    doc = yaml.safe_load(ACTION_YML.read_text())

    # Must be a composite action with the required inputs.
    inputs = doc.get("inputs", {})
    assert "prompt" in inputs, "action must declare a 'prompt' input"
    assert inputs["prompt"].get("required") is True, "'prompt' input must be required"
    assert "api_key" in inputs, "action must declare an 'api_key' input"
    assert inputs["api_key"].get("required") is True, "'api_key' input must be required"

    # Pinned jarn-cli version must match version.py major.minor (anti-drift guard).
    major, minor = __version__.split(".")[:2]
    expected_pin = f"jarn-cli@{major}.{minor}"
    # Walk all run steps in the composite action.
    steps = doc.get("runs", {}).get("steps", [])
    install_step = next(
        (s for s in steps if isinstance(s, dict) and "run" in s and "npm i" in s["run"]),
        None,
    )
    assert install_step is not None, "action must have an npm install step"
    assert expected_pin in install_step["run"], (
        f"action must pin '{expected_pin}' — got: {install_step['run']!r}"
    )


def test_action_bootstraps_fresh_runner_config_without_inlining_secret() -> None:
    """The composite action must satisfy headless's required-config contract."""
    raw = ACTION_YML.read_text()
    match = re.search(
        r"cat > \"\$JARN_HOME/config\.yaml\" <<'JARN_CONFIG'\n"
        r"(?P<body>.*?)\n\s+JARN_CONFIG",
        raw,
        re.DOTALL,
    )
    assert match is not None, "action must create JARN_HOME/config.yaml"
    config_text = textwrap.dedent(match.group("body"))
    config = yaml.safe_load(config_text)

    assert config["default_profile"] == "openrouter"
    assert config["providers"]["openrouter"]["api_key"] == "${OPENROUTER_API_KEY}"
    assert not re.search(r"sk-[A-Za-z0-9]", config_text)
    assert config["verify"]["gate"] == "auto"
    assert config["verify"]["max_repair_rounds"] == 1
    assert "JARN_HOME=$JARN_HOME" in raw


def test_action_qualifies_openrouter_model_slug() -> None:
    raw = ACTION_YML.read_text()
    assert '--model "openrouter/$MODEL_OVERRIDE"' in raw
    assert '[[ "$MODEL_OVERRIDE" == openrouter/* ]]' in raw
    assert "--ignore-project-config" in raw


def test_example_workflows_parse() -> None:
    """pr-review.yml and issue-fix.yml parse as valid YAML, each has a
    permissions block, and each references secrets.* (hygiene guard)."""
    for path in (PR_REVIEW_YML, ISSUE_FIX_YML):
        assert path.exists(), f"{path.name} must exist"
        doc = yaml.safe_load(path.read_text())
        raw = path.read_text()

        # Must have a permissions block somewhere in the workflow.
        assert "permissions" in doc or "permissions" in raw, (
            f"{path.name} must declare a permissions block"
        )

        # Must reference secrets.* — never a literal key.
        assert "secrets." in raw, (
            f"{path.name} must source API keys from secrets.* (hygiene guard)"
        )

    # The issue-fix bot grants write access on a comment trigger, so it MUST
    # keep an actor gate — guard against a refactor silently dropping it.
    issue_fix_raw = ISSUE_FIX_YML.read_text()
    assert "author_association" in issue_fix_raw, (
        "issue-fix.yml must gate on github.event.comment.author_association "
        "(actor allowlist) so arbitrary users cannot trigger the write-access bot"
    )
