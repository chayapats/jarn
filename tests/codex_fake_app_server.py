"""Deterministic stdio fixture for the Codex subscription bridge tests."""

from __future__ import annotations

import json
import os
import sys


def send(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


mode = os.environ.get("JARN_CODEX_FAKE_MODE", "final")

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        if mode == "require_safe_flags":
            expected = {"shell_tool", "unified_exec", "apps", "browser_use"}
            disabled = {
                sys.argv[idx + 1] for idx, arg in enumerate(sys.argv[:-1]) if arg == "--disable"
            }
            if not expected <= disabled:
                send({"id": request_id, "error": {"message": "unsafe feature flags"}})
                continue
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        continue
    elif method == "account/read":
        account_type = "apiKey" if mode == "api_key" else "chatgpt"
        send(
            {
                "id": request_id,
                "result": {
                    "account": {
                        "type": account_type,
                        "planType": None if account_type == "apiKey" else "plus",
                    },
                    "requiresOpenaiAuth": True,
                },
            }
        )
    elif method == "thread/start":
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": "thr_fake",
                        "ephemeral": bool(params.get("ephemeral")),
                    }
                },
            }
        )
    elif method == "turn/start":
        send(
            {
                "id": request_id,
                "result": {"turn": {"id": "turn_fake", "status": "inProgress", "items": []}},
            }
        )
        transcript = "\n".join(str(item.get("text") or "") for item in params.get("input", []))
        if mode == "turn_error":
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thr_fake",
                        "turn": {
                            "id": "turn_fake",
                            "status": "failed",
                            "error": {"message": "synthetic turn failure"},
                        },
                    },
                }
            )
            continue
        if mode == "tool" and "<TOOL RESULT" not in transcript:
            payload = {
                "kind": "tool_calls",
                "content": "",
                "calls": [
                    {
                        "name": "read_file",
                        "arguments_json": '{"path":"README.md"}',
                    }
                ],
            }
        elif mode == "bad_arguments":
            payload = {
                "kind": "tool_calls",
                "content": "",
                "calls": [{"name": "read_file", "arguments_json": "[]"}],
            }
        else:
            payload = {"kind": "final", "content": "subscription ready", "calls": []}
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thr_fake",
                    "turnId": "turn_fake",
                    "item": {
                        "id": "item_fake",
                        "type": "agentMessage",
                        "text": json.dumps(payload, separators=(",", ":")),
                    },
                },
            }
        )
        send(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thr_fake",
                    "turnId": "turn_fake",
                    "tokenUsage": {
                        "last": {
                            "inputTokens": 123,
                            "outputTokens": 7,
                            "totalTokens": 130,
                            "cachedInputTokens": 0,
                            "reasoningOutputTokens": 0,
                        },
                        "total": {
                            "inputTokens": 123,
                            "outputTokens": 7,
                            "totalTokens": 130,
                            "cachedInputTokens": 0,
                            "reasoningOutputTokens": 0,
                        },
                    },
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_fake",
                    "turn": {
                        "id": "turn_fake",
                        "status": "completed",
                        "items": [],
                        "error": None,
                    },
                },
            }
        )
