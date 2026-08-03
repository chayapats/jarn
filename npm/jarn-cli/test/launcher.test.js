'use strict'

const { test } = require('node:test')
const assert = require('node:assert')
const { spawn } = require('node:child_process')
const { EventEmitter, once } = require('node:events')
const {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const launcher = require('../bin/jarn.js')

function exitingChild(code, signal) {
  const child = new EventEmitter()
  child.kill = () => true
  queueMicrotask(() => child.emit('exit', code, signal))
  return child
}

async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  assert.fail(`condition was not met within ${timeoutMs}ms`)
}

test('platformPackage maps every supported host', () => {
  assert.equal(launcher.platformPackage('linux', 'x64'), 'jarn-cli-linux-x64')
  assert.equal(launcher.platformPackage('linux', 'arm64'), 'jarn-cli-linux-arm64')
  assert.equal(launcher.platformPackage('darwin', 'arm64'), 'jarn-cli-darwin-arm64')
})

test('platformPackage returns null for unsupported hosts', () => {
  assert.equal(launcher.platformPackage('win32', 'x64'), null)
  assert.equal(launcher.platformPackage('linux', 'ia32'), null)
  assert.equal(launcher.platformPackage('freebsd', 'arm64'), null)
  // Intel macOS is intentionally unsupported (use pip) — see jarn-cli-darwin-x64 removal.
  assert.equal(launcher.platformPackage('darwin', 'x64'), null)
})

test('SUPPORTED lists exactly the three target keys', () => {
  assert.deepEqual(
    [...launcher.SUPPORTED].sort(),
    ['darwin-arm64', 'linux-arm64', 'linux-x64']
  )
})

test('run errors on unsupported platform and exits 1', async () => {
  let out = ''
  const code = await launcher.run(['--version'], {
    platform: 'win32',
    arch: 'x64',
    stderr: { write: (s) => (out += s) },
  })
  assert.equal(code, 1)
  assert.match(out, /unsupported platform "win32-x64"/)
  assert.match(out, /WSL/)
  assert.match(out, /pip install jarn/)
})

test('run errors clearly when the platform package is not resolvable', async () => {
  let out = ''
  const code = await launcher.run(['--version'], {
    platform: 'linux',
    arch: 'x64',
    resolve: () => {
      throw new Error('Cannot find module')
    },
    stderr: { write: (s) => (out += s) },
  })
  assert.equal(code, 1)
  assert.match(out, /"jarn-cli-linux-x64"/)
  assert.match(out, /--ignore-scripts/)
})

test('run spawns the resolved binary with argv + inherited stdio, returns its status', async () => {
  let captured
  const code = await launcher.run(['chat', '--model', 'x'], {
    platform: 'darwin',
    arch: 'arm64',
    resolve: () => '/fake/jarn-cli-darwin-arm64/bin/jarn',
    spawn: (bin, argv, options) => {
      captured = { bin, argv, options }
      return exitingChild(42, null)
    },
  })
  assert.equal(code, 42)
  assert.equal(captured.bin, '/fake/jarn-cli-darwin-arm64/bin/jarn')
  assert.deepEqual(captured.argv, ['chat', '--model', 'x'])
  assert.equal(captured.options.stdio, 'inherit')
})

test('run returns 1 when the binary fails to spawn', async () => {
  const code = await launcher.run([], {
    platform: 'linux',
    arch: 'arm64',
    resolve: () => '/fake/bin/jarn',
    spawn: () => {
      const child = new EventEmitter()
      child.kill = () => true
      queueMicrotask(() => child.emit('error', new Error('EACCES')))
      return child
    },
    stderr: { write: () => {} },
  })
  assert.equal(code, 1)
})

test('run preserves the signal exit code when the binary is killed', async () => {
  const fakeProcess = new EventEmitter()
  fakeProcess.pid = 123
  const selfSignals = []
  fakeProcess.kill = (pid, signal) => selfSignals.push({ pid, signal })

  const code = await launcher.run([], {
    platform: 'linux',
    arch: 'x64',
    resolve: () => '/fake/bin/jarn',
    spawn: () => exitingChild(null, 'SIGINT'),
    stderr: { write: () => {} },
    process: fakeProcess,
  })
  assert.equal(code, 130)
  assert.deepEqual(selfSignals, [{ pid: 123, signal: 'SIGINT' }])
})

test('run forwards stop signals to the child', async () => {
  const fakeProcess = new EventEmitter()
  fakeProcess.pid = 456
  fakeProcess.kill = () => true
  const forwarded = []
  const child = new EventEmitter()
  child.kill = (signal) => {
    forwarded.push(signal)
    queueMicrotask(() => child.emit('exit', null, signal))
    return true
  }

  const result = launcher.run([], {
    platform: 'linux',
    arch: 'x64',
    resolve: () => '/fake/bin/jarn',
    spawn: () => child,
    process: fakeProcess,
  })
  fakeProcess.emit('SIGTERM')

  assert.equal(await result, 143)
  assert.deepEqual(forwarded, ['SIGTERM'])
})

test(
  'real launcher SIGTERM does not orphan its child',
  { skip: launcher.platformPackage(process.platform, process.arch) === null },
  async () => {
    const root = mkdtempSync(join(tmpdir(), 'jarn-launcher-'))
    const mainDir = join(root, 'node_modules', 'jarn-cli')
    const platformPackage = launcher.platformPackage(process.platform, process.arch)
    const platformDir = join(root, 'node_modules', platformPackage)
    const launcherPath = join(mainDir, 'bin', 'jarn.js')
    const binaryPath = join(platformDir, 'bin', 'jarn')
    const pidPath = join(root, 'child.pid')
    let launcherProcess
    let childPid

    try {
      mkdirSync(join(mainDir, 'bin'), { recursive: true })
      mkdirSync(join(platformDir, 'bin'), { recursive: true })
      copyFileSync(join(__dirname, '..', 'bin', 'jarn.js'), launcherPath)
      writeFileSync(
        binaryPath,
        '#!/usr/bin/env node\n' +
          "const fs = require('node:fs')\n" +
          "fs.writeFileSync(process.argv[2], String(process.pid))\n" +
          'setInterval(() => {}, 1000)\n'
      )
      chmodSync(binaryPath, 0o755)

      launcherProcess = spawn(process.execPath, [launcherPath, pidPath], {
        stdio: 'ignore',
      })
      await waitFor(() => existsSync(pidPath))
      childPid = Number(readFileSync(pidPath, 'utf8'))

      launcherProcess.kill('SIGTERM')
      const [code, signal] = await once(launcherProcess, 'exit')
      assert.ok(signal === 'SIGTERM' || code === 143)

      await waitFor(() => {
        try {
          process.kill(childPid, 0)
          return false
        } catch (error) {
          return error.code === 'ESRCH'
        }
      })
    } finally {
      if (launcherProcess && launcherProcess.exitCode === null) {
        launcherProcess.kill('SIGKILL')
      }
      if (childPid) {
        try {
          process.kill(childPid, 'SIGKILL')
        } catch {
          // Already reaped, which is the expected path.
        }
      }
      rmSync(root, { recursive: true, force: true })
    }
  }
)
