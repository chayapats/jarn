"""S02 — boxed composer: muted rules, locale placeholder, native scrollback."""

from __future__ import annotations

from prompt_toolkit.document import Document
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.processors import BeforeInput, TransformationInput

from jarn.config.schema import (
    Config,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
    UIConfig,
)
from jarn.repl.app import InlineApp, _ComposerPlaceholder
from jarn.tui import grammar, layout, palette
from jarn.tui.i18n import t


def _app(tmp_path, monkeypatch, locale: str) -> InlineApp:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
        ui=UIConfig(locale=locale),
    )
    return InlineApp(cfg, root)


def _composer_hsplit(built):
    return built.layout.container.content


def _prompt_control(built) -> BufferControl:
    prompt = _composer_hsplit(built).children[2]
    assert isinstance(prompt.content, BufferControl)
    return prompt.content


def _render_input_line(control: BufferControl, text: str, width: int = 80) -> str:
    fragments: list = []
    doc = Document(text)
    for proc in control.input_processors or []:
        ti = TransformationInput(
            control, doc, 0, lambda i: i, fragments, width, 1
        )
        fragments = proc.apply_transformation(ti).fragments
    return "".join(part[1] for part in fragments)


def test_80_col_box_has_rule_prompt_placeholder_rule():
    placeholder = t("composer.placeholder.first", "th")
    box = layout.composer_box(placeholder, width=80, dialect="plain")
    lines = box.splitlines()
    assert lines == [
        "─" * 80,
        f"{grammar.GLYPH_PROMPT} {placeholder}",
        "─" * 80,
    ]
    assert grammar.GLYPH_PROMPT == "›"


def test_locale_placeholders_differ():
    th = layout.composer_box(t("composer.placeholder.first", "th"), width=80)
    en = layout.composer_box(t("composer.placeholder.first", "en"), width=80)
    assert th != en
    assert "ให้ jarn วางแผน ค้นหา หรือลงมือ" in th
    assert "Ask jarn to plan, search, or build" in en
    assert th.splitlines()[0] == en.splitlines()[0] == "─" * 80


def test_typed_text_hides_placeholder():
    placeholder = t("composer.placeholder.first", "en")
    box = layout.composer_box(placeholder, width=80, typed="fix the login bug")
    mid = box.splitlines()[1]
    assert "fix the login bug" in mid
    assert placeholder not in mid
    assert mid.startswith(f"{grammar.GLYPH_PROMPT} ")


def test_build_app_sandwiches_prompt_in_dim_rules(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "th")
    try:
        built = app._build_app()
        kids = list(_composer_hsplit(built).children)
        assert kids[1].char == "─"
        assert kids[3].char == "─"
        assert palette.C_DIM in kids[1].style
        assert palette.C_DIM in kids[3].style
        assert isinstance(kids[2].content, BufferControl)
        assert kids[2].content.buffer is app.input
        assert built.full_screen is False
    finally:
        app.controller.close()


def test_placeholder_processor_first_vs_later_and_typing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "th")
    try:
        built = app._build_app()
        control = _prompt_control(built)
        procs = control.input_processors or []
        assert isinstance(procs[0], BeforeInput)
        assert procs[0].text == f"{grammar.GLYPH_PROMPT} "
        assert isinstance(procs[1], _ComposerPlaceholder)

        first = t("composer.placeholder.first", "th")
        later = t("composer.placeholder.later", "th")
        empty = _render_input_line(control, "")
        assert empty.startswith(f"{grammar.GLYPH_PROMPT} ")
        assert first in empty
        assert later not in empty

        typed = _render_input_line(control, "hello")
        assert first not in typed
        assert later not in typed
        # BeforeInput still prefixes; the draft itself lives in the document,
        # not in the processor output.
        assert typed.startswith(f"{grammar.GLYPH_PROMPT} ")

        app._mark_composer_later()
        after = _render_input_line(control, "")
        assert later in after
        assert first not in after
        assert after != empty

        app._clear_scrollback()
        reset = _render_input_line(control, "")
        assert first in reset
        assert later not in reset
    finally:
        app.controller.close()


def test_en_placeholder_from_wired_app(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "en")
    try:
        built = app._build_app()
        line = _render_input_line(_prompt_control(built), "")
        assert t("composer.placeholder.first", "en") in line
        assert t("composer.placeholder.first", "th") not in line
        th_app = _app(tmp_path / "th", monkeypatch, "th")
        try:
            th_line = _render_input_line(_prompt_control(th_app._build_app()), "")
        finally:
            th_app.controller.close()
        assert line != th_line
    finally:
        app.controller.close()


def test_composer_windows_are_not_the_old_hard_clip(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "en")
    try:
        built = app._build_app()

        def _windows(container):
            if isinstance(container, Window):
                yield container
                return
            for child in getattr(container, "children", []) or []:
                yield from _windows(child)
            content = getattr(container, "content", None)
            if content is not None and content is not container:
                yield from _windows(content)

        rules = [w for w in _windows(built.layout.container) if w.char == "─"]
        assert len(rules) == 2
    finally:
        app.controller.close()
