#!/bin/sh
# Install or update J.A.R.N. without requiring a development runtime.
#
# Recommended interactive one-liner. It downloads to a secure temporary file,
# never executes on curl failure, preserves the installer's exit status, and
# replaces the parent shell only after verified status 0 or 10:
#   jarn_installer_tmp=$(mktemp "${TMPDIR:-/tmp}/jarn-install.XXXXXX") && trap '[ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"' 0 HUP INT TERM && curl -fsSL 'https://raw.githubusercontent.com/chayapats/jarn/main/install.sh' -o "$jarn_installer_tmp" && sh "$jarn_installer_tmp"; jarn_install_rc=$?; [ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"; trap - 0 HUP INT TERM; if [ "$jarn_install_rc" -eq 0 ] || [ "$jarn_install_rc" -eq 10 ]; then exec "$SHELL" -l; else (exit "$jarn_install_rc"); fi
#
# Environment variables remain supported for automation. Command-line options
# take precedence; run `sh install.sh --help` for the complete interface.
set -eu

if [ -z "${HOME:-}" ]; then
    printf '%s\n' \
        "JARN-INSTALL-001: Installation could not start." \
        "Cause: HOME is not set; a safe user-space installation directory cannot be selected." \
        "Component: installer preflight (retryable: yes, after correcting the environment)" \
        'Next: Set HOME to the intended user home, then rerun the documented safe installer command.' \
        "Log: unavailable (preflight did not create installer state)" >&2
    exit 1
fi

REPO="${JARN_GITHUB_REPO:-chayapats/jarn}"
GITHUB_BASE="${JARN_GITHUB_BASE:-https://github.com}"
RELEASES_API="${JARN_RELEASES_API:-https://api.github.com/repos/$REPO/releases?per_page=20}"
INSTALL_DIR="${JARN_INSTALL_DIR:-${HOME}/.local/bin}"
STATE_DIR="${JARN_STATE_DIR:-${HOME}/.local/state/jarn}"
INSTALL_METHOD="${JARN_INSTALL_METHOD:-auto}"
RUN_SETUP="${JARN_RUN_SETUP:-auto}"
REQUESTED_VERSION="${JARN_VERSION:-latest}"
CHANNEL="${JARN_CHANNEL:-stable}"
UV_VERSION="${JARN_UV_VERSION:-0.12.3}"
UV_INSTALL_URL="${JARN_UV_INSTALL_URL:-https://astral.sh/uv/${UV_VERSION}/install.sh}"
UV_INSTALL_SHA256="${JARN_UV_INSTALL_SHA256:-a7e3924ea1cd06bf1518c577d635c624ae2e2db030e0fc8ff8cf426224384e17}"
MIN_DISK_KB="${JARN_MIN_DISK_KB:-524288}"
CONNECT_TIMEOUT="${JARN_CONNECT_TIMEOUT:-10}"
PUBLIC_INSTALLER_URL="https://raw.githubusercontent.com/chayapats/jarn/main/install.sh"

ASSUME_YES=0
DRY_RUN=0
VERBOSE=0
TMP_DIR=""
STAGED_PATH=""
TOOL_STAGE=""
LOCK_DIR=""
LOCK_HELD=0
PREPARED_PATH=""
PREPARED_METHOD=""
PREVIOUS_PATH=""
FAILED_PATH=""
INSTALL_CHANGED=0
ACTIVATION_IN_PROGRESS=0
HAD_PREVIOUS=0
INSTALL_RESULT=""
UV_OWNED=false
UV_BIN=""
UV_REPORTED_VERSION=""
PROFILE_UPDATES=""
SETUP_STATUS="pending"
ACTIVATION_STATUS="pending"
CURRENT_RESOLUTION=""
LOGIN_RESOLUTION=""
INTERACTIVE_RESOLUTION=""
INVENTORY_FILE=""
SHELL_TYPES_FILE=""
LOG_FILE=""
TRANSACTION_ACTIVE=0
TRANSACTION_COMMITTED=0
TRANSACTION_BACKUP_DIR=""
PROFILE_BACKUP_DIR=""
PROFILE_BACKUP_COUNT=0
PROFILE_ACTIVATION_ATTEMPT=0
PROFILE_TMP_PATH=""
MANIFEST_TMP_PATH=""
LEGACY_TMP_PATH=""
METADATA_MANIFEST_EXISTED=0
METADATA_LEGACY_EXISTED=0
TRANSACTION_JOURNAL=""
TRANSACTION_JOURNAL_TMP=""
RECEIPT_REPORT=""

info() {
    printf '%s\n' "==> $*"
}

detail() {
    if [ "$VERBOSE" -eq 1 ]; then
        printf '%s\n' "    $*"
    fi
}

warn() {
    printf '%s\n' "warning: $*" >&2
}

die() {
    install_log=${LOG_FILE:-${STATE_DIR}/install.log}
    printf '%s\n' \
        "JARN-INSTALL-001: Installation did not complete." \
        "Cause: $*" \
        "Component: installer transaction (retryable: yes, after correcting the cause)" \
        "Next: The prior executable/data were preserved or restored. Correct the cause, then rerun with --verbose; use jarn doctor --report if an older command remains usable." \
        "Log: $install_log" >&2
    exit 1
}

normalize_managed_path() {
    raw_path=$1
    path_label=$2
    [ -n "$raw_path" ] || die "$path_label must not be empty"
    case "$raw_path" in
        *'
'*) die "$path_label must not contain a newline" ;;
    esac
    case "/$raw_path/" in
        */../*) die "$path_label must not contain a parent-directory (..) component" ;;
    esac
    case "$raw_path" in
        /*) absolute_path=$raw_path ;;
        *) absolute_path="$(pwd -P)/$raw_path" ;;
    esac
    normalized_path=$(printf '%s\n' "$absolute_path" | awk -F/ '
        {
            result = ""
            for (i = 1; i <= NF; i++) {
                if ($i == "" || $i == ".") continue
                result = result "/" $i
            }
            if (result == "") print "/"
            else print result
        }
    ') || die "could not normalize $path_label"
    printf '%s\n' "$normalized_path"
}

reject_symlink_components() {
    managed_path=$1
    path_label=$2
    remaining=${managed_path#/}
    prefix=""
    while [ -n "$remaining" ]; do
        case "$remaining" in
            */*) component=${remaining%%/*}; remaining=${remaining#*/} ;;
            *) component=$remaining; remaining="" ;;
        esac
        [ -n "$component" ] || continue
        prefix="$prefix/$component"
        [ ! -L "$prefix" ] || \
            die "$path_label traverses a symbolic link: $prefix"
    done
}

usage() {
    cat <<'EOF'
J.A.R.N. installer

Usage:
  sh install.sh [options]

Recommended safe one-line install:
  jarn_installer_tmp=$(mktemp "${TMPDIR:-/tmp}/jarn-install.XXXXXX") && trap '[ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"' 0 HUP INT TERM && curl -fsSL 'https://raw.githubusercontent.com/chayapats/jarn/main/install.sh' -o "$jarn_installer_tmp" && sh "$jarn_installer_tmp"; jarn_install_rc=$?; [ -z "${jarn_installer_tmp:-}" ] || rm -f "$jarn_installer_tmp"; trap - 0 HUP INT TERM; if [ "$jarn_install_rc" -eq 0 ] || [ "$jarn_install_rc" -eq 10 ]; then exec "$SHELL" -l; else (exit "$jarn_install_rc"); fi

Options:
  --version VERSION          Install a specific version (default: latest)
  --channel stable|beta      Release channel (default: stable)
  --install-dir DIR          Command directory (default: ~/.local/bin)
  --method auto|binary|python
                             Select release binary or isolated Python fallback
  --no-setup                 Install only; do not run first-time setup
  --dry-run                  Print the detected state and planned actions only
  --yes                      Accept safe non-destructive defaults
  --verbose                  Show discovery details and dependency output
  --help                     Show this help

Exit status:
  0   Installed/current and already active in the invoking shell environment
  10  Installed and verified, but parent-shell activation is still required
  20  Installed, but first-time setup is incomplete
  1   Installation failed; the previous executable was preserved or restored

The installer never removes older J.A.R.N. commands or shared dependencies.
EOF
}

need_option_value() {
    [ "$#" -ge 2 ] || die "$1 requires a value"
    [ -n "$2" ] || die "$1 requires a non-empty value"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            need_option_value "$@"
            REQUESTED_VERSION=$2
            shift 2
            ;;
        --channel)
            need_option_value "$@"
            CHANNEL=$2
            shift 2
            ;;
        --install-dir)
            need_option_value "$@"
            INSTALL_DIR=$2
            shift 2
            ;;
        --method)
            need_option_value "$@"
            INSTALL_METHOD=$2
            shift 2
            ;;
        --no-setup)
            RUN_SETUP=never
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --yes|-y)
            ASSUME_YES=1
            shift
            ;;
        --verbose|-v)
            VERBOSE=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --)
            shift
            [ "$#" -eq 0 ] || die "unexpected positional argument: $1"
            ;;
        *) die "unknown installer option: $1 (run with --help)" ;;
    esac
done

case "$INSTALL_METHOD" in
    auto|binary|python) ;;
    *) die "--method must be auto, binary, or python" ;;
esac

case "$RUN_SETUP" in
    auto|always|never) ;;
    *) die "JARN_RUN_SETUP must be auto, always, or never" ;;
esac

case "$CHANNEL" in
    stable|beta) ;;
    *) die "--channel must be stable or beta" ;;
esac

case "$MIN_DISK_KB" in
    ''|*[!0-9]*) die "JARN_MIN_DISK_KB must be a positive integer" ;;
esac
[ "$MIN_DISK_KB" -gt 0 ] || die "JARN_MIN_DISK_KB must be greater than zero"

case "$CONNECT_TIMEOUT" in
    ''|*[!0-9]*) die "JARN_CONNECT_TIMEOUT must be a positive integer number of seconds" ;;
esac
[ "$CONNECT_TIMEOUT" -gt 0 ] || die "JARN_CONNECT_TIMEOUT must be greater than zero"

INSTALL_DIR=$(normalize_managed_path "$INSTALL_DIR" "installation directory")
STATE_DIR=$(normalize_managed_path "$STATE_DIR" "state directory")
NORMALIZED_HOME=$(normalize_managed_path "$HOME" "HOME")

[ "$INSTALL_DIR" != / ] || die "refusing to use the filesystem root as --install-dir"
[ "$STATE_DIR" != / ] || die "refusing to use the filesystem root as JARN_STATE_DIR"
[ "$INSTALL_DIR" != "$NORMALIZED_HOME" ] || \
    die "refusing to use the home directory itself as --install-dir"
