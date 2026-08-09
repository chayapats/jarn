#!/bin/sh
# Install or update J.A.R.N. from the latest GitHub release.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/chayapats/jarn/main/install.sh | sh
#
# Optional environment variables:
#   JARN_VERSION=0.11.0          Install a specific version (default: latest).
#   JARN_INSTALL_DIR=~/.local/bin
#   JARN_INSTALL_METHOD=auto     auto, binary, or python.
#   JARN_RUN_SETUP=auto          auto, always, or never.
set -eu

REPO="${JARN_GITHUB_REPO:-chayapats/jarn}"
GITHUB_BASE="${JARN_GITHUB_BASE:-https://github.com}"
INSTALL_DIR="${JARN_INSTALL_DIR:-${HOME}/.local/bin}"
INSTALL_METHOD="${JARN_INSTALL_METHOD:-auto}"
RUN_SETUP="${JARN_RUN_SETUP:-auto}"
REQUESTED_VERSION="${JARN_VERSION:-latest}"
UV_INSTALL_URL="${JARN_UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"

TMP_DIR=""
STAGED_PATH=""

info() {
    printf '%s\n' "==> $*"
}

warn() {
    printf '%s\n' "warning: $*" >&2
}

die() {
    printf '%s\n' "error: $*" >&2
    exit 1
}

cleanup() {
    if [ -n "$STAGED_PATH" ] && [ -e "$STAGED_PATH" ]; then
        rm -f "$STAGED_PATH"
    fi
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}

trap cleanup EXIT
trap 'cleanup; exit 1' HUP INT TERM

make_tmp_dir() {
    if [ -z "$TMP_DIR" ]; then
        TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/jarn-install.XXXXXX") || \
            die "could not create a temporary directory"
    fi
}

command -v curl >/dev/null 2>&1 || die "curl is required to run this installer"

case "$INSTALL_METHOD" in
    auto|binary|python) ;;
    *) die "JARN_INSTALL_METHOD must be auto, binary, or python" ;;
esac

case "$RUN_SETUP" in
    auto|always|never) ;;
    *) die "JARN_RUN_SETUP must be auto, always, or never" ;;
esac

resolve_version() {
    if [ "$REQUESTED_VERSION" = "latest" ]; then
        info "Resolving the latest J.A.R.N. release"
        latest_url=$(curl -fLsS -o /dev/null -w '%{url_effective}' \
            "$GITHUB_BASE/$REPO/releases/latest") || \
            die "could not resolve the latest GitHub release"
        latest_url=${latest_url%/}
        TAG=${latest_url##*/}
        case "$TAG" in
            v*) VERSION=${TAG#v} ;;
            *) die "GitHub returned an invalid release URL: $latest_url" ;;
        esac
    else
        VERSION=${REQUESTED_VERSION#v}
        TAG="v$VERSION"
    fi

    case "$VERSION" in
        ""|*[!0-9A-Za-z._-]*) die "invalid J.A.R.N. version: $VERSION" ;;
    esac
}

detect_platform() {
    os_raw=${JARN_OS:-$(uname -s)}
    arch_raw=${JARN_ARCH:-$(uname -m)}

    case "$os_raw" in
        Linux|linux) OS=linux ;;
        Darwin|darwin) OS=darwin ;;
        *) OS=$(printf '%s' "$os_raw" | tr '[:upper:]' '[:lower:]') ;;
    esac

    case "$arch_raw" in
        x86_64|amd64|x64) ARCH=x86_64 ;;
        aarch64|arm64) ARCH=arm64 ;;
        *) ARCH=$arch_raw ;;
    esac

    LIBC_NAME=none
    LIBC_VERSION=""
    if [ "$OS" = linux ]; then
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
}

select_asset() {
    ASSET=""
    case "$OS-$ARCH" in
        linux-x86_64)
            [ "$LIBC_NAME" != musl ] && ASSET=jarn-linux-x86_64
            ;;
        linux-arm64)
            [ "$LIBC_NAME" != musl ] && ASSET=jarn-linux-arm64
            ;;
        darwin-arm64) ASSET=jarn-macos-arm64 ;;
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

attempt_binary_install() {
    [ -n "$ASSET" ] || return 1
    make_tmp_dir

    release_url="$GITHUB_BASE/$REPO/releases/download/$TAG"
    candidate="$TMP_DIR/$ASSET"
    checksums="$TMP_DIR/checksums.txt"

    info "Downloading $ASSET for $OS/$ARCH"
    if ! curl -fLsS "$release_url/$ASSET" -o "$candidate"; then
        warn "the release binary could not be downloaded; using the Python fallback"
        return 1
    fi
    if ! curl -fLsS "$release_url/checksums.txt" -o "$checksums"; then
        warn "checksums.txt is missing; refusing the unverified binary"
        return 1
    fi

    expected=$(awk -v file="$ASSET" '$2 == file {print $1; exit}' "$checksums")
    [ -n "$expected" ] || {
        warn "checksums.txt has no entry for $ASSET"
        return 1
    }
    actual=$(sha256_file "$candidate") || {
        warn "sha256sum or shasum is required to verify the release binary"
        return 1
    }
    [ "$actual" = "$expected" ] || \
        die "SHA-256 mismatch for $ASSET (expected $expected, got $actual)"
    info "SHA-256 verified"

    chmod 755 "$candidate"
    if ! smoke_output=$("$candidate" --version 2>&1); then
        first_error=$(printf '%s\n' "$smoke_output" | sed -n '1p')
        warn "the release binary cannot run on this system: $first_error"
        return 1
    fi
    case "$smoke_output" in
        *"jarn $VERSION"*) ;;
        *)
            warn "the downloaded binary reported an unexpected version: $smoke_output"
            return 1
            ;;
    esac

    mkdir -p "$INSTALL_DIR"
    STAGED_PATH="$INSTALL_DIR/.jarn.tmp.$$"
    cp "$candidate" "$STAGED_PATH"
    chmod 755 "$STAGED_PATH"
    mv -f "$STAGED_PATH" "$INSTALL_DIR/jarn"
    STAGED_PATH=""
    printf '%s\n' "binary $VERSION" > "$INSTALL_DIR/.jarn-install-method"
    INSTALL_RESULT=binary
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

