# demo.tape — J.A.R.N. money-shot recording
# Requires: vhs (brew install vhs), JARN_DEMO=1 for deterministic canned responses.
# Run via: ./scripts/record-demo.sh
#
# Money shot: launch → ask a real task → plan approval → streamed diff →
#             verified badge (T-3-2) → /cost

Output docs/assets/demo.gif

# Terminal dimensions — wide enough to show the full diff panel
Set Width  1200
Set Height  700
Set FontSize 14
Set Theme   "Dracula"
Set Shell   "zsh"

# Ensure demo mode: canned-response provider, no real API key needed
Env JARN_DEMO 1

# ── 1. Launch jarn ──────────────────────────────────────────────────────────
Type "jarn"
Sleep 200ms
Enter
Sleep 2s

# ── 2. Ask the money-shot task ───────────────────────────────────────────────
Type "add input validation to server.py"
Sleep 300ms
Enter
Sleep 3s

# ── 3. Plan approval — jarn shows its plan first, user approves ──────────────
# The plan panel appears; press Enter to approve
Enter
Sleep 4s

# ── 4. Streamed diff — watch edits appear line by line ───────────────────────
Sleep 5s

# ── 5. Verified badge (T-3-2) — ⎿ verified: pytest ✓ 4 passed · 0.3s ────────
Sleep 3s

# ── 6. /cost — session cost summary ─────────────────────────────────────────
Type "/cost"
Sleep 200ms
Enter
Sleep 2s

# Hold on the final frame so viewers can read it
Sleep 3s
