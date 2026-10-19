"""Executable admission and evidence contracts for the fork-only Linux gate."""
from __future__ import annotations

import ast
import fnmatch
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "47909663ea283b2b77d326e456629c18dcf13f7b"
WORKFLOW = "a" * 40
PLUGIN = "22b832ab9f98ac48e2ff61fe779ed35d6c2e0f1c"
UPSTREAM = "github.repository == 'NousResearch/hermes-agent'"
GUARDS = {"ci.yaml": ["detect", "all-checks-pass", "ci-timings"],
          "docker.yml": ["mode", "release-phase-gate"], "nix.yml": ["detect"]}


def load_workflow():
    path = ROOT / ".github/workflows/ryoko-linux-qualification.yml"
    assert path.is_file(), "trusted qualification workflow missing"
    return YAML(typ="safe").load(path.read_text())


def module():
    path = ROOT / "scripts/ci/ryoko_linux_qualification.py"
    assert path.is_file(), "executable evidence checker missing"
    spec = importlib.util.spec_from_file_location("qualification", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Context(dict):
    def __getitem__(self, key):
        value = self.get(key, "")
        return Context(value) if isinstance(value, dict) else value


def expression(value, context, results=()):
    """Execute the YAML's boolean-expression subset, including implicit success().

    Fail closed on unsupported syntax instead of silently approximating a new gate.
    This consumes the actual workflow expression, not a second admission policy.
    """
    if isinstance(value, bool):
        return value and all(item == "success" for item in results)
    value = str(value or "true").strip().removeprefix("${{").removesuffix("}}").strip()
    explicit_status = bool(re.search(r"\b(?:always|success|failure|cancelled)\(", value))
    value = value.replace("&&", " and ").replace("||", " or ")
    value = re.sub(r"!(?!=)", " not ", value).strip()
    value = re.sub(r"\b(github|vars|needs|inputs)((?:\.[A-Za-z_][\w-]*)+)",
                   lambda match: match[1] + "".join("[" + repr(part) + "]" for part in match[2][1:].split(".")), value)
    tree = ast.parse("(" + value + ")", mode="eval")
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
               ast.Compare, ast.Eq, ast.NotEq, ast.Name, ast.Load, ast.Attribute,
               ast.Constant, ast.Call, ast.Subscript)
    assert all(isinstance(node, allowed) for node in ast.walk(tree)), value
    functions = {"always": lambda: True, "cancelled": lambda: "cancelled" in results,
                 "success": lambda: all(item == "success" for item in results),
                 "failure": lambda: "failure" in results}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name) and node.func.id in functions
            assert not node.args and not node.keywords
    scope = {key: Context(item) for key, item in context.items()}
    scope.update(functions, true=True, false=False)
    return (explicit_status or functions["success"]()) and bool(eval(compile(tree, "<workflow if>", "eval"), {"__builtins__": {}}, scope))


def trusted_context():
    return {"github": {"repository": "gcs8/hermes-agent", "repository_id": "1260443943",
                       "actor": "gcs8", "triggering_actor": "gcs8", "event_name": "pull_request",
                       "event": {"repository": {"full_name": "gcs8/hermes-agent", "id": 1260443943},
                                 "pull_request": {"user": {"login": "gcs8"},
                                    "head": {"repo": {"full_name": "gcs8/hermes-agent"}, "ref": "ci/linux-qualification-20261019", "sha": WORKFLOW},
                                    "base": {"repo": {"full_name": "gcs8/hermes-agent"}, "ref": "ryoko/stable-0.22.2"}}}},
            "vars": {"HERMES_LINUX_WORKFLOW_SHA": WORKFLOW, "HERMES_LINUX_SOURCE_SHA": SOURCE},
            "needs": {"qualify": {"result": "success"}}}


