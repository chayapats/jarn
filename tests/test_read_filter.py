"""Result-filter middleware — strip sensitive-file hits from grep output.

Real-tool tests (per the Codex second-eye #1 method): a real
``CancellableLocalShellBackend.grep`` runs over a real tree containing secrets,
and the middleware must remove the secret files' contents from the result while
leaving ordinary source hits intact. Pre-exec gating (see
``test_permissions``/``test_agent_mocked``) closes the *explicit-glob* repro;
this closes the *broad-search* leak the scope-only gate can't see.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from deepagents.backends.utils import format_grep_matches
from langchain_core.messages import ToolMessage

from jarn.agent.local_backend import CancellableLocalShellBackend
from jarn.agent.read_filter import ReadResultFilterMiddleware
from jarn.config.schema import PermissionRules
from jarn.permissions import Action, ActionKind, PermissionEngine


@dataclass
class _Req:
    """Minimal stand-in for ``ToolCallRequest`` (the middleware reads only
    ``.tool_call``)."""

    tool_call: dict[str, Any] = field(default_factory=dict)


def _grep_tool_message(
    backend: CancellableLocalShellBackend,
    pattern: str,
    root: str,
    output_mode: str,
    *,
    glob: str | None = None,
) -> ToolMessage:
    """Run the REAL backend grep and format it exactly as the deepagents grep
    tool would, returning the ToolMessage that reaches the middleware."""
    result = backend.grep(pattern, path=root, glob=glob)
    content = format_grep_matches(result.matches or [], output_mode)
    return ToolMessage(content=content, name="grep", tool_call_id="t1", status="success")


def _default_engine() -> PermissionEngine:
    return PermissionEngine(rules=PermissionRules())  # default sensitive globs


def _make_tree(tmp_path):
    """A tree with two non-hidden secrets (ripgrep searches these in a broad
    grep) and one benign source file, all containing 'PRIVATE KEY'."""
    (tmp_path / "server.pem").write_text("PRIVATE KEY pem-secret-material\n")
    (tmp_path / "id_rsa").write_text("PRIVATE KEY rsa-secret-material\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("# handles the PRIVATE KEY lookup\n")
    return src


def test_broad_content_grep_over_secrets_is_filtered(tmp_path):
    """The core level-2 proof: a broad ``grep(pattern, path=/tree)`` returns the
    CONTENTS of ``server.pem`` and ``id_rsa`` (the gate saw only the benign
    scope), and the middleware removes both while keeping the source hit."""
    src = _make_tree(tmp_path)
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)

    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")
    # Sanity: the REAL grep actually leaked the secret material.
    assert "pem-secret-material" in msg.content
    assert "rsa-secret-material" in msg.content

    mw = ReadResultFilterMiddleware(_default_engine())
    out = mw.wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}, "id": "t1"}),
        lambda _r: msg,
    )

    assert "pem-secret-material" not in out.content  # server.pem redacted
    assert "rsa-secret-material" not in out.content  # id_rsa redacted
    assert str(tmp_path / "server.pem") not in out.content
    assert str(tmp_path / "id_rsa") not in out.content
    # The benign source hit survives — no over-redaction.
    assert str(src / "app.py") in out.content
    assert "handles the PRIVATE KEY lookup" in out.content


def test_files_with_matches_mode_drops_secret_paths(tmp_path):
    src = _make_tree(tmp_path)
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "files_with_matches")

    mw = ReadResultFilterMiddleware(_default_engine())
    out = mw.wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "files_with_matches"}}),
        lambda _r: msg,
    )
    assert str(tmp_path / "server.pem") not in out.content
    assert str(tmp_path / "id_rsa") not in out.content
    assert str(src / "app.py") in out.content


def test_count_mode_drops_secret_paths(tmp_path):
    src = _make_tree(tmp_path)
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "count")

    mw = ReadResultFilterMiddleware(_default_engine())
    out = mw.wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "count"}}),
        lambda _r: msg,
    )
    assert "server.pem" not in out.content
    assert "id_rsa" not in out.content
    assert str(src / "app.py") in out.content


def test_ordinary_grep_result_is_untouched(tmp_path):
    """No over-redaction: a grep hitting only ordinary source returns byte-for-byte
    the original ToolMessage (same object) — no notice, no rewrite."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def alpha():\n    return 1\n")
    (src / "b.py").write_text("def beta():\n    return alpha()\n")
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "alpha", str(tmp_path), "content")

    mw = ReadResultFilterMiddleware(_default_engine())
    out = mw.wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "alpha", "output_mode": "content"}}),
        lambda _r: msg,
    )
    assert out is msg  # unchanged: nothing removed


