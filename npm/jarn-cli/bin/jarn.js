#!/usr/bin/env node
'use strict'

// Thin launcher for the `jarn` / `jarn-cli` commands.
//
// The real program is a self-contained binary shipped in a per-platform package
// (jarn-cli-<platform>-<arch>) declared as an optionalDependency of jarn-cli.
// npm installs only the package whose `os`/`cpu` match the host, so exactly one
// binary is present. This launcher resolves it and execs it, passing through
// argv, stdio (the app is a TUI — it must inherit the terminal), and exit code.
//
// No third-party dependencies: this file must run with nothing but Node.

const { spawnSync } = require('node:child_process')

// host key (`<platform>-<arch>`) → platform package name.
const PLATFORM_PACKAGES = {
  'linux-x64': 'jarn-cli-linux-x64',
  'linux-arm64': 'jarn-cli-linux-arm64',
  'darwin-arm64': 'jarn-cli-darwin-arm64',
}

const SUPPORTED = Object.keys(PLATFORM_PACKAGES)

function platformPackage(platform, arch) {
  return PLATFORM_PACKAGES[`${platform}-${arch}`] || null
}

// Returns { key, pkg, binPath }. `pkg` is null when the host is unsupported;
// `binPath` is null when the platform package (or its binary) is not installed.
function resolveBinary(platform, arch, resolve) {
  const resolver = resolve || require.resolve
  const key = `${platform}-${arch}`
  const pkg = platformPackage(platform, arch)
  if (!pkg) return { key, pkg: null, binPath: null }
  try {
    return { key, pkg, binPath: resolver(`${pkg}/bin/jarn`) }
  } catch {
    return { key, pkg, binPath: null }
  }
}

function unsupportedMessage(key) {
  return (
    `jarn: unsupported platform "${key}".\n` +
    `Supported platforms: ${SUPPORTED.join(', ')}.\n` +
    `On native Windows, run J.A.R.N. under WSL. ` +
    `You can also install via pip: pip install jarn\n`
  )
}

function missingBinaryMessage(pkg) {
  return (
    `jarn: the platform package "${pkg}" was not installed (or is missing its binary).\n` +
    `This usually means it was skipped by --ignore-scripts / --no-optional, or the\n` +
    `install was interrupted. Reinstall without those flags: npm install -g jarn-cli\n`
  )
}

// Pure entry point: returns the process exit code instead of calling exit, so it
// can be unit-tested. `opts` lets tests inject platform/arch/resolve/spawn/stderr.
function run(argv, opts) {
  const o = opts || {}
  const platform = o.platform || process.platform
  const arch = o.arch || process.arch
  const spawn = o.spawn || spawnSync
  const stderr = o.stderr || process.stderr

  const { key, pkg, binPath } = resolveBinary(platform, arch, o.resolve)
  if (!pkg) {
    stderr.write(unsupportedMessage(key))
    return 1
  }
  if (!binPath) {
    stderr.write(missingBinaryMessage(pkg))
    return 1
  }

  const result = spawn(binPath, argv, { stdio: 'inherit' })
  if (result.error) {
    stderr.write(`jarn: failed to execute ${binPath}: ${result.error.message}\n`)
    return 1
  }
  if (typeof result.status === 'number') return result.status
  if (result.signal) return 1
  return 0
}

if (require.main === module) {
  process.exit(run(process.argv.slice(2)))
}

module.exports = {
  PLATFORM_PACKAGES,
  SUPPORTED,
  platformPackage,
  resolveBinary,
  unsupportedMessage,
  missingBinaryMessage,
  run,
}
