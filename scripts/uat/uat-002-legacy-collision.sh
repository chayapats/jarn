#!/usr/bin/env bash
# shellcheck shell=bash
# UAT-002: install over a deliberately provisioned legacy global-npm command.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

UAT_ID=UAT-002
HOST=${JARN_UAT_SSH_HOST:-}
INSTALLER_URL=${JARN_UAT_INSTALLER_URL:-$DEFAULT_INSTALLER_URL}
LEGACY_PATH=${JARN_UAT_LEGACY_PATH:-/usr/local/bin/jarn}
OUTPUT=""
EXECUTE=0

usage() {
  cat <<'EOF'
UAT-002 — Legacy npm collision

Usage:
  scripts/uat/uat-002-legacy-collision.sh [--host USER@HOST] [--output FILE]
       [--installer-url HTTPS_URL] [--legacy-path ABSOLUTE_PATH] [--execute]

Default behavior is a local dry-run and makes no SSH connection. The disposable
fixture must resolve `jarn` to an old npm-owned command and contain
~/.jarn/config.yaml with config_version 0, 1, or 2. The harness never removes the
legacy command. A passing run requires byte-identical migration backup evidence.
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
    --legacy-path)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --legacy-path requires an absolute path" >&2; exit 2; }
      LEGACY_PATH=$2; shift 2 ;;
    --output)
      [[ $# -ge 2 ]] || { printf '%s\n' "error: --output requires a file" >&2; exit 2; }
      OUTPUT=$2; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf '%s\n' "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

uat_validate_https_url "$INSTALLER_URL"
[[ "$LEGACY_PATH" =~ ^/[A-Za-z0-9._/+:-]+$ && "$LEGACY_PATH" != *..* ]] || {
  printf '%s\n' "error: --legacy-path must be a normalized absolute path" >&2
  exit 2
}
DISPLAY_COMMAND="$(uat_canonical_install_command "$INSTALLER_URL") (installer receives --no-setup for collision isolation)"

if [[ "$EXECUTE" -ne 1 ]]; then
  printf '%s\n' "DRY RUN — no SSH connection or target write will occur."
  printf '%s\n' "Target: ${HOST:-[required with --execute]}"
  printf '%s\n' "Required fixture: old npm J.A.R.N. resolves at $LEGACY_PATH; config_version is 0, 1, or 2."
  printf '%s\n' "Checks: full type -a inventory, new PATH precedence, no deletion, safe cleanup text, config backup/migration."
  uat_write_not_run_if_requested \
    "$OUTPUT" "$UAT_ID" "$DISPLAY_COMMAND" \
    "Pass --execute only for a disposable legacy-collision fixture."
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
    --implementation "install.sh" --implementation "src/jarn/config/migrations.py"
    --automated-test "tests/test_installer.py"
    --automated-test "tests/test_ga_config_migrations.py"
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

FIXTURE_PROBE=$(uat_ssh_readonly "$HOST" "resolved=\$(bash -lic 'command -v jarn 2>/dev/null || true' 2>/dev/null | tail -n 1); npm_owner=no; command -v npm >/dev/null 2>&1 && npm list -g --depth=0 jarn-cli >/dev/null 2>&1 && npm_owner=yes; legacy=absent; [ -x '$LEGACY_PATH' ] && legacy=present; config=absent; config_version=absent; config_sha=absent; if [ -f \"\$HOME/.jarn/config.yaml\" ]; then config=present; config_version=\$(sed -n 's/^[[:space:]]*config_version[[:space:]]*:[[:space:]]*//p' \"\$HOME/.jarn/config.yaml\" | head -n 1); config_version=\${config_version:-0}; config_sha=\$(sha256sum \"\$HOME/.jarn/config.yaml\" | awk '{print \$1}'); fi; all=\$(bash -lic 'type -a jarn 2>/dev/null || true' 2>/dev/null | tr '\n' ';'); printf 'home=%s\nresolution=%s\nnpm_owner=%s\nlegacy=%s\nconfig=%s\nconfig_version=%s\nconfig_sha=%s\ntype_all=%s\n' \"\$HOME\" \"\${resolved:-absent}\" \"\$npm_owner\" \"\$legacy\" \"\$config\" \"\$config_version\" \"\$config_sha\" \"\$all\"") || {
  write_evidence blocked "Legacy fixture probe failed before mutation." \
    "Provision the documented disposable fixture and rerun."
  exit 2
}
REMOTE_HOME=$(uat_probe_value "$FIXTURE_PROBE" home)
BEFORE_RESOLUTION=$(uat_probe_value "$FIXTURE_PROBE" resolution)
NPM_OWNER=$(uat_probe_value "$FIXTURE_PROBE" npm_owner)
LEGACY_PRESENT=$(uat_probe_value "$FIXTURE_PROBE" legacy)
CONFIG_PRESENT=$(uat_probe_value "$FIXTURE_PROBE" config)
CONFIG_VERSION=$(uat_probe_value "$FIXTURE_PROBE" config_version)
CONFIG_SHA=$(uat_probe_value "$FIXTURE_PROBE" config_sha)

if [[ "$BEFORE_RESOLUTION" != "$LEGACY_PATH" || "$NPM_OWNER" != yes || "$LEGACY_PRESENT" != present ]]; then
  write_evidence blocked "Fixture is not the requested global-npm collision." \
    "Expected fresh-shell resolution=$LEGACY_PATH, npm jarn-cli ownership, and an executable legacy path."
  exit 2
fi
if [[ "$CONFIG_PRESENT" != present || ! "$CONFIG_VERSION" =~ ^[012]$ ]]; then
  write_evidence blocked "Fixture lacks a supported legacy config requiring migration." \
    "Expected ~/.jarn/config.yaml with config_version 0, 1, or 2; observed $CONFIG_VERSION."
  exit 2
fi

if ! uat_confirm_disposable "$HOST" "install a new user-space command and migrate the legacy config while retaining its byte backup"; then
  write_evidence not_run "Operator declined the disposable-target confirmation." \
    "No installation command was sent."
  exit 2
fi

REMOTE_COMMAND="jarn_installer_tmp=\$(mktemp \"\${TMPDIR:-/tmp}/jarn-install.XXXXXX\") || exit 1; trap 'rm -f \"\$jarn_installer_tmp\"' 0 HUP INT TERM; curl -fsSL '$INSTALLER_URL' -o \"\$jarn_installer_tmp\" || exit \$?; sh \"\$jarn_installer_tmp\" --no-setup; rc=\$?; { [ \"\$rc\" -eq 0 ] || [ \"\$rc\" -eq 10 ]; } || exit \"\$rc\"; rm -f \"\$jarn_installer_tmp\"; trap - 0 HUP INT TERM; exec \"\$SHELL\" -lic 'command -v jarn && jarn --version && jarn doctor --json >/dev/null 2>&1 || true'"
set +e
INSTALL_OUTPUT=$(ssh -o ConnectTimeout=10 "$HOST" "$REMOTE_COMMAND" 2>&1)
INSTALL_RC=$?
set -e
printf '%s\n' "$INSTALL_OUTPUT"
if [[ "$INSTALL_RC" -ne 0 ]]; then
  write_evidence failed "Installer or fresh-shell verification returned non-zero." \
    "Review the terminal-only installer output; no legacy command was deleted by the harness."
  exit 1
fi

POST_PROBE=$(uat_ssh_readonly "$HOST" "resolved=\$(bash -lic 'command -v jarn 2>/dev/null || true' 2>/dev/null | tail -n 1); version=\$(bash -lic 'jarn --version 2>/dev/null || true' 2>/dev/null | tail -n 1); legacy=absent; [ -x '$LEGACY_PATH' ] && legacy=present; all=\$(bash -lic 'type -a jarn 2>/dev/null || true' 2>/dev/null | tr '\n' ';'); config_version=absent; config_sha=absent; backup_match=no; if [ -f \"\$HOME/.jarn/config.yaml\" ]; then config_version=\$(sed -n 's/^[[:space:]]*config_version[[:space:]]*:[[:space:]]*//p' \"\$HOME/.jarn/config.yaml\" | head -n 1); config_sha=\$(sha256sum \"\$HOME/.jarn/config.yaml\" | awk '{print \$1}'); for backup in \"\$HOME\"/.jarn/config.yaml.bak.*; do [ -f \"\$backup\" ] || continue; [ \"\$(sha256sum \"\$backup\" | awk '{print \$1}')\" = '$CONFIG_SHA' ] && backup_match=yes; done; fi; printf 'resolution=%s\nversion=%s\nlegacy=%s\ntype_all=%s\nconfig_version=%s\nconfig_sha=%s\nbackup_match=%s\n' \"\${resolved:-absent}\" \"\${version:-unknown}\" \"\$legacy\" \"\$all\" \"\${config_version:-absent}\" \"\$config_sha\" \"\$backup_match\"") || true
POST_RESOLUTION=$(uat_probe_value "$POST_PROBE" resolution)
POST_VERSION=$(uat_probe_value "$POST_PROBE" version)
POST_LEGACY=$(uat_probe_value "$POST_PROBE" legacy)
POST_TYPE_ALL=$(uat_probe_value "$POST_PROBE" type_all)
POST_CONFIG_VERSION=$(uat_probe_value "$POST_PROBE" config_version)
POST_BACKUP_MATCH=$(uat_probe_value "$POST_PROBE" backup_match)

STATUS=passed
ERRORS=()
ERROR_COUNT=0
add_error() { ERRORS[$ERROR_COUNT]=$1; ERROR_COUNT=$((ERROR_COUNT + 1)); }
[[ "$POST_RESOLUTION" == "$REMOTE_HOME/.local/bin/jarn" ]] || add_error "fresh shell resolves $POST_RESOLUTION instead of the new user-space command"
[[ "$POST_VERSION" == jarn\ * ]] || add_error "new command did not report a J.A.R.N. version"
[[ "$POST_LEGACY" == present ]] || add_error "legacy command was removed"
[[ "$POST_TYPE_ALL" == *"$LEGACY_PATH"* && "$POST_TYPE_ALL" == *"$REMOTE_HOME/.local/bin/jarn"* ]] || add_error "type -a did not retain both command locations"
[[ "$POST_CONFIG_VERSION" == 3 && "$POST_BACKUP_MATCH" == yes ]] || add_error "legacy config was not migrated to version 3 with a byte-identical backup"
[[ "$INSTALL_OUTPUT" == *"optional cleanup"* && "$INSTALL_OUTPUT" == *"never deletes"* ]] || add_error "installer did not present safe optional cleanup and non-deletion guidance"
DOC_LOOKUP="external lookup was required; details were not collected"
if uat_yes_no "No external documentation lookup was needed during this UAT"; then
  DOC_LOOKUP="none; operator confirmed"
else
  add_error "operator required an external documentation lookup"
fi
[[ "$ERROR_COUNT" -eq 0 ]] || STATUS=failed

RESULT="New user-space command wins PATH; npm command remains; cleanup guidance and transactional config migration were verified."
[[ "$STATUS" == passed ]] || RESULT="One or more legacy-collision, cleanup, or config-migration checkpoints failed."
ARGS=(
  --output "$OUTPUT" --uat-id "$UAT_ID" --status "$STATUS"
  --started-at "$STARTED_AT" --ended-at "$(uat_now)"
  --duration-seconds "$(uat_duration "$START_EPOCH")"
  --command "$DISPLAY_COMMAND" --result "$RESULT"
  --implementation "install.sh" --implementation "src/jarn/config/migrations.py"
  --automated-test "tests/test_installer.py"
  --automated-test "tests/test_ga_config_migrations.py"
  --decision "legacy npm path retained: $POST_LEGACY"
  --decision "fresh-shell resolution: $POST_RESOLUTION"
  --decision "config migration: $CONFIG_VERSION -> $POST_CONFIG_VERSION; matching backup=$POST_BACKUP_MATCH"
  --documentation-lookup "$DOC_LOOKUP"
  --platform-os "$PLATFORM_OS" --platform-version "$PLATFORM_VERSION"
  --platform-arch "$PLATFORM_ARCH" --platform-libc "$PLATFORM_LIBC"
  --redact-host "$HOST" --redact-home "$REMOTE_HOME"
)
if [[ "$ERROR_COUNT" -gt 0 ]]; then
  for error in "${ERRORS[@]}"; do ARGS+=(--error "$error"); done
fi
uat_write_result "${ARGS[@]}"
[[ "$STATUS" == passed ]]
