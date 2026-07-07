import { test } from 'node:test'
import assert from 'node:assert'
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { TARGETS, platformPackageJson, mainPackageJson } from '../build-packages.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const TEMPLATE = JSON.parse(
  readFileSync(join(HERE, '..', 'jarn-cli', 'package.json'), 'utf8')
)

test('TARGETS matches the launcher platform keys', () => {
  assert.deepEqual(
    Object.keys(TARGETS).sort(),
    ['darwin-arm64', 'linux-arm64', 'linux-x64']
  )
})

test('platformPackageJson stamps name, version and the right os/cpu', () => {
  const pkg = platformPackageJson('darwin-arm64', '1.2.3')
  assert.equal(pkg.name, 'jarn-cli-darwin-arm64')
  assert.equal(pkg.version, '1.2.3')
  assert.deepEqual(pkg.os, ['darwin'])
  assert.deepEqual(pkg.cpu, ['arm64'])
  assert.deepEqual(pkg.files, ['bin/jarn', 'LICENSE'])
})

test('platformPackageJson covers every target with valid os/cpu', () => {
  const VALID_OS = new Set(['linux', 'darwin'])
  const VALID_CPU = new Set(['x64', 'arm64'])
  for (const target of Object.keys(TARGETS)) {
    const pkg = platformPackageJson(target, '0.0.1')
    assert.equal(pkg.name, `jarn-cli-${target}`)
    assert.ok(VALID_OS.has(pkg.os[0]), `${target} os`)
    assert.ok(VALID_CPU.has(pkg.cpu[0]), `${target} cpu`)
  }
})

test('platformPackageJson rejects unknown targets', () => {
  assert.throws(() => platformPackageJson('win32-x64', '1.0.0'), /unknown target/)
  // Intel macOS is no longer a target.
  assert.throws(() => platformPackageJson('darwin-x64', '1.0.0'), /unknown target/)
})

test('mainPackageJson pins every optionalDependency to the exact version', () => {
  const pkg = mainPackageJson('2.0.0', TEMPLATE)
  assert.equal(pkg.version, '2.0.0')
  assert.equal(pkg.name, 'jarn-cli')
  const deps = pkg.optionalDependencies
  assert.deepEqual(Object.keys(deps).sort(), [
    'jarn-cli-darwin-arm64',
    'jarn-cli-linux-arm64',
    'jarn-cli-linux-x64',
  ])
  for (const v of Object.values(deps)) assert.equal(v, '2.0.0')
})

test('main template exposes both jarn and jarn-cli bins', () => {
  assert.equal(TEMPLATE.bin.jarn, 'bin/jarn.js')
  assert.equal(TEMPLATE.bin['jarn-cli'], 'bin/jarn.js')
})

test('optionalDependencies in the template cover exactly the three platform packages', () => {
  assert.deepEqual(
    Object.keys(TEMPLATE.optionalDependencies).sort(),
    [
      'jarn-cli-darwin-arm64',
      'jarn-cli-linux-arm64',
      'jarn-cli-linux-x64',
    ]
  )
})
