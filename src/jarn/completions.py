"""Shell completion script emitters for jarn.

Each emitter takes the real ArgumentParser and returns a completion script
string.  They introspect the parser directly so the scripts can never drift
behind the CLI — adding a subcommand or flag to ``cli.py`` automatically
updates all three completion scripts on the next ``jarn completions`` run.
Descriptions shown in zsh/fish menus are pulled from the argparse ``help``
text (never the flag string itself), so completions read cleanly.

Install one-liners:
  bash  — jarn completions bash > ~/.bash_completions/jarn.bash
          # then: source ~/.bash_completions/jarn.bash  (add to ~/.bashrc)
  zsh   — jarn completions zsh > ~/.zfunc/_jarn
          # then: fpath=(~/.zfunc $fpath) && autoload -Uz compinit && compinit
  fish  — jarn completions fish > ~/.config/fish/completions/jarn.fish
"""

from __future__ import annotations

import argparse


def _describe(help_text: str | None) -> str:
    """Return a short, shell-safe one-line description from argparse *help_text*.

    Suppressed / missing help yields ``""`` (the caller then omits the
    description rather than falling back to the flag name). The first sentence is
    kept, characters that break shell single-quoting or zsh spec brackets are
    stripped, and the result is truncated so menus stay tidy.
    """
    if not help_text or help_text == argparse.SUPPRESS:
        return ""
    text = " ".join(help_text.split())
    # Keep just the first sentence — the rest is usually caveats.
    if ". " in text:
        text = text.split(". ", 1)[0]
    # Strip characters that would break shell single-quoting or zsh '[...:...]'.
    for ch in ("'", "[", "]", ":", "\\", "`", "$", '"'):
        text = text.replace(ch, "")
    text = text.strip()
    if len(text) > 60:
        text = text[:57].rstrip() + "..."
    return text


def _introspect(
    parser: argparse.ArgumentParser,
) -> tuple[list[str], list[str], dict[str, list[str]], dict[str, str], dict[str, str]]:
    """Introspect *parser* into the pieces the emitters need.

    Returns ``(subcommands, top_flags, per_sub_flags, flag_help, sub_help)``:

    * ``subcommands``   — names of every registered subcommand.
    * ``top_flags``     — every ``--`` long flag at the top level (excl. ``--help``).
    * ``per_sub_flags`` — mapping {subcommand: [long_flags]} for each sub.
    * ``flag_help``     — {flag: short description} sourced from the action help.
    * ``sub_help``      — {subcommand: short description} sourced from the sub help.

    All help text is sourced from the same ``_actions`` / ``_name_parser_map`` the
    coverage introspection walks, so descriptions can't drift from the CLI either.
    """
    top_flags: list[str] = []
    subcommands: list[str] = []
    per_sub_flags: dict[str, list[str]] = {}
    flag_help: dict[str, str] = {}
    sub_help: dict[str, str] = {}

    for action in parser._actions:
        for opt in action.option_strings:
            if opt.startswith("--") and opt != "--help":
                top_flags.append(opt)
                flag_help.setdefault(opt, _describe(action.help))

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Sub help lives on the pseudo-actions, not the sub-parsers.
            for pseudo in action._choices_actions:
                sub_help[pseudo.dest] = _describe(pseudo.help)
            for name, sub in action._name_parser_map.items():
                subcommands.append(name)
                flags: list[str] = []
                for sub_action in sub._actions:
                    for opt in sub_action.option_strings:
                        if opt.startswith("--") and opt != "--help":
                            flags.append(opt)
                            flag_help.setdefault(opt, _describe(sub_action.help))
                per_sub_flags[name] = flags

    return subcommands, top_flags, per_sub_flags, flag_help, sub_help


def _emit_bash(
    subcommands: list[str],
    top_flags: list[str],
    per_sub_flags: dict[str, list[str]],
    flag_help: dict[str, str],
    sub_help: dict[str, str],
) -> str:
    """Return a bash completion script using ``complete -W``.

    Bash's ``-W`` word lists don't render descriptions, so only the
    subcommand/flag words are emitted.
    """
    all_words = " ".join(subcommands + top_flags)

    # Per-subcommand flag cases
    sub_cases: list[str] = []
    for sub, flags in per_sub_flags.items():
        if flags:
            words = " ".join(flags)
            sub_cases.append(
                f"            {sub})\n"
                f'                COMPREPLY=( $(compgen -W "{words}" -- "$cur") )\n'
                f"                return 0\n"
                f"                ;;"
            )

    cases_block = "\n".join(sub_cases) if sub_cases else ""

    script = f"""\
# bash completion for jarn
# Source this file or add to ~/.bashrc:
#   source <(jarn completions bash)

_jarn_completions() {{
    local cur prev words cword
    _init_completion 2>/dev/null || {{
        COMPREPLY=()
        cur="${{COMP_WORDS[COMP_CWORD]}}"
        prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    }}

    # Top-level: first word after 'jarn'
    if [[ ${{COMP_CWORD}} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "{all_words}" -- "$cur") )
        return 0
    fi

    # Per-subcommand flags
    local subcmd="${{COMP_WORDS[1]}}"
    case "$subcmd" in
{cases_block}
    esac

    COMPREPLY=( $(compgen -W "{all_words}" -- "$cur") )
    return 0
}}

complete -F _jarn_completions jarn
"""
    return script


