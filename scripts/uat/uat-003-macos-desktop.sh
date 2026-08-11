#!/usr/bin/env bash
# shellcheck shell=bash
# UAT-003: exercise browser/loopback ChatGPT login on a disposable macOS
# desktop account. The live auth terminal is deliberately never captured.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

UAT_ID=UAT-003
HOST=${JARN_UAT_SSH_HOST:-}
OUTPUT=""
EXECUTE=0

usage() {
  cat <<'EOF'
UAT-003 — macOS desktop browser/callback flow

Usage:
  scripts/uat/uat-003-macos-desktop.sh [--host USER@MAC] [--output FILE] [--execute]

Default behavior is a local dry-run and does not contact SSH. Execute mode
requires macOS 13+, an active GUI console session owned by the SSH user, a
healthy J.A.R.N. command, no existing config/live catalog cache, and a signed-out
ChatGPT state. The browser URL, callback payload, account data, prompts, and raw
terminal output are never written to evidence.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --host requires USER@MAC" >&2; exit 2; }
      HOST=$2; shift 2 ;;
    --output)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --output requires a file" >&2; exit 2; }
      OUTPUT=$2; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf '%s\n' "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

DISPLAY_COMMAND='ssh -tt [HOST] '\''exec "$SHELL" -lic "jarn auth login --browser && jarn setup && exec jarn"'\'''

if [[ "$EXECUTE" -ne 1 ]]; then
  printf '%s\n' "DRY RUN — no SSH connection or target write will occur."
  printf '%s\n' "Target: ${HOST:-[required with --execute]}"
  printf '%s\n' "Required fixture: disposable macOS 13+ GUI account, signed out, with J.A.R.N. already installed."
  printf '%s\n' "Checks: browser opened, fallback URL visible, loopback callback, verified account/live catalog, first prompt."
  uat_write_not_run_if_requested \
    "$OUTPUT" "$UAT_ID" "$DISPLAY_COMMAND" \
    "Pass --execute only while the same disposable user owns the active Mac desktop session."
  exit 0
fi

uat_validate_host "$HOST"
[[ -n "$OUTPUT" ]] || OUTPUT=$(uat_default_output "$UAT_ID")
STARTED_AT=$(uat_now)
START_EPOCH=$(uat_epoch)
PLATFORM_OS=unknown
PLATFORM_VERSION=unknown
PLATFORM_ARCH=unknown
PLATFORM_LIBC=unknown
REMOTE_HOME=""

write_evidence() {
  local status=$1
  local result=$2
  local limitation=${3:-}
  local args=(
    --output "$OUTPUT" --uat-id "$UAT_ID" --status "$status"
    --started-at "$STARTED_AT" --ended-at "$(uat_now)"
    --duration-seconds "$(uat_duration "$START_EPOCH")"
    --command "$DISPLAY_COMMAND" --result "$result"
    --implementation "src/jarn/auth/terminal.py"
    --implementation "src/jarn/onboarding/chatgpt.py"
    --implementation "src/jarn/catalog/"
    --automated-test "tests/test_auth_service.py"
    --automated-test "tests/test_model_catalog.py"
    --platform-os "$PLATFORM_OS" --platform-version "$PLATFORM_VERSION"
    --platform-arch "$PLATFORM_ARCH" --platform-libc "$PLATFORM_LIBC"
    --redact-host "$HOST"
  )
  [[ -z "$REMOTE_HOME" ]] || args+=(--redact-home "$REMOTE_HOME")
  [[ -z "$limitation" ]] || args+=(--limitation "$limitation")
  uat_write_result "${args[@]}"
}

if ! PLATFORM_PROBE=$(uat_platform_probe "$HOST"); then
  write_evidence blocked "SSH platform probe failed before mutation." \
    "Establish non-interactive SSH access to the disposable Mac and rerun."
  exit 2
fi
PLATFORM_OS=$(uat_probe_value "$PLATFORM_PROBE" os)
PLATFORM_VERSION=$(uat_probe_value "$PLATFORM_PROBE" version)
PLATFORM_ARCH=$(uat_probe_value "$PLATFORM_PROBE" arch)
PLATFORM_LIBC=$(uat_probe_value "$PLATFORM_PROBE" libc)
MACOS_MAJOR=${PLATFORM_VERSION%%.*}
if [[ "$PLATFORM_OS" != macos || ! "$MACOS_MAJOR" =~ ^[0-9]+$ || "$MACOS_MAJOR" -lt 13 ]]; then
  write_evidence blocked "Target is not a supported macOS 13+ desktop." \
    "Use a disposable macOS 13+ account with an active GUI session."
  exit 2
fi

