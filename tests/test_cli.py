"""CLI subcommand tests (non-interactive paths)."""

from __future__ import annotations

import json

import pytest
import yaml

from jarn.cli import main


def test_profile_flag_removed_errors_with_preset_hint(capsys):
    """`jarn --profile x` exits 2 with a clear error pointing at --preset.

    T-1-9: the flag was removed in v0.6.0; without this guard argparse emits a
    confusing 'invalid choice' error about the subcommand instead of naming the
    removed flag and its replacement.
    """
    with pytest.raises(SystemExit) as exc:
        main(["--profile", "trusted-repo"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--profile" in err
    assert "--preset" in err


def test_profile_flag_equals_form_also_errors_with_hint(capsys):
    """The `--profile=NAME` spelling gets the same clear removal error."""
    with pytest.raises(SystemExit) as exc:
        main(["--profile=ci"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "removed in v0.6.0" in err
    assert "--preset" in err


def test_init_creates_jarn_md(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "Created" in out
    assert (tmp_path / "JARN.md").is_file()


def test_init_refuses_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "JARN.md").write_text("existing\n", encoding="utf-8")
    assert main(["init"]) == 1
    assert "init --force" in capsys.readouterr().err


def test_init_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "JARN.md").write_text("old\n", encoding="utf-8")
    assert main(["init", "--force"]) == 0
    assert "old" not in (tmp_path / "JARN.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# FIX 6: doctor --json surfaces git.autocheckpoint, wiki.enabled,
#         observability.transcript, context.repo_map + token budget
# ---------------------------------------------------------------------------


def _make_doctor_config(tmp_path, *, autocheckpoint=False, wiki_enabled=True,
                        transcript=True, repo_map="tool", repo_map_tokens=512):
    """Write a minimal config YAML and return its path."""
    gp = tmp_path / "config.yaml"
    gp.write_text(
        yaml.safe_dump({
            "default_profile": "openrouter",
            "providers": {
                "openrouter": {
                    "type": "openrouter",
                    "api_key": "sk-test",
                    "base_url": "http://localhost:9999/v1",
                }
            },
            "routing": {"main": "openrouter/some-model"},
            "git": {"autocheckpoint": autocheckpoint},
            "wiki": {"enabled": wiki_enabled},
            "observability": {"transcript": transcript},
            "context": {"repo_map": repo_map, "repo_map_tokens": repo_map_tokens},
        }),
        encoding="utf-8",
    )
    return gp


def test_doctor_json_includes_git_autocheckpoint(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include git.autocheckpoint."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, autocheckpoint=True)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "git" in data, "doctor --json must include 'git' key"
    assert data["git"]["autocheckpoint"] is True


def test_doctor_json_includes_wiki_enabled(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include wiki.enabled."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, wiki_enabled=True)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "wiki" in data, "doctor --json must include 'wiki' key"
    assert data["wiki"]["enabled"] is True


def test_doctor_json_includes_observability_transcript(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include observability.transcript."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, transcript=False)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "observability" in data, "doctor --json must include 'observability' key"
    assert data["observability"]["transcript"] is False


def test_doctor_json_includes_context_repo_map(tmp_path, monkeypatch, capsys):
    """jarn doctor --json must include context.repo_map and context.repo_map_tokens."""
    from jarn import cli
    from jarn.config import paths

    gp = _make_doctor_config(tmp_path, repo_map="auto", repo_map_tokens=2048)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert "context" in data, "doctor --json must include 'context' key"
    assert data["context"]["repo_map"] == "auto"
    assert data["context"]["repo_map_tokens"] == 2048


# ---------------------------------------------------------------------------
# P3.C — headless yolo startup warning (no interactive prompt)
# ---------------------------------------------------------------------------


def _make_headless_config(tmp_path):
    """Write a minimal config YAML and return its path."""
    gp = tmp_path / "config.yaml"
    gp.write_text(
        yaml.safe_dump({
            "default_profile": "openrouter",
            "providers": {
                "openrouter": {
                    "type": "openrouter",
                    "api_key": "sk-test",
                    "base_url": "http://localhost:9999/v1",
                }
            },
            "routing": {"main": "openrouter/some-model"},
        }),
        encoding="utf-8",
    )
    return gp


def test_headless_yolo_prints_warning_to_stderr(tmp_path, monkeypatch, capsys):
    """CLI --permission-mode yolo prints a one-line startup warning (no prompt)."""
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    # This test is about the yolo warning, not trust — keep it deterministic and
    # non-interactive regardless of the cwd's ambient trust state.
    monkeypatch.setattr(
        cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None)
    )

    # Patch run_headless so the test doesn't actually run the agent
    def _fake_run_headless(*a, **k):
        return 0

    monkeypatch.setattr(cli_mod, "run_headless" if hasattr(cli_mod, "run_headless") else "_run_headless",
                        _fake_run_headless, raising=False)
    import jarn.headless as hd
    monkeypatch.setattr(hd, "run_headless", _fake_run_headless)

    # Use _cmd_headless directly to avoid config-not-found guard
    result = cli_mod._cmd_headless(
        prompt_arg="do something",
        permission_mode_override="yolo",
    )
    captured = capsys.readouterr()
    assert result == 0
    assert "yolo" in captured.err.lower()
    assert "warning" in captured.err.lower()
    # Must NOT contain interactive prompt text
    assert "[y/N]" not in captured.err and "[y/n]" not in captured.err.lower()


def test_headless_non_yolo_no_warning(tmp_path, monkeypatch, capsys):
    """CLI --permission-mode ask does NOT emit the yolo warning."""
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    # Deterministic, non-interactive regardless of the cwd's ambient trust state.
    monkeypatch.setattr(
        cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None)
    )

    import jarn.headless as hd

    def _fake_run_headless(*a, **k):
        return 0

    monkeypatch.setattr(hd, "run_headless", _fake_run_headless)

    cli_mod._cmd_headless(
        prompt_arg="do something",
        permission_mode_override="ask",
    )
    captured = capsys.readouterr()
    assert "yolo" not in captured.err.lower()


def test_headless_add_dir_threads_into_run_headless(tmp_path, monkeypatch):
    """`jarn -p ... --add-dir X` (item F): X is validated and threaded into
    run_headless as add_dirs — the documented flag must not silently no-op in -p.
    """
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None)
    )

    extra = tmp_path / "extra"
    extra.mkdir()
    captured: dict = {}

    import jarn.headless as hd

    def _fake_run_headless(prompt, cfg, root, **k):
        captured["add_dirs"] = k.get("add_dirs")
        return 0

    monkeypatch.setattr(hd, "run_headless", _fake_run_headless)

    result = cli_mod._cmd_headless(prompt_arg="do something", add_dirs=[str(extra)])
    assert result == 0
    assert captured["add_dirs"] == [extra.resolve()], (
        "--add-dir must be validated and passed to run_headless in -p mode"
    )


def test_headless_add_dir_invalid_fails_fast(tmp_path, monkeypatch, capsys):
    """A nonexistent --add-dir in -p mode fails fast (fail-closed, not a
    half-promise that silently no-ops)."""
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None)
    )

    import jarn.headless as hd

    monkeypatch.setattr(hd, "run_headless", lambda *a, **k: 0)

    result = cli_mod._cmd_headless(
        prompt_arg="hi", add_dirs=[str(tmp_path / "does-not-exist")]
    )
    assert result == 1
    assert "add-dir" in capsys.readouterr().err.lower()


def test_headless_gates_diagnostics_off(tmp_path, monkeypatch):
    """Headless (-p) forces verify.diagnostics off (item G): ruff/pyright output
    is dropped in -p mode, so paying up to 30s/edit-turn for it is pure latency
    tax. The on-disk config leaves it at the default (``suggest``)."""
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None)
    )

    captured: dict = {}

    import jarn.headless as hd

    def _fake_run_headless(prompt, cfg, root, **k):
        captured["diagnostics"] = cfg.verify.diagnostics
        return 0

    monkeypatch.setattr(hd, "run_headless", _fake_run_headless)

    cli_mod._cmd_headless(prompt_arg="do something")
    assert captured["diagnostics"] == "off"


