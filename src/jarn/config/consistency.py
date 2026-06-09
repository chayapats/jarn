"""Cross-setting consistency checks for a loaded :class:`~jarn.config.schema.Config`.

Individual settings are type/range-validated in :mod:`jarn.config.loader`. This
module catches the *combinations* that don't make sense together — e.g. asking
for the kernel-level OS sandbox while the backend that would honour it (``local``)
isn't selected, or tuning a threshold whose feature is switched off.

Two severities:

* **errors** — genuine contradictions. The setting can't take effect and the
  combination is incoherent. The interactive ``/config`` editor refuses to
  *introduce* one of these (you can't "turn on" the offending knob).
* **warnings** — the value is harmless but currently has no effect (the feature
  it tunes is off, or a policy profile will overwrite it at launch). Saved, but
  surfaced as a note.

Each issue records the setting keys it involves so the editor can tell whether
the edit the user just made is what created the problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarn.config.schema import Config


@dataclass(frozen=True, slots=True)
class Issue:
    """One consistency finding: the settings it spans + a plain-language message."""

    keys: tuple[str, ...]
    message: str

    def involves(self, key: str) -> bool:
        return key in self.keys


def check_consistency(config: Config) -> tuple[list[Issue], list[Issue]]:
    """Return ``(errors, warnings)`` for ``config``.

    Pure: inspects the config only, never touches disk. Safe to call on any
    loaded config (the profile, if any, is *not* yet applied at this point —
    see :mod:`jarn.config.profiles` — so profile-driven overrides are flagged as
    warnings rather than silently masked)."""
    errors: list[Issue] = []
    warnings: list[Issue] = []

    ex = config.execution

    # -- HARD: OS sandbox is only honoured by the local backend ---------------
    # builder.py reads execution.local_sandbox only when constructing the local
    # backend; under docker/sandbox it is silently ignored. Requiring/auto-ing it
    # there is a contradiction, not just dead config.
    if ex.local_sandbox != "off" and ex.backend != "local":
        errors.append(Issue(
            ("execution.local_sandbox", "execution.backend"),
            f"OS sandbox (local_sandbox={ex.local_sandbox!r}) only applies to the "
            f"local backend, but 'Run commands in' is {ex.backend!r}. "
            "Set the backend to 'local', or set OS sandbox to 'off'.",
        ))

    # -- SOFT: sandbox network toggle with no sandbox active ------------------
    if (ex.backend == "local" and ex.local_sandbox == "off"
            and not ex.sandbox_allow_network):
        warnings.append(Issue(
            ("execution.sandbox_allow_network", "execution.local_sandbox",
             "execution.backend"),
            "Sandbox network is off, but no sandbox is active "
            "(local backend with OS sandbox off), so it has no effect.",
        ))

    # -- SOFT: budget thresholds with no budget set --------------------------
    b = config.budget
    if not b.per_session_usd:  # None or 0 == unlimited
        warnings.append(Issue(
            ("budget.hard_stop", "budget.warn_at_pct", "budget.per_session_usd"),
            "Hard-stop and warn-at % have no effect while the session budget "
            "is 0 (unlimited).",
        ))

    # -- SOFT: context thresholds whose feature is off -----------------------
    ctx = config.context
    if not ctx.auto_compact:
        warnings.append(Issue(
            ("context.compact_at_pct", "context.auto_compact"),
            "Compact-at % has no effect while Auto-compact is off.",
        ))
    if ctx.repo_map == "off":
        warnings.append(Issue(
            ("context.repo_map_tokens", "context.repo_map"),
            "Repo-map size has no effect while Repo map is off.",
        ))

    # -- SOFT: a policy profile will overwrite these at launch ----------------
    # Profiles are re-applied at the launch boundary (jarn.config.profiles), so a
    # value that disagrees with the active profile won't survive a restart.
    profile = config.policy.profile
    if profile:
        from jarn.config.profiles import PROFILES

        effect = PROFILES.get(profile)
        if effect is not None:
            current = {
                "permission_mode": config.permission_mode.value,
                "local_sandbox": ex.local_sandbox,
                "sandbox_allow_network": ex.sandbox_allow_network,
                "web_tools": config.policy.web_tools,
            }
            key_for = {
                "permission_mode": "permission_mode",
                "local_sandbox": "execution.local_sandbox",
                "sandbox_allow_network": "execution.sandbox_allow_network",
                "web_tools": "policy.web_tools",
            }
            for field_, want in effect.items():
                if current.get(field_) != want:
                    warnings.append(Issue(
                        (key_for[field_], "policy.profile"),
                        f"Profile {profile!r} sets {key_for[field_]} = {want!r} "
                        "at launch; your value will be overwritten on restart.",
                    ))

    return errors, warnings


def consistency_errors(config: Config) -> list[Issue]:
    """Just the hard contradictions (convenience for callers that ignore warnings)."""
    return check_consistency(config)[0]
