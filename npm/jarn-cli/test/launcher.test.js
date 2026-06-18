'use strict'

const { test } = require('node:test')
const assert = require('node:assert')
const launcher = require('../bin/jarn.js')

test('platformPackage maps every supported host', () => {
  assert.equal(launcher.platformPackage('linux', 'x64'), 'jarn-cli-linux-x64')
  assert.equal(launcher.platformPackage('linux', 'arm64'), 'jarn-cli-linux-arm64')
  assert.equal(launcher.platformPackage('darwin', 'x64'), 'jarn-cli-darwin-x64')
  assert.equal(launcher.platformPackage('darwin', 'arm64'), 'jarn-cli-darwin-arm64')
})

test('platformPackage returns null for unsupported hosts', () => {
  assert.equal(launcher.platformPackage('win32', 'x64'), null)
  assert.equal(launcher.platformPackage('linux', 'ia32'), null)
  assert.equal(launcher.platformPackage('freebsd', 'arm64'), null)
})

test('SUPPORTED lists exactly the four target keys', () => {
  assert.deepEqual(
    [...launcher.SUPPORTED].sort(),
    ['darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64']
  )
})

test('run errors on unsupported platform and exits 1', () => {
  let out = ''
  const code = launcher.run(['--version'], {
    platform: 'win32',
    arch: 'x64',
    stderr: { write: (s) => (out += s) },
  })
  assert.equal(code, 1)
  assert.match(out, /unsupported platform "win32-x64"/)
  assert.match(out, /WSL/)
  assert.match(out, /pip install jarn/)
})

test('run errors clearly when the platform package is not resolvable', () => {
  let out = ''
  const code = launcher.run(['--version'], {
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

test('run execs the resolved binary with argv + inherited stdio, returns its status', () => {
  let captured
  const code = launcher.run(['chat', '--model', 'x'], {
    platform: 'darwin',
    arch: 'arm64',
    resolve: () => '/fake/jarn-cli-darwin-arm64/bin/jarn',
    spawn: (bin, argv, options) => {
      captured = { bin, argv, options }
      return { status: 42 }
    },
  })
  assert.equal(code, 42)
  assert.equal(captured.bin, '/fake/jarn-cli-darwin-arm64/bin/jarn')
  assert.deepEqual(captured.argv, ['chat', '--model', 'x'])
  assert.equal(captured.options.stdio, 'inherit')
})

test('run returns 1 when the binary fails to spawn', () => {
  const code = launcher.run([], {
    platform: 'linux',
    arch: 'arm64',
    resolve: () => '/fake/bin/jarn',
    spawn: () => ({ error: new Error('EACCES') }),
    stderr: { write: () => {} },
  })
  assert.equal(code, 1)
})

test('run returns 1 when the binary is killed by a signal', () => {
  const code = launcher.run([], {
    platform: 'linux',
    arch: 'x64',
    resolve: () => '/fake/bin/jarn',
    spawn: () => ({ status: null, signal: 'SIGINT' }),
    stderr: { write: () => {} },
  })
  assert.equal(code, 1)
})