def test_explicitly_denied_file_hits_are_removed(tmp_path):
    """An explicit read-``deny`` on a non-secret file also filters its grep hits."""
    (tmp_path / "notes.txt").write_text("PRIVATE KEY note contents\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("# PRIVATE KEY doc\n")
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")

    eng = PermissionEngine(rules=PermissionRules(deny=["**/notes.txt"]))
    mw = ReadResultFilterMiddleware(eng)
    out = mw.wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}}),
        lambda _r: msg,
    )
    assert "note contents" not in out.content
    assert str(src / "app.py") in out.content


def test_allow_rule_opts_secret_back_in(tmp_path):
    """An explicit allow rule on a secret path lets its grep hits through."""
    _make_tree(tmp_path)
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")

    eng = PermissionEngine(rules=PermissionRules(allow=[str(tmp_path / "server.pem")]))
    mw = ReadResultFilterMiddleware(eng)
    out = mw.wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}}),
        lambda _r: msg,
    )
    assert "pem-secret-material" in out.content     # allow-listed → kept
    assert "rsa-secret-material" not in out.content  # still filtered


def test_async_path_filters_too(tmp_path):
    """The production driver uses astream (async): ``awrap_tool_call`` filters too."""
    _make_tree(tmp_path)
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")

    mw = ReadResultFilterMiddleware(_default_engine())

    async def _handler(_r):
        return msg

    out = asyncio.run(
        mw.awrap_tool_call(
            _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}}),
            _handler,
        )
    )
    assert "pem-secret-material" not in out.content
    assert "rsa-secret-material" not in out.content


def test_read_file_denied_backstop(tmp_path):
    """Backstop: a directly read-denied ``read_file`` result is replaced with a
    permission-denied error (defense-in-depth behind the pre-exec DENY)."""
    eng = PermissionEngine(rules=PermissionRules(deny=["**/vault.txt"]))
    mw = ReadResultFilterMiddleware(eng)
    leaked = ToolMessage(
        content="   1: root-password=hunter2", name="read_file", tool_call_id="r1", status="success"
    )
    out = mw.wrap_tool_call(
        _Req({"name": "read_file", "args": {"file_path": "/proj/vault.txt"}, "id": "r1"}),
        lambda _r: leaked,
    )
    assert "hunter2" not in out.content
    assert out.status == "error"


def test_read_file_ordinary_passes(tmp_path):
    """An ordinary read_file result is returned unchanged."""
    eng = PermissionEngine(rules=PermissionRules(deny=["**/vault.txt"]))
    mw = ReadResultFilterMiddleware(eng)
    ok = ToolMessage(content="   1: hello", name="read_file", tool_call_id="r2", status="success")
    out = mw.wrap_tool_call(
        _Req({"name": "read_file", "args": {"file_path": "/proj/src/app.py"}, "id": "r2"}),
        lambda _r: ok,
    )
    assert out is ok


def test_non_read_tool_passes_through():
    """A non-read tool's result is never touched."""
    mw = ReadResultFilterMiddleware(_default_engine())
    other = ToolMessage(content="anything", name="execute", tool_call_id="e1", status="success")
    out = mw.wrap_tool_call(_Req({"name": "execute", "args": {}}), lambda _r: other)
    assert out is other


def test_glob_over_env_pre_exec_gated_end_to_end(tmp_path):
    """The Codex explicit-glob repro at the tool boundary: the REAL backend WOULD
    return ``.env`` for ``glob='**/.env'``, and the bridge+engine gate it DENY
    when ``**/.env`` is read-denied (so it never runs)."""
    (tmp_path / ".env").write_text("TOKEN=supersecret\n")
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    # Real grep with the explicit glob DOES surface the secret (the vector).
    r = backend.grep("TOKEN", path=str(tmp_path), glob="**/.env")
    assert any(".env" in m["path"] for m in (r.matches or []))

    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.config.schema import PermissionMode

    engine = PermissionEngine(
        mode=PermissionMode.YOLO, rules=PermissionRules(deny=["**/.env"])
    )
    action = tool_to_action(
        "grep", {"pattern": "TOKEN", "path": str(tmp_path), "glob": "**/.env"}
    )
    assert engine.evaluate(action).decision.value == "deny"


