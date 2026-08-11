#!/usr/bin/env bash
# Exercise the installer lifecycle against exact, pre-publish release subjects.
#
# The release workflow runs this script on every Tier-1 OS/architecture.  It is
# deliberately network-independent for binary installs.  The macOS Intel leg
# supplies a preinstalled uv and the candidate wheel through UV_FIND_LINKS;
# dependency resolution still uses PyPI because those dependencies are not
# release assets.
set -euo pipefail

fail() {
  printf 'release lifecycle canary: %s\n' "$*" >&2
  exit 1
}

: "${JARN_CANARY_SUBJECTS:?set JARN_CANARY_SUBJECTS to the release-subjects directory}"
: "${JARN_CANARY_VERSION:?set JARN_CANARY_VERSION to the unprefixed release version}"
: "${JARN_CANARY_REPO:?set JARN_CANARY_REPO to owner/repository}"
: "${JARN_CANARY_METHOD:?set JARN_CANARY_METHOD to binary or auto}"
: "${JARN_CANARY_EXPECT_METHOD:?set JARN_CANARY_EXPECT_METHOD to binary or python}"

case "$JARN_CANARY_VERSION" in
  ''|*[!0-9A-Za-z._-]*) fail "invalid release version" ;;
esac
case "$JARN_CANARY_REPO" in
  */*) ;;
  *) fail "repository must have owner/name form" ;;
esac
case "$JARN_CANARY_METHOD:$JARN_CANARY_EXPECT_METHOD" in
  binary:binary|auto:python) ;;
  *) fail "unsupported method/expected-method pair" ;;
esac

subjects=$(cd "$JARN_CANARY_SUBJECTS" && pwd -P) || fail "subjects directory is unavailable"
installer="$subjects/install.sh"
checksums="$subjects/checksums.txt"
[ -f "$installer" ] || fail "install.sh is missing"
[ -f "$checksums" ] || fail "checksums.txt is missing"

work=$(mktemp -d "${TMPDIR:-/tmp}/jarn-release-lifecycle.XXXXXX")
# macOS commonly spells the temporary root through /var, which is a system
# symlink to /private/var.  Feed the installer the physical path so its
# intentional user-managed symlink refusal is exercised without mistaking the
# OS compatibility alias for an unsafe managed directory.
work=$(cd "$work" && pwd -P) || fail "temporary lifecycle directory is unavailable"
cleanup() {
  chmod -R u+w "$work" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

fixture_root="$work/origin"
release_dir="$fixture_root/$JARN_CANARY_REPO/releases/download/v$JARN_CANARY_VERSION"
mkdir -p "$release_dir"

if [ "$JARN_CANARY_METHOD" = binary ]; then
  : "${JARN_CANARY_ASSET:?set JARN_CANARY_ASSET for a binary lifecycle}"
  case "$JARN_CANARY_ASSET" in
    jarn-linux-x86_64|jarn-linux-arm64|jarn-macos-arm64) ;;
    *) fail "unsupported release asset" ;;
  esac
  [ -f "$subjects/$JARN_CANARY_ASSET" ] || fail "$JARN_CANARY_ASSET is missing"
  checksum_rows=$(awk -v name="$JARN_CANARY_ASSET" \
    '$2 == name || $2 == "*" name {count++} END {print count + 0}' "$checksums")
  [ "$checksum_rows" -eq 1 ] || fail "$JARN_CANARY_ASSET must have exactly one checksum"
  cp "$checksums" "$subjects/$JARN_CANARY_ASSET" "$release_dir/"
else
  : "${JARN_CANARY_UV_BIN:?set JARN_CANARY_UV_BIN for the managed-Python lifecycle}"
  [ -x "$JARN_CANARY_UV_BIN" ] || fail "the supplied uv command is not executable"
  wheel_count=$(find "$subjects" -maxdepth 1 -type f -name 'jarn-*.whl' | wc -l | tr -d ' ')
  [ "$wheel_count" -eq 1 ] || fail "managed-Python lifecycle requires exactly one candidate wheel"
fi

base_path="/usr/local/bin:/usr/bin:/bin"

run_installer() {
  canary_home=$1
  mkdir -p "$canary_home"
  set +e
  if [ "$JARN_CANARY_METHOD" = binary ]; then
    HOME="$canary_home" PATH="$canary_home/.local/bin:$base_path" SHELL=/bin/bash \
      JARN_GITHUB_BASE="file://$fixture_root" \
      JARN_GITHUB_REPO="$JARN_CANARY_REPO" JARN_RUN_SETUP=never \
      sh "$installer" --version "$JARN_CANARY_VERSION" --method binary \
        --no-setup --yes >"$canary_home/install.stdout" 2>"$canary_home/install.stderr"
  else
    HOME="$canary_home" PATH="$canary_home/.local/bin:$base_path" SHELL=/bin/bash \
      JARN_GITHUB_BASE="file://$fixture_root" \
      JARN_GITHUB_REPO="$JARN_CANARY_REPO" JARN_RUN_SETUP=never \
      JARN_UV_BIN="$JARN_CANARY_UV_BIN" UV_FIND_LINKS="$subjects" \
      sh "$installer" --version "$JARN_CANARY_VERSION" --method auto \
        --no-setup --yes >"$canary_home/install.stdout" 2>"$canary_home/install.stderr"
  fi
  install_rc=$?
  set -e
  case "$install_rc" in
    0|10) ;;
    *)
      sed -n '1,200p' "$canary_home/install.stdout" >&2
      sed -n '1,200p' "$canary_home/install.stderr" >&2
      fail "installer exited $install_rc"
      ;;
  esac
}

assert_candidate_install() {
  canary_home=$1
  active="$canary_home/.local/bin/jarn"
  manifest="$canary_home/.local/state/jarn/install.json"
  [ -x "$active" ] || fail "active command is missing"
  [ -f "$manifest" ] || fail "install receipt is missing"
  [ "$("$active" --version)" = "jarn $JARN_CANARY_VERSION" ] || \
    fail "active command reports the wrong version"
  "$active" --help >/dev/null
  grep -F "\"method\": \"$JARN_CANARY_EXPECT_METHOD\"" "$manifest" >/dev/null || \
    fail "install receipt reports the wrong method"
}

# Clean install and CLI startup.
clean_home="$work/clean-home"
run_installer "$clean_home"
assert_candidate_install "$clean_home"

# Upgrade over an independently owned older command, then rollback and roll
# forward using the actual candidate implementation.
lifecycle_home="$work/lifecycle-home"
mkdir -p "$lifecycle_home/.local/bin" "$lifecycle_home/.jarn/sessions"
cat >"$lifecycle_home/.local/bin/jarn" <<'SH'
#!/bin/sh
case "${1:-}" in
  --version) printf '%s\n' 'jarn 0.0.0' ;;
  --help) printf '%s\n' 'prior lifecycle fixture' ;;
  *) exit 0 ;;
esac
SH
chmod 755 "$lifecycle_home/.local/bin/jarn"
printf '%s\n' 'default_profile: lifecycle-preserved' >"$lifecycle_home/.jarn/config.yaml"
printf '%s\n' 'session-preserved' >"$lifecycle_home/.jarn/sessions/canary"

run_installer "$lifecycle_home"
assert_candidate_install "$lifecycle_home"
active="$lifecycle_home/.local/bin/jarn"
manifest="$lifecycle_home/.local/state/jarn/install.json"
previous=$(sed -n 's/.*"previous_path": "\([^"]*\)".*/\1/p' "$manifest" | sed -n '1p')
[ -n "$previous" ] && [ -x "$previous" ] || fail "rollback candidate was not retained"
lifecycle_path="$lifecycle_home/.local/bin:$base_path"

