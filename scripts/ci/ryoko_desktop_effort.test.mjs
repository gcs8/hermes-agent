import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import yaml from 'js-yaml'

import { FILES, validateVitest } from './ryoko_desktop_effort.mjs'

function fixture() {
  return {
    success: true, numFailedTests: 0, numPendingTests: 0,
    testResults: FILES.map(name => ({ name: `/checkout/apps/desktop/${name}`, assertionResults: [{ status: 'passed' }] }))
  }
}

test('accepts complete executed coverage and derives the test total', () => {
  assert.equal(validateVitest(fixture()), FILES.length)
})

for (const kind of ['missing', 'duplicate', 'empty', 'skipped', 'failed', 'foreign', 'unsuccessful']) {
  test(`rejects ${kind} coverage instead of publishing green`, () => {
    const report = fixture()
    if (kind === 'missing') report.testResults.pop()
    if (kind === 'duplicate') report.testResults[1] = report.testResults[0]
    if (kind === 'empty') report.testResults[0].assertionResults = []
    if (kind === 'skipped') report.testResults[0].assertionResults[0].status = 'pending'
    if (kind === 'failed') report.numFailedTests = 1
    if (kind === 'foreign') report.testResults[0].name = '/checkout/apps/desktop/src/unselected.test.ts'
    if (kind === 'unsuccessful') report.success = false
    assert.throws(() => validateVitest(report))
  })
}

const workflow = yaml.load(readFileSync('.github/workflows/ryoko-desktop-effort.yml', 'utf8'))
const source = 'a'.repeat(40)

function context() {
  return {
    github: { repository: 'gcs8/hermes-agent', repository_id: '1260443943', actor: 'gcs8', triggering_actor: 'gcs8',
      event: { pull_request: { user: { login: 'gcs8' }, head: { repo: { id: 1260443943 }, sha: source, ref: 'fix/desktop-effort-stable-0226' }, base: { repo: { id: 1260443943 } } } } },
    vars: { HERMES_LINUX_SOURCE_SHA: source, HERMES_LINUX_WORKFLOW_SHA: source }
  }
}

function admitted(job, data) {
  const expression = job.if.trim().slice(3, -2).replaceAll('always()', 'true')
  return Function('github', 'vars', `return (${expression})`)(data.github, data.vars)
}

test('admits only the exact trusted source/workflow pair before assigning either singleton job', () => {
  for (const job of Object.values(workflow.jobs)) {
    assert.equal(job['runs-on'], 'hermes-linux-qualification')
    assert.equal(admitted(job, context()), true)
    const mutations = [
      d => { d.github.actor = 'other' }, d => { d.github.triggering_actor = 'other' },
      d => { d.github.event.pull_request.user.login = 'other' },
      d => { d.github.event.pull_request.head.repo.id = 1 },
      d => { d.github.event.pull_request.base.repo.id = 1 },
      d => { d.github.event.pull_request.head.ref = 'other' },
      d => { d.vars.HERMES_LINUX_SOURCE_SHA = 'b'.repeat(40) },
      d => { d.vars.HERMES_LINUX_WORKFLOW_SHA = 'b'.repeat(40) }
    ]
    for (const mutate of mutations) {
      const data = context()
      mutate(data)
      assert.equal(admitted(job, data), false)
    }
  }
})

test('stable fork PR runs the scoped gate, not the broad CI/Docker/Nix entry workflows', () => {
  assert.deepEqual(workflow.on.pull_request.branches, ['ryoko/stable-0.22.6'])
  for (const file of ['ci.yaml', 'docker.yml', 'nix.yml']) {
    const entry = yaml.load(readFileSync(`.github/workflows/${file}`, 'utf8'))
    assert.deepEqual(entry.on.pull_request['branches-ignore'], ['ryoko/stable-*'])
    assert.deepEqual(entry.on.push.branches, ['main'])
  }
  assert.deepEqual(workflow.permissions, { contents: 'read' })
  assert.deepEqual(workflow.jobs.status.permissions, { contents: 'read', statuses: 'write' })
})

for (const kind of ['good', 'failed-job', 'wrong-sha', 'empty-report']) {
  test(`status writer is fail-closed for ${kind} without a real API request`, () => {
    const report = { qualified: true, sha: kind === 'wrong-sha' ? 'b'.repeat(40) : source, files: 18, tests: 450 }
    const step = workflow.jobs.status.steps.at(-1).run
    const body = step.match(/<<'PY'\n([\s\S]*)\nPY\n?$/)[1]
    const prelude = `import urllib.request, json\nclass Reply:\n    status = 201\n    def __enter__(self): return self\n    def __exit__(self, *args): pass\ndef fake(request, timeout):\n    print(request.full_url)\n    print(request.data.decode())\n    return Reply()\nurllib.request.urlopen = fake\n`
    const result = spawnSync('python3', ['-c', prelude + body], { encoding: 'utf8', env: {
      ...process.env, GITHUB_TOKEN: 'synthetic-ci-token', SOURCE_SHA: source,
      RESULT: kind === 'failed-job' ? 'failure' : 'success', REPORT: kind === 'empty-report' ? '' : JSON.stringify(report),
      RUN_URL: 'https://github.com/gcs8/hermes-agent/actions/runs/1'
    } })
    assert.ifError(result.error)
    assert.ok(result.stdout.includes(`/statuses/${source}`))
    const payload = JSON.parse(result.stdout.trim().split('\n').at(-1))
    assert.equal(payload.state, kind === 'good' ? 'success' : 'failure')
    assert.equal(result.status, kind === 'good' ? 0 : 1)
    assert.equal(payload.context, 'Ryoko Desktop persistence qualification')
  })
}
