"""Cross-setting consistency checks (jarn.config.consistency) and their wiring
into the interactive settings editor (controller.set_setting)."""

from __future__ import annotations

from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.config.consistency import check_consistency, consistency_errors
from jarn.config.loader import load_config
from jarn.config.schema import Config
from jarn.tui.controller import Controller

# -- pure checker ----------------------------------------------------------

def test_clean_config_has_no_issues():
    cfg = Config()  # all defaults: local backend, OS sandbox off
    errors, _ = check_consistency(cfg)
    assert errors == []


def test_os_sandbox_without_local_backend_is_a_hard_error():
    cfg = Config()
    cfg.execution.backend = "docker"
    cfg.execution.local_sandbox = "require"
    errors = consistency_errors(cfg)
    assert len(errors) == 1
    assert "execution.local_sandbox" in errors[0].keys
    assert "execution.backend" in errors[0].keys
    assert "local backend" in errors[0].message


def test_os_sandbox_off_with_docker_is_fine():
    cfg = Config()
    cfg.execution.backend = "docker"
    cfg.execution.local_sandbox = "off"
    assert consistency_errors(cfg) == []


def test_budget_threshold_without_budget_warns():
    cfg = Config()
    cfg.budget.per_session_usd = None
    _, warnings = check_consistency(cfg)
    assert any(w.involves("budget.hard_stop") for w in warnings)


def test_compact_pct_with_autocompact_off_warns():
    cfg = Config()
    cfg.context.auto_compact = False
    _, warnings = check_consistency(cfg)
    assert any(w.involves("context.compact_at_pct") for w in warnings)



# -- controller wiring -----------------------------------------------------

def _controller(tmp_path, monkeypatch, base_config) -> Controller:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    return Controller(base_config, root)


def test_set_setting_blocks_introduced_hard_conflict(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        # backend is local by default, so turning the OS sandbox on is fine...
        ok, _ = ctrl.set_setting("execution.local_sandbox", "require")
        assert ok
        # ...but now switching the backend away from local is the contradiction.
        ok, msg = ctrl.set_setting("execution.backend", "docker")
    assert not ok
    assert "Rejected" in msg and "local backend" in msg
    # the rejected value must not have been persisted
    reloaded = load_config(project_root=ctrl.project_root, project_trusted=True)
    assert reloaded.execution.backend == "local"


def test_set_setting_succeeds_with_soft_warning(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        ctrl.set_setting("budget.per_session_usd", "0")  # unlimited
        ok, msg = ctrl.set_setting("budget.hard_stop", "true")
    assert ok
    assert "⚠" in msg and "unlimited" in msg


def test_set_setting_does_not_block_unrelated_edit_under_prior_conflict(
    tmp_path, monkeypatch, base_config
):
    """A pre-existing contradiction must not block an unrelated change."""
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        # Manufacture a standing conflict directly on the live config.
        ctrl.set_setting("execution.local_sandbox", "require")
        ctrl.config.execution.backend = "docker"  # conflict now exists in-memory
        # An unrelated edit (theme) should still go through.
        ok, _ = ctrl.set_setting("ui.theme", "light")
    assert ok
