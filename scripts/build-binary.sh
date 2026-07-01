#!/usr/bin/env bash
# Build a standalone `jarn` binary with PyInstaller.
#
# Usage: ./scripts/build-binary.sh
# Requires: uv. Produces ./dist/jarn for the current OS/arch.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Installing build deps"
uv sync --extra build >/dev/null

echo "==> Building (this can take a couple of minutes)"
( cd packaging && uv run pyinstaller jarn.spec --noconfirm --distpath ../dist --workpath ../build/pyinstaller )

echo "==> Smoke-testing the binary"
./dist/jarn --version

echo "==> Done: ./dist/jarn"
