#!/usr/bin/env bash
# shellcheck shell=bash
# UAT-001: new disposable Ubuntu SSH user, published one-line install, and
# interactive ChatGPT onboarding. Raw terminal/auth output is never persisted.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

UAT_ID=UAT-001
HOST=${JARN_UAT_SSH_HOST:-}
INSTALLER_URL=${JARN_UAT_INSTALLER_URL:-$DEFAULT_INSTALLER_URL}
OUTPUT=""
EXECUTE=0

usage() {
  cat <<'EOF'
UAT-001 — New Ubuntu SSH user

Usage:
  scripts/uat/uat-001-ubuntu-ssh.sh [--host USER@HOST] [--output FILE]
                                            [--installer-url HTTPS_URL] [--execute]

The default is a local dry-run: it prints the plan and does not contact SSH.
--execute requires a disposable, fresh Ubuntu account and an exact typed
confirmation. The live SSH terminal is not captured, so device codes, URLs,
account details, and prompt contents cannot enter the evidence artifact.

Environment equivalents:
  JARN_UAT_SSH_HOST, JARN_UAT_INSTALLER_URL, JARN_UAT_RESULTS_DIR
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --host requires USER@HOST" >&2; exit 2; }
      HOST=$2
      shift 2
      ;;
    --installer-url)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --installer-url requires a URL" >&2; exit 2; }
      INSTALLER_URL=$2
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --output requires a file" >&2; exit 2; }
      OUTPUT=$2
      shift 2
      ;;
    --execute) EXECUTE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf '%s\n' "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

uat_validate_https_url "$INSTALLER_URL"
DISPLAY_COMMAND=$(uat_canonical_install_command "$INSTALLER_URL")

if [[ "$EXECUTE" -ne 1 ]]; then
  printf '%s\n' "DRY RUN — no SSH connection or target write will occur."
  printf '%s\n' "Target: ${HOST:-[required with --execute]}"
  printf '%s\n' "Prerequisite: disposable fresh Ubuntu SSH user with no J.A.R.N. config."
  printf '%s\n' "Interactive step: watch the uncaptured terminal, complete ChatGPT login, then send one prompt."
  printf '%s\n' "Published install command: $DISPLAY_COMMAND"
  uat_write_not_run_if_requested \
    "$OUTPUT" "$UAT_ID" "$DISPLAY_COMMAND" \
    "Pass --execute and confirm a disposable fresh SSH account to collect evidence."
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

write_evidence() {
  local status=$1
  local result=$2
  local limitation=${3:-}
  local args=(
    --output "$OUTPUT"
    --uat-id "$UAT_ID"
    --status "$status"
    --started-at "$STARTED_AT"
    --ended-at "$(uat_now)"
    --duration-seconds "$(uat_duration "$START_EPOCH")"
    --command "$DISPLAY_COMMAND"
    --result "$result"
    --implementation "install.sh"
    --automated-test "tests/test_installer.py"
    --platform-os "$PLATFORM_OS"
    --platform-version "$PLATFORM_VERSION"
    --platform-arch "$PLATFORM_ARCH"
    --platform-libc "$PLATFORM_LIBC"
    --redact-host "$HOST"
  )
  [[ -z "$limitation" ]] || args+=(--limitation "$limitation")
  uat_write_result "${args[@]}"
}

if ! PLATFORM_PROBE=$(uat_platform_probe "$HOST"); then
  write_evidence blocked "SSH platform probe failed before mutation." \
    "Establish non-interactive SSH access, then rerun the exact harness command."
  exit 2
fi
PLATFORM_OS=$(uat_probe_value "$PLATFORM_PROBE" os)
PLATFORM_VERSION=$(uat_probe_value "$PLATFORM_PROBE" version)
PLATFORM_ARCH=$(uat_probe_value "$PLATFORM_PROBE" arch)
PLATFORM_LIBC=$(uat_probe_value "$PLATFORM_PROBE" libc)

case "$PLATFORM_OS:$PLATFORM_VERSION" in
  ubuntu:20.04|ubuntu:22.04|ubuntu:24.04) ;;
  *)
    write_evidence blocked "Target is not a Tier-1 Ubuntu UAT platform." \
      "Use a disposable Ubuntu 20.04, 22.04, or 24.04 account."
    exit 2
    ;;
esac

INITIAL_PROBE=$(uat_ssh_readonly "$HOST" 'resolution=$(bash -lic '\''command -v jarn 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); config=absent; [ -e "$HOME/.jarn" ] && config=present; printf "resolution=%s\nconfig=%s\nnode=%s\npython=%s\nuv=%s\n" "${resolution:-absent}" "$config" "$(command -v node 2>/dev/null || printf absent)" "$(command -v python3 2>/dev/null || printf absent)" "$(command -v uv 2>/dev/null || printf absent)"') || {
  write_evidence blocked "Fresh-account probe failed before mutation." \
    "Fix SSH login-shell startup, then rerun the harness."
  exit 2
}
INITIAL_RESOLUTION=$(uat_probe_value "$INITIAL_PROBE" resolution)
INITIAL_CONFIG=$(uat_probe_value "$INITIAL_PROBE" config)
INITIAL_NODE=$(uat_probe_value "$INITIAL_PROBE" node)
INITIAL_PYTHON=$(uat_probe_value "$INITIAL_PROBE" python)
INITIAL_UV=$(uat_probe_value "$INITIAL_PROBE" uv)
if [[ "$INITIAL_RESOLUTION" != absent || "$INITIAL_CONFIG" != absent ]]; then
  write_evidence blocked "Fresh-account guard refused to install over existing J.A.R.N. state." \
    "Resolution=$INITIAL_RESOLUTION; config=$INITIAL_CONFIG. Provision a new disposable account."
  exit 2
