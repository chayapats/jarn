#!/usr/bin/env bash
# record-demo.sh — record the J.A.R.N. demo GIF from demo.tape
#
# Usage: ./scripts/record-demo.sh
#
# Requirements:
#   vhs      — brew install vhs      (charmbracelet/vhs)
#   gifsicle — brew install gifsicle (GIF optimizer, targets < 3 MB)
#
# The tape sets JARN_DEMO=1 so the run is deterministic (canned-response
# provider — no real API key is required).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAPE="${REPO_ROOT}/demo.tape"
GIF="${REPO_ROOT}/docs/assets/demo.gif"

# ── 1. Pre-flight checks ─────────────────────────────────────────────────────

if ! command -v vhs &>/dev/null; then
  echo "ERROR: 'vhs' is not installed." >&2
  echo "       Install it with: brew install vhs" >&2
  echo "       Or see: https://github.com/charmbracelet/vhs" >&2
  exit 1
fi

if ! command -v gifsicle &>/dev/null; then
  echo "ERROR: 'gifsicle' is not installed." >&2
  echo "       Install it with: brew install gifsicle" >&2
  exit 1
fi

if [[ ! -f "${TAPE}" ]]; then
  echo "ERROR: demo.tape not found at ${TAPE}" >&2
  exit 1
fi

# ── 2. Record ────────────────────────────────────────────────────────────────

echo "Recording demo.tape → ${GIF} ..."
cd "${REPO_ROOT}"
vhs "${TAPE}"

if [[ ! -f "${GIF}" ]]; then
  echo "ERROR: vhs did not produce ${GIF}" >&2
  exit 1
fi

# ── 3. Optimize (target < 3 MB) ──────────────────────────────────────────────

BEFORE=$(du -k "${GIF}" | cut -f1)
echo "Optimizing GIF (before: ${BEFORE} KB) ..."

gifsicle -O3 --lossy=30 -o "${GIF}" "${GIF}"

AFTER=$(du -k "${GIF}" | cut -f1)
echo "Done. After: ${AFTER} KB"

# Warn if still over 3 MB (3072 KB)
if [[ "${AFTER}" -gt 3072 ]]; then
  echo "WARNING: GIF is ${AFTER} KB (> 3 MB target)." >&2
  echo "         Try: gifsicle -O3 --lossy=80 --colors 128 -o ${GIF} ${GIF}" >&2
fi

echo ""
echo "demo.gif recorded at: ${GIF}"
echo "Commit it: git add docs/assets/demo.gif && git commit -m 'chore: record demo GIF'"