def test_trust_hooks_cli_writes_marker(tmp_path, monkeypatch, capsys):
    """`jarn trust-hooks` writes the one-time global-hooks accept marker and
    reports its path; a second call is idempotent."""
    from jarn import cli
    from jarn.config import paths
    from jarn.config.trust import GLOBAL_HOOKS_TRUST_MARKER, global_hooks_trusted

    home = tmp_path / "jarnhome"
    monkeypatch.setattr(paths, "global_home", lambda: home)

    assert not global_hooks_trusted()
    rc = cli._cmd_trust_hooks()
    assert rc == 0
    assert global_hooks_trusted()
    assert (home / GLOBAL_HOOKS_TRUST_MARKER).is_file()
    out = capsys.readouterr().out
    assert "global lifecycle hooks accepted" in out.lower()

    # Idempotent: running again still succeeds and keeps the marker.
    rc = cli._cmd_trust_hooks()
    assert rc == 0
    assert global_hooks_trusted()


def test_doctor_warns_custom_jarn_home(tmp_path, monkeypatch, capsys):
    """Non-default JARN_HOME is surfaced in doctor output (secrets/trust redirect)."""
    from jarn import cli
    from jarn.config import paths

    custom = tmp_path / "alt-jarn"
    custom.mkdir()
    monkeypatch.setenv("JARN_HOME", str(custom))
    gp = custom / "config.yaml"
    gp.write_text(
        yaml.safe_dump(
            {
                "default_profile": "openrouter",
                "providers": {"openrouter": {"type": "openrouter", "api_key": "sk-test"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    cli._cmd_doctor(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["jarn_home_overridden"] is True
    assert "jarn_home_warning" in data
    assert str(custom) in data["jarn_home_warning"]


# ---------------------------------------------------------------------------
# T-3-6: --output-schema headless structured output
# ---------------------------------------------------------------------------


def test_output_schema_requires_print(tmp_path, capsys):
    """``--output-schema`` without ``-p`` must argparse-error (exit 2, stderr).

    The assertion targets the specific ``parser.error(...)`` message emitted by
    the headless-only validation, not a generic argparse "unrecognized argument"
    string.  This ensures the flag is wired up AND the guard fires correctly.
    """
    with pytest.raises(SystemExit) as exc:
        main(["--output-schema", str(tmp_path / "schema.json")])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--output-schema requires -p" in err


def test_bad_schema_file_exit2(tmp_path, monkeypatch, capsys):
    """``--output-schema`` pointing at an unreadable/non-JSON file exits 2 with kind: 'usage'."""
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None))

    bad = tmp_path / "bad.txt"
    bad.write_text("this is not valid json {{{", encoding="utf-8")

    code = main(["-p", "hello", "--output-schema", str(bad), "--json"])
    assert code == 2
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["error"]["kind"] == "usage"


def test_missing_schema_file_exit2(tmp_path, monkeypatch, capsys):
    """``--output-schema`` pointing at a nonexistent file exits 2 with kind: 'usage'.

    Exercises the OSError branch of the schema-file loader (distinct from the
    bad-JSON / ValueError branch covered by ``test_bad_schema_file_exit2``).
    """
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None))

    nonexistent = tmp_path / "nonexistent.json"  # never created → OSError on read

    code = main(["-p", "hello", "--output-schema", str(nonexistent), "--json"])
    assert code == 2
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["error"]["kind"] == "usage"


# ---------------------------------------------------------------------------
# T-3-9: --add-dir multi-root workspaces
# ---------------------------------------------------------------------------


def test_add_dir_flag_repeatable(tmp_path, monkeypatch):
    """``--add-dir`` is repeatable and every dir becomes an active root.

    argparse ``action="append"`` collects each ``--add-dir``; launch resolves and
    validates them and threads the whole set into the session (captured here via
    the ``add_dirs`` kwarg handed to ``run_inline``)."""
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: root)
    monkeypatch.setattr(cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None))

    d1 = tmp_path / "sibling-a"
    d1.mkdir()
    d2 = tmp_path / "sibling-b"
    d2.mkdir()

    captured: dict = {}

    def _fake_run_inline(config, project_root, **kwargs):
        captured["add_dirs"] = kwargs.get("add_dirs")
        return 0

    import jarn.repl as repl_mod

    monkeypatch.setattr(repl_mod, "run_inline", _fake_run_inline)

    code = main(["--add-dir", str(d1), "--add-dir", str(d2)])
    assert code == 0
    roots = captured["add_dirs"]
    assert roots is not None
    resolved = {str(p) for p in roots}
    assert str(d1.resolve()) in resolved
    assert str(d2.resolve()) in resolved