[ "$STATE_DIR" != "$NORMALIZED_HOME" ] || \
    die "refusing to use the home directory itself as JARN_STATE_DIR"
reject_symlink_components "$INSTALL_DIR" "installation directory"
reject_symlink_components "$STATE_DIR" "state directory"

BIN_PATH="$INSTALL_DIR/jarn"
MANIFEST_PATH="$STATE_DIR/install.json"
LEGACY_RECORD="$INSTALL_DIR/.jarn-install-method"
TRANSACTION_JOURNAL="$STATE_DIR/install.transaction"
VERSIONS_DIR="$STATE_DIR/versions"

cleanup() {
    # A signal can arrive between retaining the old command and verifying the
    # replacement. Restore the old command before removing ordinary staging.
    if [ "$ACTIVATION_IN_PROGRESS" -eq 1 ]; then
        emergency_failed="$INSTALL_DIR/.jarn.failed.interrupted.$VERSION.$$"
        if [ "$HAD_PREVIOUS" -eq 1 ]; then
            if [ -n "$PREVIOUS_PATH" ] && \
                { [ -e "$PREVIOUS_PATH" ] || [ -L "$PREVIOUS_PATH" ]; }; then
                if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
                    mv "$BIN_PATH" "$emergency_failed" 2>/dev/null || true
                fi
                if [ ! -e "$BIN_PATH" ] && [ ! -L "$BIN_PATH" ]; then
                    mv "$PREVIOUS_PATH" "$BIN_PATH" 2>/dev/null || true
                fi
            fi
        elif [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
            mv "$BIN_PATH" "$emergency_failed" 2>/dev/null || true
        fi
        # The executable has already been restored (or removed) above. Do not
        # let the ordinary transaction rollback move that restored command a
        # second time.
        INSTALL_CHANGED=0
        ACTIVATION_IN_PROGRESS=0
    fi
    if [ "$TRANSACTION_ACTIVE" -eq 1 ] && [ "$TRANSACTION_COMMITTED" -eq 0 ]; then
        rollback_install_transaction
    fi
    [ -z "$PROFILE_TMP_PATH" ] || rm -f "$PROFILE_TMP_PATH" 2>/dev/null || true
    [ -z "$MANIFEST_TMP_PATH" ] || rm -f "$MANIFEST_TMP_PATH" 2>/dev/null || true
    [ -z "$LEGACY_TMP_PATH" ] || rm -f "$LEGACY_TMP_PATH" 2>/dev/null || true
    [ -z "$TRANSACTION_JOURNAL_TMP" ] || \
        rm -f "$TRANSACTION_JOURNAL_TMP" 2>/dev/null || true
    if [ -n "$STAGED_PATH" ] && { [ -e "$STAGED_PATH" ] || [ -L "$STAGED_PATH" ]; }; then
        rm -f "$STAGED_PATH"
    fi
    if [ -n "$TOOL_STAGE" ] && [ -d "$TOOL_STAGE" ]; then
        rm -rf "$TOOL_STAGE"
    fi
    release_install_lock
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    :
}

trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

make_tmp_dir() {
    if [ -z "$TMP_DIR" ]; then
        TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/jarn-install.XXXXXX") || \
            die "could not create a secure temporary directory"
        chmod 700 "$TMP_DIR" 2>/dev/null || true
        INVENTORY_FILE="$TMP_DIR/installations"
        SHELL_TYPES_FILE="$TMP_DIR/shell-types"
        : > "$INVENTORY_FILE"
        : > "$SHELL_TYPES_FILE"
    fi
}

strip_os_release_value() {
    printf '%s\n' "$1" | sed 's/^"//; s/"$//'
}

detect_platform() {
    os_raw=${JARN_OS:-$(uname -s 2>/dev/null || printf unknown)}
    arch_raw=${JARN_ARCH:-$(uname -m 2>/dev/null || printf unknown)}

    case "$os_raw" in
        Linux|linux) OS=linux ;;
        Darwin|darwin) OS=darwin ;;
        MINGW*|MSYS*|CYGWIN*|Windows_NT|win32) OS=windows ;;
        *) OS=$(printf '%s' "$os_raw" | tr '[:upper:]' '[:lower:]') ;;
    esac

    case "$arch_raw" in
        x86_64|amd64|x64) ARCH=x86_64 ;;
        aarch64|arm64) ARCH=arm64 ;;
        *) ARCH=$arch_raw ;;
    esac

    DISTRO_ID=${JARN_DISTRO_ID:-}
    DISTRO_VERSION=${JARN_DISTRO_VERSION:-}
    WSL=false
    if [ "$OS" = linux ]; then
        if [ -z "$DISTRO_ID" ] && [ -r /etc/os-release ]; then
            raw_id=$(sed -n 's/^ID=//p' /etc/os-release | sed -n '1p')
            DISTRO_ID=$(strip_os_release_value "$raw_id")
        fi
        if [ -z "$DISTRO_VERSION" ] && [ -r /etc/os-release ]; then
            raw_version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | sed -n '1p')
            DISTRO_VERSION=$(strip_os_release_value "$raw_version")
        fi
        if [ -n "${WSL_DISTRO_NAME:-}" ] || \
            { [ -r /proc/version ] && grep -qi microsoft /proc/version; }; then
            WSL=true
        fi
    elif [ "$OS" = darwin ]; then
        DISTRO_ID=macos
        if [ -z "$DISTRO_VERSION" ] && command -v sw_vers >/dev/null 2>&1; then
            DISTRO_VERSION=$(sw_vers -productVersion 2>/dev/null || true)
        fi
    fi

    LIBC_NAME=${JARN_LIBC_NAME:-none}
    LIBC_VERSION=${JARN_LIBC_VERSION:-}
    if [ "$OS" = linux ] && [ "$LIBC_NAME" = none ]; then
        if command -v getconf >/dev/null 2>&1; then
            libc_line=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
            case "$libc_line" in
                glibc\ *)
                    LIBC_NAME=glibc
                    LIBC_VERSION=${libc_line#glibc }
                    ;;
            esac
        fi
        if [ "$LIBC_NAME" = none ] && command -v ldd >/dev/null 2>&1; then
            libc_line=$(ldd --version 2>&1 | sed -n '1p' || true)
            case "$libc_line" in
                *musl*) LIBC_NAME=musl ;;
                *GLIBC*|*glibc*|*GNU*)
                    LIBC_NAME=glibc
                    LIBC_VERSION=$(printf '%s\n' "$libc_line" | \
                        sed -n 's/.* \([0-9][0-9.]*\)$/\1/p')
                    ;;
            esac
        fi
    fi

    SHELL_PATH=${JARN_SHELL:-${SHELL:-/bin/sh}}
    if [ ! -x "$SHELL_PATH" ]; then
        SHELL_PATH=/bin/sh
    fi
    SHELL_NAME=${SHELL_PATH##*/}

    if [ -n "${CI:-}" ]; then
        CONTEXT=ci
    elif [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ]; then
        CONTEXT=ssh
    elif [ -f /.dockerenv ] || [ -f /run/.containerenv ]; then
        CONTEXT=container
    elif [ -t 0 ] && [ -t 1 ]; then
        CONTEXT=interactive
    else
        CONTEXT=headless
    fi
}

version_at_least() {
    awk -v have="$1" -v need="$2" 'BEGIN {
        split(have, h, "."); split(need, n, ".");
        for (i = 1; i <= 3; i++) {
            hv = h[i] + 0; nv = n[i] + 0;
            if (hv > nv) exit 0;
            if (hv < nv) exit 1;
        }
        exit 0;
    }'
}

validate_platform() {
    case "$OS" in
        windows)
            die "native Windows is not supported by install.sh. Open WSL2 Ubuntu and run the installer there; PowerShell installation is intentionally unavailable."
            ;;
        linux)
            case "$ARCH" in x86_64|arm64) ;; *)
                die "unsupported Linux architecture '$ARCH'; supported architectures are x86_64 and arm64"
            esac
            case "$DISTRO_ID:$DISTRO_VERSION" in
                ubuntu:20.04*|ubuntu:22.04*|ubuntu:24.04*|debian:11*|debian:12*) ;;
                *)
                    die "unsupported Linux target ${DISTRO_ID:-unknown} ${DISTRO_VERSION:-unknown}. Supported: Ubuntu 20.04/22.04/24.04 and Debian 11/12 (x86_64 or arm64); use WSL2 Ubuntu on Windows."
                    ;;
            esac
            [ "$LIBC_NAME" != musl ] || \
                die "musl Linux is not supported. Use a supported glibc-based Ubuntu/Debian host or container."
            [ "$LIBC_NAME" = glibc ] || \
                die "could not verify glibc on this Linux host; refusing an unverified installation"
            [ -n "$LIBC_VERSION" ] || \
                die "could not determine the glibc version"
            version_at_least "$LIBC_VERSION" 2.31 || \
                die "glibc $LIBC_VERSION is older than the supported minimum 2.31 (Ubuntu 20.04)"
            ;;
        darwin)
            case "$ARCH" in arm64|x86_64) ;; *)
                die "unsupported macOS architecture '$ARCH'; supported architectures are arm64 and x86_64 (Python fallback)"
            esac
            [ -n "$DISTRO_VERSION" ] || die "could not determine the macOS version"
            mac_major=${DISTRO_VERSION%%.*}
            case "$mac_major" in ''|*[!0-9]*) die "invalid macOS version '$DISTRO_VERSION'" ;; esac
            [ "$mac_major" -ge 13 ] || die "macOS $DISTRO_VERSION is unsupported; macOS 13 or newer is required"
            ;;
        *)
            die "unsupported operating system '$OS'; use supported Ubuntu/Debian, macOS 13+, or WSL2 Ubuntu"
            ;;
    esac
}

nearest_existing_dir() {
    probe=$1
    while [ ! -d "$probe" ]; do
        next_probe=$(dirname "$probe")
        [ "$next_probe" != "$probe" ] || break
        probe=$next_probe
    done
    printf '%s\n' "$probe"
}

check_target_writable() {
    target_dir=$1
    label=$2
    reject_symlink_components "$target_dir" "$label"
    case "${JARN_TEST_READ_ONLY_TARGET:-}:$label" in
        install:installation\ directory|state:state\ directory)
            die "$label is not writable: $target_dir (injected read-only target)"
            ;;
    esac
    if [ -e "$target_dir" ] && [ ! -d "$target_dir" ]; then
        die "$label path exists but is not a directory: $target_dir"
    fi
    parent_dir=$(nearest_existing_dir "$target_dir")
    [ -d "$parent_dir" ] || die "no existing parent directory for $label: $target_dir"
    [ -w "$parent_dir" ] || die "$label is not writable: $target_dir (nearest parent: $parent_dir)"
}

