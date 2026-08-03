#!/usr/bin/env node
'use strict'

// Thin launcher for the `jarn` / `jarn-cli` commands.
//
// The real program is a self-contained binary shipped in a per-platform package
// (jarn-cli-<platform>-<arch>) declared as an optionalDependency of jarn-cli.
// npm installs only the package whose `os`/`cpu` match the host, so exactly one
// binary is present. This launcher resolves and spawns it, passing through argv
// and stdio (the app is a TUI — it must inherit the terminal), forwarding stop
// signals, and preserving its exit status.
//
// No third-party dependencies: this file must run with nothing but Node.

const { spawn } = require('node:child_process')
const { constants } = require('node:os')

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

// Async entry point: resolves to the process exit code instead of calling exit,
// so it can be unit-tested. `opts` lets tests inject dependencies.
async function run(argv, opts) {
  const o = opts || {}
  const platform = o.platform || process.platform
  const arch = o.arch || process.arch
  const spawnChild = o.spawn || spawn
  const stderr = o.stderr || process.stderr
  const parent = o.process || process

  const { key, pkg, binPath } = resolveBinary(platform, arch, o.resolve)
  if (!pkg) {
    stderr.write(unsupportedMessage(key))
    return 1
  }
  if (!binPath) {
    stderr.write(missingBinaryMessage(pkg))
    return 1
  }

  let child
  try {
    child = spawnChild(binPath, argv, { stdio: 'inherit' })
  } catch (error) {
    stderr.write(`jarn: failed to execute ${binPath}: ${error.message}\n`)
    return 1
  }

  return new Promise((resolve) => {
    const forwardedSignals = ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT']
    const forwarders = new Map()
    let settled = false

    const cleanup = () => {
      for (const [signal, handler] of forwarders) {
        parent.removeListener(signal, handler)
      }
    }

    const finish = (code) => {
      if (settled) return
      settled = true
      cleanup()
      resolve(code)
    }

    for (const signal of forwardedSignals) {
      const handler = () => {
        try {
          child.kill(signal)
        } catch {
          // The child may already have exited between signal delivery and here.
        }
      }
      forwarders.set(signal, handler)
      parent.on(signal, handler)
    }

    child.once('error', (error) => {
      stderr.write(`jarn: failed to execute ${binPath}: ${error.message}\n`)
      finish(1)
    })

    child.once('exit', (code, signal) => {
      if (!signal) {
        finish(typeof code === 'number' ? code : 0)
        return
      }

      if (settled) return
      settled = true
      cleanup()
      try {
        parent.kill(parent.pid, signal)
      } catch {
        // Fall through to a conventional shell-compatible signal exit code.
      }
      const signalNumber = constants.signals[signal]
      resolve(typeof signalNumber === 'number' ? 128 + signalNumber : 1)
    })
  })
}

if (require.main === module) {
  run(process.argv.slice(2)).then((code) => process.exit(code))
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