def test_add_dir_flag_rejects_missing_dir(tmp_path, monkeypatch, capsys):
    """``--add-dir`` pointing at a non-existent path fails fast (exit 1, stderr)."""
    from jarn import cli as cli_mod
    from jarn.config import paths

    gp = _make_headless_config(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: root)
    monkeypatch.setattr(cli_mod, "_resolve_project_trust", lambda *a, **k: (True, {}, None))

    import jarn.repl as repl_mod

    monkeypatch.setattr(repl_mod, "run_inline", lambda *a, **k: 0)

    missing = tmp_path / "nope"
    code = main(["--add-dir", str(missing)])
    assert code == 1
    assert "--add-dir" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# T-4-3: jarn bug — redacted report + prefilled GitHub issue
# ---------------------------------------------------------------------------


def test_bug_dry_run_redacts(tmp_path, monkeypatch):
    """jarn bug --dry-run writes bug-report.md; planted secret must not appear.

    Plants a fake secret in both the log tail and the doctor output, runs
    ``jarn bug --dry-run``, and asserts the secret is absent from the written
    report (i.e. redact_secrets was applied to every included line).
    """
    import jarn.doctor.collect as dc
    from jarn import cli as cli_mod
    from jarn.config import paths
    from jarn.version import __version__

    FAKE_SECRET = "sk-supersecretkey1234567890abcdef"

    home = tmp_path / "jarnhome"
    log_dir = home / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "jarn.log").write_text(
        f"INFO normal log line\nDEBUG key={FAKE_SECRET} leak\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(paths, "global_home", lambda: home)

    def fake_collect(diag, **kwargs):
        diag["ok"] = True
        diag["jarn_home"] = str(home)
        diag["secret_field"] = f"api_key={FAKE_SECRET}"
        return 0

    monkeypatch.setattr(dc, "collect_doctor", fake_collect)

    try:
        code = cli_mod.main(["bug", "--dry-run"])
    except SystemExit as e:
        pytest.fail(f"'bug' subcommand not yet implemented (exit {e.code})")

    assert code == 0

    report_path = home / "bug-report.md"
    assert report_path.is_file(), "bug-report.md was not written"

    content = report_path.read_text(encoding="utf-8")

    # Must contain version info
    assert __version__ in content, f"Version {__version__!r} not found in report"
    # Must contain platform section
    assert "platform" in content.lower(), "No platform section in report"

    # The planted secret MUST NOT appear anywhere in the report
    assert FAKE_SECRET not in content, f"Secret leaked into report: {FAKE_SECRET!r}"


def test_bug_opens_prefilled_issue(tmp_path, monkeypatch):
    """jarn bug (no --dry-run) opens a prefilled GitHub issue URL.

    Spies on webbrowser.open; asserts:
    - URL targets chayapats/jarn issues/new
    - decoded body is ≤ 6000 chars
    - body mentions attaching the bug-report.md file
    """
    import webbrowser
    from urllib.parse import parse_qs, urlparse

    import jarn.doctor.collect as dc
    from jarn import cli as cli_mod
    from jarn.config import paths

    home = tmp_path / "jarnhome"
    log_dir = home / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "jarn.log").write_text("normal log line\n", encoding="utf-8")

    monkeypatch.setattr(paths, "global_home", lambda: home)

    def fake_collect(diag, **kwargs):
        diag["ok"] = True
        diag["jarn_home"] = str(home)
        return 0

    monkeypatch.setattr(dc, "collect_doctor", fake_collect)

    opened_urls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened_urls.append(url) or True)

    try:
        code = cli_mod.main(["bug"])
    except SystemExit as e:
        pytest.fail(f"'bug' subcommand not yet implemented (exit {e.code})")

    assert code == 0
    assert len(opened_urls) == 1, "webbrowser.open was not called exactly once"

    url = opened_urls[0]
    assert "github.com/chayapats/jarn/issues/new" in url, f"Unexpected URL: {url!r}"

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert "body" in params, "URL has no 'body' parameter"
    body = params["body"][0]
    assert len(body) <= 6000, f"Body is too long: {len(body)} chars"
    assert "bug-report.md" in body, "Body doesn't mention bug-report.md (attach pointer missing)"


