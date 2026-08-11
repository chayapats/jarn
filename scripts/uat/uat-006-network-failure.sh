#!/usr/bin/env bash
# shellcheck shell=bash
# UAT-006: inject a deterministic release-download failure after preserving a
# healthy command/config fingerprint. No authentication output is collected.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

UAT_ID=UAT-006
HOST=${JARN_UAT_SSH_HOST:-}
INSTALLER_URL=${JARN_UAT_INSTALLER_URL:-$DEFAULT_INSTALLER_URL}
OUTPUT=""
EXECUTE=0

usage() {
  cat <<'EOF'
UAT-006 — Network failure preserves the prior install

Usage:
  scripts/uat/uat-006-network-failure.sh [--host USER@HOST] [--output FILE]
                                      [--installer-url HTTPS_URL] [--execute]

Default behavior is a local dry-run and makes no SSH connection. The disposable
target must already have a healthy `jarn` and ~/.jarn/config.yaml. Execute mode
downloads install.sh first, then points only its release download at closed
loopback port 9. It expects failure and compares pre/post SHA-256 fingerprints.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --host requires USER@HOST" >&2; exit 2; }
      HOST=$2; shift 2 ;;
    --installer-url)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --installer-url requires a URL" >&2; exit 2; }
      INSTALLER_URL=$2; shift 2 ;;
    --output)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --output requires a file" >&2; exit 2; }
      OUTPUT=$2; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf '%s\n' "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

uat_validate_https_url "$INSTALLER_URL"
DISPLAY_COMMAND="NO_PROXY=127.0.0.1 JARN_GITHUB_BASE=https://127.0.0.1:9 JARN_GITHUB_REPO=unreachable JARN_CONNECT_TIMEOUT=1 sh install.sh --version 99.0.0 --method binary --no-setup"

if [[ "$EXECUTE" -ne 1 ]]; then
  printf '%s\n' "DRY RUN — no SSH connection or target write will occur."
  printf '%s\n' "Target: ${HOST:-[required with --execute]}"
  printf '%s\n' "Required fixture: a healthy prior J.A.R.N. command and config on a disposable Ubuntu account."
  printf '%s\n' "Fault: release URLs only -> https://127.0.0.1:9; installer source is downloaded before injection."
  printf '%s\n' "Checks: non-zero failure, named stage, retry command, no Done/Ready, unchanged hashes, doctor JSON."
  uat_write_not_run_if_requested \
    "$OUTPUT" "$UAT_ID" "$DISPLAY_COMMAND" \
    "Pass --execute only for a disposable target containing a verified prior install."
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
    --implementation "install.sh" --implementation "src/jarn/doctor/collect.py"
    --automated-test "tests/test_installer.py"
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
    "Establish non-interactive SSH access and rerun."
  exit 2
fi
PLATFORM_OS=$(uat_probe_value "$PLATFORM_PROBE" os)
PLATFORM_VERSION=$(uat_probe_value "$PLATFORM_PROBE" version)
PLATFORM_ARCH=$(uat_probe_value "$PLATFORM_PROBE" arch)
PLATFORM_LIBC=$(uat_probe_value "$PLATFORM_PROBE" libc)

