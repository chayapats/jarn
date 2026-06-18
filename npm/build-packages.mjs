#!/usr/bin/env node
// Assemble the npm packages for a release.
//
// Produces, under --out:
//   jarn-cli-<target>/   one per platform: { package.json (os/cpu pinned), bin/jarn }
//   jarn-cli/            the launcher package: package.json (version + pinned
//                        optionalDependencies), bin/jarn.js, README.md
//
// Usage:
//   node npm/build-packages.mjs --version 0.4.1 --binaries <dir> --out <dir>
//
// <dir> must contain one binary per target at  <dir>/binary-<target>/jarn
// (this is how `actions/download-artifact` lays out the `binary-<target>`
// artifacts uploaded by the release workflow). Pass --allow-missing to skip
// targets whose binary is absent (useful for local experimentation only — a
// real release must ship all targets).
//
// The pure helpers (TARGETS, platformPackageJson, mainPackageJson) are exported
// for unit testing.

import {
  mkdirSync,
  copyFileSync,
  writeFileSync,
  readFileSync,
  chmodSync,
  rmSync,
  existsSync,
} from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const MAIN_SRC = join(HERE, 'jarn-cli')

// target key → npm os/cpu values. Keys match the launcher's PLATFORM_PACKAGES.
export const TARGETS = {
  'linux-x64': { os: 'linux', cpu: 'x64' },
  'linux-arm64': { os: 'linux', cpu: 'arm64' },
  'darwin-arm64': { os: 'darwin', cpu: 'arm64' },
}

const REPO = 'https://github.com/chayapats/jarn'

export function platformPackageJson(target, version) {
  const spec = TARGETS[target]
  if (!spec) throw new Error(`unknown target: ${target}`)
  return {
    name: `jarn-cli-${target}`,
    version,
    description: `J.A.R.N. standalone binary for ${spec.os}-${spec.cpu}.`,
    homepage: REPO,
    repository: { type: 'git', url: `git+${REPO}.git` },
    license: 'Apache-2.0',
    author: 'Chayapat',
    os: [spec.os],
    cpu: [spec.cpu],
    files: ['bin/jarn'],
  }
}

// The main package.json, derived from the committed template with the version
// stamped in and every optionalDependency pinned to the exact release version.
export function mainPackageJson(version, template) {
  const pkg = JSON.parse(JSON.stringify(template))
  pkg.version = version
  pkg.optionalDependencies = {}
  for (const target of Object.keys(TARGETS)) {
    pkg.optionalDependencies[`jarn-cli-${target}`] = version
  }
  return pkg
}

function parseArgs(argv) {
  const args = { binaries: null, out: null, version: null, allowMissing: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--version') args.version = argv[++i]
    else if (a === '--binaries') args.binaries = argv[++i]
    else if (a === '--out') args.out = argv[++i]
    else if (a === '--allow-missing') args.allowMissing = true
    else throw new Error(`unknown argument: ${a}`)
  }
  if (!args.version) throw new Error('--version is required')
  if (!args.binaries) throw new Error('--binaries <dir> is required')
  if (!args.out) throw new Error('--out <dir> is required')
  if (!/^\d+\.\d+\.\d+([.-].+)?$/.test(args.version)) {
    throw new Error(`--version "${args.version}" is not a valid version`)
  }
  return args
}

function writePkg(dir, pkg) {
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'package.json'), JSON.stringify(pkg, null, 2) + '\n')
}

function main() {
  const args = parseArgs(process.argv.slice(2))
  rmSync(args.out, { recursive: true, force: true })

  const template = JSON.parse(readFileSync(join(MAIN_SRC, 'package.json'), 'utf8'))
  const published = []

  // Platform packages first — the main package's optionalDependencies point at them.
  for (const target of Object.keys(TARGETS)) {
    const binSrc = join(args.binaries, `binary-${target}`, 'jarn')
    if (!existsSync(binSrc)) {
      if (args.allowMissing) {
        console.warn(`! skipping ${target}: no binary at ${binSrc}`)
        continue
      }
      throw new Error(`missing binary for ${target}: expected ${binSrc}`)
    }
    const dir = join(args.out, `jarn-cli-${target}`)
    writePkg(dir, platformPackageJson(target, args.version))
    mkdirSync(join(dir, 'bin'), { recursive: true })
    copyFileSync(binSrc, join(dir, 'bin', 'jarn'))
    chmodSync(join(dir, 'bin', 'jarn'), 0o755)
    published.push(dir)
    console.log(`✓ ${target} → ${dir}`)
  }

  // Main launcher package last.
  const mainDir = join(args.out, 'jarn-cli')
  writePkg(mainDir, mainPackageJson(args.version, template))
  mkdirSync(join(mainDir, 'bin'), { recursive: true })
  copyFileSync(join(MAIN_SRC, 'bin', 'jarn.js'), join(mainDir, 'bin', 'jarn.js'))
  if (existsSync(join(MAIN_SRC, 'README.md'))) {
    copyFileSync(join(MAIN_SRC, 'README.md'), join(mainDir, 'README.md'))
  }
  published.push(mainDir)
  console.log(`✓ jarn-cli → ${mainDir}`)

  // Emit the publish order (platform packages before the main package).
  writeFileSync(join(args.out, 'publish-order.txt'), published.join('\n') + '\n')
  console.log(`\nPublish order written to ${join(args.out, 'publish-order.txt')}`)
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main()
}