FIXTURE=$(uat_ssh_readonly "$HOST" 'home=$HOME; login_user=$(id -un); console_user=$(stat -f %Su /dev/console 2>/dev/null || printf unknown); resolution=$($SHELL -lic '\''command -v jarn 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); version=$($SHELL -lic '\''jarn --version 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); config=absent; [ -f "$HOME/.jarn/config.yaml" ] && config=present; catalog=absent; for root in "$HOME/Library/Caches/jarn/model-catalog" "$HOME/.cache/jarn/model-catalog"; do [ -f "$root/codex_subscription.json" ] && catalog=present; done; auth=not_ready; auth_payload=$($SHELL -lic '\''jarn auth status --json 2>/dev/null'\'' 2>/dev/null || true); printf "%s" "$auth_payload" | grep -Eq '\''"ready"[[:space:]]*:[[:space:]]*true'\'' && auth=ready; printf "home=%s\nlogin_user=%s\nconsole_user=%s\nresolution=%s\nversion=%s\nconfig=%s\ncatalog=%s\nauth=%s\n" "$home" "$login_user" "$console_user" "${resolution:-absent}" "${version:-unknown}" "$config" "$catalog" "$auth"') || {
  write_evidence blocked "macOS desktop fixture probe failed before mutation." \
    "Verify SSH, the login shell, and the active GUI console session."
  exit 2
}
REMOTE_HOME=$(uat_probe_value "$FIXTURE" home)
LOGIN_USER=$(uat_probe_value "$FIXTURE" login_user)
CONSOLE_USER=$(uat_probe_value "$FIXTURE" console_user)
BEFORE_RESOLUTION=$(uat_probe_value "$FIXTURE" resolution)
BEFORE_VERSION=$(uat_probe_value "$FIXTURE" version)
BEFORE_CONFIG=$(uat_probe_value "$FIXTURE" config)
BEFORE_CATALOG=$(uat_probe_value "$FIXTURE" catalog)
BEFORE_AUTH=$(uat_probe_value "$FIXTURE" auth)

if [[ "$LOGIN_USER" != "$CONSOLE_USER" || "$CONSOLE_USER" == loginwindow || "$CONSOLE_USER" == unknown ]]; then
  write_evidence blocked "SSH user does not own an active macOS GUI console session." \
    "Log into the desktop as the same disposable account; login=$LOGIN_USER, console=$CONSOLE_USER."
  exit 2
fi
if [[ "$BEFORE_VERSION" != jarn\ * || "$BEFORE_RESOLUTION" == absent ]]; then
  write_evidence blocked "J.A.R.N. is not healthy in the Mac user's login shell." \
    "Install/activate J.A.R.N. first, then rerun this desktop-specific UAT."
  exit 2
fi
if [[ "$BEFORE_CONFIG" != absent || "$BEFORE_CATALOG" != absent || "$BEFORE_AUTH" != not_ready ]]; then
  write_evidence blocked "Fresh browser-login fixture guard refused existing account/config/catalog state." \
    "Use a new disposable macOS account; config=$BEFORE_CONFIG, catalog=$BEFORE_CATALOG, auth=$BEFORE_AUTH."
  exit 2
fi

if ! uat_confirm_disposable "$HOST" "open a browser, change Codex-managed login state, write J.A.R.N. config/catalog cache, and run one prompt"; then
  write_evidence not_run "Operator declined the disposable-target confirmation." \
    "No login/setup command was sent."
  exit 2
fi

printf '%s\n' "The following terminal is live and uncaptured. Observe the fallback URL, finish the browser callback, choose ChatGPT in setup, send one harmless prompt, then exit."
REMOTE_COMMAND='exec "$SHELL" -lic '\''jarn auth login --browser && jarn setup && exec jarn'\'''
if ! ssh -tt -o ConnectTimeout=10 "$HOST" "$REMOTE_COMMAND"; then
  write_evidence failed "Browser login, setup, or first interactive session returned non-zero." \
    "Raw URL/callback/account/prompt output was intentionally not persisted; inspect it only in a rerun."
  exit 1
fi

