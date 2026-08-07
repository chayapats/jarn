"""Session routing for the Telegram gateway (T-DMN-2 / #38 / #51).

Maps ``(chat_id, root) → thread_id``, keeps a per-chat active root (default
``~/.jarn/personal``), and routes turns onto the per-root worker supervised by
:class:`~jarn.gateway.daemon.DaemonSupervisor`.

Busy policy (#38): while a turn is in flight on the **root**, new messages are
**queued** with an explicit notice; ``/stop`` cancels. Not interrupt-on-text.

``/repo`` may only target the personal root or an entry in ``gateway.repos``.
Any bind that resolves to ``$HOME`` or collides with the global config path is
hard-refused (#51 / #53).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jarn.config import paths
from jarn.config.schema import GatewayRepo
from jarn.gateway.daemon import DaemonSupervisor, WorkerDeadError, WorkerHandle
from jarn.gateway.lease import RootLeaseHeldError
from jarn.gateway.protocol import (
    ApprovalAskFrame,
    MediaRef,
    OutboundFrame,
    StatusFrame,
    TurnFrame,
)
from jarn.gateway.scheduler import (
    DueWork,
    Scheduler,
    clear_active_delivery,
    set_active_delivery,
)
from jarn.memory.sessions import new_thread_id

_log = logging.getLogger("jarn.gateway.sessions")

#: Notice delivered when a message is enqueued behind an in-flight turn.
QUEUED_NOTICE = (
    "Queued — a turn is already running on this root. "
    "Send /stop to cancel, or wait."
)

#: Notice when /stop finds nothing to cancel.
STOP_IDLE_NOTICE = "Nothing to stop — no turn in flight."

#: Notice when /stop is accepted.
STOP_ACCEPTED_NOTICE = "Stopping the in-flight turn…"

NoticeHook = Callable[[int, str], None]
"""``(chat_id, text)`` — transport layer posts a chat notice."""

EventHook = Callable[[int, Path, OutboundFrame], None]
"""``(chat_id, root, frame)`` — outbound frames belonging to a chat."""

DeathHook = Callable[[int, Path, WorkerDeadError], None]
"""``(chat_id, root, error)`` — fail-loud mid-turn worker death."""


class ForbiddenRootError(ValueError):
    """Raised when a root is ``$HOME``, global-config, or otherwise banned."""


class UnknownRepoError(ValueError):
    """Raised when ``/repo`` names something outside the allowlist."""


class RootBusyLeaseError(RuntimeError):
    """Gateway lost the root lease to a foreign holder (#52) — refuse, no queue."""

    def __init__(self, root: Path) -> None:
        self.root = root
        super().__init__(
            f"project root is in use by another jarn process: {root}. "
            "Finish or quit that session, then retry."
        )


@dataclass(slots=True)
class QueuedTurn:
    """One user message waiting behind an in-flight turn on a root."""

    chat_id: int
    thread_id: str
    text: str
    media: list[MediaRef] = field(default_factory=list)


@dataclass
class ChatSessionState:
    """Per-chat active root + per-root thread ids."""

    active_root: Path
    threads: dict[Path, str] = field(default_factory=dict)


class SessionRouter:
    """Route chat commands/turns onto the correct per-root worker."""

    def __init__(
        self,
        supervisor: DaemonSupervisor,
        *,
        repos: Sequence[GatewayRepo] | Mapping[str, str] | None = None,
        personal_root: Path | None = None,
        on_notice: NoticeHook | None = None,
        on_event: EventHook | None = None,
        on_worker_death: DeathHook | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._personal_root = (
            Path(personal_root).expanduser().resolve()
            if personal_root is not None
            else paths.ensure_personal_root().resolve()
        )
        validate_gateway_root(self._personal_root, personal_ok=True)
        self._repos = _normalize_repos(repos)
        self.on_notice = on_notice
        self.on_event = on_event
        self.on_worker_death = on_worker_death

        self._chats: dict[int, ChatSessionState] = {}
        # Per-root FIFO of queued turns (busy policy).
        self._queues: dict[Path, deque[QueuedTurn]] = {}
        # Roots currently dispatching a turn from this router.
        self._busy_roots: set[Path] = set()
        # chat_ids that most recently dispatched on a root (for death/event fan-out).
        self._root_owner: dict[Path, int] = {}
        self._lock = threading.RLock()

        # Wire supervisor hooks (compose with any pre-existing ones).
        prev_outbound = supervisor.on_outbound
        prev_death = supervisor.on_worker_death

        def _outbound(handle: WorkerHandle, frame: OutboundFrame) -> None:
            if prev_outbound is not None:
                prev_outbound(handle, frame)
            self._handle_outbound(handle, frame)

        def _death(
            handle: WorkerHandle,
            exit_code: int | None,
            thread_id: str | None,
        ) -> None:
            if prev_death is not None:
                prev_death(handle, exit_code, thread_id)
            self._handle_death(handle, exit_code, thread_id)

        supervisor.on_outbound = _outbound
        supervisor.on_worker_death = _death

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    @property
    def personal_root(self) -> Path:
        return self._personal_root

    @property
    def repos(self) -> list[GatewayRepo]:
        return list(self._repos)

    def active_root(self, chat_id: int) -> Path:
        with self._lock:
            return self._state(chat_id).active_root

    def thread_id_for(self, chat_id: int, root: Path | None = None) -> str:
        """Return (minting if needed) the thread id for ``(chat_id, root)``."""
        with self._lock:
            state = self._state(chat_id)
            key = (root or state.active_root).resolve()
            tid = state.threads.get(key)
            if tid is None:
                tid = new_thread_id()
                state.threads[key] = tid
            return tid

    def queue_depth(self, root: Path | str) -> int:
        key = Path(root).expanduser().resolve()
        with self._lock:
            return len(self._queues.get(key, ()))

    def is_busy(self, root: Path | str) -> bool:
        key = Path(root).expanduser().resolve()
        with self._lock:
            if key in self._busy_roots:
                return True
            handle = self._supervisor.get_worker(key)
            return bool(handle is not None and handle.turn_in_flight)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def cmd_repo(self, chat_id: int, target: str | None = None) -> Path:
        """Switch the chat's active root.

        ``None`` / ``""`` / ``"personal"`` → personal root. Otherwise *target*
        must match an allowlisted repo by ``name`` or ``path``.
        """
        root = self.resolve_repo(target)
        paths.ensure_project_gitignore(root)
        with self._lock:
            state = self._state(chat_id)
            state.active_root = root
            state.threads.setdefault(root, new_thread_id())
        return root

    def resolve_repo(self, target: str | None) -> Path:
        """Resolve a ``/repo`` argument to an absolute allowed root."""
        if target is None or not str(target).strip() or str(target).strip().lower() in {
            "personal",
            "~",
            "default",
        }:
            return self._personal_root

        raw = str(target).strip()
        # Match by name first (case-sensitive, as configured).
        for repo in self._repos:
            if repo.name is not None and repo.name == raw:
                return validate_gateway_root(Path(repo.path), personal_ok=False)

        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise UnknownRepoError(f"unknown repo: {raw}") from exc

        if resolved == self._personal_root:
            return self._personal_root

        for repo in self._repos:
            try:
                repo_path = Path(repo.path).expanduser().resolve()
            except OSError:
                continue
            if repo_path == resolved:
                return validate_gateway_root(repo_path, personal_ok=False)

        # Hard-refuse HOME / global-config even if somehow listed.
        validate_gateway_root(resolved, personal_ok=False)
        raise UnknownRepoError(
            f"repo not in gateway.repos allowlist: {raw}. "
            "Use /repo personal or an allowlisted name/path."
        )

    def cmd_new(self, chat_id: int) -> str:
        """Mint a fresh ``thread_id`` for ``(chat_id, active_root)``."""
        with self._lock:
            state = self._state(chat_id)
            tid = new_thread_id()
            state.threads[state.active_root] = tid
            return tid

    def cmd_stop(self, chat_id: int) -> bool:
        """Cancel the in-flight turn on the chat's active root.

        Returns True when a cancel frame was sent. Also drops queued turns for
        this chat on that root.
        """
        with self._lock:
            state = self._state(chat_id)
            root = state.active_root
            tid = state.threads.get(root)
            q = self._queues.get(root)
            if q is not None:
                kept = deque(item for item in q if item.chat_id != chat_id)
                if kept:
                    self._queues[root] = kept
                else:
                    self._queues.pop(root, None)

        handle = self._supervisor.get_worker(root)
        if handle is None or not handle.turn_in_flight or tid is None:
            self._notice(chat_id, STOP_IDLE_NOTICE)
            return False
        try:
            self._supervisor.cancel(root, tid)
        except (WorkerDeadError, RootLeaseHeldError) as exc:
            self._notice(chat_id, f"Could not stop: {exc}")
            return False
        self._notice(chat_id, STOP_ACCEPTED_NOTICE)
        return True

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def submit_turn(
        self,
        chat_id: int,
        text: str,
        *,
        media: Sequence[MediaRef] | None = None,
        root: Path | str | None = None,
    ) -> str:
        """Route a user message to the active root's worker.

        Returns the ``thread_id`` the turn is (or will be) bound to. When the
        root is busy the turn is queued, :data:`QUEUED_NOTICE` is emitted, and
        the same thread id is returned.

        *root* overrides the chat's active root for this turn only (used by the
        in-gateway scheduler so jobs keep their stored root).
        """
        media_list = list(media or [])
        with self._lock:
            state = self._state(chat_id)
            key = Path(root).expanduser().resolve() if root is not None else state.active_root
            tid = state.threads.get(key)
            if tid is None:
                tid = new_thread_id()
                state.threads[key] = tid

            busy = key in self._busy_roots
            handle = self._supervisor.get_worker(key)
            if handle is not None and handle.turn_in_flight:
                busy = True

            if busy:
                self._queues.setdefault(key, deque()).append(
                    QueuedTurn(
                        chat_id=chat_id,
                        thread_id=tid,
                        text=text,
                        media=media_list,
                    )
                )
                self._notice(chat_id, QUEUED_NOTICE)
                return tid

            self._busy_roots.add(key)
            self._root_owner[key] = chat_id

        try:
            self._dispatch(key, chat_id, tid, text, media_list)
        except Exception:
            with self._lock:
                self._busy_roots.discard(key)
            raise
        return tid

    def submit_due_work(self, work: DueWork) -> str:
        """Submit one scheduler firing as a normal turn (park+push approvals)."""
        return self.submit_turn(work.chat_id, work.prompt, root=work.root)

    def tick_scheduler(
        self,
        scheduler: Scheduler | None = None,
        *,
        now: object | None = None,
    ) -> list[DueWork]:
        """Run due jobs once and dispatch each via :meth:`submit_due_work`."""
        from datetime import datetime

        sched = scheduler if scheduler is not None else Scheduler()
        stamp = now if isinstance(now, datetime) else None
        return sched.dispatch_due(self.submit_due_work, now=stamp)

    def drain_queue(self, root: Path | str) -> bool:
        """Dispatch the next queued turn for *root* if idle. Returns True if sent."""
        key = Path(root).expanduser().resolve()
        with self._lock:
            if key in self._busy_roots:
                return False
            handle = self._supervisor.get_worker(key)
            if handle is not None and handle.turn_in_flight:
                return False
            q = self._queues.get(key)
            if not q:
                return False
            item = q.popleft()
            if not q:
                self._queues.pop(key, None)
            self._busy_roots.add(key)
            self._root_owner[key] = item.chat_id
            chat_id, tid, text, media = (
                item.chat_id,
                item.thread_id,
                item.text,
                item.media,
            )

        try:
            self._dispatch(key, chat_id, tid, text, media)
        except Exception:
            with self._lock:
                self._busy_roots.discard(key)
            raise
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _state(self, chat_id: int) -> ChatSessionState:
        state = self._chats.get(chat_id)
        if state is None:
            state = ChatSessionState(active_root=self._personal_root)
            self._chats[chat_id] = state
        return state

    def _dispatch(
        self,
        root: Path,
        chat_id: int,
        thread_id: str,
        text: str,
        media: list[MediaRef],
    ) -> None:
        # So schedule_task inside the worker can inherit chat_id (#42).
        try:
            set_active_delivery(chat_id=chat_id, root=root, thread_id=thread_id)
        except Exception:  # noqa: BLE001 — delivery hint must not block turns
            _log.exception("failed to record active delivery for %s", root)
        try:
            self._supervisor.send(
                root,
                TurnFrame(thread_id=thread_id, text=text, media=media),
            )
        except RootLeaseHeldError as exc:
            err = RootBusyLeaseError(root)
            self._notice(chat_id, str(err))
            raise err from exc
        except WorkerDeadError as exc:
            self._notice(
                chat_id,
                f"Worker for {root} failed (exit={exc.exit_code!r}). "
                "The turn was not replayed — please resend if needed.",
            )
            if self.on_worker_death is not None:
                self.on_worker_death(chat_id, root, exc)
            raise

    def _handle_outbound(self, handle: WorkerHandle, frame: OutboundFrame) -> None:
        with self._lock:
            chat_id = self._root_owner.get(handle.root)
        if chat_id is not None and self.on_event is not None:
            try:
                self.on_event(chat_id, handle.root, frame)
            except Exception:  # noqa: BLE001
                _log.exception("on_event hook failed chat=%s", chat_id)

        kind = getattr(frame, "kind", "")
        if (
            isinstance(frame, ApprovalAskFrame)
            or (isinstance(frame, StatusFrame) and not frame.turn_in_flight)
            or (
                isinstance(kind, str)
                and kind.lower() in {"done", "cancelled", "error"}
            )
        ):
            # ApprovalAsk = park (#37): turn released; do not keep chat busy.
            self._clear_busy_and_drain(handle.root)

    def _handle_death(
        self,
        handle: WorkerHandle,
        exit_code: int | None,
        thread_id: str | None,
    ) -> None:
        with self._lock:
            chat_id = self._root_owner.pop(handle.root, None)
            self._busy_roots.discard(handle.root)
            # Drop the dead handle so a later drain/ensure can respawn.
            # Do **not** replay the in-flight turn — only continue the queue.
        if chat_id is not None:
            err = WorkerDeadError(
                handle.root, exit_code=exit_code, thread_id=thread_id
            )
            self._notice(
                chat_id,
                f"Worker for {handle.root} died (exit={exit_code!r}). "
                "In-flight turn was not replayed — send again if needed.",
            )
            if self.on_worker_death is not None:
                try:
                    self.on_worker_death(chat_id, handle.root, err)
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "on_worker_death hook failed chat=%s", chat_id
                    )
        try:
            self.drain_queue(handle.root)
        except Exception:  # noqa: BLE001
            _log.exception(
                "failed to drain queue after worker death for %s", handle.root
            )

    def _clear_busy_and_drain(self, root: Path) -> None:
        with self._lock:
            self._busy_roots.discard(root)
            queued = bool(self._queues.get(root))
        if not queued:
            try:
                clear_active_delivery(root)
            except Exception:  # noqa: BLE001
                _log.exception("failed to clear active delivery for %s", root)
        # Dispatch next outside the lock (may block on pipe write).
        try:
            self.drain_queue(root)
        except Exception:  # noqa: BLE001
            _log.exception("failed to drain queue for %s", root)

    def _notice(self, chat_id: int, text: str) -> None:
        if self.on_notice is not None:
            try:
                self.on_notice(chat_id, text)
            except Exception:  # noqa: BLE001
                _log.exception("on_notice hook failed chat=%s", chat_id)


def validate_gateway_root(root: Path | str, *, personal_ok: bool = False) -> Path:
    """Resolve *root* and refuse ``$HOME`` / global-config collisions (#51).

    The personal root (``~/.jarn/personal``) is allowed when *personal_ok* is
    True (or when it equals :func:`paths.personal_root`).
    """
    resolved = Path(root).expanduser().resolve()
    personal = paths.personal_root().resolve()
    if resolved == personal:
        return resolved

    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None and resolved == home:
        raise ForbiddenRootError(
            f"refusing to bind gateway root to $HOME ({resolved})"
        )

    global_home = paths.global_home().resolve()
    if resolved == global_home:
        raise ForbiddenRootError(
            f"refusing to bind gateway root to global home ({resolved})"
        )

    pcfg = paths.project_config_path(resolved)
    gcfg = paths.global_config_path().resolve()
    if pcfg is not None and pcfg.resolve() == gcfg:
        raise ForbiddenRootError(
            f"refusing root whose project config collides with global config: "
            f"{resolved}"
        )

    if not personal_ok and resolved != personal:
        # Non-personal roots still pass the HOME/global guards above.
        pass
    return resolved


def _normalize_repos(
    repos: Sequence[GatewayRepo] | Mapping[str, str] | None,
) -> list[GatewayRepo]:
    if repos is None:
        return []
    if isinstance(repos, Mapping):
        return [GatewayRepo(path=str(path), name=name) for name, path in repos.items()]
    out: list[GatewayRepo] = []
    for item in repos:
        if isinstance(item, GatewayRepo):
            out.append(item)
        else:
            raise TypeError(f"expected GatewayRepo, got {type(item)!r}")
    return out


__all__ = [
    "QUEUED_NOTICE",
    "STOP_ACCEPTED_NOTICE",
    "STOP_IDLE_NOTICE",
    "ChatSessionState",
    "ForbiddenRootError",
    "QueuedTurn",
    "RootBusyLeaseError",
    "SessionRouter",
    "UnknownRepoError",
    "validate_gateway_root",
]