check_disk_space() {
    disk_root=$(nearest_existing_dir "$STATE_DIR")
    available_kb=${JARN_AVAILABLE_DISK_KB:-}
    if [ -z "$available_kb" ]; then
        available_kb=$(df -Pk "$disk_root" 2>/dev/null | awk 'NR > 1 {value=$4} END {print value}')
    fi
    case "$available_kb" in
        ''|*[!0-9]*) die "could not determine available disk space for $disk_root" ;;
    esac
    [ "$available_kb" -ge "$MIN_DISK_KB" ] || \
        die "insufficient disk space: ${available_kb} KB available, ${MIN_DISK_KB} KB required"
    detail "disk space: ${available_kb} KB available"
}

check_curl_tls() {
    command -v curl >/dev/null 2>&1 || die "curl is required to run this installer"
    case "$GITHUB_BASE$UV_INSTALL_URL" in
        *https://*)
            curl --version 2>/dev/null | grep -qi 'https' || \
                die "this curl build does not advertise HTTPS/TLS support"
            ;;
    esac
}

record_existing() {
    existing_path=$1
    existing_source=$2
    if [ -e "$existing_path" ] || [ -L "$existing_path" ]; then
        if ! grep -F "|$existing_path" "$INVENTORY_FILE" >/dev/null 2>&1; then
            printf '%s|%s\n' "$existing_source" "$existing_path" >> "$INVENTORY_FILE"
        fi
    fi
}

discover_installations() {
    make_tmp_dir
    : > "$INVENTORY_FILE"
    : > "$SHELL_TYPES_FILE"

    old_ifs=$IFS
    IFS=:
    for path_dir in ${PATH:-}; do
        [ -n "$path_dir" ] || path_dir=.
        record_existing "$path_dir/jarn" path
    done
    IFS=$old_ifs

    record_existing "$BIN_PATH" target
    record_existing "$HOME/.local/bin/jarn" user
    record_existing "/usr/local/bin/jarn" system
    record_existing "/usr/bin/jarn" system

    for nvm_jarn in "$HOME"/.nvm/versions/node/*/bin/jarn; do
        record_existing "$nvm_jarn" npm-nvm
    done

    if command -v npm >/dev/null 2>&1; then
        npm_prefix=$(npm prefix -g 2>/dev/null || true)
        [ -n "$npm_prefix" ] && record_existing "$npm_prefix/bin/jarn" npm
    fi
    if command -v pipx >/dev/null 2>&1; then
        pipx_bin=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)
        [ -n "$pipx_bin" ] && record_existing "$pipx_bin/jarn" pipx
    fi
    pip_user_python=""
    if command -v python3 >/dev/null 2>&1; then
        pip_user_python=$(command -v python3)
    elif command -v python >/dev/null 2>&1; then
        pip_user_python=$(command -v python)
    fi
    if [ -n "$pip_user_python" ]; then
        pip_user_base=$("$pip_user_python" -m site --user-base 2>/dev/null || true)
        [ -n "$pip_user_base" ] && record_existing "$pip_user_base/bin/jarn" pip-user
    fi
    if command -v brew >/dev/null 2>&1; then
        brew_prefix=$(brew --prefix 2>/dev/null || true)
        [ -n "$brew_prefix" ] && record_existing "$brew_prefix/bin/jarn" homebrew
    fi
    uv_tool_bin=${UV_TOOL_BIN_DIR:-}
    if [ -z "$uv_tool_bin" ] && command -v uv >/dev/null 2>&1; then
        uv_tool_bin=$(uv tool dir --bin 2>/dev/null || true)
    fi
    [ -n "$uv_tool_bin" ] && record_existing "$uv_tool_bin/jarn" uv-tool

    case "$SHELL_NAME" in
        bash)
            "$SHELL_PATH" -ic \
                '{ type -a jarn 2>/dev/null || true; hash -t jarn 2>/dev/null || true; }' \
                > "$SHELL_TYPES_FILE" 2>/dev/null || true
            ;;
        zsh|ksh|sh|dash)
            "$SHELL_PATH" -ic 'type -a jarn 2>/dev/null || true' \
                > "$SHELL_TYPES_FILE" 2>/dev/null || true
            ;;
        fish)
            "$SHELL_PATH" -ic 'type -a jarn 2>/dev/null; or true' \
                > "$SHELL_TYPES_FILE" 2>/dev/null || true
            ;;
    esac

    INSTALLATION_COUNT=$(awk 'END {print NR + 0}' "$INVENTORY_FILE")
    CURRENT_RESOLUTION=$(command -v jarn 2>/dev/null || true)
}

show_inventory() {
    if [ "$INSTALLATION_COUNT" -eq 0 ]; then
        detail "existing J.A.R.N. commands: none"
    else
        info "Found $INSTALLATION_COUNT existing J.A.R.N. command path(s); none will be deleted"
        while IFS='|' read -r found_source found_path; do
            [ -n "$found_path" ] || continue
            detail "$found_source: $found_path"
        done < "$INVENTORY_FILE"
    fi
    if [ -s "$SHELL_TYPES_FILE" ]; then
        detail "shell aliases/functions/resolution:"
        if [ "$VERBOSE" -eq 1 ]; then
            sed 's/^/      /' "$SHELL_TYPES_FILE"
        fi
    fi
}

show_preflight() {
    platform="$OS/$ARCH"
    [ "$OS" = linux ] && platform="$platform, $DISTRO_ID $DISTRO_VERSION, $LIBC_NAME $LIBC_VERSION"
    [ "$OS" = darwin ] && platform="$platform, macOS $DISTRO_VERSION"
    info "Preflight: $platform; shell $SHELL_NAME; context $CONTEXT"
    detail "install directory: $INSTALL_DIR"
    detail "state directory: $STATE_DIR"
    detail "package managers: npm=$(command -v npm 2>/dev/null || printf absent), pipx=$(command -v pipx 2>/dev/null || printf absent), uv=$(command -v uv 2>/dev/null || printf absent), brew=$(command -v brew 2>/dev/null || printf absent)"
    show_inventory
}

curl_failure_message() {
    case "$1" in
        5) printf '%s\n' "proxy lookup failed" ;;
        6) printf '%s\n' "DNS lookup failed" ;;
        18) printf '%s\n' "download was interrupted or only partially transferred" ;;
        23) printf '%s\n' "local download write failed (disk full or read-only filesystem)" ;;
        28) printf '%s\n' "network operation timed out" ;;
        35|51|58|60|77) printf '%s\n' "TLS handshake or certificate verification failed" ;;
        *) printf '%s\n' "network transfer failed (curl exit $1)" ;;
    esac
}

curl_get() {
    download_url=$1
    download_path=$2
    if curl -fLsS --connect-timeout "$CONNECT_TIMEOUT" --retry 2 --retry-delay 1 \
        "$download_url" -o "$download_path"; then
        return 0
    else
        curl_status=$?
    fi
    warn "$(curl_failure_message "$curl_status")"
    return "$curl_status"
}

show_download_retry() {
    warn "retry exactly after network recovery:"
    printf "  retry_tmp=\$(mktemp \"\${TMPDIR:-/tmp}/jarn-install.XXXXXX\") && trap '[ -z \"\${retry_tmp:-}\" ] || rm -f \"\$retry_tmp\"' 0 HUP INT TERM && curl -fsSL '%s' -o \"\$retry_tmp\" && sh \"\$retry_tmp\" --version '%s' --method '%s' --no-setup; retry_rc=\$?; [ -z \"\${retry_tmp:-}\" ] || rm -f \"\$retry_tmp\"; trap - 0 HUP INT TERM; (exit \"\$retry_rc\")\n" \
        "$PUBLIC_INSTALLER_URL" "$VERSION" "$INSTALL_METHOD" >&2
}

resolve_version() {
    make_tmp_dir
    if [ "$REQUESTED_VERSION" != latest ]; then
        VERSION=${REQUESTED_VERSION#v}
        TAG="v$VERSION"
    elif [ "$CHANNEL" = stable ]; then
        info "Resolving the latest stable J.A.R.N. release"
        if latest_url=$(curl -fLsS --connect-timeout "$CONNECT_TIMEOUT" --retry 2 \
            -o /dev/null -w '%{url_effective}' "$GITHUB_BASE/$REPO/releases/latest"); then
            :
        else
            curl_status=$?
            warn "$(curl_failure_message "$curl_status")"
            die "could not resolve the latest stable GitHub release (check DNS, proxy, TLS, and network access)"
        fi
        latest_url=${latest_url%/}
        TAG=${latest_url##*/}
        case "$TAG" in v*) VERSION=${TAG#v} ;; *)
            die "GitHub returned an invalid latest-release URL: $latest_url"
        esac
    else
        info "Resolving the latest beta J.A.R.N. release"
        releases_json="$TMP_DIR/releases.json"
        curl_get "$RELEASES_API" "$releases_json" || \
            die "could not query the GitHub releases API for the beta channel"
        TAG=$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$releases_json" | sed -n '1p')
        case "$TAG" in v*) VERSION=${TAG#v} ;; *)
            die "the beta release catalog did not contain a valid v-prefixed tag"
        esac
    fi

    case "$VERSION" in
        ''|*[!0-9A-Za-z._-]*) die "invalid J.A.R.N. version: $VERSION" ;;
    esac
}

select_asset() {
    ASSET=""
    case "$OS-$ARCH" in
        linux-x86_64) ASSET=jarn-linux-x86_64 ;;
        linux-arm64) ASSET=jarn-linux-arm64 ;;
        darwin-arm64) ASSET=jarn-macos-arm64 ;;
        darwin-x86_64) ASSET="" ;;
    esac
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        return 1
    fi
}

smoke_candidate() {
    smoke_path=$1
    if ! smoke_output=$("$smoke_path" --version 2>&1); then
        SMOKE_ERROR=$(printf '%s\n' "$smoke_output" | sed -n '1p')
        return 1
    fi
    if [ "$smoke_output" != "jarn $VERSION" ]; then
        SMOKE_ERROR="reported unexpected version: $smoke_output"
        return 1
    fi
    if ! help_output=$("$smoke_path" --help 2>&1); then
        SMOKE_ERROR=$(printf '%s\n' "$help_output" | sed -n '1p')
        [ -n "$SMOKE_ERROR" ] || SMOKE_ERROR="--help failed"
        return 1
    fi
    return 0
}