def set_path(context, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        context = context[part]
    context[parts[-1]] = value


@pytest.mark.parametrize("job", ["qualify", "status"])
def test_trusted_pr_admitted_before_assignment(job):
    workflow = load_workflow()
    assert expression(workflow["jobs"][job]["if"], trusted_context())
    assert workflow["jobs"][job]["runs-on"] == "hermes-linux-qualification"


@pytest.mark.parametrize("path,value", [
    ("github.repository", "outsider/hermes-agent"),
    ("github.repository_id", "999"),
    ("github.actor", "outsider"), ("github.triggering_actor", "outsider"),
    ("github.actor", "dependabot[bot]"),
    ("github.event_name", "workflow_dispatch"), ("github.event_name", "push"),
    ("github.event.pull_request.user.login", "outsider"),
    ("github.event.pull_request.user.login", "dependabot[bot]"),
    ("github.event.pull_request.head.repo.full_name", "outsider/hermes-agent"),
    ("github.event.pull_request.base.repo.full_name", "outsider/hermes-agent"),
    ("github.event.pull_request.base.ref", "main"),
    ("github.event.pull_request.head.ref", "other"),
    ("github.event.pull_request.head.sha", "b" * 40),
    ("vars.HERMES_LINUX_SOURCE_SHA", "b" * 40),
    ("vars.HERMES_LINUX_SOURCE_SHA", "47909663"),
    ("vars.HERMES_LINUX_WORKFLOW_SHA", "a" * 39),
    ("vars.HERMES_LINUX_WORKFLOW_SHA", "not-a-sha"),
    ("vars.HERMES_LINUX_WORKFLOW_SHA", ""),
])
def test_untrusted_or_stale_event_never_assigns_either_job(path, value):
    context = trusted_context()
    set_path(context, path, value)
    for job in load_workflow()["jobs"].values():
        assert not expression(job["if"], context), path


def test_owner_rerun_of_foreign_code_is_denied():
    context = trusted_context()
    context["github"]["event"]["pull_request"]["user"]["login"] = "outsider"
    context["github"]["triggering_actor"] = "gcs8"
    for job in load_workflow()["jobs"].values():
        assert not expression(job["if"], context)


def test_status_runs_after_native_failure_but_does_not_admit_untrusted_code():
    condition = load_workflow()["jobs"]["status"]["if"]
    assert expression(condition, trusted_context(), ["failure"])
    context = trusted_context()
    context["github"]["actor"] = "outsider"
    assert not expression(condition, context, ["failure"])


def test_workflow_has_no_hosted_fallback_or_source_write_permission():
    workflow = load_workflow()
    lint_config = YAML(typ="safe").load((ROOT / ".github/actionlint.yaml").read_text())
    assert "hermes-linux-qualification" in lint_config["self-hosted-runner"]["labels"]
    assert set(workflow["on"]) == {"pull_request"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["qualify"].get("permissions", workflow["permissions"]) == {"contents": "read"}
    assert workflow["jobs"]["status"]["permissions"] == {"contents": "read", "statuses": "write"}
    expected = {"control": "${{ vars.HERMES_LINUX_WORKFLOW_SHA }}",
                "candidate": "${{ vars.HERMES_LINUX_SOURCE_SHA }}", "plugin": PLUGIN}
    for name, job in workflow["jobs"].items():
        assert job["timeout-minutes"] <= 30
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w/-]+@[a-f0-9]{40}", step["uses"])
            if step.get("uses", "").startswith("actions/checkout@"):
                config = step["with"]
                assert config["persist-credentials"] is False
                assert config["fetch-depth"] == 1
                assert config["ref"] == expected[config["path"]]
                if name == "status":
                    assert config["path"] == "control"


@pytest.mark.parametrize("filename", GUARDS)
def test_inherited_semantics_identical_after_removing_only_owner_guards(filename):
    path = ".github/workflows/" + filename
    old = YAML(typ="safe").load(subprocess.check_output(["git", "show", SOURCE + ":" + path], cwd=ROOT, text=True))
    new = YAML(typ="safe").load((ROOT / path).read_text())
    for job in GUARDS[filename]:
        condition = new["jobs"][job]["if"]
        prefix = "${{ " + UPSTREAM
        assert condition.startswith(prefix), (filename, job, condition)
        original = old["jobs"][job].get("if")
        if original is None:
            assert condition == prefix + " }}"
            del new["jobs"][job]["if"]
        else:
            assert condition == prefix + " && (" + original + ") }}"
            new["jobs"][job]["if"] = original
    assert new == old


@pytest.mark.parametrize("filename", GUARDS)
@pytest.mark.parametrize("event,phase", [("pull_request", ""), ("push", ""), ("workflow_call", "test"), ("workflow_call", "publish")])
def test_inherited_graph_schedules_no_fork_job_even_with_release_inputs(filename, event, phase):
    jobs = YAML(typ="safe").load((ROOT / ".github/workflows" / filename).read_text())["jobs"]
    results = {}
    context = trusted_context()
    context["github"].update(event_name=event, ref="refs/heads/main")
    context["inputs"] = {"release": True, "release-phase": phase}
    for name, job in jobs.items():
        dependencies = job.get("needs", [])
        dependencies = [dependencies] if isinstance(dependencies, str) else dependencies
        context["needs"] = {key: {"result": results[key], "outputs": {}} for key in dependencies}
        assert not expression(job.get("if"), context, [results[key] for key in dependencies]), name
        results[name] = "skipped"


def good_report():
    m = module()
    return {"workflow_sha": WORKFLOW, "source_sha": SOURCE, "plugin_sha": PLUGIN,
            "source_before": {"head": SOURCE, "clean": True}, "source_after": {"head": SOURCE, "clean": True},
            "plugin_before": {"head": PLUGIN, "clean": True}, "plugin_after": {"head": PLUGIN, "clean": True},
            "runner": {"name": "fixture", "os": "Linux", "arch": "X64"},
            "startup_imports": list(m.STARTUP_IMPORTS), "model_resolver": True,
            "lanes": {"regressions": {"exit_code": 0, "files": list(m.REGRESSION_FILES), "passed": 926},
                      "cli": {"exit_code": 0, "files": [m.CLI_FILE], "passed": 1},
                      "plugin_unit": {"exit_code": 0, "passed": 20},
                      "plugin_e2e": {"exit_code": 0, "files": [m.PLUGIN_E2E_FILE], "passed": 2}}}


def test_complete_real_scope_is_required_for_success():
    assert module().accept_report(good_report(), WORKFLOW)


@pytest.mark.parametrize("mutation", ["missing_file", "duplicate_file", "wrong_total", "skipped_cli", "plugin_not_run", "exit_failure", "source_dirty", "source_changed", "plugin_dirty", "workflow_drift", "missing_import", "no_resolver", "not_linux", "malformed"])
def test_partial_or_misattributed_report_cannot_be_green(mutation):
    report = good_report()
    if mutation == "missing_file": report["lanes"]["regressions"]["files"].pop()
    elif mutation == "duplicate_file": report["lanes"]["regressions"]["files"][-1] = report["lanes"]["regressions"]["files"][0]
    elif mutation == "wrong_total": report["lanes"]["regressions"]["passed"] = 925
    elif mutation == "skipped_cli": report["lanes"]["cli"]["passed"] = 0
    elif mutation == "plugin_not_run": del report["lanes"]["plugin_e2e"]
    elif mutation == "exit_failure": report["lanes"]["plugin_unit"]["exit_code"] = 1
    elif mutation == "source_dirty": report["source_after"]["clean"] = False
    elif mutation == "source_changed": report["source_after"]["head"] = "b" * 40
    elif mutation == "plugin_dirty": report["plugin_after"]["clean"] = False
    elif mutation == "workflow_drift": report["workflow_sha"] = "b" * 40
    elif mutation == "missing_import": report["startup_imports"].pop()
    elif mutation == "no_resolver": report["model_resolver"] = False
    elif mutation == "not_linux": report["runner"]["os"] = "Windows"
    elif mutation == "malformed": report = {}
    assert not module().accept_report(report, WORKFLOW)


def native_log(files, total):
    lines = [f"[100.0% | 1/~1 | ✓1 | ✗0] ✓ {file} ({total if index == 0 else 1}✓, 1.0s)" for index, file in enumerate(files)]
    count = total + len(files) - 1
    return "\n".join(lines) + f"\n=== Summary: {len(files)} files, {count} tests passed, 0 failed (100% complete) in 1.0s (2 workers) ===\n"


def test_parser_requires_completed_unique_files_and_native_summary():
    m = module()
    text = native_log([m.CLI_FILE], 1)
    assert m.verify_native(text, [m.CLI_FILE], 1)["passed"] == 1
    for broken in ["", text.replace("100% complete", "99% complete"), text.replace("(1✓,", "(1s,"), text + text,
                   text.replace(m.CLI_FILE, "tests/other.py"), text.replace("0 failed", "1 failed")]:
        with pytest.raises(ValueError):
            m.verify_native(broken, [m.CLI_FILE], 1)


def test_unit_parser_rejects_empty_skipped_or_failed_runs():
    m = module()
    assert m.verify_unit("20 passed in 1.00s\n", 20)["passed"] == 20
    for text in ["", "20 skipped in 1.00s", "19 passed, 1 skipped in 1.00s", "20 passed, 1 failed in 1.00s"]:
        with pytest.raises(ValueError): m.verify_unit(text, 20)


def test_status_payload_is_bound_to_source_and_requires_native_job_success():
    m = module()
    report = good_report()
    for result, expected in [("success", "success"), ("failure", "failure"), ("skipped", "failure"), ("cancelled", "failure")]:
        payload = m.status_payload(result, report, WORKFLOW, "https://github.com/gcs8/hermes-agent/actions/runs/1")
        assert payload["state"] == expected
        assert payload["context"] == "Ryoko Linux qualification"
    assert m.status_payload("success", {}, WORKFLOW, "https://example.invalid")["state"] == "failure"


def test_error_receipt_cannot_be_accepted_even_with_prior_complete_results():
    report = good_report()
    report["error"] = "post-test verification failed"
    assert not module().accept_report(report, WORKFLOW)


def test_zero_test_file_cannot_hide_behind_a_compensating_total():
    m = module()
    text = native_log(["tests/a.py", "tests/b.py"], 2).replace("tests/b.py (1✓", "tests/b.py (0✓").replace("tests/a.py (2✓", "tests/a.py (3✓")
    with pytest.raises(ValueError):
        m.verify_native(text, ["tests/a.py", "tests/b.py"], 3)


def test_sibling_pr_workflows_do_not_match_this_frozen_scope():
    changed = subprocess.check_output(["git", "diff", "--name-only", SOURCE], cwd=ROOT, text=True).splitlines()
    changed += subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, text=True).splitlines()
    for path in (ROOT / ".github/workflows").glob("*"):
        data = YAML(typ="safe").load(path.read_text())
        events = data.get("on", {})
        if not isinstance(events, dict) or "pull_request" not in events or path.name in {*GUARDS, "ryoko-linux-qualification.yml"}:
            continue
        event = events["pull_request"] or {}
        if not set(event.get("types", ["opened", "synchronize", "reopened"])) & {"opened", "synchronize", "reopened", "ready_for_review"}:
            continue
        assert "paths" in event, path.name
        for filename in changed:
            selected = False
            for pattern in event["paths"]:
                if fnmatch.fnmatchcase(filename, pattern.removeprefix("!")):
                    selected = not pattern.startswith("!")
            assert not selected, (path.name, filename)


