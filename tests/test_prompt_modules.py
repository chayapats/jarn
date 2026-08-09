"""Prompt-module registry activation, budgets, trust, and controller lifecycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent.prompt_modules import (
    PromptModule,
    PromptModuleContext,
    PromptModuleRegistry,
    create_prompt_module_registry,
    render_prompt_module,
    with_context_budgets,
)
from jarn.config.schema import Config, ContextConfig, PermissionMode, WikiConfig
from jarn.extensibility.skills import Skill
from jarn.memory.tokens import count_tokens, truncate_to_token_budget


def _context(
    config: Config,
    root: Path,
    *,
    trusted: bool = True,
    skills: dict[str, Skill] | None = None,
    activations: dict[str, str] | None = None,
) -> PromptModuleContext:
    return PromptModuleContext(
        config=config,
        project_root=root,
        project_trusted=trusted,
        skills=skills or {},
        explicit_scopes=activations or {},  # type: ignore[arg-type]
    )


def test_registry_toggles_only_plan_module_with_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    cfg = Config(permission_mode=PermissionMode.ASK)
    context = _context(cfg, tmp_path)
    registry = with_context_budgets(create_prompt_module_registry(), context)

    ask = registry.assemble(context)
    assert "mode.plan" not in {module.name for module in ask.modules}

    cfg.permission_mode = PermissionMode.PLAN
    plan = registry.assemble(context)
    assert "mode.plan" in {module.name for module in plan.modules}
    assert "Active mode: plan" in plan.text

    cfg.permission_mode = PermissionMode.AUTO_EDIT
    auto = registry.assemble(context)
    assert "mode.plan" not in {module.name for module in auto.modules}
    assert "Active mode: plan" not in auto.text


def test_registry_orders_and_deduplicates_modules_deterministically(
    tmp_path: Path,
) -> None:
    cfg = Config()
    context = _context(cfg, tmp_path)

    def _module(name: str, priority: int, text: str) -> PromptModule:
        return PromptModule(
            name=name,
            description=text,
            priority=priority,
            scope="runtime",
            trust="global",
            default_budget=100,
            render=lambda _context, value=text: value,
            active_when=lambda _context: True,
            activation_reason=lambda _context: "test activation",
            source="test",
        )

    registry = PromptModuleRegistry(
        [_module("later", 30, "later"), _module("same", 20, "loser"),
         _module("first", 10, "first"), _module("same", 15, "winner")]
    )
    assembly = registry.assemble(context, kernel="kernel")

    assert [module.name for module in assembly.modules] == ["first", "same", "later"]
    assert assembly.text.index("first") < assembly.text.index("winner")
    assert "loser" not in assembly.text


def test_per_module_and_aggregate_budgets_are_strict_for_tiny_values(
    tmp_path: Path,
) -> None:
    cfg = Config()
    context = _context(cfg, tmp_path)
    for budget in range(1, 12):
        bounded = truncate_to_token_budget("long prompt content " * 100, budget)
        assert count_tokens(bounded) <= budget

    module = PromptModule(
        name="large",
        description="large",
        priority=10,
        scope="runtime",
        trust="global",
        default_budget=20,
        render=lambda _context: "large content " * 100,
        active_when=lambda _context: True,
        activation_reason=lambda _context: "test",
        source="test",
    )
    registry = PromptModuleRegistry([module])
    rendered = registry.assemble(context, kernel="kernel", aggregate_budget=18)
    assert count_tokens(rendered.text) <= 18
    assert rendered.modules[0].token_count <= 20
    assert rendered.modules[0].truncated is True


def test_trusted_project_content_never_enters_untrusted_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    (tmp_path / "JARN.md").write_text("PROJECT-ONLY-GUIDANCE", encoding="utf-8")
    skill = Skill(
        name="project-only",
        description="project body",
        body="PROJECT-ONLY-SKILL-BODY",
        scope="project",
    )
    cfg = Config(context=ContextConfig(repo_map="auto"))
    context = _context(
        cfg,
        tmp_path,
        trusted=False,
        skills={skill.name: skill},
        activations={"skill.project-only": "session"},
    )
    registry = with_context_budgets(
        create_prompt_module_registry(context.skills), context
    )
    assembly = registry.assemble(context)
    statuses = {status.name: status for status in registry.statuses(context)}

    assert "PROJECT-ONLY-GUIDANCE" not in assembly.text
    assert "PROJECT-ONLY-SKILL-BODY" not in assembly.text
    assert "project-only" not in assembly.text
    assert statuses["context.project"].active is False
    assert statuses["skill.project-only"].active is False


def test_skill_body_scope_is_turn_or_session_and_bounded(tmp_path: Path) -> None:
    skill = Skill(
        name="deploy",
        description="Deploy safely",
        body="step " * 10_000,
        scope="global",
    )
    cfg = Config()
    registry = create_prompt_module_registry({skill.name: skill})

    turn_context = _context(
        cfg, tmp_path, skills={skill.name: skill},
        activations={"skill.deploy": "turn"},
    )
    turn = registry.assemble(turn_context)
    assert "skill.deploy" not in {module.name for module in turn.modules}
    body = render_prompt_module(registry.resolve("deploy"), turn_context)  # type: ignore[arg-type]
    assert body.scope == "turn"
    assert body.token_count <= body.configured_budget  # type: ignore[operator]
    assert body.truncated is True

    session_context = _context(
        cfg, tmp_path, skills={skill.name: skill},
        activations={"skill.deploy": "session"},
    )
    session = registry.assemble(session_context)
    assert "skill.deploy" in {module.name for module in session.modules}


def test_date_has_registry_metadata_but_is_not_in_static_prompt(tmp_path: Path) -> None:
    cfg = Config()
    context = _context(cfg, tmp_path)
    registry = with_context_budgets(create_prompt_module_registry(), context)
    assembly = registry.assemble(context)
    statuses = {status.name: status for status in registry.statuses(context)}

    assert "Current date:" not in assembly.text
    assert statuses["session.date"].active is True
    assert statuses["session.date"].scope == "session"
    assert statuses["session.date"].token_count > 0
    assert statuses["session.date"].source == "local clock"


def test_override_gets_no_hidden_wiki_repo_or_registry_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    (tmp_path / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    from jarn.memory.wiki import WikiStore

    WikiStore.build(tmp_path).write("note", "wiki content", tier="project")
    cfg = Config(
        default_model="openrouter/test-model",
        context=ContextConfig(repo_map="auto"),
        wiki=WikiConfig(enabled=True),
    )
    fake = GenericFakeChatModel(messages=iter([]))
    from jarn.agent.runtime import build_runtime

    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        runtime = build_runtime(
            cfg,
            project_root=tmp_path,
            system_prompt_override="CONTROLLED OVERRIDE",
        )

    assert runtime.system_prompt == "CONTROLLED OVERRIDE"
    assert runtime.prompt_assembly is not None
    assert runtime.prompt_assembly.modules == ()
    assert runtime.prompt_modules_enabled is False


def test_controller_commands_report_and_consume_explicit_turn_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".jarn").mkdir(parents=True)
    skill = Skill(
        name="deploy",
        description="Deploy safely",
        body="Step 1. Test.\nStep 2. Ship.",
        scope="project",
    )
    cfg = Config()
    context = _context(
        cfg,
        root,
        skills={skill.name: skill},
    )
    registry = with_context_budgets(
        create_prompt_module_registry(context.skills), context
    )
    assembly = registry.assemble(context)

    from jarn.agent.runtime import JarnRuntime
    from jarn.controller import Controller

    ctrl = Controller(cfg, root)
    ctrl.runtime = JarnRuntime(
        agent=object(),
        config=cfg,
        factory=object(),
        project_root=root,
        system_prompt=assembly.text,
        capabilities=object(),  # type: ignore[arg-type]
        prompt_registry=registry,
        prompt_context=context,
        prompt_assembly=assembly,
        skills={skill.name: skill},
    )

    activated = ctrl.handle_command("module", "on deploy turn")
    assert "Activated skill.deploy" in activated.text
    listing = ctrl.handle_command("modules", "active").text
    assert "skill.deploy" in listing
    assert "turn" in listing

    rendered = ctrl._consume_turn_prompt_modules()
    assert [module.name for module in rendered] == ["skill.deploy"]
    assert "Step 1. Test." in rendered[0].content
    assert "skill.deploy" not in ctrl.handle_command("modules", "active").text
    ctrl.close()


@pytest.mark.asyncio
async def test_turn_module_is_consumed_only_when_fresh_turn_is_submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".jarn").mkdir(parents=True)
    skill = Skill(
        name="audit",
        description="Audit carefully",
        body="AUDIT-MODULE-BODY",
        scope="project",
    )
    cfg = Config()
    context = _context(cfg, root, skills={skill.name: skill})
    registry = with_context_budgets(
        create_prompt_module_registry(context.skills), context
    )
    assembly = registry.assemble(context)

    class RecordingAgent:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def astream(self, payload, config, stream_mode=None, **kwargs):
            self.payloads.append(payload)
            yield ("messages", (type("Chunk", (), {"content": "ok"})(),))
            yield ("updates", {})

    from jarn.agent.runtime import JarnRuntime
    from jarn.controller import Controller

    agent = RecordingAgent()
    ctrl = Controller(cfg, root)
    ctrl.runtime = JarnRuntime(
        agent=agent,
        config=cfg,
        factory=object(),
        project_root=root,
        system_prompt=assembly.text,
        capabilities=object(),  # type: ignore[arg-type]
        prompt_registry=registry,
        prompt_context=context,
        prompt_assembly=assembly,
        skills={skill.name: skill},
    )
    ctrl.activate_prompt_module("audit", "turn")
    driver = ctrl.make_driver(lambda _request: None)  # type: ignore[arg-type]

    # Driver construction alone (including approval-inspection paths) cannot
    # consume the queued body.
    assert ctrl._prompt_module_activations == {"skill.audit": "turn"}
    async for _event in driver.run_turn("inspect this"):
        pass

    assert ctrl._prompt_module_activations == {}
    contents = [message["content"] for message in agent.payloads[0]["messages"]]
    assert sum("AUDIT-MODULE-BODY" in content for content in contents) == 1
    ctrl.close()


def test_session_activation_survives_driver_consumption_but_not_new_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".jarn").mkdir(parents=True)
    skill = Skill(
        name="review",
        description="Review carefully",
        body="Review instructions",
        scope="project",
    )
    cfg = Config()
    from jarn.agent.runtime import JarnRuntime
    from jarn.controller import Controller

    ctrl = Controller(cfg, root)
    ctrl.runtime = JarnRuntime(
        agent=object(), config=cfg, factory=object(), project_root=root,
        system_prompt="", capabilities=object(), skills={skill.name: skill},  # type: ignore[arg-type]
    )
    result = ctrl.handle_command("module", "on review session")
    assert "thread" in result.text
    assert ctrl._prompt_module_activations == {"skill.review": "session"}
    assert ctrl._consume_turn_prompt_modules() == ()
    assert ctrl._prompt_module_activations == {"skill.review": "session"}

    ctrl.new_thread()
    assert ctrl._prompt_module_activations == {}
    ctrl.close()