prepare_binary() {
    [ -n "$ASSET" ] || return 1
    make_tmp_dir
    release_url="$GITHUB_BASE/$REPO/releases/download/$TAG"
    candidate="$TMP_DIR/$ASSET"
    checksums="$TMP_DIR/checksums.txt"

    info "Downloading verified $ASSET"
    if ! curl_get "$release_url/$ASSET" "$candidate"; then
        warn "release asset $ASSET is unavailable; the prior installation is unchanged"
        show_download_retry
        return 1
    fi
    if ! curl_get "$release_url/checksums.txt" "$checksums"; then
        warn "checksums.txt is unavailable; refusing the unverified binary"
        show_download_retry
        return 1
    fi

    expected=$(awk -v file="$ASSET" '$2 == file || $2 == "*" file {print $1; exit}' "$checksums")
    printf '%s\n' "$expected" | grep -Eq '^[0-9A-Fa-f]{64}$' || \
        die "checksums.txt has no valid SHA-256 entry for $ASSET"
    actual=$(sha256_file "$candidate") || \
        die "sha256sum or shasum is required to verify the release binary"
    expected=$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')
    actual=$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')
    [ "$actual" = "$expected" ] || \
        die "SHA-256 mismatch for $ASSET (expected $expected, got $actual); refusing activation"
    info "SHA-256 verified"

    chmod 755 "$candidate"
    if ! smoke_candidate "$candidate"; then
        warn "release binary cannot run here: $SMOKE_ERROR"
        return 1
    fi
    PREPARED_PATH=$candidate
    PREPARED_METHOD=binary
    return 0
}

find_uv() {
    if [ -n "${JARN_UV_BIN:-}" ]; then
        UV_BIN=$JARN_UV_BIN
    elif command -v uv >/dev/null 2>&1; then
        UV_BIN=$(command -v uv)
    elif [ -x "$HOME/.local/bin/uv" ]; then
        UV_BIN="$HOME/.local/bin/uv"
    else
        UV_BIN=""
    fi
}

run_logged() {
    if [ "$VERBOSE" -eq 1 ]; then
        "$@"
    else
        "$@" >> "$LOG_FILE" 2>&1
    fi
}

bootstrap_uv() {
    make_tmp_dir
    uv_installer="$TMP_DIR/uv-install.sh"
    info "External dependency: uv $UV_VERSION (managed runtime installer)"
    info "  source: $UV_INSTALL_URL"
    info "  destination: $HOME/.local/bin; channel: pinned stable $UV_VERSION"
    curl_get "$UV_INSTALL_URL" "$uv_installer" || \
        die "could not download uv from $UV_INSTALL_URL (prior J.A.R.N. remains unchanged)"
    case "$UV_INSTALL_SHA256" in
        '') die "the pinned uv installer SHA-256 is empty; refusing to execute it" ;;
        *[!0-9A-Fa-f]*) die "the pinned uv installer SHA-256 is invalid; refusing to execute it" ;;
    esac
    [ "${#UV_INSTALL_SHA256}" -eq 64 ] || \
        die "the pinned uv installer SHA-256 is invalid; refusing to execute it"
    uv_installer_digest=$(sha256_file "$uv_installer") || \
        die "no SHA-256 tool is available to verify the uv installer; refusing to execute it"
    uv_expected_digest=$(printf '%s' "$UV_INSTALL_SHA256" | tr 'A-F' 'a-f')
    [ "$uv_installer_digest" = "$uv_expected_digest" ] || \
        die "uv installer SHA-256 mismatch; refusing to execute it (prior J.A.R.N. remains unchanged)"
    info "uv installer SHA-256 verified"
    if ! UV_NO_MODIFY_PATH=1 UV_VERSION="$UV_VERSION" \
        run_logged sh "$uv_installer"; then
        die "uv installation failed; details: $LOG_FILE"
    fi
    find_uv
    [ -n "$UV_BIN" ] || die "uv reported success but its executable was not found"
    UV_OWNED=true
}

prepare_python() {
    find_uv
    [ -n "$UV_BIN" ] || bootstrap_uv

    UV_REPORTED_VERSION=$("$UV_BIN" --version 2>/dev/null | sed -n '1p' || true)
    [ -n "$UV_REPORTED_VERSION" ] || UV_REPORTED_VERSION=unknown
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    reject_symlink_components "$VERSIONS_DIR" "managed versions directory"
    # uv writes absolute launcher symlinks and virtual-environment shebangs.
    # Moving a completed tool root corrupts a candidate that already passed its
    # first smoke check. This unique version directory is therefore both the
    # inactive staging location and its final identity; only INSTALL_DIR/jarn is
    # activated transactionally after verification.
    TOOL_STAGE="$VERSIONS_DIR/python-$VERSION-$timestamp-$$"
    final_tool_root=$TOOL_STAGE
    mkdir -p "$TOOL_STAGE"

    info "Installing jarn==$VERSION in an isolated managed Python 3.12 environment"
    info "  dependency: uv ($UV_REPORTED_VERSION); source: PyPI; destination: $final_tool_root"
    if ! UV_TOOL_DIR="$TOOL_STAGE/tools" UV_TOOL_BIN_DIR="$TOOL_STAGE/bin" \
        run_logged "$UV_BIN" tool install --quiet --no-progress \
            --python 3.12 --managed-python "jarn==$VERSION"; then
        die "isolated Python installation failed; the prior J.A.R.N. is unchanged; details: $LOG_FILE"
    fi

    python_candidate="$TOOL_STAGE/bin/jarn"
    [ -x "$python_candidate" ] || \
        die "uv completed but did not create the staged J.A.R.N. command; details: $LOG_FILE"
    if ! smoke_candidate "$python_candidate"; then
        die "the isolated Python candidate failed verification: $SMOKE_ERROR; prior J.A.R.N. is unchanged"
    fi

    PREPARED_PATH="$final_tool_root/bin/jarn"
    PREPARED_METHOD=python
    TOOL_STAGE=""
}

quarantine_existing_lock() {
    lock_reason=$1
    lock_stamp=$(date -u +%Y%m%dT%H%M%SZ)
    lock_quarantine="$STATE_DIR/install.lock.recovered.$lock_stamp.$$"
    lock_suffix=0
    while [ -e "$lock_quarantine" ] || [ -L "$lock_quarantine" ]; do
        lock_suffix=$((lock_suffix + 1))
        lock_quarantine="$STATE_DIR/install.lock.recovered.$lock_stamp.$$.$lock_suffix"
    done
    mv "$LOCK_DIR" "$lock_quarantine" || \
        die "could not quarantine $lock_reason installer lock; another installer may be racing"
    warn "Recovered $lock_reason installer lock; retained for diagnosis: $lock_quarantine"
}

recover_existing_lock() {
    if [ -L "$LOCK_DIR" ]; then
        quarantine_existing_lock "symbolic-link"
        return
    fi
    if [ ! -d "$LOCK_DIR" ]; then
        quarantine_existing_lock "malformed"
        return
    fi

    lock_pid_file="$LOCK_DIR/pid"
    if [ ! -e "$lock_pid_file" ] && [ ! -L "$lock_pid_file" ]; then
        # A new owner has a very small mkdir→pid publication window. Give it one
        # bounded chance to finish before treating an abandoned directory as
        # malformed crash state.
        sleep 1
        [ -e "$LOCK_DIR" ] || return
    fi
    if [ -L "$lock_pid_file" ] || [ ! -f "$lock_pid_file" ]; then
        quarantine_existing_lock "malformed"
        return
    fi
    lock_pid=$(sed -n '1p' "$lock_pid_file" 2>/dev/null || true)
    lock_pid_lines=$(wc -l < "$lock_pid_file" 2>/dev/null | tr -d ' ' || printf 0)
    case "$lock_pid" in
        ''|0|0*|*[!0-9]*)
            quarantine_existing_lock "malformed"
            return
            ;;
    esac
    if [ "$lock_pid_lines" != 1 ]; then
        quarantine_existing_lock "malformed"
        return
    fi
    if kill -0 "$lock_pid" 2>/dev/null; then
        die "another install/update is active with pid $lock_pid ($LOCK_DIR)"
    fi
    quarantine_existing_lock "stale pid $lock_pid"
}

release_install_lock() {
    [ "$LOCK_HELD" -eq 1 ] || return 0
    if [ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] && \
        [ -f "$LOCK_DIR/pid" ] && [ ! -L "$LOCK_DIR/pid" ]; then
        owned_pid=$(sed -n '1p' "$LOCK_DIR/pid" 2>/dev/null || true)
        if [ "$owned_pid" = "$$" ]; then
            rm -f "$LOCK_DIR/pid" 2>/dev/null || true
            rmdir "$LOCK_DIR" 2>/dev/null || \
                warn "installer lock contains unexpected entries and was retained: $LOCK_DIR"
        else
            warn "installer lock ownership changed; refusing to remove it: $LOCK_DIR"
        fi
    else
        warn "installer lock identity changed; refusing to remove it: $LOCK_DIR"
    fi
    LOCK_HELD=0
}

acquire_lock() {
    reject_symlink_components "$STATE_DIR" "state directory"
    reject_symlink_components "$VERSIONS_DIR" "managed versions directory"
    mkdir -p "$STATE_DIR" "$VERSIONS_DIR"
    reject_symlink_components "$STATE_DIR" "state directory"
    reject_symlink_components "$VERSIONS_DIR" "managed versions directory"
    chmod 700 "$STATE_DIR" 2>/dev/null || true
    LOCK_DIR="$STATE_DIR/install.lock"
    lock_attempt=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        lock_attempt=$((lock_attempt + 1))
        [ "$lock_attempt" -le 3 ] || \
            die "could not acquire installer lock after crash-state recovery attempts"
        recover_existing_lock
    done
    lock_pid_tmp="$LOCK_DIR/.pid.$$"
    if ! printf '%s\n' "$$" > "$lock_pid_tmp" || \
        ! chmod 600 "$lock_pid_tmp" 2>/dev/null || \
        ! mv "$lock_pid_tmp" "$LOCK_DIR/pid"; then
        rm -f "$lock_pid_tmp" 2>/dev/null || true
        rmdir "$LOCK_DIR" 2>/dev/null || true
        die "could not publish installer lock ownership"
    fi
    LOCK_HELD=1
    LOG_FILE="$STATE_DIR/install-$VERSION-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
    : > "$LOG_FILE"
    chmod 600 "$LOG_FILE" 2>/dev/null || true
}