def test_single_cpu_lane_uses_one_native_worker_everywhere(tmp_path):
    m = module()
    env = m.isolated_environment(Path(sys.executable), tmp_path / "home", tmp_path / "tmp")
    assert env["HERMES_TEST_WORKERS"] == "1"
    assert env["PYTHON_CPU_COUNT"] == "1"
    assert m.runner_args() == ["bash", "scripts/run_tests.sh", "-j", "1", "--file-retries", "0"]
    assert m.runner_args("tests/e2e/run.sh") == ["bash", "tests/e2e/run.sh", "-j", "1", "--file-retries", "0"]


def test_test_subprocesses_receive_only_task_owned_state(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-must-not-propagate")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-propagate.invalid")
    env = m.isolated_environment(Path(sys.executable), tmp_path / "home", tmp_path / "tmp")
    assert "OPENAI_API_KEY" not in env and "OPENAI_BASE_URL" not in env
    rc, text = m.run_command([sys.executable, "-c", "import os; print(os.environ.get('OPENAI_API_KEY', 'absent'))"], tmp_path, env, tmp_path / "command.log")
    assert rc == 0 and text == "absent\n"


def test_private_site_hook_survives_env_i_without_mutating_source(tmp_path):
    m = module()
    venv = tmp_path / "test-env"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    python = venv / "bin/python"
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    m.prepare_interpreter(python, scratch)
    output = subprocess.check_output([str(python), "-c", "import tempfile,os,json; print(json.dumps([tempfile.gettempdir(), os.environ['HERMES_DISABLE_LAZY_INSTALLS']]))"],
                                     env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, text=True)
    assert json.loads(output) == [str(scratch), "1"]
    with pytest.raises(ValueError, match="existing task site hook"):
        m.prepare_interpreter(python, scratch)


