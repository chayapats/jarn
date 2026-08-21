#!/usr/bin/env bash
# shellcheck shell=bash
# UAT-004: configure OpenCode Go from a pre-provisioned environment reference.
# The key is never accepted as an argument and the live setup terminal is not captured.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

UAT_ID=UAT-004
HOST=${JARN_UAT_SSH_HOST:-}
OUTPUT=""
EXECUTE=0

usage() {
  cat <<'EOF'
UAT-004 — OpenCode Go API-key user

Usage:
  scripts/uat/uat-004-opencode.sh [--host USER@HOST] [--output FILE] [--execute]

Default behavior is a local dry-run and does not contact SSH. The disposable
target must already have a healthy J.A.R.N. command, no existing config, and a
non-empty OPENCODE_API_KEY available in its login shell. Never pass the key to
this harness. Execute mode streams setup directly to the terminal without
capturing it, then checks only reference/source/leak booleans.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --host requires USER@HOST" >&2; exit 2; }
      HOST=$2; shift 2 ;;
    --output)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --output requires a file" >&2; exit 2; }
      OUTPUT=$2; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf '%s\n' "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

DISPLAY_COMMAND='ssh -tt [HOST] '\''exec "$SHELL" -lic "jarn setup"'\'''

if [[ "$EXECUTE" -ne 1 ]]; then
  printf '%s\n' "DRY RUN — no SSH connection, key access, or target write will occur."
  printf '%s\n' "Target: ${HOST:-[required with --execute]}"
  printf '%s\n' "Required fixture: disposable account; J.A.R.N. installed; config absent; OPENCODE_API_KEY pre-provisioned in login shell."
  printf '%s\n' "Checks: no key echo/plaintext config/log, environment reference, honest model provenance, billable-validation disclosure."
  uat_write_not_run_if_requested \
    "$OUTPUT" "$UAT_ID" "$DISPLAY_COMMAND" \
    "Pass --execute only after securely provisioning OPENCODE_API_KEY on a disposable target."
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
    --implementation "src/jarn/onboarding/wizard.py"
    --implementation "src/jarn/config/secrets.py"
    --implementation "src/jarn/catalog/"
    --automated-test "tests/test_secrets.py"
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
  write_evidence blocked "SSH platform probe failed before reading key presence." \
    "Establish non-interactive SSH access and rerun; no key value was collected."
  exit 2
fi
PLATFORM_OS=$(uat_probe_value "$PLATFORM_PROBE" os)
PLATFORM_VERSION=$(uat_probe_value "$PLATFORM_PROBE" version)
PLATFORM_ARCH=$(uat_probe_value "$PLATFORM_PROBE" arch)
PLATFORM_LIBC=$(uat_probe_value "$PLATFORM_PROBE" libc)

FIXTURE=$(uat_ssh_readonly "$HOST" 'home=$HOME; resolution=$($SHELL -lic '\''command -v jarn 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); version=$($SHELL -lic '\''jarn --version 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); config=absent; [ -f "$HOME/.jarn/config.yaml" ] && config=present; key=$($SHELL -lic '\''if [ -n "${OPENCODE_API_KEY:-}" ] && [ "${#OPENCODE_API_KEY}" -ge 8 ]; then printf present; else printf missing; fi'\'' 2>/dev/null); printf "home=%s\nresolution=%s\nversion=%s\nconfig=%s\nkey=%s\n" "$home" "${resolution:-absent}" "${version:-unknown}" "$config" "$key"') || {
  write_evidence blocked "OpenCode fixture probe failed before mutation." \
    "Verify the login shell and pre-provisioned environment reference."
  exit 2
}
REMOTE_HOME=$(uat_probe_value "$FIXTURE" home)
BEFORE_RESOLUTION=$(uat_probe_value "$FIXTURE" resolution)
BEFORE_VERSION=$(uat_probe_value "$FIXTURE" version)
BEFORE_CONFIG=$(uat_probe_value "$FIXTURE" config)
KEY_STATE=$(uat_probe_value "$FIXTURE" key)
if [[ "$BEFORE_VERSION" != jarn\ * || "$BEFORE_RESOLUTION" == absent ]]; then
  write_evidence blocked "J.A.R.N. is not healthy in the target login shell." \
    "Install/activate J.A.R.N. first; do not pass the API key to this harness."
  exit 2
