#!/usr/bin/env bash
# Shared helpers for the release UAT harnesses. This file is sourced, not run.

set -euo pipefail

UAT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$UAT_DIR/../.." && pwd)
DEFAULT_INSTALLER_URL="https://raw.githubusercontent.com/chayapats/jarn/main/install.sh"
DEFAULT_RESULTS_DIR="${TMPDIR:-/tmp}/jarn-uat-results"

uat_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

uat_epoch() {
  date +%s
}

uat_python() {
  if [[ -n "${JARN_UAT_PYTHON:-}" ]]; then
    printf '%s\n' "$JARN_UAT_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$REPO_ROOT/.venv/bin/python"
  else
    printf '%s\n' "error: Python 3 is required only on the UAT controller to write redacted evidence" >&2
    return 1
  fi
}

uat_default_output() {
  local uat_id=$1
  local lower_id
  lower_id=$(printf '%s' "$uat_id" | tr '[:upper:]' '[:lower:]')
  printf '%s/%s-%s.json\n' \
    "${JARN_UAT_RESULTS_DIR:-$DEFAULT_RESULTS_DIR}" \
    "$lower_id" \
    "$(date -u +%Y%m%dT%H%M%SZ)"
}

uat_validate_host() {
  local host=$1
  [[ -n "$host" ]] || {
    printf '%s\n' "error: --host USER@HOST is required for --execute" >&2
    return 1
  }
  [[ "$host" =~ ^[A-Za-z0-9_.:@-]+$ ]] || {
    printf '%s\n' "error: unsafe SSH host syntax; use USER@HOST without spaces or options" >&2
    return 1
  }
  [[ "$host" != -* ]] || {
    printf '%s\n' "error: SSH host must not begin with '-'" >&2
    return 1
  }
}

uat_validate_https_url() {
  local url=$1
  [[ "$url" =~ ^https://[A-Za-z0-9._~/:@%+-]+$ ]] || {
    printf '%s\n' \
      "error: installer URL must be HTTPS and contain no shell/query characters" >&2
    return 1
  }
}

uat_validate_local_http_url() {
  local url=$1
  local port
  [[ "$url" =~ ^http://(localhost|127\.0\.0\.1|\[::1\]):[0-9]{1,5}$ ]] || {
    printf '%s\n' \
      "error: local endpoint must be http://localhost:PORT, http://127.0.0.1:PORT, or http://[::1]:PORT" >&2
    return 1
  }
  port=${url##*:}
  [[ "$port" != 0 && "$port" != 0* && "$port" -le 65535 ]] || {
    printf '%s\n' "error: local endpoint port must be between 1 and 65535" >&2
    return 1
  }
}

uat_canonical_install_command() {
  local url=$1
  uat_validate_https_url "$url"
  printf '%s\n' "jarn_installer_tmp=\$(mktemp \"\${TMPDIR:-/tmp}/jarn-install.XXXXXX\") && trap '[ -z \"\${jarn_installer_tmp:-}\" ] || rm -f \"\$jarn_installer_tmp\"' 0 HUP INT TERM && curl -fsSL '$url' -o \"\$jarn_installer_tmp\" && sh \"\$jarn_installer_tmp\"; jarn_install_rc=\$?; [ -z \"\${jarn_installer_tmp:-}\" ] || rm -f \"\$jarn_installer_tmp\"; trap - 0 HUP INT TERM; if [ \"\$jarn_install_rc\" -eq 0 ] || [ \"\$jarn_install_rc\" -eq 10 ]; then exec \"\$SHELL\" -l; else (exit \"\$jarn_install_rc\"); fi"
}

uat_confirm_disposable() {
  local host=$1
  local reason=$2
  local answer
  printf '%s\n' "This UAT will change J.A.R.N. state on $host: $reason" >&2
  printf '%s' "Type the exact host '$host' to confirm it is disposable: " >&2
  IFS= read -r answer
  [[ "$answer" == "$host" ]] || {
    printf '%s\n' "Aborted; confirmation did not match." >&2
    return 1
  }
}

uat_ssh_readonly() {
  local host=$1
  shift
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" "$@"
}

uat_platform_probe() {
  local host=$1
  uat_ssh_readonly "$host" 'sh -c '\''
    os_id=unknown
    os_version=unknown
    if [ "$(uname -s)" = Darwin ]; then
      os_id=macos
      os_version=$(sw_vers -productVersion 2>/dev/null || true)
    elif [ -r /etc/os-release ]; then
      os_id=$(sed -n "s/^ID=//p" /etc/os-release | sed -n "1p" | tr -d "\\\"")
      os_version=$(sed -n "s/^VERSION_ID=//p" /etc/os-release | sed -n "1p" | tr -d "\\\"")
    fi
    libc=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
    printf "os=%s\\nversion=%s\\narch=%s\\nlibc=%s\\n" \
      "$os_id" "$os_version" "$(uname -m)" "${libc:-unknown}"
  '\'''
}

uat_probe_value() {
  local probe=$1
  local key=$2
  printf '%s\n' "$probe" | sed -n "s/^${key}=//p" | sed -n '1p'
}

uat_write_result() {
  local python
  python=$(uat_python)
  "$python" "$UAT_DIR/write_result.py" "$@"
}

uat_write_not_run_if_requested() {
  local output=$1
  local uat_id=$2
  local command=$3
  local limitation=$4
  [[ -n "$output" ]] || return 0
  uat_write_result \
    --output "$output" \
    --uat-id "$uat_id" \
    --status not_run \
    --started-at "$(uat_now)" \
    --ended-at "$(uat_now)" \
    --command "$command" \
    --result "Dry-run plan only; no target state changed." \
    --limitation "$limitation"
}

uat_yes_no() {
  local prompt=$1
  local answer
  printf '%s' "$prompt [y/N]: " >&2
  IFS= read -r answer
  [[ "$answer" == y || "$answer" == Y || "$answer" == yes || "$answer" == YES ]]
}

uat_duration() {
  local started_epoch=$1
  local ended_epoch
  ended_epoch=$(uat_epoch)
  printf '%s\n' "$((ended_epoch - started_epoch))"
}