@pytest.mark.parametrize("state", ["success", "failure"])
def test_status_writer_uses_fixed_source_endpoint_without_logging_token(state, monkeypatch, capsys):
    m = module()
    report = good_report() if state == "success" else {}
    for key, value in {"QUALIFICATION_REPORT": json.dumps(report), "QUALIFICATION_RESULT": "success",
                       "APPROVED_WORKFLOW_SHA": WORKFLOW, "QUALIFICATION_RUN_URL": "https://github.com/gcs8/hermes-agent/actions/runs/1",
                       "GITHUB_TOKEN": "synthetic-test-token"}.items():
        monkeypatch.setenv(key, value)
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({"state": state, "context": "Ryoko Linux qualification"}).encode()
    def post(request, timeout):
        captured.append(request)
        return Response()
    monkeypatch.setattr(m.urllib.request, "urlopen", post)
    if state == "failure":
        with pytest.raises(SystemExit): m.publish_status()
    else:
        m.publish_status()
    request = captured[0]
    assert request.full_url == f"https://api.github.com/repos/gcs8/hermes-agent/statuses/{SOURCE}"
    assert json.loads(request.data)["state"] == state
    assert request.get_header("Authorization") == "Bearer synthetic-test-token"
    assert "synthetic-test-token" not in capsys.readouterr().out
