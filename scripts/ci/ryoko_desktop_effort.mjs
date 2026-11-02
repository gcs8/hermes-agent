// Scoped fork qualification. No provider, browser, plugin or packaging workload.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const FILES = [
  'src/lib/reasoning-step.test.ts',
  'src/app/shell/use-model-menu-controller.test.tsx',
  'src/app/session/hooks/use-prompt-actions/slash-reasoning.test.ts',
  'src/app/session/hooks/use-message-stream/gateway-event/session-info.test.ts',
  'src/app/session/hooks/use-session-state-cache.test.tsx',
  'src/app/session/hooks/use-session-actions/utils.test.ts',
  'src/app/session/hooks/use-session-actions/effort-persistence.test.ts',
  'src/app/session/hooks/use-session-actions/session-create-request.test.ts',
  'src/app/session/hooks/use-session-actions.test.tsx',
  'src/store/reasoning-effort-wire-scope.test.ts',
  'src/app/session/hooks/use-hermes-config.test.ts',
  'src/app/session/hooks/use-model-controls-stale-pick.test.tsx',
  'src/store/model-presets.test.ts',
  'src/api/canonical-protocol.test.ts',
  'src/api/canonical-reasoning-config-set.test.ts',
  'src/api/canonical-live-pick.test.ts',
  'src/store/model-presets-effort.test.ts',
  'src/app/hooks/use-keybinds-reasoning.test.tsx'
]

export function validateVitest(report) {
  assert.equal(report.success, true)
  assert.equal(report.numFailedTests, 0)
  assert.equal(report.numPendingTests, 0)
  const seen = new Set()
  let total = 0
  for (const result of report.testResults) {
    const name = FILES.find(file => result.name.endsWith(`/apps/desktop/${file}`))
    assert.ok(name && !seen.has(name), 'foreign or duplicate test file')
    seen.add(name)
    assert.ok(result.assertionResults.length > 0, 'selected file executed no tests')
    for (const item of result.assertionResults) assert.equal(item.status, 'passed')
    total += result.assertionResults.length
  }
  assert.equal(seen.size, FILES.length, 'missing selected test file')
  return total
}

function run(command, args, evidence, label) {
  const result = spawnSync(command, args, { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 })
  writeFileSync(resolve(evidence, `${label}.log`), `${result.stdout ?? ''}${result.stderr ?? ''}`)
  process.stdout.write(result.stdout ?? '')
  process.stderr.write(result.stderr ?? '')
  assert.ifError(result.error)
  assert.equal(result.status, 0, `${label} native exit`)
  return result.stdout.trim()
}

function main() {
  const evidence = process.env.QUALIFICATION_EVIDENCE
  assert.ok(evidence)
  const head = run('git', ['rev-parse', 'HEAD'], evidence, 'head-before')
  assert.match(process.env.APPROVED_SHA ?? '', /^[0-9a-f]{40}$/)
  assert.match(process.env.BASE_SHA ?? '', /^[0-9a-f]{40}$/)
  assert.equal(head, process.env.APPROVED_SHA)
  assert.equal(run('git', ['status', '--porcelain'], evidence, 'status-before'), '')
  run('node', ['--test', 'scripts/ci/ryoko_desktop_effort.test.mjs'], evidence, 'contracts')
  const output = resolve(evidence, 'vitest.json')
  run('npm', ['exec', '--workspace', 'apps/desktop', '--', 'vitest', 'run', '--project', 'ui',
    '--maxWorkers', '1', '--reporter=json', `--outputFile=${output}`, ...FILES], evidence, 'vitest')
  const tests = validateVitest(JSON.parse(readFileSync(output, 'utf8')))
  // Full Desktop typecheck is a separate exact-head local gate: its compiler
  // exceeds this singleton pod's 2 GiB budget. Do not claim a CI typecheck pass.
  const changed = run('git', ['diff', '--name-only', process.env.BASE_SHA, head], evidence, 'changed-paths')
    .split('\n').filter(path => path.startsWith('apps/desktop/') && /\.tsx?$/.test(path))
    .map(path => path.slice('apps/desktop/'.length))
  run('npm', ['exec', '--workspace', 'apps/desktop', '--', 'eslint', ...changed], evidence, 'eslint')
  run('npm', ['exec', '--', 'eslint', '--config', 'eslint.config.shared.mjs', '--global', 'process', '--global', 'console',
    'scripts/ci/ryoko_desktop_effort.mjs', 'scripts/ci/ryoko_desktop_effort.test.mjs'], evidence, 'ci-eslint')
  run('python3', ['scripts/check', '--base', process.env.BASE_SHA], evidence, 'source-checks')
  assert.equal(run('git', ['rev-parse', 'HEAD'], evidence, 'head-after'), head)
  assert.equal(run('git', ['status', '--porcelain'], evidence, 'status-after'), '')
  const report = { qualified: true, sha: head, files: FILES.length, tests,
    run: process.env.GITHUB_RUN_ID ?? null, attempt: process.env.GITHUB_RUN_ATTEMPT ?? null,
    runner: process.env.RUNNER_NAME ?? null, typecheck: 'separate-local-gate',
    scope: 'Desktop effort persistence; Linux tests, lint and source checks only' }
  writeFileSync(resolve(evidence, 'qualification.json'), JSON.stringify(report, null, 2))
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `report=${JSON.stringify(report)}\n`)
  console.log(JSON.stringify(report))
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main()