fi
if [[ "$BEFORE_CONFIG" != absent || "$KEY_STATE" != present ]]; then
  write_evidence blocked "Fresh OpenCode fixture contract is not satisfied." \
    "Expected config=absent and OPENCODE_API_KEY=present; observed config=$BEFORE_CONFIG, key=$KEY_STATE."
  exit 2
fi

if ! uat_confirm_disposable "$HOST" "read only the presence of the pre-provisioned OpenCode key, write config by reference, and optionally perform a disclosed billable validation"; then
  write_evidence not_run "Operator declined the disposable-target confirmation." \
    "No setup command was sent and no key value was collected."
  exit 2
fi

printf '%s\n' "The following setup terminal is live and uncaptured. Choose OpenCode, reuse the detected environment key, observe model provenance and validation disclosure, then finish setup."
REMOTE_COMMAND='exec "$SHELL" -lic '\''jarn setup'\'''
if ! ssh -tt -o ConnectTimeout=10 "$HOST" "$REMOTE_COMMAND"; then
  write_evidence failed "OpenCode setup returned non-zero." \
    "Raw terminal/key/validation output was intentionally not persisted; rerun interactively to inspect it."
  exit 1
fi

POST_FILE=$(mktemp "${TMPDIR:-/tmp}/jarn-uat004-post.XXXXXX") || {
  write_evidence failed "Controller could not create a secure temporary result file." \
    "No secret or remote output was collected."
  exit 1
}
chmod 600 "$POST_FILE"
trap 'rm -f "$POST_FILE"' 0 HUP INT TERM
POST_RC=0
uat_ssh_readonly "$HOST" 'exec "$SHELL" -lic '\''bash -s'\''' > "$POST_FILE" <<'REMOTE_SCRIPT' || POST_RC=$?
set -eu
secret=${OPENCODE_API_KEY:-}
config="$HOME/.jarn/config.yaml"
config_present=no
config_mode=unknown
config_ref=invalid
config_endpoint=invalid
config_leak=unknown
logs_leak=no
doctor=invalid
profile=invalid
route=invalid

if [ -f "$config" ]; then
  config_present=yes
  config_mode=$(stat -f %Lp "$config" 2>/dev/null || stat -c %a "$config" 2>/dev/null || printf unknown)
  section=$(awk '/^  opencode:/{inside=1; next} inside && /^  [A-Za-z0-9_]+:/{exit} inside{print}' "$config")
  printf '%s' "$section" | grep -Fq 'api_key: ${OPENCODE_API_KEY}' && config_ref=environment
  printf '%s' "$section" | grep -Fq 'opencode.ai/zen/go' && config_endpoint=go
  config_leak=no
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in *"$secret"*) config_leak=yes; break ;; esac
  done < "$config"
fi

if [ -n "$secret" ]; then
  while IFS= read -r file; do
    while IFS= read -r line || [ -n "$line" ]; do
      if [[ "$line" == *"$secret"* ]]; then
        logs_leak=yes
        break 2
      fi
    done < "$file"
  done < <(
    for root in "$HOME/.jarn/logs" "$HOME/.jarn/sessions" "$HOME/.jarn/transcripts"; do
      [ -d "$root" ] || continue
      find "$root" -type f -print 2>/dev/null
    done
  )
fi

doctor_payload=$(jarn doctor --json 2>/dev/null || true)
printf '%s' "$doctor_payload" | grep -Eq '^[[:space:]]*\{' && doctor=valid
printf '%s' "$doctor_payload" | grep -Eq '"default_profile"[[:space:]]*:[[:space:]]*"opencode"' && profile=verified
printf '%s' "$doctor_payload" | grep -Eq '"main_model"[[:space:]]*:[[:space:]]*"opencode/' && route=verified
printf 'config_present=%s\nconfig_mode=%s\nconfig_ref=%s\nconfig_endpoint=%s\nconfig_leak=%s\nlogs_leak=%s\ndoctor=%s\nprofile=%s\nroute=%s\n' \
  "$config_present" "$config_mode" "$config_ref" "$config_endpoint" "$config_leak" \
  "$logs_leak" "$doctor" "$profile" "$route"
REMOTE_SCRIPT
POST=$(sed -n '1,40p' "$POST_FILE")
rm -f "$POST_FILE"
trap - 0 HUP INT TERM
if [[ "$POST_RC" -ne 0 ]]; then
  write_evidence failed "Post-setup no-leak/doctor probe returned non-zero." \
    "Only bounded marker output was staged locally; no key value or raw terminal output was collected."
  exit 1