private_install_path() {
    private_path=$1
    private_prefix=$2
    [ "${private_path%/*}" = "$INSTALL_DIR" ] || return 1
    private_name=${private_path##*/}
    case "$private_name" in
        "$private_prefix"*) ;;
        *) return 1 ;;
    esac
    case "$private_name" in
        *[!0-9A-Za-z._-]*) return 1 ;;
    esac
    return 0
}

quarantine_transaction_journal() {
    journal_reason=$1
    journal_stamp=$(date -u +%Y%m%dT%H%M%SZ)
    journal_quarantine="$STATE_DIR/install.transaction.recovered.$journal_stamp.$$"
    journal_suffix=0
    while [ -e "$journal_quarantine" ] || [ -L "$journal_quarantine" ]; do
        journal_suffix=$((journal_suffix + 1))
        journal_quarantine="$STATE_DIR/install.transaction.recovered.$journal_stamp.$$.$journal_suffix"
    done
    mv "$TRANSACTION_JOURNAL" "$journal_quarantine" || \
        die "could not quarantine $journal_reason install transaction journal"
    warn "Recovered $journal_reason install transaction journal; retained: $journal_quarantine"
}

retain_interrupted_path() {
    interrupted_path=$1
    interrupted_label=$2
    [ -e "$interrupted_path" ] || [ -L "$interrupted_path" ] || return 0
    interrupted_stamp=$(date -u +%Y%m%dT%H%M%SZ)
    interrupted_name=${interrupted_path##*/}
    interrupted_target="$INSTALL_DIR/.jarn.failed.interrupted.$interrupted_name.$interrupted_stamp.$$"
    interrupted_suffix=0
    while [ -e "$interrupted_target" ] || [ -L "$interrupted_target" ]; do
        interrupted_suffix=$((interrupted_suffix + 1))
        interrupted_target="$INSTALL_DIR/.jarn.failed.interrupted.$interrupted_name.$interrupted_stamp.$$.$interrupted_suffix"
    done
    mv "$interrupted_path" "$interrupted_target" || \
        die "could not retain interrupted $interrupted_label at $interrupted_target"
    warn "Interrupted $interrupted_label retained for diagnosis: $interrupted_target"
}

reconcile_orphan_activation_files() {
    for orphan_stage in "$INSTALL_DIR"/.jarn.activate.*; do
        if [ -e "$orphan_stage" ] || [ -L "$orphan_stage" ]; then
            retain_interrupted_path "$orphan_stage" "activation candidate"
        fi
    done
}

restore_single_orphan_rollback() {
    if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
        return 0
    fi
    orphan_count=0
    orphan_previous=""
    for rollback_path in "$INSTALL_DIR"/.jarn.rollback.*; do
        if [ -e "$rollback_path" ] || [ -L "$rollback_path" ]; then
            orphan_count=$((orphan_count + 1))
            orphan_previous=$rollback_path
        fi
    done
    case "$orphan_count" in
        0) return 0 ;;
        1)
            mv "$orphan_previous" "$BIN_PATH" || \
                die "could not restore the sole retained executable after interruption"
            warn "Restored interrupted prior executable to $BIN_PATH"
            ;;
        *)
            die "active command is missing and multiple rollback candidates exist; refusing to guess"
            ;;
    esac
}

write_transaction_journal() {
    journal_phase=$1
    [ ! -L "$TRANSACTION_JOURNAL" ] || \
        die "refusing to replace a symbolic-link install transaction journal"
    TRANSACTION_JOURNAL_TMP="$STATE_DIR/.install.transaction.$$"
    {
        printf '%s\n' 'schema=1'
        printf 'pid=%s\n' "$$"
        printf 'version=%s\n' "$VERSION"
        printf 'phase=%s\n' "$journal_phase"
        printf 'active_path=%s\n' "$BIN_PATH"
        printf 'staged_path=%s\n' "$STAGED_PATH"
        printf 'had_previous=%s\n' "$HAD_PREVIOUS"
        printf 'previous_path=%s\n' "$PREVIOUS_PATH"
    } > "$TRANSACTION_JOURNAL_TMP" || return 1
    chmod 600 "$TRANSACTION_JOURNAL_TMP" 2>/dev/null || return 1
    mv "$TRANSACTION_JOURNAL_TMP" "$TRANSACTION_JOURNAL" || return 1
    TRANSACTION_JOURNAL_TMP=""
    return 0
}

remove_own_transaction_journal() {
    if [ ! -e "$TRANSACTION_JOURNAL" ] && [ ! -L "$TRANSACTION_JOURNAL" ]; then
        return 0
    fi
    [ -f "$TRANSACTION_JOURNAL" ] && [ ! -L "$TRANSACTION_JOURNAL" ] || return 1
    journal_owner=$(sed -n 's/^pid=//p' "$TRANSACTION_JOURNAL" | sed -n '1p')
    [ "$journal_owner" = "$$" ] || return 1
    rm -f "$TRANSACTION_JOURNAL"
}

reconcile_interrupted_transaction() {
    if [ ! -e "$TRANSACTION_JOURNAL" ] && [ ! -L "$TRANSACTION_JOURNAL" ]; then
        reconcile_orphan_activation_files
        restore_single_orphan_rollback
        return 0
    fi
    if [ -L "$TRANSACTION_JOURNAL" ] || [ ! -f "$TRANSACTION_JOURNAL" ]; then
        quarantine_transaction_journal "malformed"
        reconcile_orphan_activation_files
        restore_single_orphan_rollback
        return 0
    fi

    journal_schema=$(sed -n 's/^schema=//p' "$TRANSACTION_JOURNAL")
    journal_pid=$(sed -n 's/^pid=//p' "$TRANSACTION_JOURNAL")
    journal_version=$(sed -n 's/^version=//p' "$TRANSACTION_JOURNAL")
    journal_phase=$(sed -n 's/^phase=//p' "$TRANSACTION_JOURNAL")
    journal_active=$(sed -n 's/^active_path=//p' "$TRANSACTION_JOURNAL")
    journal_staged=$(sed -n 's/^staged_path=//p' "$TRANSACTION_JOURNAL")
    journal_had_previous=$(sed -n 's/^had_previous=//p' "$TRANSACTION_JOURNAL")
    journal_previous=$(sed -n 's/^previous_path=//p' "$TRANSACTION_JOURNAL")

    journal_valid=1
    [ "$journal_schema" = 1 ] || journal_valid=0
    case "$journal_pid" in ''|0|0*|*[!0-9]*) journal_valid=0 ;; esac
    case "$journal_version" in ''|*[!0-9A-Za-z._-]*) journal_valid=0 ;; esac
    case "$journal_phase" in
        prepared|activating)
            private_install_path "$journal_staged" ".jarn.activate." || journal_valid=0
            ;;
        activated)
            # The staged name has already become BIN_PATH by this phase. Older
            # journal writers may still retain the private staging name, so
            # accept either representation but nothing outside our namespace.
            if [ -n "$journal_staged" ]; then
                private_install_path "$journal_staged" ".jarn.activate." || journal_valid=0
            fi
            ;;
        *) journal_valid=0 ;;
    esac
    [ "$journal_active" = "$BIN_PATH" ] || journal_valid=0
    case "$journal_had_previous" in
        0) [ -z "$journal_previous" ] || journal_valid=0 ;;
        1) private_install_path "$journal_previous" ".jarn.rollback." || journal_valid=0 ;;
        *) journal_valid=0 ;;
    esac
    if [ "$journal_valid" -ne 1 ]; then
        quarantine_transaction_journal "malformed"
        reconcile_orphan_activation_files
        restore_single_orphan_rollback
        return 0
    fi
    if kill -0 "$journal_pid" 2>/dev/null; then
        die "an install transaction journal still belongs to live pid $journal_pid"
    fi

    journal_active_ok=0
    if [ -x "$BIN_PATH" ]; then
        journal_active_version=$("$BIN_PATH" --version 2>/dev/null || true)
        if [ "$journal_active_version" = "jarn $journal_version" ] && \
            "$BIN_PATH" --help >/dev/null 2>&1; then
            journal_active_ok=1
        fi
    fi

    if [ "$journal_phase" = prepared ]; then
        journal_previous_present=0
        if [ -e "$journal_previous" ] || [ -L "$journal_previous" ]; then
            journal_previous_present=1
        fi
        if [ "$journal_had_previous" -eq 1 ]; then
            if [ "$journal_previous_present" -eq 0 ]; then
                [ "$journal_active_ok" -eq 0 ] || \
                    die "interrupted activation lost its retained prior executable"
                [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ] || \
                    die "interrupted activation lost both active and prior executables"
                warn "Recovered install interrupted before retaining the prior executable"
                if [ -e "$journal_staged" ] || [ -L "$journal_staged" ]; then
                    retain_interrupted_path "$journal_staged" "activation candidate"
                fi
                rm -f "$TRANSACTION_JOURNAL" || \
                    die "could not clear reconciled install transaction journal"
                reconcile_orphan_activation_files
                return 0
            fi
            if [ "$journal_active_ok" -eq 0 ] && \
                { [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; }; then
                die "prepared install journal has both a prior candidate and an unexpected active command"
            fi
        elif [ "$journal_active_ok" -eq 0 ]; then
            [ ! -e "$BIN_PATH" ] && [ ! -L "$BIN_PATH" ] || \
                die "prepared fresh install journal has an unexpected active command"
            warn "Recovered fresh install interrupted before activation"
            if [ -e "$journal_staged" ] || [ -L "$journal_staged" ]; then
                retain_interrupted_path "$journal_staged" "activation candidate"
            fi
            rm -f "$TRANSACTION_JOURNAL" || \
                die "could not clear reconciled install transaction journal"
            reconcile_orphan_activation_files
            return 0
        fi
    fi

    if [ "$journal_active_ok" -eq 1 ]; then
        if [ "$journal_version" = "$VERSION" ] && [ "$journal_had_previous" -eq 1 ]; then
            [ -e "$journal_previous" ] || [ -L "$journal_previous" ] || \
                die "interrupted install lost its retained prior executable: $journal_previous"
            PREVIOUS_PATH=$journal_previous
            HAD_PREVIOUS=1
        fi
        warn "Recovered verified J.A.R.N. $journal_version from interrupted activation"
    else
        if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
            retain_interrupted_path "$BIN_PATH" "active candidate"
        fi
        if [ "$journal_had_previous" -eq 1 ]; then
            [ -e "$journal_previous" ] || [ -L "$journal_previous" ] || \
                die "interrupted install cannot restore missing prior executable: $journal_previous"
            mv "$journal_previous" "$BIN_PATH" || \
                die "could not restore prior executable after interrupted activation"
            warn "Restored prior executable after interrupted activation"
        fi
    fi

    if [ -e "$journal_staged" ] || [ -L "$journal_staged" ]; then
        retain_interrupted_path "$journal_staged" "activation candidate"
    fi
    rm -f "$TRANSACTION_JOURNAL" || \
        die "could not clear reconciled install transaction journal"
    reconcile_orphan_activation_files
}

