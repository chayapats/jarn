"""In-gateway scheduled jobs + catch-up-once tick (T-SCHED-1 / #42).

Persists under ``~/.jarn/gateway/jobs.json``. Jobs default to the personal root
(:func:`~jarn.config.paths.ensure_personal_root`) and carry a ``chat_id`` for
Telegram delivery. Missed firings after downtime run **once** (α), then the
schedule advances to the next future tick — never a burst of every missed slot.

Scheduled turns are ordinary turns: dangerous tools still park+push via #37.
This module only owns the job store and due-work yield; the daemon/session
router submits those turns through the normal path.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarn.config import paths
from jarn.gateway.approvals import GATEWAY_DIR_NAME
from jarn.util.atomic import atomic_write_text, file_lock

_log = logging.getLogger("jarn.gateway.scheduler")

#: Durable job store under ``JARN_HOME``.
JOBS_FILENAME = "jobs.json"

#: Cross-process delivery hint so ``schedule_task`` in a worker can inherit
#: ``chat_id`` for the in-flight turn on a root (daemon writes; tool reads).
ACTIVE_DELIVERY_FILENAME = "active_delivery.json"

#: Wire schema version for the JSON document (bump on incompatible shape).
_STORE_VERSION = 1

_CRON_FIELD_RE = re.compile(
    r"^(\*|\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)(?:/(\d+))?$"
)


class ScheduleError(ValueError):
    """Invalid schedule expression, job id, or tool arguments."""


@dataclass(slots=True)
class ScheduledJob:
    """One persisted gateway schedule (#42)."""

    id: str
    prompt: str
    chat_id: int
    root: str
    enabled: bool = True
    cron: str | None = None
    at: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    last_run: str | None = None
    next_run: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScheduledJob:
        chat_id = data["chat_id"]
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise ValueError("chat_id must be an int")
        cron = data.get("cron")
        at = data.get("at")
        if cron is not None:
            cron = str(cron)
        if at is not None:
            at = str(at)
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        return cls(
            id=str(data["id"]),
            prompt=str(data.get("prompt") or data.get("payload", {}).get("prompt") or ""),
            chat_id=chat_id,
            root=str(data["root"]),
            enabled=bool(data.get("enabled", True)),
            cron=cron,
            at=at,
            payload=dict(payload),
            last_run=_opt_str(data.get("last_run")),
            next_run=_opt_str(data.get("next_run")),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass(slots=True, frozen=True)
class DueWork:
    """One job firing the daemon/backend should run as a normal turn."""

    job_id: str
    chat_id: int
    root: Path
    prompt: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ActiveDelivery:
    """Daemon→worker hint: which chat owns the in-flight turn on a root."""

    chat_id: int
    root: str
    thread_id: str | None = None


def jobs_path(*, home: Path | None = None) -> Path:
    """Return ``~/.jarn/gateway/jobs.json`` (or under *home*)."""
    base = Path(home) if home is not None else paths.global_home()
    return base / GATEWAY_DIR_NAME / JOBS_FILENAME


def active_delivery_path(*, home: Path | None = None) -> Path:
    """Return ``~/.jarn/gateway/active_delivery.json``."""
    base = Path(home) if home is not None else paths.global_home()
    return base / GATEWAY_DIR_NAME / ACTIVE_DELIVERY_FILENAME


def mint_job_id() -> str:
    """Opaque short job id."""
    return secrets.token_hex(8)


def utcnow() -> datetime:
    """Timezone-aware UTC now (tests may monkeypatch)."""
    return datetime.now(UTC)


def parse_iso(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_iso(dt: datetime) -> str:
    """Serialize *dt* as UTC ISO-8601 with a ``Z`` suffix."""
    return parse_iso(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_job_root(root: str | Path | None = None) -> Path:
    """Resolve a job root; ``None`` / ``personal`` → :func:`ensure_personal_root`."""
    if root is None or str(root).strip() == "" or str(root).strip().lower() in {
        "personal",
        "~",
        "default",
    }:
        return paths.ensure_personal_root().resolve()
    return Path(root).expanduser().resolve()


# ---------------------------------------------------------------------------
# Minimal 5-field cron (UTC)
# ---------------------------------------------------------------------------


def _parse_cron_field(raw: str, *, minimum: int, maximum: int) -> set[int]:
    text = raw.strip()
    match = _CRON_FIELD_RE.fullmatch(text)
    if match is None:
        raise ScheduleError(f"invalid cron field: {raw!r}")
    base, step_s = match.group(1), match.group(2)
    step = int(step_s) if step_s else 1
    if step < 1:
        raise ScheduleError(f"invalid cron step in {raw!r}")
    values: set[int] = set()
    if base == "*":
        values = set(range(minimum, maximum + 1))
    else:
        for part in base.split(","):
            if "-" in part:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s), int(end_s)
            else:
                start = end = int(part)
            if start > end or start < minimum or end > maximum:
                raise ScheduleError(f"cron field out of range: {raw!r}")
            values.update(range(start, end + 1))
    if step > 1:
        ordered = sorted(values)
        origin = ordered[0] if base != "*" else minimum
        values = {v for v in values if (v - origin) % step == 0}
    return values


@dataclass(slots=True, frozen=True)
class CronExpr:
    """Parsed 5-field cron (minute hour day-of-month month day-of-week)."""

    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    source: str

    @classmethod
    def parse(cls, expr: str) -> CronExpr:
        parts = expr.strip().split()
        if len(parts) != 5:
            raise ScheduleError(
                f"cron must have 5 fields (min hour dom month dow), got {expr!r}"
            )
        return cls(
            minute=frozenset(_parse_cron_field(parts[0], minimum=0, maximum=59)),
            hour=frozenset(_parse_cron_field(parts[1], minimum=0, maximum=23)),
            day=frozenset(_parse_cron_field(parts[2], minimum=1, maximum=31)),
            month=frozenset(_parse_cron_field(parts[3], minimum=1, maximum=12)),
            # 0 and 7 are Sunday (crontab convention).
            dow=frozenset(_parse_cron_field(parts[4], minimum=0, maximum=7)),
            source=expr.strip(),
        )

    def matches(self, dt: datetime) -> bool:
        dt = parse_iso(dt)
        dow = dt.isoweekday() % 7  # Mon=1…Sun=7 → Sun=0
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        day_wildcard = self.day == frozenset(range(1, 32))
        dow_wildcard = self.dow == frozenset(range(0, 8))
        day_ok = dt.day in self.day
        dow_ok = dow in self.dow or (dow == 0 and 7 in self.dow)
        # Standard cron: when both dom and dow are restricted, either may match.
        if not day_wildcard and not dow_wildcard:
            return day_ok or dow_ok
        if not day_wildcard:
            return day_ok
        if not dow_wildcard:
            return dow_ok
        return True


def next_cron_after(expr: str | CronExpr, after: datetime) -> datetime:
    """Next UTC minute matching *expr* strictly after *after*."""
    cron = expr if isinstance(expr, CronExpr) else CronExpr.parse(expr)
    cursor = parse_iso(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Bound search to ~2 years of minutes.
    for _ in range(366 * 2 * 24 * 60):
        if cron.matches(cursor):
            return cursor
        cursor += timedelta(minutes=1)
    raise ScheduleError(f"no next fire within two years for cron {cron.source!r}")


def compute_next_run(
    *,
    cron: str | None,
    at: str | None,
    after: datetime | None = None,
) -> datetime | None:
    """Compute the next fire time after *after* (default: now)."""
    now = after if after is not None else utcnow()
    if cron and at:
        raise ScheduleError("specify either cron or at, not both")
    if cron:
        return next_cron_after(cron, now)
    if at:
        when = parse_iso(at)
        return when if when > parse_iso(now) else None
    raise ScheduleError("schedule requires cron= or at=")


# ---------------------------------------------------------------------------
# Active delivery hint (daemon ↔ worker)
# ---------------------------------------------------------------------------


def set_active_delivery(
    *,
    chat_id: int,
    root: Path | str,
    thread_id: str | None = None,
    path: Path | None = None,
) -> ActiveDelivery:
    """Record which chat owns the in-flight turn on *root* (cross-process)."""
    store_path = path if path is not None else active_delivery_path()
    root_s = str(Path(root).expanduser().resolve())
    record = ActiveDelivery(chat_id=chat_id, root=root_s, thread_id=thread_id)
    with file_lock(store_path):
        data = _load_delivery_unlocked(store_path)
        data[root_s] = {
            "chat_id": record.chat_id,
            "root": record.root,
            "thread_id": record.thread_id,
        }
        _save_delivery_unlocked(store_path, data)
    return record


def clear_active_delivery(
    root: Path | str,
    *,
    path: Path | None = None,
) -> None:
    """Drop the active-delivery row for *root*."""
    store_path = path if path is not None else active_delivery_path()
    root_s = str(Path(root).expanduser().resolve())
    with file_lock(store_path):
        data = _load_delivery_unlocked(store_path)
        if root_s in data:
            data.pop(root_s, None)
            _save_delivery_unlocked(store_path, data)


def get_active_delivery(
    root: Path | str | None = None,
    *,
    path: Path | None = None,
) -> ActiveDelivery | None:
    """Return the active delivery for *root*, or any single entry when omitted."""
    store_path = path if path is not None else active_delivery_path()
    with file_lock(store_path):
        data = _load_delivery_unlocked(store_path)
    if not data:
        return None
    if root is not None:
        raw = data.get(str(Path(root).expanduser().resolve()))
        if raw is None:
            return None
        return _delivery_from_raw(raw)
    if len(data) == 1:
        return _delivery_from_raw(next(iter(data.values())))
    return None


def _delivery_from_raw(raw: Mapping[str, Any]) -> ActiveDelivery:
    chat_id = raw["chat_id"]
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ValueError("chat_id must be an int")
    tid = raw.get("thread_id")
    return ActiveDelivery(
        chat_id=chat_id,
        root=str(raw["root"]),
        thread_id=str(tid) if tid is not None else None,
    )


def _load_delivery_unlocked(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("active delivery unreadable (%s); starting empty", exc)
        return {}
    if not isinstance(doc, dict):
        return {}
    body = doc.get("delivery", doc)
    if not isinstance(body, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in body.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def _save_delivery_unlocked(path: Path, delivery: dict[str, dict[str, Any]]) -> None:
    doc = {"version": _STORE_VERSION, "delivery": delivery}
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_text(path, text, mode=0o600)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """JSON-backed job store with catch-up-once ``run_due`` / ``tick``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else jobs_path()

    # -- CRUD ---------------------------------------------------------------

    def add(
        self,
        *,
        prompt: str,
        chat_id: int,
        cron: str | None = None,
        at: str | None = None,
        root: str | Path | None = None,
        payload: Mapping[str, Any] | None = None,
        enabled: bool = True,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> ScheduledJob:
        """Create and persist a job. Defaults *root* to personal."""
        prompt_s = str(prompt).strip()
        if not prompt_s:
            raise ScheduleError("prompt must be non-empty")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise ScheduleError("chat_id must be an int")
        if cron and at:
            raise ScheduleError("specify either cron or at, not both")
        if not cron and not at:
            raise ScheduleError("schedule requires cron= or at=")
        if cron:
            CronExpr.parse(cron)  # validate early
        stamp = now if now is not None else utcnow()
        root_path = resolve_job_root(root)
        next_run = compute_next_run(cron=cron, at=at, after=stamp)
        if at and next_run is None:
            raise ScheduleError(f"at= timestamp is not in the future: {at}")
        job = ScheduledJob(
            id=job_id or mint_job_id(),
            prompt=prompt_s,
            chat_id=chat_id,
            root=str(root_path),
            enabled=enabled,
            cron=cron.strip() if cron else None,
            at=format_iso(parse_iso(at)) if at else None,
            payload=dict(payload or {}),
            last_run=None,
            next_run=format_iso(next_run) if next_run is not None else None,
            created_at=format_iso(stamp),
        )
        with file_lock(self.path):
            data = self._load_unlocked()
            if job.id in data:
                raise ScheduleError(f"job id already exists: {job.id}")
            data[job.id] = job.to_dict()
            self._save_unlocked(data)
        return job

    def list(self, *, chat_id: int | None = None) -> list[ScheduledJob]:
        """Return jobs, optionally filtered by *chat_id*."""
        with file_lock(self.path):
            data = self._load_unlocked()
        out: list[ScheduledJob] = []
        for raw in data.values():
            try:
                job = ScheduledJob.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                _log.warning("skipping corrupt schedule row: %s", exc)
                continue
            if chat_id is not None and job.chat_id != chat_id:
                continue
            out.append(job)
        out.sort(key=lambda j: (j.next_run or "", j.id))
        return out

    def get(self, job_id: str) -> ScheduledJob | None:
        with file_lock(self.path):
            raw = self._load_unlocked().get(job_id)
        if raw is None:
            return None
        return ScheduledJob.from_dict(raw)

    def remove(self, job_id: str) -> ScheduledJob | None:
        """Delete *job_id* if present; return the removed row."""
        with file_lock(self.path):
            data = self._load_unlocked()
            raw = data.pop(job_id, None)
            if raw is None:
                return None
            self._save_unlocked(data)
            return ScheduledJob.from_dict(raw)

    def enable(self, job_id: str, enabled: bool = True, *, now: datetime | None = None) -> ScheduledJob:
        """Enable or disable a job; recompute ``next_run`` when enabling."""
        stamp = now if now is not None else utcnow()
        with file_lock(self.path):
            data = self._load_unlocked()
            raw = data.get(job_id)
            if raw is None:
                raise ScheduleError(f"unknown job id: {job_id}")
            job = ScheduledJob.from_dict(raw)
            job.enabled = bool(enabled)
            if job.enabled:
                nxt = compute_next_run(cron=job.cron, at=job.at, after=stamp)
                job.next_run = format_iso(nxt) if nxt is not None else None
                if job.at and job.next_run is None:
                    raise ScheduleError(
                        f"cannot enable one-shot job with past at=: {job.at}"
                    )
            data[job_id] = job.to_dict()
            self._save_unlocked(data)
            return job

    # -- tick / catch-up ----------------------------------------------------

    def tick(self, now: datetime | None = None) -> list[DueWork]:
        """Alias for :meth:`run_due`."""
        return self.run_due(now=now)

    def run_due(self, now: datetime | None = None) -> list[DueWork]:
        """Yield due work and advance each firing **once** (catch-up α).

        For each enabled job with ``next_run <= now``, emit one :class:`DueWork`,
        set ``last_run``, and advance ``next_run`` to the next future cron tick
        (or disable a one-shot ``at`` job). Intermediate missed ticks are skipped.
        """
        stamp = parse_iso(now) if now is not None else utcnow()
        due: list[DueWork] = []
        with file_lock(self.path):
            data = self._load_unlocked()
            dirty = False
            for job_id, raw in list(data.items()):
                try:
                    job = ScheduledJob.from_dict(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    _log.warning("skipping corrupt schedule row: %s", exc)
                    continue
                if not job.enabled or not job.next_run:
                    continue
                try:
                    next_at = parse_iso(job.next_run)
                except ValueError:
                    _log.warning("job %s has bad next_run %r", job_id, job.next_run)
                    continue
                if next_at > stamp:
                    continue
                due.append(
                    DueWork(
                        job_id=job.id,
                        chat_id=job.chat_id,
                        root=Path(job.root),
                        prompt=job.prompt,
                        payload=dict(job.payload),
                    )
                )
                job.last_run = format_iso(stamp)
                if job.cron:
                    # Catch-up once: jump to the next fire *after now*, not after
                    # the missed next_run (which would still be in the past).
                    nxt = next_cron_after(job.cron, stamp)
                    job.next_run = format_iso(nxt)
                else:
                    # One-shot: disable after the single catch-up run.
                    job.enabled = False
                    job.next_run = None
                data[job_id] = job.to_dict()
                dirty = True
            if dirty:
                self._save_unlocked(data)
        return due

    def dispatch_due(
        self,
        submit: Callable[[DueWork], Any],
        *,
        now: datetime | None = None,
    ) -> list[DueWork]:
        """``run_due`` then call *submit* for each item (e.g. ``router.submit_turn``)."""
        due = self.run_due(now=now)
        for work in due:
            submit(work)
        return due

    # -- persistence --------------------------------------------------------

    def _load_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("schedule store unreadable (%s); starting empty", exc)
            return {}
        if not isinstance(doc, dict):
            return {}
        jobs = doc.get("jobs", doc)
        if not isinstance(jobs, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in jobs.items():
            if isinstance(value, dict):
                out[str(key)] = value
        return out

    def _save_unlocked(self, jobs: dict[str, dict[str, Any]]) -> None:
        doc = {"version": _STORE_VERSION, "jobs": jobs}
        text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        atomic_write_text(self.path, text, mode=0o600)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# Agent self-schedule tool helpers (T-SCHED-2)
# ---------------------------------------------------------------------------


def schedule_tool_call(
    *,
    action: str,
    prompt: str = "",
    cron: str = "",
    at: str = "",
    job_id: str = "",
    root: str = "personal",
    chat_id: int | None = None,
    enabled: bool | None = None,
    scheduler: Scheduler | None = None,
    default_root: Path | str | None = None,
) -> str:
    """Execute a ``schedule_task`` tool action; returns a model-facing string."""
    sched = scheduler or Scheduler()
    act = (action or "create").strip().lower()
    if act in {"create", "add", "propose"}:
        cid = chat_id
        if cid is None:
            hint = None
            if default_root is not None:
                hint = get_active_delivery(default_root)
            if hint is None:
                hint = get_active_delivery()
            cid = hint.chat_id if hint is not None else None
        if cid is None:
            raise ScheduleError(
                "chat_id is required to create a schedule "
                "(pass chat_id= or run inside a gateway turn)"
            )
        job = sched.add(
            prompt=prompt,
            chat_id=cid,
            cron=cron or None,
            at=at or None,
            root=root or "personal",
        )
        when = job.next_run or job.at or job.cron
        return (
            f"Scheduled job {job.id}: next_run={when} "
            f"chat_id={job.chat_id} root={job.root} "
            f"prompt={job.prompt!r}"
        )
    if act == "list":
        jobs = sched.list(chat_id=chat_id)
        if not jobs:
            return "No scheduled jobs."
        lines = ["Scheduled jobs:"]
        for job in jobs:
            kind = f"cron={job.cron!r}" if job.cron else f"at={job.at!r}"
            lines.append(
                f"- {job.id}: enabled={job.enabled} {kind} "
                f"next_run={job.next_run} chat_id={job.chat_id} "
                f"prompt={job.prompt!r}"
            )
        return "\n".join(lines)
    if act in {"remove", "delete", "cancel"}:
        jid = job_id.strip()
        if not jid:
            raise ScheduleError("job_id is required for remove")
        removed = sched.remove(jid)
        if removed is None:
            return f"No job with id {jid!r}."
        return f"Removed scheduled job {jid}."
    if act in {"enable", "disable"}:
        jid = job_id.strip()
        if not jid:
            raise ScheduleError("job_id is required for enable/disable")
        want = True if act == "enable" else False
        if enabled is not None:
            want = bool(enabled)
        job = sched.enable(jid, want)
        return (
            f"{'Enabled' if job.enabled else 'Disabled'} job {job.id}; "
            f"next_run={job.next_run}"
        )
    raise ScheduleError(
        f"unknown action {action!r}; use create|list|remove|enable|disable"
    )


def iter_due_prompts(due: Sequence[DueWork]) -> Iterator[tuple[int, str, Path]]:
    """Convenience: ``(chat_id, prompt, root)`` for each due item."""
    for work in due:
        yield work.chat_id, work.prompt, work.root


__all__ = [
    "ACTIVE_DELIVERY_FILENAME",
    "JOBS_FILENAME",
    "ActiveDelivery",
    "CronExpr",
    "DueWork",
    "ScheduleError",
    "ScheduledJob",
    "Scheduler",
    "active_delivery_path",
    "clear_active_delivery",
    "compute_next_run",
    "format_iso",
    "get_active_delivery",
    "iter_due_prompts",
    "jobs_path",
    "mint_job_id",
    "next_cron_after",
    "parse_iso",
    "resolve_job_root",
    "schedule_tool_call",
    "set_active_delivery",
    "utcnow",
]
