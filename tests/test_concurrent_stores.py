"""Concurrency regressions for the stores that share a project or global root.

Two plain `jarn` CLIs on one repo is enough to reach every case below. Before the
shared write primitive, each of these was a read-modify-write with no lock and a
publish with a FIXED temp name, and the measured losses were:

  - permission ALWAYS-grants: 50% across threads, 78% across processes, plus an
    uncaught FileNotFoundError that escaped `remember()` into the turn loop
  - wiki appends: 89-97% lost, and readers saw an EMPTY index.md
  - MEMORY.md: same shape, and its rebuild runs on the PROMPT-ASSEMBLY path

The thread counts here are deliberately modest — these assert the invariant
(nothing is lost, nothing is torn), not a specific failure rate.
"""

from __future__ import annotations

import threading
import time

import pytest
import yaml

from jarn.memory.store import MemoryStore
from jarn.memory.wiki import WikiStore
from jarn.permissions.rule_store import PermissionRuleStore


def _run(fn, n: int) -> list[BaseException]:
    """Run ``fn(i)`` on ``n`` threads, returning anything that escaped."""
    errors: list[BaseException] = []

    def wrapper(i: int) -> None:
        try:
            fn(i)
        except BaseException as exc:  # noqa: BLE001 - the point is to catch all
            errors.append(exc)

    # daemon=True is mandatory, not tidiness: without it the interpreter's own
    # shutdown join re-hangs the run after the deadline has already failed it.
    threads = [threading.Thread(target=wrapper, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 30
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    assert not any(t.is_alive() for t in threads), (
        "workers did not finish within 30s — a lock almost certainly deadlocked"
    )
    return errors


# -- #45: PermissionRuleStore ------------------------------------------------


def test_concurrent_always_grants_are_all_persisted(tmp_path):
    config = tmp_path / ".jarn" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("permissions:\n  allow: []\n", encoding="utf-8")
    store = PermissionRuleStore(config)

    n = 24
    errors = _run(lambda i: store.add_allow(f"read:file-{i}"), n)

    assert errors == [], f"a grant raised instead of persisting: {errors[:3]}"
    allow = yaml.safe_load(config.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert sorted(allow) == sorted(f"read:file-{i}" for i in range(n))


def test_concurrent_grants_leave_no_temp_files(tmp_path):
    """A shared `<path>.tmp` was the second half of the defect: colliding writers
    raised FileNotFoundError from os.replace and could publish a partial file."""
    config = tmp_path / ".jarn" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("permissions:\n  allow: []\n", encoding="utf-8")
    store = PermissionRuleStore(config)

    _run(lambda i: store.add_allow(f"rule-{i}"), 16)
    assert [p.name for p in config.parent.iterdir() if p.name.endswith(".tmp")] == []
    # The file is still valid YAML — never torn by a racing rename.
    assert isinstance(yaml.safe_load(config.read_text(encoding="utf-8")), dict)


def test_persist_failure_does_not_escape_remember(tmp_path):
    """An OSError from the rule store used to propagate `remember` → `_stream_turn`
    → `run_turn` and end the turn. The in-memory allow must still apply."""
    from jarn.config.schema import PermissionMode
    from jarn.permissions import Action, ActionKind, PermissionEngine, RememberScope

    def explode(_rule: str) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    eng = PermissionEngine(mode=PermissionMode.ASK, project_root=tmp_path)
    eng.persist = explode
    action = Action(ActionKind.SHELL, "npm test")

    rule = eng.remember(action, RememberScope.ALWAYS)

    assert rule is not None
    assert eng.evaluate(action).decision.value == "allow"


# -- #46: WikiStore / MemoryStore -------------------------------------------


@pytest.fixture
def wiki(tmp_path) -> WikiStore:
    return WikiStore(
        global_wiki_dir=tmp_path / "global" / "wiki",
        project_wiki_dir=tmp_path / "project" / ".jarn" / "wiki",
    )


def test_concurrent_wiki_appends_all_survive(wiki):
    wiki.write("notes", "# Notes\n\nstart", tier="project")
    writers, per_writer = 8, 12

    def appender(i: int) -> None:
        for j in range(per_writer):
            wiki.append("notes", f"line-{i}-{j}", tier="project")

    errors = _run(appender, writers)
    assert errors == [], f"an append raised: {errors[:3]}"

    page = (wiki.project_wiki_dir / "pages" / "notes.md").read_text(encoding="utf-8")
    missing = [
        f"line-{i}-{j}"
        for i in range(writers)
        for j in range(per_writer)
        if f"line-{i}-{j}" not in page
    ]
    assert missing == [], f"{len(missing)}/{writers * per_writer} appends lost"


def test_concurrent_appends_to_a_new_page_do_not_clobber(wiki):
    """The create path raced too: two writers both saw "no page yet" and one
    create overwrote the other. Resolving the target inside the lock closes it."""
    writers = 8

    errors = _run(lambda i: wiki.append("fresh", f"entry-{i}", tier="project"), writers)
    assert errors == []

    page = (wiki.project_wiki_dir / "pages" / "fresh.md").read_text(encoding="utf-8")
    assert sum(f"entry-{i}" in page for i in range(writers)) == writers


def test_cross_tier_appends_to_one_global_page_are_serialized(tmp_path):
    """The page mutated is chosen by a two-tier SEARCH, so it can live in the tier
    the caller does not write to. A lock key derived from the write dir let two
    sessions with different project roots mutate one global page while each held a
    different lock — so the key is derived from the slug in the global tier."""
    global_wiki = tmp_path / "global" / "wiki"
    (global_wiki / "pages").mkdir(parents=True)
    (global_wiki / "pages" / "shared.md").write_text("# Shared", encoding="utf-8")

    stores = [
        WikiStore(
            global_wiki_dir=global_wiki,
            project_wiki_dir=tmp_path / f"proj{i}" / ".jarn" / "wiki",
        )
        for i in range(6)
    ]

    def appender(i: int) -> None:
        for j in range(15):
            stores[i].append("shared", f"entry-{i}-{j}", tier="project")

    errors = _run(appender, 6)
    assert errors == []

    page = (global_wiki / "pages" / "shared.md").read_text(encoding="utf-8")
    missing = [
        f"entry-{i}-{j}" for i in range(6) for j in range(15)
        if f"entry-{i}-{j}" not in page
    ]
    assert missing == [], f"{len(missing)}/90 cross-tier appends lost"


def test_appending_to_a_global_page_does_not_materialize_a_project_wiki(tmp_path):
    """A cross-tier append must not leave an empty tree (and a stray lock) in the
    tier it never writes to."""
    global_wiki = tmp_path / "global" / "wiki"
    (global_wiki / "pages").mkdir(parents=True)
    (global_wiki / "pages" / "shared.md").write_text("# Shared", encoding="utf-8")
    project_wiki = tmp_path / "proj" / ".jarn" / "wiki"

    store = WikiStore(global_wiki_dir=global_wiki, project_wiki_dir=project_wiki)
    store.append("shared", "added", tier="project")

    assert "added" in (global_wiki / "pages" / "shared.md").read_text(encoding="utf-8")
    assert not project_wiki.exists()


def test_wiki_index_is_never_read_empty(wiki):
    """A bare write_text truncates before it writes; a reader landing in that
    window saw nothing at all."""
    wiki.write("notes", "# Notes\n\nstart", tier="project")
    index = wiki.project_wiki_dir / "index.md"
    stop = threading.Event()
    empties = [0]
    reads = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                text = index.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            reads[0] += 1
            if not text.strip():
                empties[0] += 1

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for r in readers:
        r.start()
    try:
        _run(lambda i: [wiki.append("notes", f"x-{i}-{j}", tier="project") for j in range(10)], 6)
    finally:
        stop.set()
        for r in readers:
            r.join(timeout=5)

    assert reads[0] > 0, "the reader threads never observed the index"
    assert empties[0] == 0


def test_memory_index_is_never_read_empty(tmp_path):
    """MemoryStore._rebuild_index has the same shape AND runs on the read path —
    index_text() rebuilds on demand, and that text goes into the system prompt."""
    from jarn.memory.store import Memory

    store = MemoryStore(root=tmp_path / "memory")
    store.ensure()
    store.save(Memory(name="seed", description="first", type="project", body="b"))

    stop = threading.Event()
    empties = [0]
    reads = [0]

    def reader() -> None:
        while not stop.is_set():
            text = store.index_text()
            reads[0] += 1
            if not text.strip():
                empties[0] += 1

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for r in readers:
        r.start()
    try:
        _run(
            lambda i: store.save(
                Memory(name=f"mem-{i}", description=f"d{i}", type="project", body="b")
            ),
            8,
        )
    finally:
        stop.set()
        for r in readers:
            r.join(timeout=5)

    assert reads[0] > 0
    assert empties[0] == 0
    index = store.index_path.read_text(encoding="utf-8")
    assert all(f"mem-{i}" in index for i in range(8))


# -- TrustStore: the machine-global map every jarn process on the host writes --


def test_concurrent_trust_grants_for_different_roots_all_survive(tmp_path):
    """load → prompt the user → save spans a long window, and every process on the
    host writes this one file. A plain overwrite dropped whichever grant landed in
    between."""
    from jarn.config.trust import TrustStore

    store_path = tmp_path / "trust.yaml"
    n = 16

    def grant(i: int) -> None:
        store = TrustStore.load(store_path)  # each gets its own snapshot
        store.trust(tmp_path / f"proj{i}", f"fingerprint-{i}")
        store.save()

    errors = _run(grant, n)
    assert errors == []
    assert len(TrustStore.load(store_path).entries()) == n


def test_a_stale_writer_cannot_resurrect_a_revoked_root(tmp_path):
    """The dangerous direction. A lost grant merely re-prompts (fail-closed); a lost
    REVOKE brings an untrusted root back as trusted (fail-open). The merge applies
    only what a writer actually changed, so a root it never touched stays revoked."""
    from jarn.config.trust import TrustStore

    store_path = tmp_path / "trust.yaml"
    victim = tmp_path / "revoked"
    victim.mkdir()
    seed = TrustStore.load(store_path)
    seed.trust(victim, "fp-old")
    seed.save()

    revoker = TrustStore.load(store_path)
    stale = TrustStore.load(store_path)  # loaded BEFORE the revoke

    assert revoker.untrust(victim) is True
    revoker.save()

    stale.trust(tmp_path / "unrelated", "fp-new")  # touches a different root only
    stale.save()

    entries = TrustStore.load(store_path).entries()
    assert str(victim.resolve()) not in entries
    assert str((tmp_path / "unrelated").resolve()) in entries


def test_a_genuine_conflict_on_one_root_is_last_writer_wins(tmp_path):
    """Only a real same-root conflict resolves by order — not a silent whole-map
    overwrite."""
    from jarn.config.trust import TrustStore

    store_path = tmp_path / "trust.yaml"
    root = tmp_path / "proj"
    root.mkdir()

    first = TrustStore.load(store_path)
    second = TrustStore.load(store_path)
    first.trust(root, "fp-first")
    first.save()
    second.trust(root, "fp-second")
    second.save()

    assert TrustStore.load(store_path).entries()[str(root.resolve())] == "fp-second"