BEFORE=$(uat_ssh_readonly "$HOST" 'resolved=$(bash -lic '\''command -v jarn 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); version=$(bash -lic '\''jarn --version 2>/dev/null || true'\'' 2>/dev/null | tail -n 1); executable_sha=absent; [ -n "$resolved" ] && [ -f "$resolved" ] && executable_sha=$(sha256sum "$resolved" | awk '\''{print $1}'\''); config_sha=absent; [ -f "$HOME/.jarn/config.yaml" ] && config_sha=$(sha256sum "$HOME/.jarn/config.yaml" | awk '\''{print $1}'\''); printf "home=%s\nresolution=%s\nversion=%s\nexecutable_sha=%s\nconfig_sha=%s\n" "$HOME" "${resolved:-absent}" "${version:-unknown}" "$executable_sha" "$config_sha"') || {
  write_evidence blocked "Healthy-install probe failed before mutation." \
    "Provision the documented disposable fixture and rerun."
  exit 2
}
REMOTE_HOME=$(uat_probe_value "$BEFORE" home)
BEFORE_RESOLUTION=$(uat_probe_value "$BEFORE" resolution)
BEFORE_VERSION=$(uat_probe_value "$BEFORE" version)
BEFORE_EXECUTABLE_SHA=$(uat_probe_value "$BEFORE" executable_sha)
BEFORE_CONFIG_SHA=$(uat_probe_value "$BEFORE" config_sha)
if [[ "$BEFORE_VERSION" != jarn\ * || ! "$BEFORE_EXECUTABLE_SHA" =~ ^[0-9a-f]{64}$ || ! "$BEFORE_CONFIG_SHA" =~ ^[0-9a-f]{64}$ ]]; then
  write_evidence blocked "Fixture lacks a healthy, fingerprintable prior command and config." \
    "Expected jarn --version plus SHA-256 for the resolved executable and ~/.jarn/config.yaml."
  exit 2
fi

if ! uat_confirm_disposable "$HOST" "run a deterministic failed update while checking that the existing command/config remain byte-identical"; then
  write_evidence not_run "Operator declined the disposable-target confirmation." \
    "No injected failure command was sent."
  exit 2
fi

REMOTE_COMMAND="installer=\$(mktemp \"\${TMPDIR:-/tmp}/jarn-uat006.XXXXXX\") || exit 1; trap 'rm -f \"\$installer\"' 0 HUP INT TERM; curl -fsSL '$INSTALLER_URL' -o \"\$installer\" || exit \$?; set +e; install_output=\$(NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 JARN_GITHUB_BASE=https://127.0.0.1:9 JARN_GITHUB_REPO=unreachable JARN_CONNECT_TIMEOUT=1 sh \"\$installer\" --version 99.0.0 --method binary --no-setup 2>&1); install_rc=\$?; set -e; done_printed=no; printf '%s' \"\$install_output\" | grep -Eq '(^|[^[:alpha:]])(Done|Ready)([^[:alpha:]]|$)' && done_printed=yes; stage=missing; printf '%s' \"\$install_output\" | grep -q 'release asset' && stage=release_asset_download; retry=missing; printf '%s' \"\$install_output\" | grep -qi 'retry' && retry=present; error_line=\$(printf '%s\n' \"\$install_output\" | sed -n '/^error:/p' | head -n 1); resolved=\$(bash -lic 'command -v jarn 2>/dev/null || true' 2>/dev/null | tail -n 1); version=\$(bash -lic 'jarn --version 2>/dev/null || true' 2>/dev/null | tail -n 1); executable_sha=absent; [ -n \"\$resolved\" ] && [ -f \"\$resolved\" ] && executable_sha=\$(sha256sum \"\$resolved\" | awk '{print \$1}'); config_sha=absent; [ -f \"\$HOME/.jarn/config.yaml\" ] && config_sha=\$(sha256sum \"\$HOME/.jarn/config.yaml\" | awk '{print \$1}'); doctor_json=invalid; doctor_output=\$(bash -lic 'jarn doctor --json 2>/dev/null' 2>/dev/null || true); printf '%s' \"\$doctor_output\" | grep -Eq '^[[:space:]]*\{' && doctor_json=valid; printf 'install_rc=%s\ndone_printed=%s\nstage=%s\nretry=%s\nerror_line=%s\nresolution=%s\nversion=%s\nexecutable_sha=%s\nconfig_sha=%s\ndoctor_json=%s\n' \"\$install_rc\" \"\$done_printed\" \"\$stage\" \"\$retry\" \"\${error_line:-missing}\" \"\${resolved:-absent}\" \"\${version:-unknown}\" \"\$executable_sha\" \"\$config_sha\" \"\$doctor_json\""
AFTER=$(uat_ssh_readonly "$HOST" "$REMOTE_COMMAND") || {
  write_evidence failed "Failure-injection harness could not complete its bounded checks." \
    "The prior fingerprints are recorded only in controller memory; rerun the read-only probe before any repair."
  exit 1
}