# ---------------------------------------------------------------------------
# T-4-4: jarn completions {bash,zsh,fish} — anti-drift parity
# ---------------------------------------------------------------------------


def _build_parser():
    """Return the real jarn ArgumentParser (same object used by main())."""
    from jarn.cli import build_parser
    return build_parser()


def _introspect_parser(parser):
    """Return (subcommands: set[str], long_flags: set[str]) from a parser."""
    subcommands: set[str] = set()
    long_flags: set[str] = set()

    for action in parser._actions:
        for opt in action.option_strings:
            if opt.startswith("--") and opt != "--help":
                long_flags.add(opt)

    for action in parser._actions:
        if hasattr(action, "_name_parser_map"):
            for name, sub in action._name_parser_map.items():
                subcommands.add(name)
                for sub_action in sub._actions:
                    for opt in sub_action.option_strings:
                        if opt.startswith("--") and opt != "--help":
                            long_flags.add(opt)

    return subcommands, long_flags


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completions_cover_parser(shell: str) -> None:
    """Emitted script must carry a REAL completion declaration for every
    subcommand and long flag the parser defines.

    Anti-drift: introspects the real parser so adding a future subcommand or
    flag automatically makes this test enforce its inclusion. The per-shell
    match targets the actual declaration (not merely the string appearing
    somewhere), so dropping a flag's real `-l`/`-W`/spec entry fails the test.
    """
    from jarn.completions import emit_completions

    parser = _build_parser()
    subcommands, long_flags = _introspect_parser(parser)
    script = emit_completions(shell, parser)

    missing_subs = [cmd for cmd in subcommands if cmd not in script]
    assert not missing_subs, (
        f"[{shell}] completions missing subcommands: {missing_subs}"
    )

    if shell == "fish":
        # Honest: the real `complete ... -l <name>` DECLARATION must exist —
        # not just the flag string buried in a description.
        missing_flags = [
            f for f in long_flags if f"-l {f.lstrip('-')}" not in script
        ]
    else:
        # bash: `-W "… --flag …"`; zsh: `'--flag[…]'` — the flag itself appears
        # in the real completion spec.
        missing_flags = [f for f in long_flags if f not in script]

    assert not missing_flags, (
        f"[{shell}] completions missing long-flag declarations: {missing_flags}"
    )