POST=$(uat_ssh_readonly "$HOST" 'auth=invalid; auth_payload=$($SHELL -lic '\''jarn auth status --json 2>/dev/null'\'' 2>/dev/null || true); printf "%s" "$auth_payload" | grep -Eq '\''"state"[[:space:]]*:[[:space:]]*"authenticated_chatgpt"'\'' && printf "%s" "$auth_payload" | grep -Eq '\''"ready"[[:space:]]*:[[:space:]]*true'\'' && auth=verified; doctor=invalid; profile=invalid; route=invalid; doctor_payload=$($SHELL -lic '\''jarn doctor --json 2>/dev/null'\'' 2>/dev/null || true); printf "%s" "$doctor_payload" | grep -Eq '\''^[[:space:]]*\{'\'' && doctor=valid; printf "%s" "$doctor_payload" | grep -Eq '\''"default_profile"[[:space:]]*:[[:space:]]*"codex_subscription"'\'' && profile=verified; printf "%s" "$doctor_payload" | grep -Eq '\''"main_model"[[:space:]]*:[[:space:]]*"codex_subscription/'\'' && route=verified; catalog=invalid; catalog_path=absent; for root in "$HOME/Library/Caches/jarn/model-catalog" "$HOME/.cache/jarn/model-catalog"; do candidate="$root/codex_subscription.json"; if [ -f "$candidate" ]; then catalog_path=present; grep -Eq '\''"source"[[:space:]]*:[[:space:]]*"codex_live"'\'' "$candidate" && grep -Eq '\''"availability_verified"[[:space:]]*:[[:space:]]*true'\'' "$candidate" && grep -q '\''"model_id"'\'' "$candidate" && catalog=live_verified; fi; done; printf "auth=%s\ndoctor=%s\nprofile=%s\nroute=%s\ncatalog_path=%s\ncatalog=%s\n" "$auth" "$doctor" "$profile" "$route" "$catalog_path" "$catalog"') || true
POST_AUTH=$(uat_probe_value "$POST" auth)
POST_DOCTOR=$(uat_probe_value "$POST" doctor)
POST_PROFILE=$(uat_probe_value "$POST" profile)
POST_ROUTE=$(uat_probe_value "$POST" route)
POST_CATALOG=$(uat_probe_value "$POST" catalog)

ERRORS=()
ERROR_COUNT=0
add_error() { ERRORS[$ERROR_COUNT]=$1; ERROR_COUNT=$((ERROR_COUNT + 1)); }
[[ "$POST_AUTH" == verified ]] || add_error "post-login status was not authenticated_chatgpt/ready"
[[ "$POST_DOCTOR" == valid && "$POST_PROFILE" == verified ]] || add_error "doctor did not verify the ChatGPT configuration"
[[ "$POST_ROUTE" == verified ]] || add_error "selected route is not a ChatGPT catalog model"
[[ "$POST_CATALOG" == live_verified ]] || add_error "no non-empty, availability-verified codex_live catalog cache was produced"

MANUAL_OK=1
DECISIONS=()
for checkpoint in \
  "The default browser opened automatically on the Mac desktop" \
  "A fallback login URL remained visible in the terminal" \
  "The loopback callback returned control to the CLI" \
  "J.A.R.N. visibly verified the ChatGPT account and plan" \
  "Setup visibly used a live account catalog and selected its supported default" \
  "The first prompt completed successfully immediately after setup" \
  "No manual PATH or config edit was needed" \
  "No external documentation lookup was needed" \
  "No step reported success before verification completed"
do
  if uat_yes_no "$checkpoint"; then
    DECISIONS+=("passed: $checkpoint")
  else
    DECISIONS+=("failed: $checkpoint")
    MANUAL_OK=0
  fi
done

[[ "$ERROR_COUNT" -eq 0 && "$MANUAL_OK" -eq 1 ]] && STATUS=passed || STATUS=failed
RESULT="macOS browser/callback, verified account/live catalog, and immediate first-prompt checkpoints passed."
[[ "$STATUS" == passed ]] || RESULT="One or more macOS browser, callback, account, catalog, or usability checkpoints failed."
ARGS=(
  --output "$OUTPUT" --uat-id "$UAT_ID" --status "$STATUS"
  --started-at "$STARTED_AT" --ended-at "$(uat_now)"
  --duration-seconds "$(uat_duration "$START_EPOCH")"
  --command "$DISPLAY_COMMAND" --result "$RESULT"
  --implementation "src/jarn/auth/terminal.py"
  --implementation "src/jarn/onboarding/chatgpt.py"
  --implementation "src/jarn/catalog/"
  --automated-test "tests/test_auth_service.py"
  --automated-test "tests/test_model_catalog.py"
  --decision "post checks: auth=$POST_AUTH, doctor=$POST_DOCTOR, profile=$POST_PROFILE, route=$POST_ROUTE, catalog=$POST_CATALOG"
  --limitation "Browser launch, fallback URL, callback, and first prompt are operator-observed; raw terminal output is prohibited evidence."
  --platform-os "$PLATFORM_OS" --platform-version "$PLATFORM_VERSION"
  --platform-arch "$PLATFORM_ARCH" --platform-libc "$PLATFORM_LIBC"
  --redact-host "$HOST" --redact-home "$REMOTE_HOME"
)
for decision in "${DECISIONS[@]}"; do
  ARGS+=(--decision "$decision")
  [[ "$decision" == failed:* ]] && ARGS+=(--error "$decision")
done
if [[ " ${DECISIONS[*]} " == *"passed: No external documentation lookup was needed"* ]]; then
  ARGS+=(--documentation-lookup "none; operator confirmed")
else
  ARGS+=(--documentation-lookup "external lookup was required; details were not collected")
fi
if [[ "$ERROR_COUNT" -gt 0 ]]; then
  for error in "${ERRORS[@]}"; do ARGS+=(--error "$error"); done
fi
uat_write_result "${ARGS[@]}"
[[ "$STATUS" == passed ]]