fi

if ! uat_confirm_disposable "$HOST" "install J.A.R.N., dependencies, shell profile entries, and login/config state"; then
  write_evidence not_run "Operator declined the disposable-target confirmation." \
    "No installation command was sent."
  exit 2
fi

printf '%s\n' "The following SSH terminal is live and uncaptured. Complete onboarding, send one harmless prompt, then exit J.A.R.N."
REMOTE_COMMAND="jarn_installer_tmp=\$(mktemp \"\${TMPDIR:-/tmp}/jarn-install.XXXXXX\") || exit 1; trap 'rm -f \"\$jarn_installer_tmp\"' 0 HUP INT TERM; curl -fsSL '$INSTALLER_URL' -o \"\$jarn_installer_tmp\" || exit \$?; sh \"\$jarn_installer_tmp\"; rc=\$?; { [ \"\$rc\" -eq 0 ] || [ \"\$rc\" -eq 10 ]; } || exit \"\$rc\"; rm -f \"\$jarn_installer_tmp\"; trap - 0 HUP INT TERM; exec \"\$SHELL\" -lic 'command -v jarn && jarn --version && exec jarn'"
if ! ssh -tt -o ConnectTimeout=10 "$HOST" "$REMOTE_COMMAND"; then
  write_evidence failed "Published install/onboarding session returned non-zero." \
    "Raw terminal and authentication output was intentionally not persisted; rerun interactively to inspect it."
  exit 1
fi

POST_PROBE=$(uat_ssh_readonly "$HOST" 'resolved=$(bash -lic '\''command -v jarn 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); version=$(bash -lic '\''jarn --version 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); help=failed; bash -lic '\''jarn --help >/dev/null 2>&1'\'' 2>/dev/null && help=passed; auth=invalid; auth_payload=$(bash -lic '\''jarn codex status --json 2>/dev/null'\'' 2>/dev/null || true); printf "%s" "$auth_payload" | grep -Eq '\''"state"[[:space:]]*:[[:space:]]*"authenticated_chatgpt"'\'' && printf "%s" "$auth_payload" | grep -Eq '\''"ready"[[:space:]]*:[[:space:]]*true'\'' && auth=verified; printf "resolution=%s\nversion=%s\nhelp=%s\nauth_json=%s\n" "${resolved:-absent}" "${version:-unknown}" "$help" "$auth"') || true
POST_RESOLUTION=$(uat_probe_value "$POST_PROBE" resolution)
POST_VERSION=$(uat_probe_value "$POST_PROBE" version)
POST_HELP=$(uat_probe_value "$POST_PROBE" help)
POST_AUTH=$(uat_probe_value "$POST_PROBE" auth_json)

AUTOMATED_OK=1
[[ "$POST_RESOLUTION" == */.local/bin/jarn ]] || AUTOMATED_OK=0
[[ "$POST_VERSION" == jarn\ * ]] || AUTOMATED_OK=0
[[ "$POST_HELP" == passed && "$POST_AUTH" == verified ]] || AUTOMATED_OK=0

MANUAL_OK=1
DECISIONS=()
for checkpoint in \
  "Device URL/code stayed visible until login completed" \
  "J.A.R.N. verified the ChatGPT account and plan" \
  "A live-supported default model was selected automatically" \
  "The first prompt completed successfully" \
  "No manual PATH or config edit was needed" \
  "No external documentation lookup was needed" \
  "No step reported success before its verification completed"
do
  if uat_yes_no "$checkpoint"; then
    DECISIONS+=("passed: $checkpoint")
  else
    DECISIONS+=("failed: $checkpoint")
    MANUAL_OK=0
  fi
done

STATUS=passed
RESULT="Fresh-shell resolution, version/help, auth JSON, onboarding, live model, and first-prompt checkpoints passed."
if [[ "$AUTOMATED_OK" -ne 1 || "$MANUAL_OK" -ne 1 ]]; then
  STATUS=failed
  RESULT="One or more automated or operator-observed UAT-001 checkpoints failed."
fi

EVIDENCE_ARGS=(
  --output "$OUTPUT"
  --uat-id "$UAT_ID"
  --status "$STATUS"
  --started-at "$STARTED_AT"
  --ended-at "$(uat_now)"
  --duration-seconds "$(uat_duration "$START_EPOCH")"
  --command "$DISPLAY_COMMAND"
  --result "$RESULT"
  --implementation "install.sh"
  --automated-test "tests/test_installer.py"
  --decision "preflight runtimes: node=$INITIAL_NODE, python=$INITIAL_PYTHON, uv=$INITIAL_UV"
  --platform-os "$PLATFORM_OS"
  --platform-version "$PLATFORM_VERSION"
  --platform-arch "$PLATFORM_ARCH"
  --platform-libc "$PLATFORM_LIBC"
  --redact-host "$HOST"
)
for decision in "${DECISIONS[@]}"; do
  EVIDENCE_ARGS+=(--decision "$decision")
  [[ "$decision" == failed:* ]] && EVIDENCE_ARGS+=(--error "$decision")
done
if [[ " ${DECISIONS[*]} " == *"passed: No external documentation lookup was needed"* ]]; then
  EVIDENCE_ARGS+=(--documentation-lookup "none; operator confirmed")
else
  EVIDENCE_ARGS+=(--documentation-lookup "external lookup was required; details were not collected")
fi
[[ "$AUTOMATED_OK" -eq 1 ]] || EVIDENCE_ARGS+=(--error "Fresh-shell probe: resolution=$POST_RESOLUTION, version=$POST_VERSION, help=$POST_HELP, auth_json=$POST_AUTH")
uat_write_result "${EVIDENCE_ARGS[@]}"
[[ "$STATUS" == passed ]]
