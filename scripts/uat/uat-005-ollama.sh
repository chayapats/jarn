#!/usr/bin/env bash
# shellcheck shell=bash
# UAT-005: configure and exercise a loopback Ollama endpoint while cloud
# egress is deterministically sent to a closed local proxy.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

UAT_ID=UAT-005
HOST=${JARN_UAT_SSH_HOST:-}
ENDPOINT=${JARN_UAT_OLLAMA_ENDPOINT:-http://localhost:11434}
OUTPUT=""
EXECUTE=0

usage() {
  cat <<'EOF'
UAT-005 — Local Ollama user

Usage:
  scripts/uat/uat-005-ollama.sh [--host USER@HOST] [--endpoint LOOPBACK_URL]
                                  [--output FILE] [--execute]

Default behavior is a local dry-run and does not contact SSH. The disposable
target must have a healthy J.A.R.N., GNU `timeout`, no existing config, and an
Ollama-compatible endpoint on localhost/127.0.0.1/[::1] with at least one loaded
model. Execute mode blocks cloud HTTP(S) through closed loopback port 9 while
leaving the local endpoint in NO_PROXY.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --host requires USER@HOST" >&2; exit 2; }
      HOST=$2; shift 2 ;;
    --endpoint)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --endpoint requires a loopback URL" >&2; exit 2; }
      ENDPOINT=$2; shift 2 ;;
    --output)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --output requires a file" >&2; exit 2; }
      OUTPUT=$2; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf '%s\n' "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

uat_validate_local_http_url "$ENDPOINT"
DISPLAY_COMMAND='ssh -tt [HOST] '\''HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 NO_PROXY=localhost,127.0.0.1,::1 exec "$SHELL" -lic "jarn setup"'\'''

if [[ "$EXECUTE" -ne 1 ]]; then
  printf '%s\n' "DRY RUN — no SSH connection, endpoint request, or target write will occur."
  printf '%s\n' "Target: ${HOST:-[required with --execute]}"
  printf '%s\n' "Endpoint: $ENDPOINT (validated loopback only)"
  printf '%s\n' "Required fixture: disposable account; J.A.R.N. installed; config absent; Ollama has at least one model."
  printf '%s\n' "Checks: endpoint/catalog discovery, no key prompt/config, cloud-offline local turn, missing-model remediation."
  uat_write_not_run_if_requested \
    "$OUTPUT" "$UAT_ID" "$DISPLAY_COMMAND" \
    "Pass --execute only for a disposable target with a loopback Ollama fixture."
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
    --implementation "src/jarn/catalog/"
    --implementation "src/jarn/providers/"
    --automated-test "tests/test_model_catalog.py"
    --automated-test "tests/test_local_backend.py"
    --platform-os "$PLATFORM_OS" --platform-version "$PLATFORM_VERSION"
    --platform-arch "$PLATFORM_ARCH" --platform-libc "$PLATFORM_LIBC"
    --redact-host "$HOST"
  )
  [[ -z "$REMOTE_HOME" ]] || args+=(--redact-home "$REMOTE_HOME")
  [[ -z "$limitation" ]] || args+=(--limitation "$limitation")
  uat_write_result "${args[@]}"
}

if ! PLATFORM_PROBE=$(uat_platform_probe "$HOST"); then
  write_evidence blocked "SSH platform probe failed before endpoint access." \
    "Establish non-interactive SSH access and rerun."
  exit 2
fi
PLATFORM_OS=$(uat_probe_value "$PLATFORM_PROBE" os)
PLATFORM_VERSION=$(uat_probe_value "$PLATFORM_PROBE" version)
PLATFORM_ARCH=$(uat_probe_value "$PLATFORM_PROBE" arch)
PLATFORM_LIBC=$(uat_probe_value "$PLATFORM_PROBE" libc)

FIXTURE=$(uat_ssh_readonly "$HOST" "home=\$HOME; resolution=\$(\$SHELL -lic 'command -v jarn 2>/dev/null || true' 2>/dev/null | tail -n 1); version=\$(\$SHELL -lic 'jarn --version 2>/dev/null || true' 2>/dev/null | tail -n 1); config=absent; [ -f \"\$HOME/.jarn/config.yaml\" ] && config=present; timeout_cmd=absent; command -v timeout >/dev/null 2>&1 && timeout_cmd=present; endpoint=unreachable; catalog=empty; tags=\$(NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 curl -fsS --connect-timeout 3 '$ENDPOINT/api/tags' 2>/dev/null || true); printf '%s' \"\$tags\" | grep -q '\"models\"' && endpoint=reachable; printf '%s' \"\$tags\" | grep -q '\"name\"' && catalog=nonempty; printf 'home=%s\nresolution=%s\nversion=%s\nconfig=%s\ntimeout=%s\nendpoint=%s\ncatalog=%s\n' \"\$home\" \"\${resolution:-absent}\" \"\${version:-unknown}\" \"\$config\" \"\$timeout_cmd\" \"\$endpoint\" \"\$catalog\"") || {
  write_evidence blocked "Ollama fixture probe failed before mutation." \
    "Verify curl, the login shell, and the loopback endpoint."
  exit 2
}
REMOTE_HOME=$(uat_probe_value "$FIXTURE" home)
BEFORE_RESOLUTION=$(uat_probe_value "$FIXTURE" resolution)
BEFORE_VERSION=$(uat_probe_value "$FIXTURE" version)
BEFORE_CONFIG=$(uat_probe_value "$FIXTURE" config)
TIMEOUT_CMD=$(uat_probe_value "$FIXTURE" timeout)
ENDPOINT_STATE=$(uat_probe_value "$FIXTURE" endpoint)
CATALOG_STATE=$(uat_probe_value "$FIXTURE" catalog)
if [[ "$BEFORE_VERSION" != jarn\ * || "$BEFORE_RESOLUTION" == absent ]]; then
  write_evidence blocked "J.A.R.N. is not healthy in the target login shell." \
    "Install/activate J.A.R.N. before running the local-provider UAT."
  exit 2
