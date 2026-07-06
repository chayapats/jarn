"""Shell completion script emitters for jarn.

Each emitter takes the real ArgumentParser and returns a completion script
string.  They introspect the parser directly so the scripts can never drift
behind the CLI — adding a subcommand or flag to ``cli.py`` automatically
updates all three completion scripts on the next ``jarn completions`` run.

Install one-liners:
  bash  — jarn completions bash > ~/.bash_completions/jarn.bash
          # then: source ~/.bash_completions/jarn.bash  (add to ~/.bashrc)
  zsh   — jarn completions zsh > ~/.zfunc/_jarn
          # then: fpath=(~/.zfunc $fpath) && autoload -Uz compinit && compinit
  fish  — jarn completions fish > ~/.config/fish/completions/jarn.fish
"""

from __future__ import annotations

import argparse


def _introspect(parser: argparse.ArgumentParser) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Return (subcommands, top_flags, per_sub_flags) from *parser*.

    * ``subcommands``   — names of every registered subcommand.
    * ``top_flags``     — every ``--`` long flag at the top level (excluding
                          ``--help``).
    * ``per_sub_flags`` — mapping {subcommand: [long_flags]} for each sub.
    """
    top_flags: list[str] = []
    subcommands: list[str] = []
    per_sub_flags: dict[str, list[str]] = {}

    for action in parser._actions:
        for opt in action.option_strings:
            if opt.startswith("--") and opt != "--help":
                top_flags.append(opt)

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action._name_parser_map.items():
                subcommands.append(name)
                flags: list[str] = []
                for sub_action in sub._actions:
                    for opt in sub_action.option_strings:
                        if opt.startswith("--") and opt != "--help":
                            flags.append(opt)
                per_sub_flags[name] = flags

    return subcommands, top_flags, per_sub_flags


def _emit_bash(
    subcommands: list[str],
    top_flags: list[str],
    per_sub_flags: dict[str, list[str]],
) -> str:
    """Return a bash completion script using ``complete -W``."""
    all_words = " ".join(subcommands + top_flags)

    # Per-subcommand flag cases
    sub_cases: list[str] = []
    for sub, flags in per_sub_flags.items():
        if flags:
            words = " ".join(flags)
            sub_cases.append(f"            {sub})\n                COMPREPLY=( $(compgen -W \"{words}\" -- \"$cur\") )\n                return 0\n                ;;")

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


def _emit_zsh(
    subcommands: list[str],
    top_flags: list[str],
    per_sub_flags: dict[str, list[str]],
) -> str:
    """Return a zsh completion script using ``#compdef`` + ``_arguments``."""
    # Build subcommand descriptions for _describe
    sub_descriptions = "\n".join(
        f"        '{sub}:{sub} subcommand'" for sub in subcommands
    )

    # Top-level flag arguments for _arguments
    top_flag_args = "\n".join(
        f"    '{flag}[{flag} option]'" for flag in top_flags
    )

    # Per-subcommand cases
    sub_flag_cases: list[str] = []
    for sub, flags in per_sub_flags.items():
        if flags:
            flag_args = " ".join(f"'{f}[{f} option]'" for f in flags)
            sub_flag_cases.append(
                f"        ({sub})\n            _arguments {flag_args}\n            ;;"
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
{top_flag_args}
        '1:command:->subcommand' \\
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
) -> str:
    """Return a fish completion script using ``complete -c jarn``."""
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
        lines.append(f"complete -c jarn -n '__fish_use_subcommand' -a {sub} -d '{sub} subcommand'")

    lines.append("")
    lines.append("# Top-level flags")
    for flag in top_flags:
        flag_name = flag.lstrip("-")
        lines.append(f"complete -c jarn -l {flag_name} -d '{flag}'")

    for sub, flags in per_sub_flags.items():
        if flags:
            lines.append("")
            lines.append(f"# Flags for '{sub}'")
            for flag in flags:
                flag_name = flag.lstrip("-")
                lines.append(
                    f"complete -c jarn -n '__fish_seen_subcommand_from {sub}' -l {flag_name} -d '{flag}'"
                )

    lines.append("")
    return "\n".join(lines)


def emit_completions(shell: str, parser: argparse.ArgumentParser) -> str:
    """Return the completion script for *shell* derived from *parser*.

    :param shell: One of ``"bash"``, ``"zsh"``, ``"fish"``.
    :param parser: The top-level ``ArgumentParser`` to introspect.
    :raises ValueError: For unknown shells.
    """
    subcommands, top_flags, per_sub_flags = _introspect(parser)

    if shell == "bash":
        return _emit_bash(subcommands, top_flags, per_sub_flags)
    if shell == "zsh":
        return _emit_zsh(subcommands, top_flags, per_sub_flags)
    if shell == "fish":
        return _emit_fish(subcommands, top_flags, per_sub_flags)
    raise ValueError(f"Unknown shell: {shell!r}. Expected 'bash', 'zsh', or 'fish'.")
