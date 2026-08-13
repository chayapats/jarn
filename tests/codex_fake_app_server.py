"""Deterministic stdio fixture for the Codex subscription bridge tests."""

from __future__ import annotations

import json
import os
import signal
import sys
import time


def send(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


mode = os.environ.get("JARN_CODEX_FAKE_MODE", "final")

if mode in {"hang_close", "hang_turn"} and hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

if "--version" in sys.argv:
    version = "0.1.0" if mode == "outdated" else "1.2.3"
    print(f"codex-cli {version}")
    raise SystemExit(0)

if mode == "appserver_nonzero":
    print("synthetic app-server startup failure", file=sys.stderr, flush=True)
    raise SystemExit(9)

logged_in = mode not in {"signed_out", "signed_out_then_login"}


def account_payload() -> dict | None:
    if not logged_in:
        return None
    account_type = "apiKey" if mode == "api_key" else "chatgpt"
    if account_type == "apiKey":
        return {"type": account_type, "planType": None}
    payload = {
        "type": "chatgpt",
        "planType": "plus",
    }
    if mode == "workspace_metadata":
        payload["workspace"] = {"id": "workspace-secret-id", "name": "Personal"}
    return payload


def catalog_entry(
    model: str,
    *,
    default: bool = False,
    hidden: bool = False,
) -> dict:
    return {
        "id": f"catalog-{model}",
        "model": model,
        "displayName": model.replace("-", " ").title(),
        "description": f"Fixture model {model}",
        "hidden": hidden,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Fast"},
            {"reasoningEffort": "medium", "description": "Balanced"},
            {"reasoningEffort": "high", "description": "Deep"},
        ],
        "inputModalities": ["text", "image"],
        "supportsPersonality": True,
        "isDefault": default,
    }


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        if mode == "invalid_json":
            print("not-json", flush=True)
            continue
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
        if mode == "old_cli":
            send(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not found: account/read"},
                }
            )
            continue
        if mode == "refresh_failure" and params.get("refreshToken"):
            send({"id": request_id, "error": {"code": 401, "message": "refresh failed"}})
            continue
        if mode == "login_verification_timeout" and params.get("refreshToken"):
            # Login ceremony completed, but account verification never answers.
            continue
        if mode == "workspace_denied":
            send(
                {
                    "id": request_id,
                    "error": {"code": 403, "message": "workspace access denied"},
                }
            )
            continue
        if mode == "expired":
            send(
                {
                    "id": request_id,
                    "error": {"code": 401, "message": "credential expired or revoked"},
                }
            )
            continue
        if mode == "network_failure":
            send(
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": "network connection unavailable"},
                }
            )
            continue
        send(
            {
                "id": request_id,
                "result": {
                    "account": account_payload(),
                    "requiresOpenaiAuth": True,
                },
            }
        )
    elif method == "account/login/start":
        login_type = params.get("type")
        if mode == "malformed_login":
            send({"id": request_id, "result": {"loginId": "login_fake"}})
            continue
        if login_type == "chatgptDeviceCode":
            result = {
                "loginId": "login_fake",
                "verificationUrl": "https://example.test/device",
                "userCode": "ABCD-EFGH",
                "expiresIn": 900,
            }
        elif login_type == "chatgpt":
            result = {
                "loginId": "login_fake",
                "authUrl": "https://example.test/browser-login",
            }
        else:
            send({"id": request_id, "error": {"message": "unsupported login type"}})
            continue
        send({"id": request_id, "result": result})
        if mode == "login_timeout":
            continue
        if mode == "login_update_first":
            send({"method": "account/updated", "params": {"account": account_payload()}})
        if mode == "login_failure":
            send(
                {
                    "method": "account/login/completed",
                    "params": {
                        "loginId": "login_fake",
                        "success": False,
                        "error": {"message": "browser callback failed"},
                    },
                }
            )
            continue
        if mode == "device_expired":
            send(
                {
                    "method": "account/login/completed",
                    "params": {
                        "loginId": "login_fake",
                        "success": False,
                        "error": {"message": "device code expired"},
                    },
                }
            )
            continue
        logged_in = mode != "login_zero_signed_out"
        send(
            {
                "method": "account/login/completed",
                "params": {"loginId": "login_fake", "success": True},
            }
        )
        if mode != "login_update_first":
            send({"method": "account/updated", "params": {"account": account_payload()}})
    elif method == "account/logout":
        logged_in = False
        send({"id": request_id, "result": {}})
        send({"method": "account/updated", "params": {"account": None}})
    elif method == "model/list":
        if mode == "model_network_failure":
            send(
                {
                    "id": request_id,
                    "error": {"code": -32000, "message": "model catalog network unavailable"},
                }
            )
            continue
        if mode == "model_require_params" and (
            "limit" not in params or "includeHidden" not in params
        ):
            send({"id": request_id, "error": {"message": "missing model/list params"}})
            continue
        if mode == "model_malformed":
            send({"id": request_id, "result": {"data": "not-an-array"}})
            continue
        if mode == "model_empty":
            send({"id": request_id, "result": {"data": [], "nextCursor": None}})
            continue
        if mode == "model_bad_effort":
            entry = catalog_entry("gpt-invalid")
            entry["defaultReasoningEffort"] = "impossible"
            send(
                {
                    "id": request_id,
                    "result": {"data": [entry], "nextCursor": None},
                }
            )
            continue
        if mode in {"model_service_tier_objects", "model_bad_service_tier"}:
            entry = catalog_entry("gpt-tiered", default=True)
            if mode == "model_service_tier_objects":
                entry["serviceTiers"] = [
                    {
                        "id": "priority",
                        "name": "Priority",
                        "description": "Fixture priority service",
                    },
                    {"id": "flex", "name": "Flex", "description": "Fixture flex"},
                ]
            else:
                entry["serviceTiers"] = [
                    {"id": "priority", "name": ["not", "a", "string"]}
                ]
            send(
                {
                    "id": request_id,
                    "result": {"data": [entry], "nextCursor": None},
                }
            )
            continue
        if mode == "model_retired":
            retired = catalog_entry("gpt-old", default=True)
            retired["deprecated"] = True
            retired["replacementModel"] = "gpt-new"
            replacement = catalog_entry("gpt-new")
            unavailable = catalog_entry("gpt-account-blocked")
            unavailable["accountAvailable"] = False
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": [retired, replacement, unavailable],
                        "nextCursor": None,
                    },
                }
            )
            continue
        if mode == "model_cycle":
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": [catalog_entry("gpt-cycle")],
                        "nextCursor": "same-cursor",
                    },
                }
            )
            continue
        if mode == "model_paginated":
            cursor = params.get("cursor")
            if cursor is None:
                data = [
                    catalog_entry("gpt-default", default=True),
                    catalog_entry("gpt-hidden", hidden=True),
                ]
                next_cursor = "page-two"
            elif cursor == "page-two":
                data = [catalog_entry("gpt-fast")]
                next_cursor = None
            else:
                send({"id": request_id, "error": {"message": "unknown cursor"}})
                continue
            send(
                {
                    "id": request_id,
                    "result": {"data": data, "nextCursor": next_cursor},
                }
            )
            continue
        send(
            {
                "id": request_id,
                "result": {
                    "data": [catalog_entry("gpt-default", default=True)],
                    "nextCursor": None,
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
        if mode == "hang_turn":
            continue
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

if mode in {"hang_close", "hang_turn"}:
    # Closing stdin normally stops the fixture. This mode models a wedged
    # app-server so the client must enforce its cancellation bound.
    while True:
        time.sleep(60)