begin_install_transaction() {
    [ "$TRANSACTION_ACTIVE" -eq 0 ] || return 0
    reject_symlink_components "$INSTALL_DIR" "installation directory"
    reject_symlink_components "$STATE_DIR" "state directory"
    make_tmp_dir
    TRANSACTION_BACKUP_DIR="$TMP_DIR/transaction"
    PROFILE_BACKUP_DIR="$TRANSACTION_BACKUP_DIR/profiles"
    mkdir -p "$PROFILE_BACKUP_DIR" || \
        die "could not create the install transaction journal"

    if [ -e "$MANIFEST_PATH" ] || [ -L "$MANIFEST_PATH" ]; then
        cp -p "$MANIFEST_PATH" "$TRANSACTION_BACKUP_DIR/install.json" || \
            die "could not back up existing install metadata"
        METADATA_MANIFEST_EXISTED=1
    fi
    if [ -e "$LEGACY_RECORD" ] || [ -L "$LEGACY_RECORD" ]; then
        cp -p "$LEGACY_RECORD" "$TRANSACTION_BACKUP_DIR/install-method" || \
            die "could not back up existing install method metadata"
        METADATA_LEGACY_EXISTED=1
    fi

    TRANSACTION_ACTIVE=1
    TRANSACTION_COMMITTED=0
}

backup_profile_for_transaction() {
    backup_profile_path=$1
    backup_profile_index=1
    while [ "$backup_profile_index" -le "$PROFILE_BACKUP_COUNT" ]; do
        backup_profile_item="$PROFILE_BACKUP_DIR/$backup_profile_index"
        saved_profile_path=$(sed -n '1p' "$backup_profile_item/path" 2>/dev/null || true)
        [ "$saved_profile_path" != "$backup_profile_path" ] || return 0
        backup_profile_index=$((backup_profile_index + 1))
    done

    PROFILE_BACKUP_COUNT=$((PROFILE_BACKUP_COUNT + 1))
    backup_profile_item="$PROFILE_BACKUP_DIR/$PROFILE_BACKUP_COUNT"
    mkdir -p "$backup_profile_item" || \
        die "could not journal shell profile before editing: $backup_profile_path"
    printf '%s\n' "$backup_profile_path" > "$backup_profile_item/path" || \
        die "could not journal shell profile path: $backup_profile_path"
    if [ -e "$backup_profile_path" ] || [ -L "$backup_profile_path" ]; then
        cp -p "$backup_profile_path" "$backup_profile_item/content" || \
            die "could not back up shell profile before editing: $backup_profile_path"
        printf '%s\n' 1 > "$backup_profile_item/existed"
    else
        printf '%s\n' 0 > "$backup_profile_item/existed"
    fi
}

restore_tracked_file() {
    restore_target=$1
    restore_backup=$2
    restore_existed=$3
    restore_label=$4
    if [ "$restore_existed" -eq 1 ]; then
        restore_tmp="$restore_target.jarn-restore.$$"
        if cp -p "$restore_backup" "$restore_tmp" 2>/dev/null && \
            mv "$restore_tmp" "$restore_target" 2>/dev/null; then
            :
        else
            rm -f "$restore_tmp" 2>/dev/null || true
            warn "automatic rollback could not restore $restore_label: $restore_target"
        fi
    elif [ -e "$restore_target" ] || [ -L "$restore_target" ]; then
        rm -f "$restore_target" 2>/dev/null || \
            warn "automatic rollback could not remove newly-created $restore_label: $restore_target"
    fi
}

restore_profile_backups() {
    restore_profile_index=$PROFILE_BACKUP_COUNT
    while [ "$restore_profile_index" -gt 0 ]; do
        restore_profile_item="$PROFILE_BACKUP_DIR/$restore_profile_index"
        restore_profile_path=$(sed -n '1p' "$restore_profile_item/path" 2>/dev/null || true)
        restore_profile_existed=$(sed -n '1p' "$restore_profile_item/existed" 2>/dev/null || printf 0)
        if [ -n "$restore_profile_path" ]; then
            restore_tracked_file "$restore_profile_path" \
                "$restore_profile_item/content" "$restore_profile_existed" "shell profile"
        fi
        restore_profile_index=$((restore_profile_index - 1))
    done
}

restore_metadata_backups() {
    restore_tracked_file "$MANIFEST_PATH" \
        "$TRANSACTION_BACKUP_DIR/install.json" "$METADATA_MANIFEST_EXISTED" \
        "install metadata"
    restore_tracked_file "$LEGACY_RECORD" \
        "$TRANSACTION_BACKUP_DIR/install-method" "$METADATA_LEGACY_EXISTED" \
        "install method metadata"
}

rollback_install_transaction() {
    # Disable recursion before attempting best-effort recovery: any individual
    # restoration warning must not trigger a second rollback pass at exit.
    TRANSACTION_ACTIVE=0
    if [ "$INSTALL_CHANGED" -eq 1 ]; then
        rollback_activation \
            "installation transaction did not commit; the prior executable was restored"
    fi
    restore_profile_backups
    restore_metadata_backups
    if ! remove_own_transaction_journal; then
        warn "automatic rollback retained an install transaction journal for diagnosis"
    fi
}

commit_install_transaction() {
    remove_own_transaction_journal || return 1
    TRANSACTION_COMMITTED=1
    TRANSACTION_ACTIVE=0
}

shell_single_quote() {
    escaped=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
    printf "'%s'" "$escaped"
}

append_profile_block() {
    profile_path=$1
    export_line=$2
    marker_start="# >>> J.A.R.N. managed PATH >>>"
    marker_end="# <<< J.A.R.N. managed PATH <<<"
    profile_dir=$(dirname "$profile_path")
    mkdir -p "$profile_dir" || die "could not create shell profile directory: $profile_dir"
    if [ -f "$profile_path" ] && grep -F "$marker_start" "$profile_path" >/dev/null 2>&1; then
        return
    fi
    backup_profile_for_transaction "$profile_path"
    PROFILE_TMP_PATH="$profile_path.jarn-tmp.$$"
    if [ -f "$profile_path" ]; then
        cp -p "$profile_path" "$PROFILE_TMP_PATH" || \
            die "could not stage shell profile: $profile_path"
    else
        (umask 077 && : > "$PROFILE_TMP_PATH") || \
            die "could not create shell profile: $profile_path"
    fi
    {
        printf '\n%s\n' "$marker_start"
        printf '%s\n' "$export_line"
        printf '%s\n' "$marker_end"
    } >> "$PROFILE_TMP_PATH" || die "could not update staged shell profile: $profile_path"
    PROFILE_ACTIVATION_ATTEMPT=$((PROFILE_ACTIVATION_ATTEMPT + 1))
    if [ "${JARN_TEST_FAIL_PROFILE_ACTIVATION_AT:-}" = "$PROFILE_ACTIVATION_ATTEMPT" ]; then
        die "injected shell profile activation failure: $profile_path"
    fi
    mv "$PROFILE_TMP_PATH" "$profile_path" || \
        die "could not activate shell profile: $profile_path"
    PROFILE_TMP_PATH=""
    PROFILE_UPDATES="${PROFILE_UPDATES}${PROFILE_UPDATES:+, }$profile_path"
}

ensure_shell_profiles() {
    quoted_dir=$(shell_single_quote "$INSTALL_DIR")
    posix_export="export PATH=$quoted_dir:\"\$PATH\""
    case "$SHELL_NAME" in
        bash)
            if [ -f "$HOME/.bash_profile" ]; then
                login_profile="$HOME/.bash_profile"
            elif [ -f "$HOME/.bash_login" ]; then
                login_profile="$HOME/.bash_login"
            else
                login_profile="$HOME/.profile"
            fi
            append_profile_block "$login_profile" "$posix_export"
            append_profile_block "$HOME/.bashrc" "$posix_export"
            ;;
        zsh)
            append_profile_block "$HOME/.zprofile" "$posix_export"
            append_profile_block "$HOME/.zshrc" "$posix_export"
            ;;
        fish)
            fish_dir=$(printf '%s' "$INSTALL_DIR" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\$/\\$/g')
            append_profile_block "$HOME/.config/fish/conf.d/jarn.fish" \
                "set -gx PATH \"$fish_dir\" \$PATH"
            ;;
        *)
            append_profile_block "$HOME/.profile" "$posix_export"
            ;;
    esac
}

activate_prepared() {
    reject_symlink_components "$INSTALL_DIR" "installation directory"
    mkdir -p "$INSTALL_DIR"
    reject_symlink_components "$INSTALL_DIR" "installation directory"
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    STAGED_PATH="$INSTALL_DIR/.jarn.activate.$VERSION.$timestamp.$$"
    if [ "$PREPARED_METHOD" = binary ]; then
        cp "$PREPARED_PATH" "$STAGED_PATH" || die "could not stage the verified binary in $INSTALL_DIR"
        chmod 755 "$STAGED_PATH"
    else
        ln -s "$PREPARED_PATH" "$STAGED_PATH" || \
            die "could not stage the isolated Python command in $INSTALL_DIR"
    fi

    if ! smoke_candidate "$STAGED_PATH"; then
        die "the staged command failed verification before activation: $SMOKE_ERROR"
    fi

    PREVIOUS_PATH=""
    HAD_PREVIOUS=0
    if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
        HAD_PREVIOUS=1
        previous_label=$("$BIN_PATH" --version 2>/dev/null | sed -n 's/^jarn //p' | sed -n '1p' || true)
        [ -n "$previous_label" ] || previous_label=unknown
        previous_label=$(printf '%s' "$previous_label" | sed 's/[^0-9A-Za-z._-]/_/g')
        PREVIOUS_PATH="$INSTALL_DIR/.jarn.rollback.$previous_label.$timestamp.$$"
    fi

    # Publish the intended paths before the first rename. A SIGKILL can then be
    # reconciled whether it lands before or after retaining the prior command.
    write_transaction_journal prepared || \
        die "could not persist the install transaction journal before activation"
    ACTIVATION_IN_PROGRESS=1
    if [ "$HAD_PREVIOUS" -eq 1 ]; then
        mv "$BIN_PATH" "$PREVIOUS_PATH" || {
            ACTIVATION_IN_PROGRESS=0
            die "could not retain the previous executable at $PREVIOUS_PATH"
        }
    fi

    write_transaction_journal activating || \
        die "could not persist retained activation state"

    if ! mv "$STAGED_PATH" "$BIN_PATH"; then
        if [ -n "$PREVIOUS_PATH" ] && { [ -e "$PREVIOUS_PATH" ] || [ -L "$PREVIOUS_PATH" ]; }; then
            mv "$PREVIOUS_PATH" "$BIN_PATH" 2>/dev/null || true
        fi
        ACTIVATION_IN_PROGRESS=0
        die "atomic activation failed; attempted to restore the previous executable"
    fi
    STAGED_PATH=""
    INSTALL_CHANGED=1

    if ! smoke_candidate "$BIN_PATH"; then
        rollback_activation "activated command failed verification: $SMOKE_ERROR"
        die "activation verification failed; the prior executable was restored"
    fi
    write_transaction_journal activated || \
        die "could not persist activated transaction state"
    ACTIVATION_IN_PROGRESS=0
}