def test_completions_zsh_arguments_is_single_continued_call() -> None:
    """The zsh `_arguments -C` block must be ONE continued command.

    Every flag-spec line (and `'1:command:->subcommand'`) must end with a `\\`
    line-continuation so the specs flow into the same `_arguments` call and
    `$state` is reachable. Without the continuations, only the first spec is
    seen, `'1:command:->subcommand'` is orphaned, `$state` is never set, and the
    whole `case $state in` block is dead — subcommand + per-sub completion break.
    """
    from jarn.completions import emit_completions

    parser = _build_parser()
    script = emit_completions("zsh", parser)
    lines = script.splitlines()

    start = next(i for i, ln in enumerate(lines) if "_arguments -C" in ln)
    end = next(i for i, ln in enumerate(lines) if "'*::args:->args'" in ln)
    assert end > start, "malformed zsh _arguments block"

    # Every line from `_arguments -C` up to the last spec must continue with `\`.
    for i in range(start, end):
        assert lines[i].rstrip().endswith("\\"), (
            f"zsh _arguments line not continued (orphans the rest): {lines[i]!r}"
        )
    # The final spec terminates the call (no dangling continuation).
    assert not lines[end].rstrip().endswith("\\")


@pytest.mark.parametrize("shell", ["zsh", "fish"])
def test_completions_use_real_help_not_flag_names(shell: str) -> None:
    """Descriptions come from argparse help, never from the flag string itself.

    A flag used as its own description (`-d '--resume'` / `'--resume[--resume …]'`)
    is ugly and hollow. Assert real help text surfaces and no flag-as-description
    slips through.
    """
    from jarn.completions import emit_completions

    parser = _build_parser()
    script = emit_completions(shell, parser)

    if shell == "fish":
        assert "-d '--" not in script, "fish uses a flag string as its description"
    else:  # zsh
        assert "[--" not in script, "zsh uses a flag string as its description"

    # A real help string must appear (setup --force: 'Overwrite …').
    assert "Overwrite" in script


