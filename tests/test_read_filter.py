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
from jarn.permissions import PermissionEngine


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