def _zsh_spec(flag: str, flag_help: dict[str, str]) -> str:
    """Return a single zsh ``_arguments`` spec for *flag* (with description)."""
    desc = flag_help.get(flag, "")
    if desc:
        return f"'{flag}[{desc}]'"
    return f"'{flag}'"


def _emit_zsh(
    subcommands: list[str],
    top_flags: list[str],
    per_sub_flags: dict[str, list[str]],
    flag_help: dict[str, str],
    sub_help: dict[str, str],
) -> str:
    """Return a zsh completion script using ``#compdef`` + ``_arguments``.

    Every flag spec is emitted on its own line with a trailing ``\\``
    continuation so the whole ``_arguments -C`` call — flags plus the
    ``'1:command:->subcommand'`` / ``'*::args:->args'`` state specs — is one
    continued command and ``$state`` is reachable.
    """
    # Subcommand descriptions for _describe.
    sub_descriptions = "\n".join(
        f"        '{sub}:{sub_help.get(sub, '') or (sub + ' subcommand')}'"
        for sub in subcommands
    )

    # Top-level flags: each line ends with ` \` so it flows into the state specs.
    top_flag_lines = "".join(
        f"        {_zsh_spec(flag, flag_help)} \\\n" for flag in top_flags
    )

    # Per-subcommand cases. Each is its own continued `_arguments` call: all but
    # the last spec end with ` \`; a flag-less subcommand emits no case at all.
    sub_flag_cases: list[str] = []
    for sub, flags in per_sub_flags.items():
        if not flags:
            continue
        spec_lines = " \\\n".join(
            f"                {_zsh_spec(flag, flag_help)}" for flag in flags
        )
        sub_flag_cases.append(
            f"        ({sub})\n            _arguments \\\n{spec_lines}\n            ;;"
        )
    cases_str = "\n".join(sub_flag_cases)

    script = f"""\
#compdef jarn
# zsh completion for jarn
# Install: jarn completions zsh > ~/.zfunc/_jarn
# Then add to ~/.zshrc: fpath=(~/.zfunc $fpath) && autoload -Uz compinit && compinit

_jarn() {{
    local state

    _arguments -C \\
{top_flag_lines}        '1:command:->subcommand' \\
        '*::args:->args'

    case $state in
        subcommand)
            local -a subcommands
            subcommands=(
{sub_descriptions}
            )
            _describe 'jarn subcommand' subcommands
            ;;
        args)
            local subcmd="${{words[1]}}"
            case $subcmd in
{cases_str}
            esac
            ;;
    esac
}}

_jarn "$@"
"""
    return script


def _emit_fish(
    subcommands: list[str],
    top_flags: list[str],
    per_sub_flags: dict[str, list[str]],
    flag_help: dict[str, str],
    sub_help: dict[str, str],
) -> str:
    """Return a fish completion script using ``complete -c jarn``.

    Each flag is a real ``-l <name>`` declaration; the description (``-d``) is
    the argparse help and is omitted entirely when there is none (never the flag
    string itself).
    """
    lines: list[str] = [
        "# fish completion for jarn",
        "# Install: jarn completions fish > ~/.config/fish/completions/jarn.fish",
        "",
        "# Disable file completions by default",
        "complete -c jarn -f",
        "",
        "# Subcommands",
    ]

    for sub in subcommands:
        desc = sub_help.get(sub, "")
        line = f"complete -c jarn -n '__fish_use_subcommand' -a {sub}"
        if desc:
            line += f" -d '{desc}'"
        lines.append(line)

    lines.append("")
    lines.append("# Top-level flags")
    for flag in top_flags:
        line = f"complete -c jarn -l {flag.lstrip('-')}"
        desc = flag_help.get(flag, "")
        if desc:
            line += f" -d '{desc}'"
        lines.append(line)

    for sub, flags in per_sub_flags.items():
        if flags:
            lines.append("")
            lines.append(f"# Flags for '{sub}'")
            for flag in flags:
                line = (
                    f"complete -c jarn -n '__fish_seen_subcommand_from {sub}' "
                    f"-l {flag.lstrip('-')}"
                )
                desc = flag_help.get(flag, "")
                if desc:
                    line += f" -d '{desc}'"
                lines.append(line)

    lines.append("")
    return "\n".join(lines)


def emit_completions(shell: str, parser: argparse.ArgumentParser) -> str:
    """Return the completion script for *shell* derived from *parser*.

    :param shell: One of ``"bash"``, ``"zsh"``, ``"fish"``.
    :param parser: The top-level ``ArgumentParser`` to introspect.
    :raises ValueError: For unknown shells.
    """
    subcommands, top_flags, per_sub_flags, flag_help, sub_help = _introspect(parser)

    if shell == "bash":
        return _emit_bash(subcommands, top_flags, per_sub_flags, flag_help, sub_help)
    if shell == "zsh":
        return _emit_zsh(subcommands, top_flags, per_sub_flags, flag_help, sub_help)
    if shell == "fish":
        return _emit_fish(subcommands, top_flags, per_sub_flags, flag_help, sub_help)
    raise ValueError(f"Unknown shell: {shell!r}. Expected 'bash', 'zsh', or 'fish'.")