# ---------------------------------------------------------------------------
# T-4-5 — jarn uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_home_and_keys(tmp_path, monkeypatch, capsys):
    """jarn uninstall --yes removes ~/.jarn and calls delete_password for every provider.

    Uses a fake global home (tmp_path subdir) and monkeypatched keyring so the
    real ~/.jarn and the real keychain are never touched.
    """
    import keyring

    from jarn.config import paths
    from jarn.config.defaults import ALL_PROVIDERS

    # Build a fake global home with some content.
    fake_home = tmp_path / "fake_jarn_home"
    fake_home.mkdir()
    (fake_home / "config.yaml").write_text("key: val\n", encoding="utf-8")
    (fake_home / "secrets").mkdir()
    (fake_home / "secrets" / "dummy.txt").write_text("secret", encoding="utf-8")

    monkeypatch.setattr(paths, "global_home", lambda: fake_home)

    deleted_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(keyring, "delete_password", lambda s, a: deleted_calls.append((s, a)))

    assert main(["uninstall", "--yes"]) == 0

    # Global home is gone.
    assert not fake_home.exists(), "global home was not removed"

    # delete_password called for every provider candidate.
    for provider in ALL_PROVIDERS:
        assert ("jarn", provider) in deleted_calls, (
            f"delete_password not called for provider {provider!r}"
        )


