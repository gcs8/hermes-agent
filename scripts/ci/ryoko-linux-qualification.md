# Trusted Linux qualification

`ryoko-linux-qualification.yml` qualifies a fixed Linux deployment candidate. It is not
upstream CI, a release workflow, an installer, or permission to restart Hermes.

## Identities and admission

- Source S: `47909663ea283b2b77d326e456629c18dcf13f7b`.
- Plugin P: `22b832ab9f98ac48e2ff61fe779ed35d6c2e0f1c` from `gcs8/hermes-codex-lb`.
- Workflow W: the separately reviewed commit containing this workflow and its checker.
- Repository: `gcs8/hermes-agent`, immutable repository ID `1260443943`.
- Base: `ryoko/stable-0.22.2`. Head: `ci/linux-qualification-20261019`.

Before publishing W or opening its PR, the parent must set and read back
`HERMES_LINUX_WORKFLOW_SHA=W` and `HERMES_LINUX_SOURCE_SHA=S`. Both jobs reject an
unapproved head before runner assignment. Author, actor and triggering actor must all
be `gcs8`; head and base repositories must both be `gcs8/hermes-agent`. Owner reruns
of another author's PR are not admitted. Public-fork execution also depends on the
separately verified `all_external_contributors` approval policy. Workflow expressions
do not constrain a collaborator who can replace the workflow and authorize its run.

Only native PR events are enabled. There is no manual dispatch path. GitHub requires
a dispatch workflow on the default branch before manual dispatch can be used; this
candidate deliberately leaves default `main` unchanged.

The fixed `hermes-linux-qualification` label must match the separately reviewed,
repository-scoped min0/max1 ARC lane. No hosted fallback, privileged install, socket,
Docker, browser, production credential or live model endpoint is part of this gate.
The workflow concurrency group does not cancel an earlier run. The native job has a
30-minute timeout; the separate status writer has five minutes.

## Execution and acceptance

Controls W, source S and plugin P have separate shallow checkouts, with no persisted
checkout credentials. A setup-python interpreter is used only for stdlib bootstrap.
S's `scripts/ci/setup_toolchain.py prepare/install` provisions the PM-pinned Python
and uv into task-owned state. `python -m pm.build_env` builds a new locked test
venv with extras `all,discord,fal,firecrawl` and groups `dev,test`. No live or shared
Hermes runtime is activated or modified. There is no restored dependency cache.

The canonical S `scripts/run_tests.sh` receives explicit `HERMES_PYTHON`, at most
two workers and zero file retries. It clears `TMPDIR` with `env -i`, so a site hook
in this fresh task-owned venv supplies private scratch and disables lazy installs
for sanitized test and CLI/compute children. It does not edit S or add source paths.
`PYTHON_CPU_COUNT=2` also bounds the inherited precompile step before its env reset.

Success requires all of these actual executions, not skips:

| Scope | Required result |
| --- | --- |
| The 25 files listed in `REGRESSION_FILES` | 926 passes, every unique file completed |
| Real CLI synthetic Responses round trip | 1 pass, `test_parallel_function_calls_round_trip_once` with integration enabled |
| P `tests/run.sh` | 20 passes |
| P `tests/e2e/run.sh` | 2 passes, in-process and isolated compute against local fakes |
| Startup imports | requests, urllib3, fastapi, uvicorn, starlette, opentelemetry.trace, mcp.client.streamable_http |
| Model resolver import | `hermes_cli.model_switch.resolve_startup_model_route` callable |
| Checkout identities | S and P at exact clean HEAD before and after execution |

The plugin E2E runner temporarily copies its module into S and removes it on exit.
The final clean check verifies that cleanup. Canonical completion lines and totals
are parsed together with native exit codes. Missing/duplicate/zero-test files,
skipped tests, incomplete totals and explicit error receipts fail closed. The
report binds W, S, P, runner name/OS/architecture and actual lane totals. Uploads
contain only the report and scoped logs, not HOME, PM state or tokens.

GitHub permissions are job-level, not step-level. The native job has only
`contents: read`. A separate writer job has `contents: read, statuses: write`,
checks out only W, and runs no product/plugin code. Checkout receives its standard
job token without persistence; only the final stdlib API step exports `GITHUB_TOKEN`
as an environment variable. It posts `Ryoko Linux qualification` to **S**, with the
run URL, after independently validating the report and requiring native job success.
Failure/incomplete evidence posts failure when that writer executes. A cancellation,
failed writer bootstrap/API request, unavailable runner or admission rejection is
not green; the parent must inspect the exact run and read the status back. No token
is printed or uploaded.

## Inherited CI and publication limits

Only entry/always jobs in `ci.yaml`, `docker.yml`, and `nix.yml` gain upstream-owner
conditions. All other upstream YAML data, commands, names, matrices and results
remain identical. Tests remove precisely those conditions and compare the entire
parsed documents against S. The executable graph tests deny every fork job even
with release inputs. Upstream cross-platform checks remain **skipped/blocked**, not
passed by this Linux status. Historical author attribution is not rewritten.

Sibling PR workflows are audited against the complete W-vs-S file list. Path-filtered
plugin catalog, PM bundle, sandbox, Termux and Windows SDK workflows do not match
this candidate. `label-rerun.yml` only reacts to `labeled`; do not apply `run-e2e`.
Other manual, schedule, release and default-branch `workflow_run` workflows are not
enabled or dispatched here. Do not publish this candidate to `main`, dispatch
releases or expand its file scope without repeating that audit.

## Local control verification

Use the repository's canonical runner with an existing isolated interpreter, never
activate the assigned checkout or install into the agent's environment:

```sh
env -i HOME="$TASK/home" PATH=/usr/bin:/bin \
  HERMES_PYTHON=/path/to/isolated/test-env/bin/python PYTHON_CPU_COUNT=2 \
  HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 \
  bash scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/scripts/test_ryoko_linux_qualification.py
```

The tests evaluate actual YAML admission expressions with trusted, fork, foreign
repository, rerun, bot, stale approval and malformed approval fixtures. Their small
boolean-expression evaluator fails on unsupported syntax rather than approximating
new functions. YAML parsing uses the already installed `ruamel.yaml`. Evidence
acceptance and status request construction are executable tests; status API tests
use a synthetic token and intercepted transport, not GitHub writes.

Local Linux proof does not establish actual ARC image bootstrap, 1 CPU/2 GiB peak
capacity, runner assignment or native GitHub status delivery. Those remain the
parent's exact-W/S destination execution gate. No broader model, network, browser,
Windows, macOS or Desktop-rendering claim follows from these tests.