install_with_python() {
    find_uv
    if [ -z "$UV_BIN" ]; then
        make_tmp_dir
        info "Installing uv (user-space dependency manager)"
        uv_installer="$TMP_DIR/uv-install.sh"
        curl -fLsS "$UV_INSTALL_URL" -o "$uv_installer" || \
            die "could not download the uv installer"
        UV_NO_MODIFY_PATH=1 sh "$uv_installer" || die "uv installation failed"
        find_uv
        [ -n "$UV_BIN" ] || die "uv installed but its executable was not found"
    fi

    mkdir -p "$INSTALL_DIR"
    info "Installing jarn==$VERSION with a managed Python 3.12"
    UV_TOOL_BIN_DIR="$INSTALL_DIR" "$UV_BIN" tool install \
        --python 3.12 --managed-python --force "jarn==$VERSION" || \
        die "Python fallback installation failed"

    [ -x "$INSTALL_DIR/jarn" ] || \
        die "uv completed but $INSTALL_DIR/jarn was not created"
    if ! smoke_output=$("$INSTALL_DIR/jarn" --version 2>&1); then
        die "the Python installation completed but failed to start: $smoke_output"
    fi
    case "$smoke_output" in
        *"jarn $VERSION"*) ;;
        *) die "the installed command reported an unexpected version: $smoke_output" ;;
    esac

    printf '%s\n' "python $VERSION" > "$INSTALL_DIR/.jarn-install-method"
    INSTALL_RESULT=python
}

ensure_path() {
    PATH="$INSTALL_DIR:${PATH:-}"
    export PATH
    case ":${PATH#"$INSTALL_DIR:"}:" in
        *":$INSTALL_DIR:"*) return ;;
    esac

    if [ "$INSTALL_DIR" != "$HOME/.local/bin" ]; then
        warn "$INSTALL_DIR is not on PATH; add it to your shell profile"
        return
    fi

    shell_name=${SHELL##*/}
    case "$shell_name" in
        zsh) profile="$HOME/.zshrc" ;;
        bash) profile="$HOME/.bashrc" ;;
        *) profile="$HOME/.profile" ;;
    esac
    marker="# Added by the J.A.R.N. installer"
    if ! grep -F "$marker" "$profile" >/dev/null 2>&1; then
        {
            printf '\n%s\n' "$marker"
            printf '%s\n' 'export PATH="$HOME/.local/bin:$PATH"'
        } >> "$profile"
        PATH_UPDATED=$profile
    fi
}

maybe_run_setup() {
    jarn_home=${JARN_HOME:-$HOME/.jarn}
    case "$RUN_SETUP" in
        never) return ;;
        auto) [ -f "$jarn_home/config.yaml" ] && return ;;
    esac

    if ( : </dev/tty ) 2>/dev/null; then
        info "Starting first-time setup"
        if ! "$INSTALL_DIR/jarn" setup </dev/tty >/dev/tty 2>&1; then
            warn "setup did not finish; run $INSTALL_DIR/jarn setup when ready"
        fi
    else
        info "Run '$INSTALL_DIR/jarn setup' to finish first-time configuration"
    fi
}

resolve_version
detect_platform
select_asset

platform_label="$OS/$ARCH"
if [ "$OS" = linux ]; then
    platform_label="$platform_label ($LIBC_NAME${LIBC_VERSION:+ $LIBC_VERSION})"
fi
info "Detected $platform_label; installing J.A.R.N. $VERSION"

mkdir -p "$INSTALL_DIR"
BIN_PATH="$INSTALL_DIR/jarn"
if [ -x "$BIN_PATH" ]; then
    if installed_output=$("$BIN_PATH" --version 2>/dev/null); then
        case "$installed_output" in
            *"jarn $VERSION"*)
                INSTALL_RESULT=existing
                info "J.A.R.N. $VERSION is already installed"
                ;;
            *) INSTALL_RESULT="" ;;
        esac
    else
        INSTALL_RESULT=""
    fi
else
    INSTALL_RESULT=""
fi

if [ -z "$INSTALL_RESULT" ]; then
    case "$INSTALL_METHOD" in
        binary)
            [ -n "$ASSET" ] || die "no release binary exists for $OS/$ARCH/$LIBC_NAME"
            attempt_binary_install || die "release binary installation failed"
            ;;
        python) install_with_python ;;
        auto)
            if [ -n "$ASSET" ] && attempt_binary_install; then
                :
            else
                info "Using the portable Python fallback"
                install_with_python
            fi
            ;;
    esac
fi

PATH_UPDATED=""
ensure_path
info "Installed via $INSTALL_RESULT: $INSTALL_DIR/jarn"
if [ -n "$PATH_UPDATED" ]; then
    info "Added $INSTALL_DIR to PATH in $PATH_UPDATED (open a new shell to apply it)"
fi
maybe_run_setup
info "Done — run 'jarn --version' in a new shell"
