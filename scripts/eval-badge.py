#!/usr/bin/env python
"""Shields.io endpoint generator for JARN nightly evals.

Reads a summary JSON in ``{pass, fail, total, model, cost}`` format (written by
``eval.py --summary``) and emits a shields.io ENDPOINT JSON to stdout or a file.

Usage
-----
    uv run python scripts/eval-badge.py evals/latest.json
    uv run python scripts/eval-badge.py evals/latest.json --out evals/badge.json

The nightly workflow writes the badge JSON to the ``eval-results`` branch so
the README badge can reference it via a raw GitHub URL:

    https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/<owner>/<repo>/eval-results/evals/badge.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def make_badge_payload(summary: dict) -> dict:
    """Convert a ``{pass, fail, total, ...}`` summary to a shields.io endpoint dict.

    Color rules:
    - green  — all tasks pass (pass == total)
    - yellow — some tasks fail (pass > 0 but < total)
    - red    — no tasks pass (pass == 0)
    """
    n_pass: int = int(summary["pass"])
    n_total: int = int(summary["total"])

    message = f"{n_pass}/{n_total} nightly"

    if n_pass == n_total:
        color = "green"
    elif n_pass > 0:
        color = "yellow"
    else:
        color = "red"

    return {
        "schemaVersion": 1,
        "label": "evals",
        "message": message,
        "color": color,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a shields.io endpoint JSON from an eval summary.")
    parser.add_argument("summary", help="path to the summary JSON ({pass,fail,total,model,cost})")
    parser.add_argument("--out", metavar="PATH", help="write output to PATH instead of stdout")
    args = parser.parse_args(argv)

    summary_path = Path(args.summary)
    if not summary_path.is_file():
        print(f"error: summary file not found: {summary_path}", file=sys.stderr)
        return 1

    try:
        summary = json.loads(summary_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {summary_path}: {exc}", file=sys.stderr)
        return 1

    payload = make_badge_payload(summary)
    out_text = json.dumps(payload, indent=2) + "\n"

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text)
    else:
        sys.stdout.write(out_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