fi
POST_CONFIG=$(uat_probe_value "$POST" config_present)
POST_MODE=$(uat_probe_value "$POST" config_mode)
POST_REF=$(uat_probe_value "$POST" config_ref)
POST_ENDPOINT=$(uat_probe_value "$POST" config_endpoint)
POST_CONFIG_LEAK=$(uat_probe_value "$POST" config_leak)
POST_LOGS_LEAK=$(uat_probe_value "$POST" logs_leak)
POST_DOCTOR=$(uat_probe_value "$POST" doctor)
POST_PROFILE=$(uat_probe_value "$POST" profile)
POST_ROUTE=$(uat_probe_value "$POST" route)

ERRORS=()
ERROR_COUNT=0
add_error() { ERRORS[$ERROR_COUNT]=$1; ERROR_COUNT=$((ERROR_COUNT + 1)); }
[[ "$POST_CONFIG" == yes && "$POST_REF" == environment ]] || add_error "config did not retain the OPENCODE_API_KEY environment reference"
[[ "$POST_ENDPOINT" == go ]] || add_error "config did not retain the OpenCode Go endpoint"
[[ "$POST_MODE" == 600 ]] || add_error "config permissions are $POST_MODE instead of 600"
[[ "$POST_CONFIG_LEAK" == no ]] || add_error "raw key appeared in config"
[[ "$POST_LOGS_LEAK" == no ]] || add_error "raw key appeared in logs/session transcripts"
[[ "$POST_DOCTOR" == valid && "$POST_PROFILE" == verified && "$POST_ROUTE" == verified ]] || add_error "doctor did not verify OpenCode profile/model construction"

MANUAL_OK=1
DECISIONS=()
for checkpoint in \
  "The API key never echoed in the setup terminal" \
  "The model choices were current or visibly labeled with honest cache/fallback provenance" \
  "Before validation, setup clearly disclosed that it could make a billable OpenCode request" \
  "No manual config edit was needed" \
  "No external documentation lookup was needed" \
  "Setup did not claim validation success unless a model response was verified"
do
  if uat_yes_no "$checkpoint"; then
    DECISIONS+=("passed: $checkpoint")
  else
    DECISIONS+=("failed: $checkpoint")
    MANUAL_OK=0
  fi
done

VALIDATION_DECISION="skipped by operator after disclosure"
if uat_yes_no "Did you choose to perform the optional OpenCode validation"; then
  VALIDATION_DECISION="performed"
  if uat_yes_no "Did the disclosed validation receive a verified model response"; then
    VALIDATION_DECISION="performed and verified"
  else
    VALIDATION_DECISION="performed but not verified"
    add_error "optional OpenCode validation was attempted but not verified"
    MANUAL_OK=0
  fi
fi

[[ "$ERROR_COUNT" -eq 0 && "$MANUAL_OK" -eq 1 ]] && STATUS=passed || STATUS=failed
RESULT="OpenCode key-reference, no-leak, model provenance, and billable-validation disclosure checkpoints passed."
[[ "$STATUS" == passed ]] || RESULT="One or more OpenCode key safety, model provenance, or validation-disclosure checkpoints failed."
ARGS=(
  --output "$OUTPUT" --uat-id "$UAT_ID" --status "$STATUS"
  --started-at "$STARTED_AT" --ended-at "$(uat_now)"
  --duration-seconds "$(uat_duration "$START_EPOCH")"
  --command "$DISPLAY_COMMAND" --result "$RESULT"
  --implementation "src/jarn/onboarding/wizard.py"
  --implementation "src/jarn/config/secrets.py"
  --implementation "src/jarn/catalog/"
  --automated-test "tests/test_secrets.py"
  --automated-test "tests/test_model_catalog.py"
  --decision "post checks: ref=$POST_REF, endpoint=$POST_ENDPOINT, config_leak=$POST_CONFIG_LEAK, logs_leak=$POST_LOGS_LEAK, doctor=$POST_DOCTOR, profile=$POST_PROFILE, route=$POST_ROUTE"
  --decision "optional validation: $VALIDATION_DECISION"
  --limitation "Key echo, model provenance, and billable disclosure are operator-observed; raw setup output is prohibited evidence."
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
