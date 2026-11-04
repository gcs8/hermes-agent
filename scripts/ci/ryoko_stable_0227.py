"""Fixed-source Linux synthetic qualification, using stdlib controls only.

Source/counts are bound to the parent's final exact-head native receipts.
An all-zero source is deliberately non-runnable. Desktop typecheck is a separate
exact-head LOCAL gate, never a CI claim from this lane.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET

SOURCE = "28b6c169d21d34a38ef36edac96629c73089a47f"
BASE = "9f02d92c30efff4729ab80da2c593fc0357f3502"
# Parent PYTHON-COUNTS.json at SOURCE; CI execution remains pending.
FILES = {'tests/tui_gateway/test_busy_compression_demotion.py': 10, 'tests/tui_gateway/test_closed_session_turn_lease.py': 3, 'tests/tui_gateway/test_compute_host.py': 1, 'tests/tui_gateway/test_auto_continue.py': 23, 'tests/tui_gateway/test_compute_host_borrowed_lease.py': 4, 'tests/tui_gateway/test_compute_host_automatic_turn_busy.py': 9, 'tests/tui_gateway/test_compute_host_corrections.py': 8, 'tests/tui_gateway/test_compute_host_late_compress_ack.py': 10, 'tests/tui_gateway/test_compute_host_pending_model_switch.py': 3, 'tests/tui_gateway/test_compute_host_session_ownership.py': 2, 'tests/tui_gateway/test_compute_host_phase1.py': 9, 'tests/tui_gateway/test_diagnostic_turn_end_to_end.py': 4, 'tests/tui_gateway/test_compute_host_turn_protocol.py': 17, 'tests/tui_gateway/test_foreground_notification_snapshot.py': 10, 'tests/tui_gateway/test_failed_turn_retention.py': 19, 'tests/tui_gateway/test_interrupt_dead_compute_host.py': 5, 'tests/tui_gateway/test_isolated_orphan_activity.py': 6, 'tests/tui_gateway/test_heartbeat_tui_tick.py': 7, 'tests/tui_gateway/test_none_agent_turn_guard.py': 3, 'tests/tui_gateway/test_notification_admission_refusal.py': 2, 'tests/tui_gateway/test_notification_turn_release.py': 6, 'tests/tui_gateway/test_queued_prompt_persistence.py': 9, 'tests/tui_gateway/test_submit_time_user_row.py': 6, 'tests/tui_gateway/test_tui_gateway_notification_poller.py': 1, 'tests/tui_gateway/test_tui_gateway_queue_on_busy.py': 32, 'tests/tui_gateway/test_tui_gateway_server.py': 635}
DESKTOP_FILES = {'src/api/canonical-live-pick.test.ts': 5, 'src/api/canonical-protocol.test.ts': 16, 'src/api/canonical-reasoning-config-set.test.ts': 1, 'src/app/hooks/use-keybinds-reasoning.test.tsx': 6, 'src/app/session/hooks/use-hermes-config.test.ts': 9, 'src/app/session/hooks/use-message-stream/gateway-event/session-info.test.ts': 14, 'src/app/session/hooks/use-model-controls-stale-pick.test.tsx': 8, 'src/app/session/hooks/use-prompt-actions/slash-reasoning.test.ts': 33, 'src/app/session/hooks/use-session-actions.test.tsx': 147, 'src/app/session/hooks/use-session-actions/effort-persistence.test.ts': 8, 'src/app/session/hooks/use-session-actions/session-create-request.test.ts': 3, 'src/app/session/hooks/use-session-actions/utils.test.ts': 141, 'src/app/session/hooks/use-session-state-cache.test.tsx': 38, 'src/app/settings/config-field-effort.test.tsx': 2, 'src/app/settings/config-field-voice-locale.test.tsx': 2, 'src/app/settings/config-field.test.ts': 3, 'src/app/shell/model-catalog-menu.test.tsx': 37, 'src/app/shell/model-edit-submenu.test.tsx': 7, 'src/app/shell/use-model-menu-controller.test.tsx': 2, 'src/i18n/backend-packs.test.ts': 6, 'src/i18n/bots-advanced.test.tsx': 2, 'src/i18n/bots-cron.test.tsx': 2, 'src/i18n/bots-editor.test.tsx': 2, 'src/i18n/canonical-group-labels.test.ts': 1, 'src/i18n/catalog-completeness.test.ts': 20, 'src/i18n/context.test.tsx': 17, 'src/i18n/languages.test.ts': 4, 'src/i18n/overlay-completeness.test.ts': 2, 'src/i18n/plugin-i18n.test.tsx': 7, 'src/i18n/profile-locale.test.tsx': 5, 'src/i18n/registry.test.ts': 11, 'src/i18n/runtime.test.ts': 5, 'src/lib/reasoning-step.test.ts': 6, 'src/store/daybreak.test.ts': 6, 'src/store/model-presets-effort.test.ts': 7, 'src/store/model-presets.test.ts': 5, 'src/store/reasoning-effort-wire-scope.test.ts': 2}
DESKTOP_TESTS = 592
LIMITS = {"typecheck": "separate-local-gate",
          "ci_typecheck": "not run here; separate exact-head LOCAL gate required",
          "native_browser": "not qualified", "windows": "not qualified",
          "provider_live": "not qualified", "electron": "not qualified"}
SCOPE = "Linux synthetic Python and Desktop UI only"
CONTEXT = "Ryoko session busy qualification"

def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], text=True, encoding="utf-8",
                                   env={"PATH": "/usr/bin:/bin"}, timeout=30).strip()


def provenance(source):
    if not re.fullmatch(r"[a-f0-9]{40}", SOURCE) or int(SOURCE, 16) == 0:
        raise ValueError("source pin is not frozen")
    if git(source, "rev-parse", "HEAD") != SOURCE:
        raise ValueError("wrong source SHA")
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("source changed or is dirty")
    if git(source, "rev-parse", BASE) != BASE:
        raise ValueError("missing pinned base")


def validate_junit(path, file, expected, seen):
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError("not JUnit")
    cases = list(root.iter("testcase"))
    if len(cases) != expected or not cases:
        raise ValueError("wrong or empty case count")
    for suite in root.iter("testsuite"):
        if int(suite.get("tests", "-1")) != len(list(suite.iter("testcase"))):
            raise ValueError("inconsistent suite count")
        if any(int(suite.get(key, "0")) for key in ("errors", "failures", "skipped")):
            raise ValueError("nonpassing suite")
    prefix = file.removesuffix(".py").replace("/", ".")
    for case in cases:
        identity = (case.get("classname", ""), case.get("name", ""))
        if not identity[1] or not (identity[0] == prefix or identity[0].startswith(prefix + ".")):
            raise ValueError("foreign or unnamed testcase")
        if identity in seen:
            raise ValueError("duplicate testcase")
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise ValueError("nonpassing testcase")
        seen.add(identity)
    return len(cases)


def validate_checks(text):
    if re.findall(r"^scripts/check: (.+)$", text, re.MULTILINE) != ["14 checks, ok"]:
        raise ValueError("source checks incomplete")


def run(argv, source, env, log):
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(argv, cwd=source, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                timeout=900, check=False)
    if result.returncode != 0:
        raise ValueError(f"native command failed ({result.returncode}): {log.name}")


def validate_coverage(records):
    if not isinstance(records, list) or len(records) != len(FILES):
        raise ValueError("missing or extra Python files")
    found = {}
    for rec in records:
        if not isinstance(rec, dict) or rec.get("file") not in FILES or rec["file"] in found:
            raise ValueError("foreign or duplicate Python file")
        count = rec.get("tests")
        if type(count) is not int or count <= 0 or count != FILES[rec["file"]]:
            raise ValueError("wrong per-file Python count")
        if rec.get("status") != "passed":
            raise ValueError("incomplete Python file")
        found[rec["file"]] = count
    return sum(found.values())


def validate_desktop(data, source):
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("Desktop native report failed")
    rows = data.get("testResults")
    if not isinstance(rows, list):
        raise ValueError("missing Desktop files")
    found, total = set(), 0
    desktop = Path(source).resolve() / "apps/desktop"
    for row in rows:
        name = Path(row["name"]).resolve().relative_to(desktop).as_posix()
        if name not in DESKTOP_FILES or name in found or row.get("status") != "passed":
            raise ValueError("foreign, duplicate or nonpassing Desktop file")
        assertions = row.get("assertionResults")
        if not isinstance(assertions, list) or not assertions or len(assertions) != DESKTOP_FILES[name]:
            raise ValueError("empty Desktop file")
        identities = set()
        for case in assertions:
            identity = case.get("fullName")
            if not isinstance(identity, str) or not identity or identity in identities:
                raise ValueError("duplicate or unnamed Desktop assertion")
            if case.get("status") != "passed":
                raise ValueError("nonpassing Desktop assertion")
            identities.add(identity)
        found.add(name)
        total += len(assertions)
    if found != set(DESKTOP_FILES) or total != DESKTOP_TESTS:
        raise ValueError("incomplete Desktop coverage")
    for key, expected in (("numTotalTests", total), ("numPassedTests", total),
                          ("numFailedTests", 0), ("numPendingTests", 0)):
        if type(data.get(key)) is not int or data[key] != expected:
            raise ValueError("inconsistent Desktop totals")
    return {"files": sorted(found), "tests": total, "status": "passed", "per_file": dict(DESKTOP_FILES)}


def validate_report(report, workflow, run_id, attempt):
    if not re.fullmatch(r"[a-f0-9]{40}", SOURCE) or int(SOURCE, 16) == 0:
        raise ValueError("source pin is not frozen")
    expected = {"schema": 1, "qualified": True, "source": SOURCE, "base": BASE,
                "workflow": workflow, "run": run_id, "attempt": attempt,
                "checks": 14, "scope": SCOPE, "limits": LIMITS,
                "native_exit": 0, "source_clean_before": True, "source_clean_after": True}
    if not isinstance(report, dict):
        raise ValueError("not a qualification receipt")
    for key, value in expected.items():
        if type(report.get(key)) is not type(value) or report[key] != value:
            raise ValueError("wrong receipt field: " + key)
    for key in ("runner", "runner_os", "runner_arch"):
        if not isinstance(report.get(key), str) or not report[key]:
            raise ValueError("missing runner identity")
    validate_coverage(report.get("coverage"))
    if report.get("desktop") != {"files": sorted(DESKTOP_FILES), "tests": DESKTOP_TESTS,
                                  "status": "passed", "per_file": dict(DESKTOP_FILES)}:
        raise ValueError("incomplete Desktop receipt")
    if report.get("helpers") != {"tests": 14, "status": "passed"}:
        raise ValueError("incomplete helper receipt")
    return True


def publish_status(report_text, result, workflow, run_id, attempt, token, transport=None):
    try:
        report = json.loads(report_text)
        good = result == "success" and validate_report(report, workflow, run_id, attempt)
    except (ValueError, TypeError, KeyError):
        good = False
    if int(SOURCE, 16) == 0:
        raise ValueError("placeholder cannot receive status")
    body = {"state": "success" if good else "failure", "context": CONTEXT,
            "description": "Linux synthetic only; CI typecheck not run" if good else "Scoped workload failed or receipt incomplete",
            "target_url": f"https://github.com/gcs8/hermes-agent/actions/runs/{run_id}"}
    request = urllib.request.Request(
        f"https://api.github.com/repos/gcs8/hermes-agent/statuses/{SOURCE}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    with (transport or urllib.request.urlopen)(request, timeout=30) as response:
        if response.status != 201:
            raise ValueError("status delivery failed")
    if not good:
        raise ValueError("scoped workload failed or receipt incomplete")


def desktop_checks(source, evidence, env, node, npm):
    run([str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        source, env, evidence / "npm-ci.log")
    vitest = source / "node_modules/vitest/vitest.mjs"
    eslint = source / "node_modules/eslint/bin/eslint.js"
    for tool in (vitest, eslint):
        if not tool.is_file():
            raise ValueError("missing source-installed tool: " + str(tool))
    report = evidence / "vitest.json"
    run([str(node), str(vitest), "run", "--project", "ui", "--maxWorkers=1",
         "--reporter=default", "--reporter=json", f"--outputFile.json={report}",
         *DESKTOP_FILES], source / "apps/desktop", env, evidence / "vitest.log")
    desktop = validate_desktop(json.loads(report.read_text(encoding="utf-8-sig")), source)
    changed = git(source, "diff", "--name-only", "--diff-filter=ACMR", BASE, SOURCE).splitlines()
    lint = [f for f in changed if f.startswith("apps/desktop/") and
            Path(f).suffix in (".ts", ".tsx", ".mjs", ".cjs")]
    if lint:
        run([str(node), str(eslint), *[str(source / f) for f in lint]],
            source / "apps/desktop", env, evidence / "eslint.log")
    helpers = ["scripts/ci/ryoko_desktop_effort.mjs", "scripts/ci/ryoko_desktop_effort.test.mjs"]
    run([str(node), str(eslint), "--config", "eslint.config.shared.mjs", "--global", "process",
         "--global", "console", *helpers], source, env, evidence / "helpers-eslint.log")
    run([str(node), "--test", "--test-reporter=tap", helpers[1]], source, env, evidence / "helpers.log")
    text = (evidence / "helpers.log").read_text(encoding="utf-8-sig")
    if not all(re.search(r"^# " + key + r" " + str(value) + r"$", text, re.MULTILINE)
               for key, value in (("tests", 14), ("pass", 14), ("fail", 0), ("skipped", 0), ("cancelled", 0))):
        raise ValueError("helper contracts incomplete")
    run([str(node), "scripts/i18n-keys.mjs", "--check"], source / "apps/desktop",
        env, evidence / "i18n-keys.log")
    return desktop


def install_temp_hook(python, root, env, log):
    # The canonical runner clears TMPDIR. This hook belongs only to this job's
    # freshly built test venv, never to source, PM's store, or a shared interpreter.
    venv = root / "venv"
    if python != venv / "bin/python" or not (venv / "pyvenv.cfg").is_file():
        raise ValueError("temporary hook requires the job-owned test venv")
    site = subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        env=env, text=True, timeout=30, encoding="utf-8").strip()
    site = Path(site)
    site.relative_to(venv)
    hook = site / "ryoko_qualification_tmp.pth"
    with hook.open("x", encoding="utf-8") as stream:
        stream.write("import tempfile; tempfile.tempdir = " + repr(str(root / "tmp")) + "\n")
    try:
        child_env = {key: env[key] for key in ("PATH", "HOME", "LD_LIBRARY_PATH")}
        run([str(python), "-I", "-c",
             "import tempfile; assert tempfile.gettempdir() == " + repr(str(root / "tmp"))],
            root, child_env, log)
    except (ValueError, subprocess.SubprocessError):
        hook.unlink()
        raise
    return hook


def qualify(source, evidence, python, node, npm):
    source, evidence = Path(source).absolute(), Path(evidence).absolute()
    python, node, npm = Path(python).absolute(), Path(node).absolute(), Path(npm).absolute()
    provenance(source)
    evidence.mkdir(parents=True, mode=0o700, exist_ok=True)
    if any(evidence.iterdir()):
        raise ValueError("evidence directory must be empty")
    root = evidence.parents[1]
    for name in ("home", "tmp", "state", "cache"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    for name in ("npm-user.conf", "npm-global.conf"):
        (root / name).write_text("", encoding="utf-8")
    env = {"PATH": f"{python.parent}:{npm.parent}:{node.parent}:/usr/sbin:/usr/bin:/sbin:/bin",
           "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
           "HERMES_HOME": str(root / "state"), "HERMES_RUNTIME_DIR": str(root / "state/tools"),
           "HERMES_PYTHON": str(python), "HERMES_DISABLE_LAZY_INSTALLS": "1",
           "LANG": "C.UTF-8", "TZ": "UTC", "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
           "NPM_CONFIG_USERCONFIG": str(root / "npm-user.conf"),
           "NPM_CONFIG_GLOBALCONFIG": str(root / "npm-global.conf"),
           "NPM_CONFIG_CACHE": str(root / "home/.npm")}
    workflow = os.environ["WORKFLOW_SHA"]
    if not re.fullmatch(r"[a-f0-9]{40}", workflow):
        raise ValueError("wrong workflow SHA")
    py_dir, js_dir = evidence / "python", evidence / "desktop"
    py_dir.mkdir()
    js_dir.mkdir()
    seen, records = set(), []
    hook = None
    try:
        hook = install_temp_hook(python, root, env, py_dir / "child-temp.log")
        for index, (file, expected) in enumerate(FILES.items()):
            xml = py_dir / f"{index:02d}.xml"
            run(["bash", "scripts/run_tests.sh", "-j", "1", file, "--file-retries", "0",
                 f"--junitxml={xml}"], source, env, py_dir / f"{index:02d}.log")
            count = validate_junit(xml, file, expected, seen)
            records.append({"file": file, "tests": count, "xml": xml.name, "status": "passed"})
        validate_coverage(records)
        desktop = desktop_checks(source, js_dir, env, node, npm)
        run([str(python), "scripts/check", "--base", BASE], source, env, py_dir / "checks.log")
        validate_checks((py_dir / "checks.log").read_text(encoding="utf-8-sig"))
    finally:
        if hook is not None:
            hook.unlink()
        provenance(source)
    report = {"schema": 1, "qualified": True, "source": SOURCE, "base": BASE,
              "workflow": workflow, "run": os.environ["GITHUB_RUN_ID"],
              "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
              "runner": os.environ["RUNNER_NAME"], "runner_os": os.environ["RUNNER_OS"],
              "runner_arch": os.environ["RUNNER_ARCH"], "checks": 14, "scope": SCOPE,
              "limits": LIMITS, "coverage": records, "desktop": desktop,
              "helpers": {"tests": 14, "status": "passed"}, "native_exit": 0,
              "source_clean_before": True, "source_clean_after": True}
    validate_report(report, workflow, report["run"], report["attempt"])
    text = json.dumps(report, separators=(",", ":"))
    (evidence / "qualification.json").write_text(text + "\n", encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("report=" + text + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    for name in ("source", "evidence", "python", "node", "npm"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.status:
        publish_status(os.environ.get("REPORT", ""), os.environ["RESULT"], os.environ["WORKFLOW_SHA"],
                       os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], os.environ["GITHUB_TOKEN"])
    else:
        if any(getattr(args, name) is None for name in ("source", "evidence", "python", "node", "npm")):
            parser.error("--source, --evidence, --python, --node and --npm are required")
        qualify(args.source, args.evidence, args.python, args.node, args.npm)


if __name__ == "__main__":
    main()
