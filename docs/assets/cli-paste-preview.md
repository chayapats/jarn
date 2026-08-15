# CLI paste preview (P5-4)

Shipped in [#96](https://github.com/chayapats/jarn/pull/96) (P1-1). The
bracketed-paste token in the **input buffer** is unchanged (`repl/keys.py`
`_paste` / `_expand_pastes`). Only the **submitted echo** in scrollback
collapses.

Glyphs: `›` is `grammar.GLYPH_PROMPT`. The after line is dim
(`layout.muted` / `palette.C_DIM`).

## Before (waves A–K grammar)

Submitting a 12-line paste echoed `layout.prompt` of the expanded wall (or of
the token plus a later expansion), so scrollback grew by every pasted line:

```text
› line 1
line 2
line 3
line 4
line 5
line 6
line 7
line 8
line 9
line 10
line 11
line 12
```

The input buffer already used `[Pasted text #1 +12 lines]` for pastes with
≥3 newlines or >800 characters. That token was the before-state *buffer*;
the after-state is the *scrollback echo*.

## After (P1)

One dim preview line. The agent still receives the original payload.

```text
› [Pasted text #1 +12 lines]
```

A 2-line paste (below the collapse threshold, so no token in the buffer)
still echos as one dim label, not two prompt lines:

```text
› [Pasted text +2 lines]
```

Single-line submit is unchanged (`layout.prompt`):

```text
› hello
```

Host-direct `!` stays error-red (`layout.host_shell`), never a dim paste
preview:

```text
! ls -la (host shell)
```