rollback_activation() {
    rollback_reason=$1
    rollback_stamp=$(date -u +%Y%m%dT%H%M%SZ)
    FAILED_PATH="$INSTALL_DIR/.jarn.failed.$VERSION.$rollback_stamp.$$"
    if [ -e "$BIN_PATH" ] || [ -L "$BIN_PATH" ]; then
        mv "$BIN_PATH" "$FAILED_PATH" 2>/dev/null || true
    fi
    if [ -n "$PREVIOUS_PATH" ] && { [ -e "$PREVIOUS_PATH" ] || [ -L "$PREVIOUS_PATH" ]; }; then
        mv "$PREVIOUS_PATH" "$BIN_PATH" 2>/dev/null || \
            warn "automatic rollback could not restore $PREVIOUS_PATH to $BIN_PATH"
    fi
    warn "$rollback_reason"
    [ -n "$FAILED_PATH" ] && warn "failed candidate retained for diagnosis: $FAILED_PATH"
    INSTALL_CHANGED=0
    ACTIVATION_IN_PROGRESS=0
}

resolve_in_shell() {
    shell_mode=$1
    shell_output="$TMP_DIR/resolve-$shell_mode"
    case "$SHELL_NAME:$shell_mode" in
        fish:login)
            "$SHELL_PATH" -lc 'set r (command -s jarn 2>/dev/null); printf "__JARN_RESOLVE__%s\n" "$r"' \
                > "$shell_output" 2>/dev/null || true
            ;;
        fish:interactive)
            "$SHELL_PATH" -ic 'set r (command -s jarn 2>/dev/null); printf "__JARN_RESOLVE__%s\n" "$r"' \
                > "$shell_output" 2>/dev/null || true
            ;;
        *:login)
            "$SHELL_PATH" -lic 'r=$(command -v jarn 2>/dev/null || true); printf "__JARN_RESOLVE__%s\n" "$r"' \
                > "$shell_output" 2>/dev/null || true
            ;;
        *:interactive)
            "$SHELL_PATH" -ic 'r=$(command -v jarn 2>/dev/null || true); printf "__JARN_RESOLVE__%s\n" "$r"' \
                > "$shell_output" 2>/dev/null || true
            ;;
    esac
    sed -n 's/^__JARN_RESOLVE__//p' "$shell_output" | sed -n '$p'
}

verify_user_resolution() {
    CURRENT_RESOLUTION=$(command -v jarn 2>/dev/null || true)
    LOGIN_RESOLUTION=$(resolve_in_shell login)
    INTERACTIVE_RESOLUTION=$(resolve_in_shell interactive)

    if [ "$LOGIN_RESOLUTION" != "$BIN_PATH" ] || \
        [ "$INTERACTIVE_RESOLUTION" != "$BIN_PATH" ]; then
        warn "the user's shell would not invoke the installed executable"
        warn "installed path: $BIN_PATH"
        warn "login-shell resolution: ${LOGIN_RESOLUTION:-not found}"
        warn "interactive-shell resolution: ${INTERACTIVE_RESOLUTION:-not found}"
        warn "J.A.R.N. did not remove aliases, functions, npm, pip, pipx, uv, Homebrew, or system installs"
        if [ "$INSTALL_CHANGED" -eq 1 ]; then
            rollback_activation "shell-resolution verification failed; prior executable restored"
        fi
        return 1
    fi

    if [ "$INSTALL_CHANGED" -eq 1 ]; then
        ACTIVATION_STATUS=required
    elif [ "$CURRENT_RESOLUTION" = "$BIN_PATH" ]; then
        ACTIVATION_STATUS=active
    else
        ACTIVATION_STATUS=required
    fi
    return 0
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\r/\\r/g'
}

write_metadata() {
    reject_symlink_components "$INSTALL_DIR" "installation directory"
    reject_symlink_components "$STATE_DIR" "state directory"
    MANIFEST_TMP_PATH="$MANIFEST_PATH.tmp.$$"
    previous_json=null
    if [ -n "$PREVIOUS_PATH" ]; then
        previous_json="\"$(json_escape "$PREVIOUS_PATH")\""
    fi
    receipt_candidate=${PREPARED_PATH:-$BIN_PATH}
    case "$INSTALL_RESULT" in
        binary|existing) receipt_candidate=$BIN_PATH ;;
    esac
    {
        printf '%s\n' '{'
        printf '  "schema_version": 1,\n'
        printf '  "version": "%s",\n' "$(json_escape "$VERSION")"
        printf '  "method": "%s",\n' "$(json_escape "$INSTALL_RESULT")"
        printf '  "channel": "%s",\n' "$(json_escape "$CHANNEL")"
        printf '  "active_path": "%s",\n' "$(json_escape "$BIN_PATH")"
        printf '  "candidate_path": "%s",\n' "$(json_escape "$receipt_candidate")"
        printf '  "previous_path": %s,\n' "$previous_json"
        printf '  "state_dir": "%s",\n' "$(json_escape "$STATE_DIR")"
        printf '  "platform": {"os": "%s", "distribution": "%s", "distribution_version": "%s", "architecture": "%s", "libc": "%s", "libc_version": "%s", "wsl": %s},\n' \
            "$(json_escape "$OS")" "$(json_escape "$DISTRO_ID")" \
            "$(json_escape "$DISTRO_VERSION")" "$(json_escape "$ARCH")" \
            "$(json_escape "$LIBC_NAME")" "$(json_escape "$LIBC_VERSION")" "$WSL"
        printf '  "dependency": {"uv_path": "%s", "uv_version": "%s", "uv_owned_by_jarn": %s},\n' \
            "$(json_escape "$UV_BIN")" "$(json_escape "$UV_REPORTED_VERSION")" "$UV_OWNED"
        printf '  "activation": {"status": "%s", "current_resolution": "%s", "login_resolution": "%s", "interactive_resolution": "%s", "profiles_updated": "%s"},\n' \
            "$(json_escape "$ACTIVATION_STATUS")" "$(json_escape "$CURRENT_RESOLUTION")" \
            "$(json_escape "$LOGIN_RESOLUTION")" "$(json_escape "$INTERACTIVE_RESOLUTION")" \
            "$(json_escape "$PROFILE_UPDATES")"
        printf '  "setup_status": "%s",\n' "$(json_escape "$SETUP_STATUS")"
        printf '  "installed_at": "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '%s\n' '}'
    } > "$MANIFEST_TMP_PATH" || return 1
    chmod 600 "$MANIFEST_TMP_PATH" 2>/dev/null || true
    [ "${JARN_TEST_FAIL_METADATA_AT:-}" != manifest ] || return 1
    mv "$MANIFEST_TMP_PATH" "$MANIFEST_PATH" || return 1
    MANIFEST_TMP_PATH=""

    LEGACY_TMP_PATH="$LEGACY_RECORD.tmp.$$"
    printf '%s %s\n' "$INSTALL_RESULT" "$VERSION" > "$LEGACY_TMP_PATH" || return 1
    [ "${JARN_TEST_FAIL_METADATA_AT:-}" != legacy ] || return 1
    mv "$LEGACY_TMP_PATH" "$LEGACY_RECORD" || return 1
    LEGACY_TMP_PATH=""
    return 0
}