fi
if [[ "$BEFORE_CONFIG" != absent || "$TIMEOUT_CMD" != present || "$ENDPOINT_STATE" != reachable || "$CATALOG_STATE" != nonempty ]]; then
  write_evidence blocked "Fresh Ollama fixture contract is not satisfied." \
    "Expected config=absent, timeout=present, endpoint=reachable, catalog=nonempty; observed config=$BEFORE_CONFIG, timeout=$TIMEOUT_CMD, endpoint=$ENDPOINT_STATE, catalog=$CATALOG_STATE."
  exit 2
fi

if ! uat_confirm_disposable "$HOST" "write a local-provider config, run one harmless local turn, and request a deliberately missing local model"; then
  write_evidence not_run "Operator declined the disposable-target confirmation." \
    "No setup or model command was sent."
  exit 2
fi

printf '%s\n' "The following setup terminal is live and uncaptured. Choose Local -> Ollama, enter $ENDPOINT if asked, select a reported model, validate it, and finish setup."
REMOTE_COMMAND='HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9 NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 exec "$SHELL" -lic '\''jarn setup'\'''
if ! ssh -tt -o ConnectTimeout=10 "$HOST" "$REMOTE_COMMAND"; then
  write_evidence failed "Ollama setup under blocked cloud egress returned non-zero." \
    "Raw setup/model output was intentionally not persisted; the loopback endpoint was not modified by the harness."
  exit 1
fi

POST=$(uat_ssh_readonly "$HOST" "config=\"\$HOME/.jarn/config.yaml\"; config_present=no; profile=invalid; route=invalid; endpoint_config=invalid; keyless=invalid; doctor=invalid; endpoint=unreachable; catalog=empty; [ -f \"\$config\" ] && config_present=yes; if [ -f \"\$config\" ]; then section=\$(awk '/^  ollama:/{inside=1; next} inside && /^  [A-Za-z0-9_]+:/{exit} inside{print}' \"\$config\"); printf '%s' \"\$section\" | grep -Fq 'base_url: $ENDPOINT' && endpoint_config=verified; printf '%s' \"\$section\" | grep -q 'api_key:' || keyless=verified; fi; doctor_payload=\$(\$SHELL -lic 'jarn doctor --json 2>/dev/null' 2>/dev/null || true); printf '%s' \"\$doctor_payload\" | grep -Eq '^[[:space:]]*\\{' && doctor=valid; printf '%s' \"\$doctor_payload\" | grep -Eq '\"default_profile\"[[:space:]]*:[[:space:]]*\"ollama\"' && profile=verified; printf '%s' \"\$doctor_payload\" | grep -Eq '\"main_model\"[[:space:]]*:[[:space:]]*\"ollama/' && route=verified; tags=\$(NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 curl -fsS --connect-timeout 3 '$ENDPOINT/api/tags' 2>/dev/null || true); printf '%s' \"\$tags\" | grep -q '\"models\"' && endpoint=reachable; printf '%s' \"\$tags\" | grep -q '\"name\"' && catalog=nonempty; set +e; local_output=\$(HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9 NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 timeout 120 \$SHELL -lic 'jarn -p \"Reply only with UAT_OK\" --output-format json' 2>&1); local_rc=\$?; missing_output=\$(HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9 NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 timeout 45 \$SHELL -lic 'jarn -p \"Reply only with UAT_OK\" --model ollama/__jarn_uat_missing__ --output-format json' 2>&1); missing_rc=\$?; set -e; local_turn=failed; [ \"\$local_rc\" -eq 0 ] && printf '%s' \"\$local_output\" | grep -q '\"result\"' && local_turn=passed; missing_failure=invalid; [ \"\$missing_rc\" -ne 0 ] && missing_failure=nonzero; remediation=missing; printf '%s' \"\$missing_output\" | grep -Eqi '(ollama[[:space:]]+pull|pull(ing)? (the )?model|/model refresh|select an available model)' && remediation=actionable; success_text=no; printf '%s' \"\$missing_output\" | grep -Eq '(^|[^[:alpha:]])(Done|Ready)([^[:alpha:]]|$)' && success_text=yes; printf 'config_present=%s\nprofile=%s\nroute=%s\nendpoint_config=%s\nkeyless=%s\ndoctor=%s\nendpoint=%s\ncatalog=%s\nlocal_turn=%s\nlocal_rc=%s\nmissing_failure=%s\nmissing_rc=%s\nremediation=%s\nsuccess_text=%s\n' \"\$config_present\" \"\$profile\" \"\$route\" \"\$endpoint_config\" \"\$keyless\" \"\$doctor\" \"\$endpoint\" \"\$catalog\" \"\$local_turn\" \"\$local_rc\" \"\$missing_failure\" \"\$missing_rc\" \"\$remediation\" \"\$success_text\"") || true
POST_CONFIG=$(uat_probe_value "$POST" config_present)
POST_PROFILE=$(uat_probe_value "$POST" profile)
POST_ROUTE=$(uat_probe_value "$POST" route)
POST_ENDPOINT_CONFIG=$(uat_probe_value "$POST" endpoint_config)
POST_KEYLESS=$(uat_probe_value "$POST" keyless)
POST_DOCTOR=$(uat_probe_value "$POST" doctor)
POST_ENDPOINT=$(uat_probe_value "$POST" endpoint)
POST_CATALOG=$(uat_probe_value "$POST" catalog)
LOCAL_TURN=$(uat_probe_value "$POST" local_turn)
LOCAL_RC=$(uat_probe_value "$POST" local_rc)
MISSING_FAILURE=$(uat_probe_value "$POST" missing_failure)
MISSING_RC=$(uat_probe_value "$POST" missing_rc)
REMEDIATION=$(uat_probe_value "$POST" remediation)
SUCCESS_TEXT=$(uat_probe_value "$POST" success_text)

