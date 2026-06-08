# Examples

Copy these into your global (`~/.jarn/`) or project (`.jarn/`) directories.

| File | Copy to | What it does |
|---|---|---|
| `skills/safe-refactor.md` | `.jarn/skills/` | Auto-triggered skill: refactor in verified steps |
| `commands/review.md` | `.jarn/commands/` | `/review` slash command for diff review |
| `agents/test-writer.md` | `.jarn/agents/` | A `test-writer` subagent (uses a cheaper model) |
| `project.config.yaml` | `.jarn/config.yaml` | Project config with hooks, allowlist, MCP placeholder |
| `mcp-filesystem.snippet.yaml` | merge into `.jarn/config.yaml` | Optional MCP stdio server (requires Node + `npx`) |

**End-to-end walkthrough:** [docs/EXTENDING.md § Quick start](../docs/EXTENDING.md#quick-start-wire-skill--hook--mcp)

See [../docs/EXTENDING.md](../docs/EXTENDING.md) for the file formats.

```bash
mkdir -p .jarn/skills .jarn/commands .jarn/agents
cp examples/skills/safe-refactor.md .jarn/skills/
cp examples/commands/review.md      .jarn/commands/
cp examples/agents/test-writer.md   .jarn/agents/
cp examples/project.config.yaml     .jarn/config.yaml
# Optional MCP (Node required): merge mcp-filesystem.snippet.yaml into .jarn/config.yaml
```
