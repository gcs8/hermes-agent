"""Run directly with stdlib Python; no project dependencies or network required."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("control", HERE / "ryoko_stable_0227.py")
assert spec is not None and spec.loader is not None
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
WORKFLOW = (HERE.parents[1] / ".github/workflows/ryoko-stable-0227.yml").read_text()


def namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: namespace(v) for k, v in value.items()})
    return value


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def xml(self, count=3):
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite", tests=str(count), failures="0", errors="0", skipped="0")
        for n in range(count):
            ET.SubElement(suite, "testcase", classname=next(iter(control.FILES)).removesuffix(".py").replace("/", "."), name=f"test_{n}")
        return root

    def validate(self, root, seen=None):
        path = self.root / "cases.xml"
        ET.ElementTree(root).write(path)
        return control.validate_junit(path, next(iter(control.FILES)), 3, set() if seen is None else seen)

    def test_frozen_coverage(self):
        self.assertEqual((len(control.FILES), sum(control.FILES.values())), (52, 1309))

    def test_valid_xml(self):
        self.assertEqual(self.validate(self.xml()), 3)

    def test_bad_junit(self):
        for bad in ("empty", "count", "duplicate", "skip", "error", "failure", "foreign", "suite", "unnamed"):
            with self.subTest(bad=bad):
                root = self.xml(0 if bad == "empty" else 2 if bad == "count" else 3)
                cases = list(root.iter("testcase"))
                if bad == "duplicate":
                    cases[1].set("name", "test_0")
                elif bad in ("skip", "error", "failure"):
                    ET.SubElement(cases[0], "skipped" if bad == "skip" else bad)
                elif bad == "foreign":
                    cases[0].set("classname", "foreign")
                elif bad == "unnamed":
                    cases[0].set("name", "")
                elif bad == "suite":
                    root[0].set("errors", "1")
                with self.assertRaises(ValueError):
                    self.validate(root)
        seen = set()
        self.validate(self.xml(), seen)
        with self.assertRaises(ValueError):
            self.validate(self.xml(), seen)

    def test_missing_or_malformed_xml(self):
        with self.assertRaises(FileNotFoundError):
            control.validate_junit(self.root / "absent", next(iter(control.FILES)), 3, set())
        (self.root / "cases.xml").write_text("not XML")
        with self.assertRaises(ET.ParseError):
            control.validate_junit(self.root / "cases.xml", next(iter(control.FILES)), 3, set())

    def test_native_nonzero_and_cancel(self):
        for code in (1, -15, 130):
            with patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=code)):
                with self.assertRaises(ValueError):
                    control.run(["false"], self.root, {}, self.root / "run.log")
        with patch.object(control.subprocess, "run", side_effect=subprocess.TimeoutExpired("test", 900)):
            with self.assertRaises(subprocess.TimeoutExpired):
                control.run(["false"], self.root, {}, self.root / "run.log")

    def test_checks_exact(self):
        control.validate_checks("scripts/check: 14 checks, ok\n")
        for text in ("", "scripts/check: 13 checks, ok", "scripts/check: 14 checks, FAILED: ruff",
                     "scripts/check: 14 checks, ok\nscripts/check: 14 checks, ok"):
            with self.assertRaises(ValueError):
                control.validate_checks(text)

    def test_provenance(self):
        for values in (["wrong"], [control.SOURCE, " M agent.py"], [control.SOURCE, "", "wrong"]):
            with patch.object(control, "git", side_effect=values):
                with self.assertRaises(ValueError):
                    control.provenance(self.root)
        with patch.object(control, "git", side_effect=[control.SOURCE, "", control.BASE]):
            control.provenance(self.root)

    def test_source_changed_after_native(self):
        output = self.root / "github-output"
        output.write_text("")
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "WORKFLOW_SHA": control.BASE}), \
             patch.object(control, "provenance", side_effect=[None, ValueError("source changed")]), \
             patch.object(control, "git", return_value=control.BASE), \
             patch.object(control, "install_temp_hook", return_value=None), \
             patch.object(control, "run", side_effect=ValueError("native failed")):
            with self.assertRaisesRegex(ValueError, "source changed"):
                control.qualify(self.root / "source", self.root / "evidence/native", "/usr/bin/python3", "/usr/bin/node", "/usr/bin/npm")
        self.assertFalse((self.root / "evidence/qualification.json").exists())
        self.assertEqual(output.read_text(), "")

    def test_admission_as_data(self):
        guards = re.findall(r"if: >-\n(.*?)\n    runs-on:", WORKFLOW, re.DOTALL)
        self.assertEqual(len(guards), 2)
        pr = {"user": {"login": "gcs8"}, "head": {"repo": {"id": 1260443943},
              "ref": "ci/stable-0227-20261103", "sha": control.BASE}, "base": {"ref": "ryoko/stable-0.22.7", "repo": {"id": 1260443943}}}
        good = {"event_name": "pull_request", "repository": "gcs8/hermes-agent", "repository_id": "1260443943", "actor": "gcs8",
                "triggering_actor": "gcs8", "event": {"pull_request": pr}}
        variables = {"HERMES_0227_WORKFLOW_SHA": control.BASE, "HERMES_0227_SOURCE_SHA": control.SOURCE}
        for guard in guards:
            expr = guard.strip()[3:-2].replace("&&", " and ")
            expr = " ".join(expr.split())
            def allowed(github, vars):
                return eval(expr, {"__builtins__": {}}, {"github": namespace(github), "vars": namespace(vars), "always": lambda: True})
            self.assertTrue(allowed(good, variables))
            paths = [("event_name",), ("event", "pull_request", "base", "ref"), ("repository",), ("repository_id",), ("actor",), ("triggering_actor",),
                     ("event", "pull_request", "user", "login"),
                     ("event", "pull_request", "head", "repo", "id"),
                     ("event", "pull_request", "base", "repo", "id"),
                     ("event", "pull_request", "head", "ref"), ("event", "pull_request", "head", "sha")]
            for path in paths:
                bad = copy.deepcopy(good)
                target = bad
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "foreign"
                self.assertFalse(allowed(bad, variables), path)
            for key in variables:
                self.assertFalse(allowed(good, {**variables, key: "wrong"}))

    def test_clean_bootstrap_preserves_loader_without_secrets(self):
        lines = [line.strip() for line in WORKFLOW.splitlines() if line.strip().startswith("env -i ")]
        self.assertEqual(len(lines), 3)
        (self.root / "setup").mkdir()
        marker = self.root / "executed"
        stub = self.root / "bootstrap"
        stub.write_text('#!/bin/sh\n'
                        '[ "${LD_LIBRARY_PATH-}" = "synthetic-loader" ] || exit 71\n'
                        '[ -z "${SENTINEL_SECRET+x}" ] || exit 72\n'
                        f"printf 'ok\\n' >> '{marker}'\n")
        stub.chmod(0o700)
        env = {key: str(self.root) for key in
               ("HOME", "TMPDIR", "HERMES_HOME", "HERMES_RUNTIME_DIR", "QUALIFICATION_ROOT",
                "GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH", "QUALIFICATION_EVIDENCE")}
        env.update(PATH="/usr/bin:/bin", LD_LIBRARY_PATH="synthetic-loader",
                   SENTINEL_SECRET="must-not-inherit", BOOTSTRAP_PYTHON=str(stub), HERMES_PYTHON=str(stub))
        for line in lines:
            with self.subTest(line=line):
                broken = line.replace('LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" ', '')
                self.assertEqual(subprocess.run(["bash", "-o", "pipefail", "-c", broken], env=env, check=False).returncode, 71)
                self.assertEqual(subprocess.run(["bash", "-o", "pipefail", "-c", line], env=env, check=False).returncode, 0)
        self.assertEqual(marker.read_text().splitlines(), ["ok"] * 3)

    def test_workflow_boundaries(self):
        self.assertEqual(WORKFLOW.count("runs-on: hermes-linux-qualification"), 2)
        self.assertIn("group: hermes-linux-qualification\n  cancel-in-progress: false", WORKFLOW)
        workload, status = WORKFLOW.split("\n  status:\n")
        self.assertNotIn("statuses: write", workload)
        self.assertNotIn("path: source", status)
        self.assertIn("path: controls", status)
        self.assertNotIn("GITHUB_TOKEN", workload)
        self.assertEqual(workload.count("persist-credentials: false"), 2)
        self.assertIn("--group dev --group test", workload)
        self.assertIn("branches: [ryoko/stable-0.22.7]", WORKFLOW)
        self.assertIn("types: [opened, synchronize, reopened, ready_for_review]", WORKFLOW)
        self.assertNotIn("workflow_dispatch:", WORKFLOW)
        self.assertNotIn("pull_request_target:", WORKFLOW)
        self.assertIn("retention-days: 7", workload)




class Extended(unittest.TestCase):
    root: Path
    setUp = Contracts.setUp
    xml = Contracts.xml
    def receipt(self):
        return {"schema": 1, "qualified": True, "source": control.SOURCE, "base": control.BASE,
                "workflow": control.BASE, "run": "42", "attempt": "2", "runner": "singleton-123",
                "runner_os": "Linux", "runner_arch": "X64", "checks": 14, "scope": control.SCOPE,
                "limits": copy.deepcopy(control.LIMITS), "native_exit": 0,
                "source_clean_before": True, "source_clean_after": True,
                "coverage": [{"file": file, "tests": count, "status": "passed"} for file, count in control.FILES.items()],
                "desktop": {"files": sorted(control.DESKTOP_FILES), "tests": control.DESKTOP_TESTS,
                            "status": "passed", "per_file": dict(control.DESKTOP_FILES)},
                "helpers": {"tests": 14, "status": "passed"}}

    def test_file_coverage_refuses_compensating_totals(self):
        good = self.receipt()["coverage"]
        self.assertEqual(control.validate_coverage(good), sum(control.FILES.values()))
        variants = [good[:-1], good + [good[0]], [{**good[0], "file": "foreign"}, *good[1:]]]
        for value in (0, -1, True, good[0]["tests"] + 1):
            variants.append([{**good[0], "tests": value}, *good[1:]])
        variants.append([{**good[0], "status": "skipped"}, *good[1:]])
        bad = copy.deepcopy(good)
        bad[1]["tests"] += bad[0]["tests"]
        bad[0]["tests"] = 0
        variants.append(bad)
        for bad in variants:
            with self.assertRaises(ValueError):
                control.validate_coverage(bad)

    def desktop_report(self):
        rows = []
        for i, name in enumerate(control.DESKTOP_FILES):
            count = control.DESKTOP_FILES[name]
            rows.append({"name": str(self.root / "apps/desktop" / name), "status": "passed",
                         "assertionResults": [{"fullName": f"case{n}", "status": "passed"} for n in range(count)]})
        return {"success": True, "numTotalTests": control.DESKTOP_TESTS, "numPassedTests": control.DESKTOP_TESTS,
                "numFailedTests": 0, "numPendingTests": 0, "testResults": rows}

    def test_desktop_report_membership_and_status(self):
        good = self.desktop_report()
        self.assertEqual(control.validate_desktop(good, self.root)["tests"], control.DESKTOP_TESTS)
        for kind in ("missing", "duplicate", "foreign", "zero", "compensating", "skipped", "failed",
                     "rowfailed", "false", "wrongtotal", "duplicatecase"):
            bad = copy.deepcopy(good)
            rows = bad["testResults"]
            if kind == "missing": rows.pop()
            if kind == "duplicate": rows[1] = rows[0]
            if kind == "foreign": rows[0]["name"] = str(self.root / "foreign.ts")
            if kind == "zero": rows[0]["assertionResults"] = []
            if kind == "compensating":
                rows[0]["assertionResults"] += rows[1]["assertionResults"]
                rows[1]["assertionResults"] = []
            if kind in ("skipped", "failed"): rows[0]["assertionResults"][0]["status"] = kind
            if kind == "rowfailed": rows[0]["status"] = "failed"
            if kind == "false": bad["success"] = False
            if kind == "wrongtotal": bad["numPassedTests"] -= 1
            if kind == "duplicatecase": rows[0]["assertionResults"][1] = rows[0]["assertionResults"][0]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                control.validate_desktop(bad, self.root)

    def test_receipt_schema(self):
        good = self.receipt()
        control.validate_report(good, control.BASE, "42", "2")
        for key in good:
            bad = copy.deepcopy(good)
            del bad[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                control.validate_report(bad, control.BASE, "42", "2")
        for key, value in (("schema", True), ("checks", True), ("native_exit", False), ("source_clean_after", False),
                           ("attempt", "1"), ("workflow", "wrong"), ("runner", ""), ("limits", {})):
            with self.assertRaises(ValueError):
                control.validate_report({**good, key: value}, control.BASE, "42", "2")

    def test_status_transport(self):
        good = self.receipt()
        for report, result, accepted in ((good, "success", True), (good, "failure", False),
                                         (good, "cancelled", False), ({}, "success", False),
                                         ([], "success", False), ("{", "success", False)):
            response = SimpleNamespace(status=201)
            with patch("urllib.request.urlopen") as transport:
                transport.return_value.__enter__.return_value = response
                text = report if isinstance(report, str) else json.dumps(report)
                if accepted:
                    control.publish_status(text, result, control.BASE, "42", "2", "synthetic-only")
                else:
                    with self.assertRaises(ValueError):
                        control.publish_status(text, result, control.BASE, "42", "2", "synthetic-only")
                request = transport.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.full_url, "https://api.github.com/repos/gcs8/hermes-agent/statuses/" + control.SOURCE)
                self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-only")
                self.assertEqual(body["state"], "success" if accepted else "failure")
                self.assertEqual(body["context"], control.CONTEXT)
                self.assertNotIn("synthetic-only", json.dumps(body))
        with patch("urllib.request.urlopen") as transport:
            transport.return_value.__enter__.return_value = SimpleNamespace(status=500)
            with self.assertRaisesRegex(ValueError, "delivery"):
                control.publish_status(json.dumps(good), "success", control.BASE, "42", "2", "synthetic-only")
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                control.publish_status(json.dumps(good), "success", control.BASE, "42", "2", "synthetic-only")

    def test_placeholder_is_refused_before_source_commands(self):
        with patch.object(control, "SOURCE", "0" * 40), patch.object(control, "git") as git:
            with self.assertRaises(ValueError): control.provenance(self.root)
            git.assert_not_called()

    def test_hook_does_not_accept_shared_interpreter(self):
        with self.assertRaises(ValueError):
            control.install_temp_hook(Path("/usr/bin/python3"), self.root, {}, self.root / "log")

    def test_native_hook_survives_env_cleared_child(self):
        (self.root / "tmp").mkdir()
        subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(self.root / "venv")],
                       check=True, timeout=30, stdout=subprocess.DEVNULL)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
               "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", "")}
        hook = control.install_temp_hook(self.root / "venv/bin/python", self.root, env, self.root / "child.log")
        self.assertTrue(hook.is_file())
        hook.unlink()
        self.assertFalse(hook.exists())

    def test_desktop_native_command_contract(self):
        evidence = self.root / "desktop-evidence"
        evidence.mkdir()
        for entry in ("node_modules/vitest/vitest.mjs", "node_modules/eslint/bin/eslint.js"):
            path = self.root / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        calls = []
        def native(argv, cwd, env, log):
            calls.append((argv, cwd))
            log.write_text("")
            if log.name == "vitest.log":
                (evidence / "vitest.json").write_text(json.dumps(self.desktop_report()))
            if log.name == "helpers.log":
                log.write_text("# tests 14\n# pass 14\n# fail 0\n# skipped 0\n# cancelled 0\n")
        with patch.object(control, "run", side_effect=native), \
             patch.object(control, "git", return_value="apps/desktop/src/lib/changed.ts\n"):
            report = control.desktop_checks(self.root, evidence, {}, Path("/pinned/node"), Path("/pinned/npm"))
        self.assertEqual(report["per_file"], control.DESKTOP_FILES)
        self.assertEqual(calls[0][0], ["/pinned/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"])
        self.assertIn("--maxWorkers=1", calls[1][0])
        self.assertEqual(calls[-1], (["/pinned/node", "scripts/i18n-keys.mjs", "--check"], self.root / "apps/desktop"))
        self.assertFalse(any("npx" in argv for argv, _ in calls))

    def test_qualify_actual_functions_with_synthetic_native_reports(self):
        file = next(iter(control.FILES))
        def native(argv, source, env, log):
            log.write_text("scripts/check: 14 checks, ok\n")
            if argv[0] == "bash":
                self.assertEqual(argv[2:7], ["-j", "1", file, "--file-retries", "0"])
                xml = Path(argv[-1].split("=", 1)[1])
                ET.ElementTree(self.xml()).write(xml)
            self.assertEqual(env["HERMES_PYTHON"], "/usr/bin/python3")
            self.assertNotIn("GITHUB_TOKEN", env)
        env = {"WORKFLOW_SHA": control.BASE, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
               "RUNNER_NAME": "synthetic", "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64",
               "GITHUB_OUTPUT": str(self.root / "output")}
        with patch.dict(os.environ, env), patch.object(control, "FILES", {file: 3}), \
             patch.object(control, "provenance") as proof, patch.object(control, "install_temp_hook", return_value=None), \
             patch.object(control, "run", side_effect=native), \
             patch.object(control, "desktop_checks", return_value=self.receipt()["desktop"]):
            report = control.qualify(self.root / "source", self.root / "evidence/native", "/usr/bin/python3", "/usr/bin/node", "/usr/bin/npm")
            self.assertTrue(report["qualified"])
            self.assertEqual(proof.call_count, 2)
            self.assertTrue((self.root / "evidence/native/qualification.json").exists())

    def test_event_filter_as_data(self):
        branch = re.search(r"branches: \[(.*?)\]", WORKFLOW).group(1)
        actions = re.search(r"types: \[(.*?)\]", WORKFLOW).group(1).split(", ")
        for event, base, action, expected in (
                ("pull_request", "ryoko/stable-0.22.7", "opened", True),
                ("pull_request", "ryoko/stable-0.22.7", "synchronize", True),
                ("pull_request", "main", "opened", False),
                ("push", "ryoko/stable-0.22.7", "opened", False),
                ("pull_request_target", "ryoko/stable-0.22.7", "opened", False),
                ("pull_request", "ryoko/stable-0.22.7", "closed", False)):
            self.assertEqual(event == "pull_request" and base == branch and action in actions, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