# ---------------------------------------------------------------------------
# Subagent coverage (second-eye #1 residual): the result filter must ride the
# COMPILED subagent sub-graphs too, not just the main agent — otherwise a
# subagent's broad grep re-opens the exact leak on the delegated path.


def _find_subagent_read_filters(compiled: Any) -> list[ReadResultFilterMiddleware]:
    """Collect the unique ``ReadResultFilterMiddleware`` instances wrapping a compiled
    (sub)graph's tool node.

    langchain composes ``wrap_tool_call`` middleware into the ``ToolNode``'s
    ``_wrap_tool_call`` / ``_awrap_tool_call`` chain; each middleware surfaces as the
    ``__self__`` of a bound method captured in that chain's closures. Walking those
    (deduped by id) proves the filter survived compilation into the subagent graph —
    the same closure-walk approach ``test_fanout`` uses to find the HITL middleware.
    """
    tn = compiled.nodes["tools"].bound
    found: dict[int, ReadResultFilterMiddleware] = {}
    seen: set[int] = set()

    def walk(fn: Any, depth: int = 0) -> None:
        if fn is None or id(fn) in seen or depth > 12:
            return
        seen.add(id(fn))
        owner = getattr(fn, "__self__", None)
        if isinstance(owner, ReadResultFilterMiddleware):
            found[id(owner)] = owner
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, ReadResultFilterMiddleware):
                found[id(value)] = value
            if callable(value):
                walk(value, depth + 1)

    walk(getattr(tn, "_wrap_tool_call", None))
    walk(getattr(tn, "_awrap_tool_call", None))
    return list(found.values())