def test_uninstall_spares_projects(tmp_path, monkeypatch):
    """jarn uninstall --yes removes only ~/.jarn — project-local .jarn/ dirs are untouched.

    The implementation must remove ONLY paths.global_home() and never enumerate
    or touch project-local .jarn/ directories that live under a project root.
    """
    import keyring

    from jarn.config import paths

    # Fake global home.
    fake_home = tmp_path / "global_jarn"
    fake_home.mkdir()

    # A separate project root with its own .jarn/.
    project_root = tmp_path / "myproject"
    project_root.mkdir()
    project_jarn = project_root / ".jarn"
    project_jarn.mkdir()
    (project_jarn / "config.yaml").write_text("project: true\n", encoding="utf-8")

    monkeypatch.setattr(paths, "global_home", lambda: fake_home)
    monkeypatch.setattr(keyring, "delete_password", lambda s, a: None)

    assert main(["uninstall", "--yes"]) == 0

    # Global home removed.
    assert not fake_home.exists(), "global home was not removed"

    # Project-local .jarn/ is untouched.
    assert project_jarn.exists(), "project .jarn/ was incorrectly removed"
    assert (project_jarn / "config.yaml").is_file(), "project config was lost"


def test_uninstall_confirm_flow(tmp_path, monkeypatch, capsys):
    """Without --yes an itemized summary is shown; declining aborts with everything intact.

    Verifies: (1) the summary mentions dir size, keychain entries, and trust
    entries; (2) a 'n' answer leaves the home dir AND keychain untouched (no
    delete_password calls, no shutil.rmtree call).
    """
    import keyring

    from jarn.config import paths

    # Fake global home with a trust.yaml containing 2 entries.
    fake_home = tmp_path / "fake_jarn_home"
    fake_home.mkdir()
    (fake_home / "config.yaml").write_text("key: val\n", encoding="utf-8")

    import yaml

    (fake_home / "trust.yaml").write_text(
        yaml.safe_dump({"/projects/alpha": "aabbcc", "/projects/beta": "ddeeff"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(paths, "global_home", lambda: fake_home)

    deleted_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(keyring, "delete_password", lambda s, a: deleted_calls.append((s, a)))

    # Decline the confirmation prompt.
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    result = main(["uninstall"])

    # Aborted → non-zero exit.
    assert result != 0, "declining should return a non-zero exit code"

    # Global home is INTACT.
    assert fake_home.exists(), "home dir was removed despite declining"

    # Keychain untouched.
    assert deleted_calls == [], f"delete_password was called despite declining: {deleted_calls}"

    # Summary was printed (path + size + keychain + trust info).
    out = capsys.readouterr().out
    assert str(fake_home) in out, "summary did not show the home path being removed"
    assert "B" in out, "summary did not show a size token (B/KB/MB/GB)"
    assert "keychain" in out.lower(), "summary did not mention keychain entries"
    assert "trust" in out.lower(), "summary did not mention trust-store entries"


def test_uninstall_channel_hint(tmp_path, monkeypatch, capsys):
    """Final message uses 'npm' when sys.frozen is truthy, 'pip' when it is not.

    sys.frozen is injected via monkeypatch so we can test both branches in one
    test without touching the actual process state persistently.
    """
    import sys

    import keyring

    from jarn.config import paths

    monkeypatch.setattr(keyring, "delete_password", lambda s, a: None)

    # --- pip branch: sys.frozen is absent ---
    if hasattr(sys, "frozen"):
        monkeypatch.delattr(sys, "frozen")
    fake_home_pip = tmp_path / "jarn_home_pip"
    fake_home_pip.mkdir()
    monkeypatch.setattr(paths, "global_home", lambda: fake_home_pip)

    assert main(["uninstall", "--yes"]) == 0
    out_pip = capsys.readouterr().out
    assert "pip" in out_pip, f"expected 'pip' in output, got: {out_pip!r}"
    assert "npm" not in out_pip, f"'npm' should not appear in pip-branch output: {out_pip!r}"

    # --- npm branch: sys.frozen is True ---
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_home_npm = tmp_path / "jarn_home_npm"
    fake_home_npm.mkdir()
    monkeypatch.setattr(paths, "global_home", lambda: fake_home_npm)

    assert main(["uninstall", "--yes"]) == 0
    out_npm = capsys.readouterr().out
    assert "npm" in out_npm, f"expected 'npm' in output, got: {out_npm!r}"