ERRORS=()
ERROR_COUNT=0
add_error() { ERRORS[$ERROR_COUNT]=$1; ERROR_COUNT=$((ERROR_COUNT + 1)); }
[[ "$POST_CONFIG" == yes && "$POST_PROFILE" == verified && "$POST_ROUTE" == verified ]] || add_error "config/doctor did not verify the Ollama profile and route"
[[ "$POST_ENDPOINT_CONFIG" == verified && "$POST_KEYLESS" == verified ]] || add_error "Ollama config did not preserve the loopback endpoint as keyless"
[[ "$POST_ENDPOINT" == reachable && "$POST_CATALOG" == nonempty ]] || add_error "post-setup endpoint/catalog probe failed"
[[ "$LOCAL_TURN" == passed ]] || add_error "local model turn failed under blocked cloud egress (exit=$LOCAL_RC)"
[[ "$MISSING_FAILURE" == nonzero && "$REMEDIATION" == actionable ]] || add_error "missing local model did not return non-zero with actionable pull/select remediation (exit=$MISSING_RC)"
[[ "$SUCCESS_TEXT" == no ]] || add_error "missing-model failure printed Done or Ready"

MANUAL_OK=1
DECISIONS=()
for checkpoint in \
  "Setup visibly discovered the local Ollama endpoint and its models" \
  "No API-key prompt appeared for Ollama" \
  "The selected model came from the local endpoint or was honestly labeled" \
  "Local validation completed while cloud egress was blocked" \
  "No manual config edit was needed" \
  "No external documentation lookup was needed" \
  "Unavailable-model output clearly explained how to pull or select a model"
do
  if uat_yes_no "$checkpoint"; then
    DECISIONS+=("passed: $checkpoint")
  else
    DECISIONS+=("failed: $checkpoint")
    MANUAL_OK=0
  fi
done

[[ "$ERROR_COUNT" -eq 0 && "$MANUAL_OK" -eq 1 ]] && STATUS=passed || STATUS=failed
RESULT="Ollama endpoint/catalog, keyless setup, cloud-offline local turn, and unavailable-model remediation passed."
[[ "$STATUS" == passed ]] || RESULT="One or more Ollama discovery, keyless, offline-independence, or remediation checkpoints failed."
ARGS=(
  --output "$OUTPUT" --uat-id "$UAT_ID" --status "$STATUS"
  --started-at "$STARTED_AT" --ended-at "$(uat_now)"
  --duration-seconds "$(uat_duration "$START_EPOCH")"
  --command "$DISPLAY_COMMAND" --result "$RESULT"
  --implementation "src/jarn/onboarding/wizard.py"
  --implementation "src/jarn/catalog/"
  --implementation "src/jarn/providers/"
  --automated-test "tests/test_model_catalog.py"
  --automated-test "tests/test_local_backend.py"
  --decision "endpoint/catalog: $POST_ENDPOINT/$POST_CATALOG; keyless=$POST_KEYLESS; route=$POST_ROUTE"
  --decision "cloud-blocked local turn: $LOCAL_TURN (exit=$LOCAL_RC)"
  --decision "missing model: $MISSING_FAILURE (exit=$MISSING_RC), remediation=$REMEDIATION, success_text=$SUCCESS_TEXT"
  --limitation "Provider/model discovery and absence of an API-key prompt include operator-observed checkpoints; raw setup/model output is not persisted."
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