def test_subagent_grep_over_secrets_is_filtered(base_config, tmp_path):
    """The core subagent proof: build a REAL agent (create_deep_agent NOT mocked),
    extract the compiled general-purpose sub-graph (the agent the ``task`` tool and
    the fan-out spawn), confirm its tool node is wrapped by the result filter, and
    show that filter really redacts a broad grep that leaked two secrets — while
    keeping an ordinary source hit. This is the delegated-path equivalent of
    ``test_broad_content_grep_over_secrets_is_filtered``."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder
    from jarn.agent.fanout import extract_subagent_graphs

    src = _make_tree(tmp_path)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = builder.build_runtime(base_config, project_root=tmp_path)

    gp = extract_subagent_graphs(rt.agent).get("general-purpose")
    assert gp is not None, "the general-purpose sub-graph (task/fan-out target) must exist"
    filters = _find_subagent_read_filters(gp)
    assert len(filters) == 1, "the subagent tool node must carry exactly one result filter"

    # The subagent's broad grep really leaks the secret material (the gate saw only
    # the benign scope) — then the wired middleware strips it, exactly as on main.
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")
    assert "pem-secret-material" in msg.content
    assert "rsa-secret-material" in msg.content

    out = filters[0].wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}, "id": "t1"}),
        lambda _r: msg,
    )
    assert "pem-secret-material" not in out.content  # server.pem redacted for subagent
    assert "rsa-secret-material" not in out.content   # id_rsa redacted for subagent
    assert str(src / "app.py") in out.content         # benign source hit preserved
    assert "handles the PRIVATE KEY lookup" in out.content


# ---------------------------------------------------------------------------
# Session-deny sharing (second-eye A, BUG A): the result filter must reference the
# CONTROLLER's authoritative, session-aware engine — the same instance interrupts.py
# records `deny_session`/`remember` on — not a fresh rule-only copy. Otherwise a user
# denying a runtime read of a secret is ignored by a later broad grep.


def test_session_deny_filters_subsequent_grep(tmp_path):
    """A runtime READ denial recorded on the engine the middleware HOLDS (the path
    ``interrupts.py`` takes: ``engine.deny_session(action)``) filters that path out
    of a later broad grep — the filter honors session state, not only static rules.

    ``notes.txt`` is an ordinary file (no sensitive glob, no config deny), so before
    the deny its hit passes through; after the session deny it is redacted."""
    (tmp_path / "notes.txt").write_text("PRIVATE KEY secret-note-material\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("# PRIVATE KEY doc\n")
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")

    engine = _default_engine()
    mw = ReadResultFilterMiddleware(engine)
    req = _Req(
        {"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}}
    )

    before = mw.wrap_tool_call(req, lambda _r: msg)
    assert "secret-note-material" in before.content  # ordinary file: not yet filtered

    # A user's runtime read-denial writes to THIS engine (exactly as interrupts.py).
    engine.deny_session(Action(ActionKind.READ, target=str(tmp_path / "notes.txt")))

    after = mw.wrap_tool_call(req, lambda _r: msg)
    assert "secret-note-material" not in after.content  # session-denied → filtered
    assert str(src / "app.py") in after.content         # ordinary hit still kept


def test_shared_engine_session_deny_covers_main_and_subagent(base_config, tmp_path):
    """BUG A end-to-end: passing the controller's authoritative engine to
    ``build_runtime`` must thread THAT SAME object into the result filter on the
    MAIN agent AND the general-purpose sub-graph (the ``task``/fan-out target), so a
    session deny recorded on it — the ``deny_session`` path — is honored everywhere.

    Proves both: (1) engine object identity (``filter._engine is engine``) on both
    stacks, and (2) a session deny on the shared engine redacts a later broad grep
    on both, while an ordinary source hit survives."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder
    from jarn.agent.fanout import extract_subagent_graphs

    (tmp_path / "notes.txt").write_text("PRIVATE KEY secret-note-material\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("# PRIVATE KEY doc\n")

    engine = PermissionEngine(rules=PermissionRules())
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = builder.build_runtime(base_config, project_root=tmp_path, engine=engine)

    # The MAIN agent and the general-purpose sub-graph both carry the filter, and it
    # references the SAME shared engine object (fan-out reuses the GP sub-graph).
    main_filters = _find_subagent_read_filters(rt.agent)
    assert len(main_filters) == 1, "the main tool node must carry exactly one filter"
    assert main_filters[0]._engine is engine

    gp = extract_subagent_graphs(rt.agent).get("general-purpose")
    assert gp is not None, "the general-purpose sub-graph (task/fan-out target) must exist"
    gp_filters = _find_subagent_read_filters(gp)
    assert len(gp_filters) == 1, "the subagent tool node must carry exactly one filter"
    assert gp_filters[0]._engine is engine

    # A runtime read-denial recorded on the shared engine now filters that path out
    # of a broad grep on BOTH stacks.
    engine.deny_session(Action(ActionKind.READ, target=str(tmp_path / "notes.txt")))

    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")
    assert "secret-note-material" in msg.content  # the raw grep really leaked it

    req = _Req(
        {
            "name": "grep",
            "args": {"pattern": "PRIVATE KEY", "output_mode": "content"},
            "id": "t1",
        }
    )
    for flt in (main_filters[0], gp_filters[0]):
        out = flt.wrap_tool_call(req, lambda _r: msg)
        assert "secret-note-material" not in out.content  # session-denied → filtered
        assert str(src / "app.py") in out.content         # ordinary hit preserved


# ---------------------------------------------------------------------------
# Second-eye FINAL (path-spelling bypass): a RELATIVE sensitive glob or session
# deny must catch the ABSOLUTE grep-result header for the SAME file. Before the
# fix READ matching was lexical, so a relative rule never met deepagents' absolute
# grep paths and the secret leaked (real-backend repro). The engine now matches by
# file identity via project_root-anchored aliases; these prove it end-to-end.


def _make_secrets_tree(tmp_path):
    """A tree whose secret lives at RELATIVE ``secrets/notes.txt`` (so a relative
    rule can name it) but which the real grep reports by ABSOLUTE path."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "notes.txt").write_text("PRIVATE KEY leaked-secret-material\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("# handles the PRIVATE KEY lookup\n")
    return src


def test_relative_sensitive_glob_redacts_absolute_grep_header(tmp_path):
    """A RELATIVE custom ``sensitive_read_glob`` (``secrets/*.txt``) redacts the
    ABSOLUTE grep header for the same file — the exact leak the Codex repro proved."""
    src = _make_secrets_tree(tmp_path)
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")
    assert "leaked-secret-material" in msg.content  # real grep leaked it (absolute header)

    engine = PermissionEngine(
        rules=PermissionRules(sensitive_read_globs=["secrets/*.txt"]),
        project_root=tmp_path,
    )
    mw = ReadResultFilterMiddleware(engine)
    out = mw.wrap_tool_call(
        _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}}),
        lambda _r: msg,
    )
    assert "leaked-secret-material" not in out.content  # relative glob caught absolute header
    assert str(src / "app.py") in out.content           # benign source hit preserved


def test_relative_session_deny_redacts_absolute_grep_header(tmp_path):
    """A session deny recorded with a RELATIVE path (``./secrets/notes.txt``) redacts
    the ABSOLUTE grep header for the same file (resolved-absolute identity)."""
    src = _make_secrets_tree(tmp_path)
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=False)
    msg = _grep_tool_message(backend, "PRIVATE KEY", str(tmp_path), "content")

    engine = PermissionEngine(rules=PermissionRules(), project_root=tmp_path)
    mw = ReadResultFilterMiddleware(engine)
    req = _Req({"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}})

    before = mw.wrap_tool_call(req, lambda _r: msg)
    assert "leaked-secret-material" in before.content  # ordinary file: not yet denied

    # Relative spelling, exactly as a user would type it at the interrupt prompt.
    engine.deny_session(Action(ActionKind.READ, target="./secrets/notes.txt"))

    after = mw.wrap_tool_call(req, lambda _r: msg)
    assert "leaked-secret-material" not in after.content  # relative deny caught absolute header
    assert str(src / "app.py") in after.content           # ordinary hit preserved


def test_relative_deny_covers_main_and_subagent_absolute_grep(base_config, tmp_path):
    """End-to-end: a RELATIVE session deny (``secrets/notes.txt``) on the shared,
    project-rooted engine redacts a PRODUCTION (virtual-mode) grep header on BOTH the
    main agent and the general-purpose sub-graph (the ``task``/fan-out target).
    ``build_runtime`` marks the shared engine ``virtual_reads`` for the local backend,
    so the header is fed as the real backend emits it (virtual-absolute)."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder
    from jarn.agent.fanout import extract_subagent_graphs

    _make_secrets_tree(tmp_path)

    engine = PermissionEngine(rules=PermissionRules(), project_root=tmp_path)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = builder.build_runtime(base_config, project_root=tmp_path, engine=engine)

    main_filters = _find_subagent_read_filters(rt.agent)
    gp = extract_subagent_graphs(rt.agent).get("general-purpose")
    assert gp is not None, "the general-purpose sub-graph (task/fan-out target) must exist"
    gp_filters = _find_subagent_read_filters(gp)
    assert len(main_filters) == 1 and len(gp_filters) == 1
    assert main_filters[0]._engine is engine and gp_filters[0]._engine is engine

    # A relative-path session deny (as typed by the user) on the shared engine.
    engine.deny_session(Action(ActionKind.READ, target="secrets/notes.txt"))

    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    msg = _grep_tool_message(backend, "PRIVATE KEY", "/", "content")
    assert "leaked-secret-material" in msg.content  # real grep leaked it (virtual header)

    req = _Req(
        {"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}, "id": "t1"}
    )
    for flt in (main_filters[0], gp_filters[0]):
        out = flt.wrap_tool_call(req, lambda _r: msg)
        assert "leaked-secret-material" not in out.content  # relative deny caught virtual header
        assert "/src/app.py" in out.content                 # benign source hit preserved


# ---------------------------------------------------------------------------
# Second-eye round-5/6 #1/#2 — PRODUCTION virtual-mode backend. The local backend
# (backends_factory: ``virtual_mode=True``) reports read/grep paths as
# VIRTUAL-absolute (``/secrets/notes.txt``), NOT the host-absolute paths the
# round-4 tests above used. The engine now canonicalizes virtual READ targets to
# host identity when ``engine.virtual_reads`` is set (round-6 #1) — at BOTH the
# pre-exec gate (see test_permissions) AND this result filter — so a RELATIVE
# sensitive-glob / session-deny matches by file identity and a remembered allow of
# the displayed path takes effect. It also normalizes dot-relative globs (#2).


def _virtual_grep_message(tmp_path) -> ToolMessage:
    """A real ``virtual_mode=True`` grep over the secrets tree — headers are VIRTUAL
    (``/secrets/notes.txt``), exactly as the production local backend emits (the
    blind spot: the round-4 regression tests all used ``virtual_mode=False``)."""
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    msg = _grep_tool_message(backend, "PRIVATE KEY", "/", "content")
    assert "/secrets/notes.txt:" in msg.content     # VIRTUAL header (not the host path)
    assert "leaked-secret-material" in msg.content   # real grep leaked it
    return msg


def _grep_req() -> _Req:
    return _Req(
        {"name": "grep", "args": {"pattern": "PRIVATE KEY", "output_mode": "content"}, "id": "t1"}
    )


def _virtual_engine(tmp_path, **rule_kw) -> PermissionEngine:
    """A project-rooted engine configured as the production local backend is:
    ``virtual_reads=True`` (its read/grep paths are virtual-absolute)."""
    engine = PermissionEngine(rules=PermissionRules(**rule_kw), project_root=tmp_path)
    engine.virtual_reads = True
    return engine


def test_virtual_relative_sensitive_glob_redacts_virtual_header(tmp_path):
    """PRODUCTION repro (#1): a RELATIVE ``sensitive_read_glob`` (``secrets/*.txt``)
    redacts a VIRTUAL grep header. Leaks without ``engine.virtual_reads``
    canonicalization — the exact round-5 blocker."""
    _make_secrets_tree(tmp_path)
    msg = _virtual_grep_message(tmp_path)
    engine = _virtual_engine(tmp_path, sensitive_read_globs=["secrets/*.txt"])
    mw = ReadResultFilterMiddleware(engine)
    out = mw.wrap_tool_call(_grep_req(), lambda _r: msg)
    assert "leaked-secret-material" not in out.content  # secret redacted
    assert "/src/app.py" in out.content                 # benign virtual hit preserved


def test_virtual_relative_session_deny_redacts_virtual_header(tmp_path):
    """PRODUCTION repro (#1): a RELATIVE session deny (``./secrets/notes.txt``)
    redacts the VIRTUAL grep header for the same file."""
    _make_secrets_tree(tmp_path)
    msg = _virtual_grep_message(tmp_path)
    engine = _virtual_engine(tmp_path)
    mw = ReadResultFilterMiddleware(engine)
    req = _grep_req()
    before = mw.wrap_tool_call(req, lambda _r: msg)
    assert "leaked-secret-material" in before.content   # ordinary file: not yet denied
    engine.deny_session(Action(ActionKind.READ, target="./secrets/notes.txt"))
    after = mw.wrap_tool_call(req, lambda _r: msg)
    assert "leaked-secret-material" not in after.content  # relative deny caught virtual header
    assert "/src/app.py" in after.content


def test_virtual_dot_relative_sensitive_glob_redacts_virtual_header(tmp_path):
    """#2: a DOT-relative ``sensitive_read_glob`` (``./secrets/*.txt``) redacts the
    virtual header — engine dot-segment pattern normalization."""
    _make_secrets_tree(tmp_path)
    msg = _virtual_grep_message(tmp_path)
    engine = _virtual_engine(tmp_path, sensitive_read_globs=["./secrets/*.txt"])
    mw = ReadResultFilterMiddleware(engine)
    out = mw.wrap_tool_call(_grep_req(), lambda _r: msg)
    assert "leaked-secret-material" not in out.content
    assert "/src/app.py" in out.content


def test_virtual_no_over_redaction_of_benign_virtual_hits(tmp_path):
    """Canonicalization must NOT redact benign hits: with a secrets-only glob the
    source file's virtual header and body survive (no false positives)."""
    _make_secrets_tree(tmp_path)
    msg = _virtual_grep_message(tmp_path)
    engine = _virtual_engine(tmp_path, sensitive_read_globs=["secrets/*.txt"])
    mw = ReadResultFilterMiddleware(engine)
    out = mw.wrap_tool_call(_grep_req(), lambda _r: msg)
    assert "/src/app.py" in out.content
    assert "handles the PRIVATE KEY lookup" in out.content


def test_virtual_remembered_allow_surfaces_denied_looking_hit(tmp_path):
    """#1 precedence/escape-hatch (round-6 B): a user who APPROVES+REMEMBERS the
    DISPLAYED virtual path gets its later broad-grep hit SURFACED (allow beats
    sensitive), because the remembered allow is stored by host identity too."""
    _make_secrets_tree(tmp_path)
    msg = _virtual_grep_message(tmp_path)
    engine = _virtual_engine(tmp_path, sensitive_read_globs=["secrets/*.txt"])
    mw = ReadResultFilterMiddleware(engine)
    # Baseline: sensitive → redacted.
    assert "leaked-secret-material" not in mw.wrap_tool_call(_grep_req(), lambda _r: msg).content
    # User approves+remembers the DISPLAYED virtual path (as interrupts.py does).
    from jarn.permissions import RememberScope

    engine.remember(
        Action(ActionKind.READ, target="/secrets/notes.txt"), RememberScope.SESSION
    )
    out = mw.wrap_tool_call(_grep_req(), lambda _r: msg)
    assert "leaked-secret-material" in out.content  # explicit allow escape hatch works


def test_virtual_relative_deny_covers_main_and_subagent(base_config, tmp_path):
    """End-to-end WIRING (#1): ``build_runtime`` sets ``engine.virtual_reads`` so BOTH
    the main and general-purpose read filters (sharing the engine) redact a PRODUCTION
    virtual grep header from a RELATIVE session deny (main agent + task/fan-out)."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder
    from jarn.agent.fanout import extract_subagent_graphs

    _make_secrets_tree(tmp_path)
    engine = PermissionEngine(rules=PermissionRules(), project_root=tmp_path)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = builder.build_runtime(base_config, project_root=tmp_path, engine=engine)

    main_filters = _find_subagent_read_filters(rt.agent)
    gp = extract_subagent_graphs(rt.agent).get("general-purpose")
    assert gp is not None, "the general-purpose sub-graph (task/fan-out target) must exist"
    gp_filters = _find_subagent_read_filters(gp)
    assert len(main_filters) == 1 and len(gp_filters) == 1
    # Wiring proof: build_runtime marked the shared engine as virtual-reads, and both
    # filters reference that same engine.
    assert engine.virtual_reads is True
    assert main_filters[0]._engine is engine and gp_filters[0]._engine is engine

    engine.deny_session(Action(ActionKind.READ, target="secrets/notes.txt"))
    msg = _virtual_grep_message(tmp_path)
    req = _grep_req()
    for flt in (main_filters[0], gp_filters[0]):
        out = flt.wrap_tool_call(req, lambda _r: msg)
        assert "leaked-secret-material" not in out.content  # virtual header redacted on both
        assert "/src/app.py" in out.content                 # benign hit preserved


# ---------------------------------------------------------------------------
# Windows path shape (CI regression: the redaction filter silently no-op'd on
# Windows). The real local backend (``virtual_mode=False``) reports grep hits by
# OS-NATIVE path — on Windows that is a drive-letter absolute ``C:\...`` (backslash;
# see deepagents ``to_posix_path``: "Backends running on Windows return OS-native
# paths using backslashes"), NOT a leading-``/`` POSIX path. ``_looks_absolute`` only
# recognized ``/x``, so every grep header on Windows failed the path-line test and
# NO secret was ever dropped — the exact leak the middleware exists to close.
#
# These feed SYNTHETIC drive-letter / UNC grep output straight to the pure parser
# (``_filter_grep_content``) with a stub ``blocked`` callback, so they reproduce the
# Windows leak on any host (Linux/macOS) with no Windows runner — the reproduction
# the CI-only failures (``test_broad_content_grep_over_secrets_is_filtered`` et al.)
# could not give locally. They RED before the ``_looks_absolute`` fix and GREEN after.

from jarn.agent.read_filter import _filter_grep_content, _looks_absolute  # noqa: E402


def _blocks_pem_or_rsa(path: str) -> bool:
    """Stub for ``PermissionEngine.read_content_blocked`` — a secret is any ``.pem``
    or ``id_rsa`` file, spelled with either separator (mirrors the real engine, which
    normalizes ``\\`` → ``/`` before matching)."""
    norm = path.replace("\\", "/")
    return norm.endswith(".pem") or norm.endswith("/id_rsa")


def test_looks_absolute_recognizes_windows_paths():
    """The path-line test must accept Windows drive-letter (both separators) and UNC
    absolutes, still accept POSIX/virtual ``/x``, and reject indented match lines and
    relative/plain text."""
    assert _looks_absolute("/proj/server.pem:")               # POSIX / virtual
    assert _looks_absolute(r"C:\Users\me\server.pem:")        # drive-letter, backslash
    assert _looks_absolute("C:/Users/me/server.pem:")         # drive-letter, forward slash
    assert _looks_absolute(r"\\host\share\server.pem:")       # UNC
    assert not _looks_absolute("  1: PRIVATE KEY material")    # indented match line
    assert not _looks_absolute("relative/server.pem:")        # relative — not a header
    assert not _looks_absolute("No matches found")            # backend sentinel


def test_windows_content_mode_redacts_backslash_header():
    """content-mode grep whose headers are Windows ``C:\\...`` paths: the secret file's
    header + body are dropped, the benign source hit survives."""
    content = (
        r"C:\Users\runner\Temp\proj\server.pem:" + "\n"
        "  1: PRIVATE KEY pem-secret-material\n"
        r"C:\Users\runner\Temp\proj\src\app.py:" + "\n"
        "  1: # handles the PRIVATE KEY lookup"
    )
    out, removed = _filter_grep_content(content, "content", _blocks_pem_or_rsa)
    assert removed
    assert "pem-secret-material" not in out              # secret redacted on Windows shape
    assert r"C:\Users\runner\Temp\proj\server.pem" not in out
    assert r"C:\Users\runner\Temp\proj\src\app.py" in out  # benign source hit preserved
    assert "handles the PRIVATE KEY lookup" in out


def test_windows_content_mode_redacts_forward_slash_header():
    """A drive-letter path spelled with forward slashes (``C:/...``) is redacted too."""
    content = (
        "C:/Users/runner/Temp/proj/id_rsa:\n"
        "  1: PRIVATE KEY rsa-secret-material\n"
        "C:/Users/runner/Temp/proj/src/app.py:\n"
        "  1: # PRIVATE KEY doc"
    )
    out, removed = _filter_grep_content(content, "content", _blocks_pem_or_rsa)
    assert removed
    assert "rsa-secret-material" not in out
    assert "C:/Users/runner/Temp/proj/src/app.py" in out


def test_windows_files_with_matches_mode_drops_secret_paths():
    content = (
        r"C:\Users\runner\Temp\proj\id_rsa" + "\n"
        r"C:\Users\runner\Temp\proj\server.pem" + "\n"
        r"C:\Users\runner\Temp\proj\src\app.py"
    )
    out, removed = _filter_grep_content(content, "files_with_matches", _blocks_pem_or_rsa)
    assert removed
    assert "id_rsa" not in out
    assert "server.pem" not in out
    assert r"C:\Users\runner\Temp\proj\src\app.py" in out


def test_windows_count_mode_drops_secret_paths():
    content = (
        r"C:\Users\runner\Temp\proj\server.pem: 3" + "\n"
        r"C:\Users\runner\Temp\proj\src\app.py: 1"
    )
    out, removed = _filter_grep_content(content, "count", _blocks_pem_or_rsa)
    assert removed
    assert "server.pem" not in out
    assert r"C:\Users\runner\Temp\proj\src\app.py: 1" in out


def test_engine_classifies_windows_secret_path_as_blocked():
    """The engine side already handles Windows paths (it normalizes ``\\`` → ``/`` and
    matches by lexical alias), so once the parser recognizes the header, the real
    ``read_content_blocked`` callback blocks a Windows-shaped secret and passes a
    benign one. Proves the ONLY product gap was the parser's path-line test — no engine
    change is needed. Runs on any host (pure lexical, no filesystem resolution)."""
    engine = _default_engine()  # default sensitive globs include **/*.pem, **/id_rsa
    assert engine.read_content_blocked(r"C:\Users\runner\Temp\proj\server.pem")
    assert engine.read_content_blocked(r"C:\Users\runner\Temp\proj\id_rsa")
    assert not engine.read_content_blocked(r"C:\Users\runner\Temp\proj\src\app.py")