set +e
HOME="$lifecycle_home" PATH="$lifecycle_path" "$active" rollback --json \
  >"$work/rollback.json" 2>"$work/rollback.stderr"
rollback_rc=$?
set -e
if [ "$rollback_rc" -ne 0 ]; then
  cat "$work/rollback.json" "$work/rollback.stderr" >&2
  fail "rollback exited $rollback_rc"
fi
[ "$("$active" --version)" = 'jarn 0.0.0' ] || fail "rollback did not restore prior command"
set +e
HOME="$lifecycle_home" PATH="$lifecycle_path" "$previous" rollback --json \
  >"$work/roll-forward.json" 2>"$work/roll-forward.stderr"
roll_forward_rc=$?
set -e
if [ "$roll_forward_rc" -ne 0 ]; then
  cat "$work/roll-forward.json" "$work/roll-forward.stderr" >&2
  fail "forward rollback exited $roll_forward_rc"
fi
[ "$("$active" --version)" = "jarn $JARN_CANARY_VERSION" ] || \
  fail "forward rollback did not restore candidate"

set +e
HOME="$lifecycle_home" PATH="$lifecycle_path" "$active" \
  uninstall --yes --executable --dependencies \
  >"$work/uninstall.txt" 2>"$work/uninstall.stderr"
uninstall_rc=$?
set -e
if [ "$uninstall_rc" -ne 0 ]; then
  cat "$work/uninstall.txt" "$work/uninstall.stderr" >&2
  fail "uninstall exited $uninstall_rc"
fi
[ ! -e "$active" ] || fail "uninstall retained the active command"
[ ! -e "$manifest" ] || fail "uninstall retained the install receipt"
[ "$(cat "$lifecycle_home/.jarn/config.yaml")" = 'default_profile: lifecycle-preserved' ] || \
  fail "uninstall changed user config"
[ "$(cat "$lifecycle_home/.jarn/sessions/canary")" = 'session-preserved' ] || \
  fail "uninstall changed session data"

# Reinstall must remain possible without erasing preserved data.
run_installer "$lifecycle_home"
assert_candidate_install "$lifecycle_home"
[ "$(cat "$lifecycle_home/.jarn/config.yaml")" = 'default_profile: lifecycle-preserved' ] || \
  fail "reinstall changed user config"
[ "$(cat "$lifecycle_home/.jarn/sessions/canary")" = 'session-preserved' ] || \
  fail "reinstall changed session data"

printf 'release lifecycle canary passed: %s/%s (%s)\n' \
  "$(uname -s)" "$(uname -m)" "$JARN_CANARY_EXPECT_METHOD"