validate_emitted_receipt() {
    [ -f "$MANIFEST_PATH" ] && [ ! -L "$MANIFEST_PATH" ] || return 1
    receipt_candidate=${PREPARED_PATH:-$BIN_PATH}
    case "$INSTALL_RESULT" in
        binary|existing) receipt_candidate=$BIN_PATH ;;
    esac
    escaped_version=$(json_escape "$VERSION")
    escaped_method=$(json_escape "$INSTALL_RESULT")
    escaped_active=$(json_escape "$BIN_PATH")
    escaped_candidate=$(json_escape "$receipt_candidate")
    escaped_state=$(json_escape "$STATE_DIR")
    escaped_manifest=$(json_escape "$MANIFEST_PATH")

    [ "$(grep -F -c '"schema_version": 1' "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    [ "$(grep -F -c "\"version\": \"$escaped_version\"" "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    [ "$(grep -F -c "\"method\": \"$escaped_method\"" "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    [ "$(grep -F -c "\"active_path\": \"$escaped_active\"" "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    [ "$(grep -F -c "\"candidate_path\": \"$escaped_candidate\"" "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    [ "$(grep -F -c "\"state_dir\": \"$escaped_state\"" "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    if [ -n "$PREVIOUS_PATH" ]; then
        escaped_previous=$(json_escape "$PREVIOUS_PATH")
        [ "$(grep -F -c "\"previous_path\": \"$escaped_previous\"" "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    else
        [ "$(grep -F -c '"previous_path": null' "$MANIFEST_PATH" || true)" -eq 1 ] || return 1
    fi
    smoke_candidate "$BIN_PATH" || return 1
    receipt_before=$(sha256_file "$MANIFEST_PATH") || return 1

    RECEIPT_REPORT="$TMP_DIR/receipt-validation.json"
    set +e
    JARN_INSTALL_RECEIPT_VALIDATION=1 PATH="$INSTALL_DIR:$PATH" \
        "$BIN_PATH" doctor --json > "$RECEIPT_REPORT" 2>> "$LOG_FILE"
    receipt_status=$?
    set -e
    case "$receipt_status" in 0|1) ;; *) return 1 ;; esac
    receipt_after=$(sha256_file "$MANIFEST_PATH") || return 1
    [ "$receipt_after" = "$receipt_before" ] || return 1
    [ -f "$RECEIPT_REPORT" ] && [ ! -L "$RECEIPT_REPORT" ] || return 1
    receipt_size=$(wc -c < "$RECEIPT_REPORT" | tr -d ' ')
    case "$receipt_size" in ''|*[!0-9]*) return 1 ;; esac
    [ "$receipt_size" -le 2097152 ] || return 1
    grep -F '"metadata_present": true' "$RECEIPT_REPORT" >/dev/null || return 1
    grep -F '"metadata_source": "canonical-install-record"' "$RECEIPT_REPORT" >/dev/null || return 1
    grep -F "\"metadata_path\": \"$escaped_manifest\"" "$RECEIPT_REPORT" >/dev/null || return 1
    grep -F "\"version\": \"$escaped_version\"" "$RECEIPT_REPORT" >/dev/null || return 1
    grep -F '"active_matches_record": true' "$RECEIPT_REPORT" >/dev/null || return 1
    ! grep -F '"canonical_record_error":' "$RECEIPT_REPORT" >/dev/null || return 1
    return 0
}

existing_install_method() {
    existing_method=""
    if [ -f "$MANIFEST_PATH" ]; then
        existing_method=$(sed -n \
            's/^[[:space:]]*"method"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$MANIFEST_PATH" | sed -n '1p')
    fi
    if [ -z "$existing_method" ] && [ -f "$LEGACY_RECORD" ]; then
        existing_method=$(awk 'NR == 1 {print $1}' "$LEGACY_RECORD")
    fi
    case "$existing_method" in
        binary|python) printf '%s\n' "$existing_method" ;;
        *) printf '%s\n' existing ;;
    esac
}

adopt_manifest_previous_path() {
    [ -e "$MANIFEST_PATH" ] || [ -L "$MANIFEST_PATH" ] || return 0
    [ -f "$MANIFEST_PATH" ] && [ ! -L "$MANIFEST_PATH" ] || \
        die "existing install metadata is not a regular non-symlink file: $MANIFEST_PATH"
    previous_lines=$(sed -n \
        's/^[[:space:]]*"previous_path"[[:space:]]*:[[:space:]]*\(.*\),[[:space:]]*$/\1/p' \
        "$MANIFEST_PATH")
    [ "$(printf '%s\n' "$previous_lines" | sed '/^$/d' | wc -l | tr -d ' ')" -le 1 ] || \
        die "existing install metadata has duplicate previous_path fields"
    case "$previous_lines" in
        ''|null) return 0 ;;
        \"*\") ;;
        *) die "existing install metadata has an invalid previous_path field" ;;
    esac
    manifest_previous=$(printf '%s' "$previous_lines" | \
        sed 's/^"//; s/"$//; s/\\"/"/g; s/\\\\/\\/g')
    private_install_path "$manifest_previous" ".jarn.rollback." || \
        die "existing install metadata points outside the managed rollback namespace"
    if [ -e "$manifest_previous" ] || [ -L "$manifest_previous" ]; then
        PREVIOUS_PATH=$manifest_previous
        HAD_PREVIOUS=1
    else
        warn "Recorded rollback candidate is missing and will not be advertised: $manifest_previous"
    fi
}

prepare_setup() {
    jarn_home=${JARN_HOME:-$HOME/.jarn}
    SETUP_ACTION=run
    case "$RUN_SETUP" in
        never)
            SETUP_STATUS=skipped
            SETUP_ACTION=skip
            return 0
            ;;
        auto)
            if [ -f "$jarn_home/config.yaml" ]; then
                # Existence alone is not readiness: the file may be corrupt,
                # its credential expired, or a routed model removed. Persist a
                # pending receipt first, then let the activated candidate run
                # the same bounded auth/catalog/route checks as normal doctor.
                SETUP_STATUS=pending
                SETUP_ACTION=verify
                return 0
            fi
            ;;
    esac

    if [ "${JARN_TEST_FORCE_INTERACTIVE_SETUP:-0}" = 1 ] || ( : </dev/tty ) 2>/dev/null; then
        SETUP_STATUS=pending
        return 0
    fi

    SETUP_STATUS=required
    SETUP_ACTION=required
    return 0
}

manifest_setup_status() {
    [ -f "$MANIFEST_PATH" ] || return 1
    sed -n \
        's/.*"setup_status"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$MANIFEST_PATH" | sed -n '1p'
}

run_setup() {
    case "$SETUP_ACTION" in
        skip)
            return 0
            ;;
        verify)
            info "Verifying the existing configuration, authentication, and model routes"
            if PATH="$INSTALL_DIR:$PATH" "$BIN_PATH" doctor --json >/dev/null 2>&1; then
                SETUP_STATUS=existing
                if write_metadata; then
                    return 0
                fi
                SETUP_STATUS=failed
                warn "Setup incomplete: readiness passed but its install status could not be persisted"
                warn "diagnose with: $BIN_PATH doctor --json"
                return 1
            fi
            SETUP_STATUS=failed
            if ! write_metadata; then
                warn "Setup incomplete: existing readiness and status persistence both failed"
            else
                warn "Setup incomplete: the existing config/auth/model route is not ready"
            fi
            warn "diagnose with: $BIN_PATH doctor --json"
            warn "repair safely with: $BIN_PATH doctor --fix --dry-run"
            return 1
            ;;
        required)
            warn "Setup incomplete: no interactive terminal is available"
            warn "resume with: $BIN_PATH setup"
            return 1
            ;;
    esac

    info "Starting first-time setup"
    # Setup's final identity gate checks the command an ordinary child process
    # resolves, not merely argv[0]. Prefix the just-activated directory for this
    # ceremony so a missing parent PATH or preserved old npm command cannot make
    # the new binary reject itself. Fresh-login and interactive-shell resolution
    # were independently verified before the install transaction was committed.
    if [ "${JARN_TEST_FORCE_INTERACTIVE_SETUP:-0}" = 1 ]; then
        PATH="$INSTALL_DIR:$PATH" "$BIN_PATH" setup
        setup_status=$?
    else
        PATH="$INSTALL_DIR:$PATH" "$BIN_PATH" setup </dev/tty >/dev/tty 2>&1
        setup_status=$?
    fi
    observed_setup_status=$(manifest_setup_status 2>/dev/null || true)
    if [ "$setup_status" -eq 0 ] && [ "$observed_setup_status" = complete ]; then
        SETUP_STATUS=complete
        return 0
    fi
    SETUP_STATUS=failed
    if [ "$setup_status" -eq 0 ]; then
        warn "Setup incomplete: child exited zero but the verified install record is not complete"
    else
        warn "Setup incomplete: first-time setup did not verify successfully"
    fi
    warn "resume with: $BIN_PATH setup"
    return 1
}

show_collisions_after_install() {
    discover_installations
    if [ "$INSTALLATION_COUNT" -gt 1 ]; then
        warn "other J.A.R.N. installations remain (preserved intentionally):"
        while IFS='|' read -r found_source found_path; do
            [ "$found_path" = "$BIN_PATH" ] && continue
            printf '%s\n' "  - $found_source: $found_path" >&2
        done < "$INVENTORY_FILE"
        warn "safe choice: keep them and use the verified PATH precedence above"
        warn "optional cleanup: inspect each owner/package manager first; this installer never deletes it"
    fi
}

# Platform rejection deliberately happens before curl/network or large downloads.
detect_platform
validate_platform
check_target_writable "$INSTALL_DIR" "installation directory"
check_target_writable "$STATE_DIR" "state directory"
check_disk_space
check_curl_tls
discover_installations
show_preflight
resolve_version
select_asset

info "Plan: install J.A.R.N. $VERSION ($CHANNEL) via $INSTALL_METHOD into $BIN_PATH"
if [ "$DRY_RUN" -eq 1 ]; then
    info "Dry run complete; no persistent files were changed"
    exit 0
fi

acquire_lock
reconcile_interrupted_transaction

# A current target is reusable only after both offline smoke commands pass.
if [ -x "$BIN_PATH" ] && smoke_candidate "$BIN_PATH"; then
    INSTALL_RESULT=$(existing_install_method)
    PREPARED_PATH=$BIN_PATH
    adopt_manifest_previous_path
    info "J.A.R.N. $VERSION is already healthy at $BIN_PATH"
else
    case "$INSTALL_METHOD" in
        binary)
            [ -n "$ASSET" ] || \
                die "no release binary exists for $OS/$ARCH; choose --method python"
            prepare_binary || die "release binary installation failed; prior J.A.R.N. remains unchanged"
            ;;
        python)
            prepare_python
            ;;
        auto)
            if [ -n "$ASSET" ] && prepare_binary; then
                :
            else
                info "Using the isolated Python fallback"
                prepare_python
            fi
            ;;
    esac
    INSTALL_RESULT=$PREPARED_METHOD
    begin_install_transaction
    ensure_shell_profiles
    activate_prepared
fi

# Existing installs may predate the managed PATH block; repair it idempotently.
begin_install_transaction
ensure_shell_profiles
if ! verify_user_resolution; then
    die "installation is not active in the user's fresh shell; review the resolutions above"
fi

prepare_setup
if ! write_metadata; then
    die "could not persist install metadata; rolling back executable and shell profiles"
fi
if ! validate_emitted_receipt; then
    die "the activated candidate rejected or could not verify the emitted install receipt"
fi
# Installation and onboarding are separate transactions. From this point the
# verified executable/profile/receipt remain active even if the user cancels or
# setup commits config and the parent later receives a signal.
if ! commit_install_transaction; then
    die "could not finalize the durable install transaction journal"
fi
setup_ok=1
if ! run_setup; then
    setup_ok=0
fi
show_collisions_after_install

info "Installed path verified: $BIN_PATH (jarn $VERSION, method $INSTALL_RESULT)"
info "Install metadata: $MANIFEST_PATH"
if [ -n "$PREVIOUS_PATH" ]; then
    info "Rollback candidate retained: $PREVIOUS_PATH"
fi
if [ -n "$PROFILE_UPDATES" ]; then
    info "Shell profile PATH updated atomically: $PROFILE_UPDATES"
fi

if [ "$setup_ok" -ne 1 ]; then
    warn "Installation is healthy, but setup is incomplete; no success status was emitted"
    exit 20
fi

if [ "$ACTIVATION_STATUS" = required ]; then
    warn "Activation required: a child installer cannot alter or clear the parent shell"
    warn "current resolution: ${CURRENT_RESOLUTION:-not found}"
    warn "verified fresh-shell resolution: $LOGIN_RESOLUTION"
    warn "run exactly: exec \"$SHELL_PATH\" -l"
    warn "status 10 means installed and verified, but not yet active in the parent shell"
    exit 10
fi

info "Ready — the user-visible command resolves to $BIN_PATH"
exit 0