INSTALL_RC=$(uat_probe_value "$AFTER" install_rc)
DONE_PRINTED=$(uat_probe_value "$AFTER" done_printed)
FAILED_STAGE=$(uat_probe_value "$AFTER" stage)
RETRY=$(uat_probe_value "$AFTER" retry)
ERROR_LINE=$(uat_probe_value "$AFTER" error_line)
AFTER_RESOLUTION=$(uat_probe_value "$AFTER" resolution)
AFTER_VERSION=$(uat_probe_value "$AFTER" version)
AFTER_EXECUTABLE_SHA=$(uat_probe_value "$AFTER" executable_sha)
AFTER_CONFIG_SHA=$(uat_probe_value "$AFTER" config_sha)
DOCTOR_JSON=$(uat_probe_value "$AFTER" doctor_json)

ERRORS=()
ERROR_COUNT=0
add_error() { ERRORS[$ERROR_COUNT]=$1; ERROR_COUNT=$((ERROR_COUNT + 1)); }
[[ "$INSTALL_RC" =~ ^[1-9][0-9]*$ ]] || add_error "injected release download did not return non-zero"
[[ "$DONE_PRINTED" == no ]] || add_error "failed installer printed Done or Ready"
[[ "$FAILED_STAGE" == release_asset_download ]] || add_error "failed stage was not identified as release asset download"
[[ "$RETRY" == present ]] || add_error "installer did not provide a retry command"
[[ "$AFTER_RESOLUTION" == "$BEFORE_RESOLUTION" && "$AFTER_VERSION" == "$BEFORE_VERSION" ]] || add_error "prior command no longer resolves/runs identically"
[[ "$AFTER_EXECUTABLE_SHA" == "$BEFORE_EXECUTABLE_SHA" ]] || add_error "prior executable bytes changed"
[[ "$AFTER_CONFIG_SHA" == "$BEFORE_CONFIG_SHA" ]] || add_error "prior config bytes changed"
[[ "$DOCTOR_JSON" == valid ]] || add_error "jarn doctor --json did not return machine-readable diagnostics"
DOC_LOOKUP="external lookup was required; details were not collected"
if uat_yes_no "No external documentation lookup was needed during this UAT"; then
  DOC_LOOKUP="none; operator confirmed"
else
  add_error "operator required an external documentation lookup"
fi

STATUS=passed
RESULT="Injected release-download failure named its stage/retry, emitted no success, preserved prior bytes, and remained diagnosable."
if [[ "$ERROR_COUNT" -ne 0 ]]; then
  STATUS=failed
  RESULT="One or more network-failure preservation or diagnostic checkpoints failed."
fi

ARGS=(
  --output "$OUTPUT" --uat-id "$UAT_ID" --status "$STATUS"
  --started-at "$STARTED_AT" --ended-at "$(uat_now)"
  --duration-seconds "$(uat_duration "$START_EPOCH")"
  --command "$DISPLAY_COMMAND" --result "$RESULT"
  --implementation "install.sh" --implementation "src/jarn/doctor/collect.py"
  --automated-test "tests/test_installer.py"
  --decision "failure exit=$INSTALL_RC; stage=$FAILED_STAGE; retry=$RETRY; success_text=$DONE_PRINTED"
  --decision "prior resolution/version preserved: $AFTER_RESOLUTION / $AFTER_VERSION"
  --decision "doctor JSON: $DOCTOR_JSON"
  --documentation-lookup "$DOC_LOOKUP"
  --platform-os "$PLATFORM_OS" --platform-version "$PLATFORM_VERSION"
  --platform-arch "$PLATFORM_ARCH" --platform-libc "$PLATFORM_LIBC"
  --redact-host "$HOST" --redact-home "$REMOTE_HOME"
)
[[ "$ERROR_LINE" == missing ]] || ARGS+=(--error "$ERROR_LINE")
if [[ "$ERROR_COUNT" -gt 0 ]]; then
  for error in "${ERRORS[@]}"; do ARGS+=(--error "$error"); done
fi
uat_write_result "${ARGS[@]}"
[[ "$STATUS" == passed ]]
