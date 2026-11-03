"""Run directly with stdlib Python; no project dependencies or network required."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("control", HERE / "ryoko_send_effort.py")
assert spec is not None and spec.loader is not None
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
WORKFLOW = (HERE.parents[1] / ".github/workflows/ryoko-send-effort.yml").read_text()


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
            ET.SubElement(suite, "testcase", classname="tests.agent.test_reasoning_effort_wire_translation", name=f"test_{n}")
        return root

    def validate(self, root, seen=None):
        path = self.root / "cases.xml"
        ET.ElementTree(root).write(path)
        return control.validate_junit(path, next(iter(control.FILES)), 3, set() if seen is None else seen)

    def test_frozen_coverage(self):
        self.assertEqual((len(control.FILES), sum(control.FILES.values())), (19, 211))

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
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
             patch.object(control, "provenance", side_effect=[None, ValueError("source changed")]), \
             patch.object(control, "git", return_value=control.BASE), \
             patch.object(control, "run", side_effect=ValueError("native failed")):
            with self.assertRaisesRegex(ValueError, "source changed"):
                control.qualify(self.root / "source", self.root / "evidence", "/usr/bin/python3")
        self.assertFalse((self.root / "evidence/qualification.json").exists())
        self.assertEqual(output.read_text(), "")

    def test_admission_as_data(self):
        guards = re.findall(r"if: >-\n(.*?)\n    runs-on:", WORKFLOW, re.DOTALL)
        self.assertEqual(len(guards), 2)
        pr = {"user": {"login": "gcs8"}, "head": {"repo": {"id": 1260443943},
              "ref": "ci/send-effort-20261103", "sha": control.BASE}, "base": {"repo": {"id": 1260443943}}}
        good = {"repository": "gcs8/hermes-agent", "repository_id": "1260443943", "actor": "gcs8",
                "triggering_actor": "gcs8", "event": {"pull_request": pr}}
        variables = {"HERMES_SEND_WORKFLOW_SHA": control.BASE, "HERMES_SEND_SOURCE_SHA": control.SOURCE}
        for guard in guards:
            expr = guard.strip()[3:-2].replace("&&", " and ")
            expr = " ".join(expr.split())
            def allowed(github, vars):
                return eval(expr, {"__builtins__": {}}, {"github": namespace(github), "vars": namespace(vars), "always": lambda: True})
            self.assertTrue(allowed(good, variables))
            paths = [("repository",), ("repository_id",), ("actor",), ("triggering_actor",),
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

    def test_status_transport_and_fail_closed(self):
        code = textwrap.dedent(WORKFLOW.split('"$STATUS_PYTHON" -I - <<\'PY\'\n')[1].rsplit("          PY", 1)[0])
        good = {"qualified": True, "source": control.SOURCE, "workflow": control.BASE,
                "files": 19, "tests": 211, "checks": 14, "scope": "Linux synthetic only"}
        variants = [(good, "success", True), (good, "failure", False), (good, "cancelled", False),
                    ({}, "success", False), ([], "success", False), (None, "success", False),
                    ("malformed", "success", False)]
        for key in good:
            variants.append(({k: v for k, v in good.items() if k != key}, "success", False))
        for key, value in (("source", "wrong"), ("workflow", "wrong"), ("files", 18), ("tests", 0),
                           ("checks", 13), ("qualified", False), ("files", True), ("scope", "full")):
            variants.append(({**good, key: value}, "success", False))
        for report, result, accepted in variants:
            env = {"REPORT": json.dumps(report) if report != "malformed" else "{", "RESULT": result,
                   "WORKFLOW_SHA": control.BASE, "GITHUB_TOKEN": "synthetic-only", "RUN_URL": "https://example.invalid/run"}
            response = SimpleNamespace(status=201)
            with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen") as transport:
                transport.return_value.__enter__.return_value = response
                if accepted:
                    exec(compile(code, "status-inline", "exec"), {})
                else:
                    with self.assertRaises(SystemExit):
                        exec(compile(code, "status-inline", "exec"), {})
                request = transport.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(body["state"], "success" if accepted else "failure")
                self.assertEqual(body["context"], "Ryoko backend effort qualification")
                self.assertTrue(request.full_url.endswith("/statuses/" + control.SOURCE))
                self.assertNotIn("synthetic-only", json.dumps(body))

    def test_clean_bootstrap_preserves_loader_without_secrets(self):
        lines = [line.strip() for line in WORKFLOW.splitlines() if line.strip().startswith("env -i ")]
        self.assertEqual(len(lines), 3)
        marker = self.root / "executed"
        stub = self.root / "bootstrap"
        stub.write_text('#!/bin/sh\n'
                        '[ "${LD_LIBRARY_PATH-}" = "synthetic-loader" ] || exit 71\n'
                        '[ -z "${SENTINEL_SECRET+x}" ] || exit 72\n'
                        f"printf 'ok\\n' >> '{marker}'\n")
        stub.chmod(0o700)
        env = {key: str(self.root) for key in
               ("HOME", "TMPDIR", "HERMES_HOME", "HERMES_RUNTIME_DIR", "QUALIFICATION_ROOT",
                "GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH")}
        env.update(PATH="/usr/bin:/bin", LD_LIBRARY_PATH="synthetic-loader",
                   SENTINEL_SECRET="must-not-inherit", BOOTSTRAP_PYTHON=str(stub), HERMES_PYTHON=str(stub))
        for line in lines:
            with self.subTest(line=line):
                broken = line.replace('LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" ', '')
                self.assertEqual(subprocess.run(["bash", "-c", broken], env=env, check=False).returncode, 71)
                self.assertEqual(subprocess.run(["bash", "-c", line], env=env, check=False).returncode, 0)
        self.assertEqual(marker.read_text().splitlines(), ["ok"] * 3)

    def test_workflow_boundaries(self):
        self.assertEqual(WORKFLOW.count("runs-on: hermes-linux-qualification"), 2)
        self.assertIn("group: hermes-linux-qualification\n  cancel-in-progress: false", WORKFLOW)
        workload, status = WORKFLOW.split("\n  status:\n")
        self.assertNotIn("statuses: write", workload)
        self.assertNotIn("actions/checkout", status)
        self.assertNotIn("GITHUB_TOKEN", workload)
        self.assertEqual(workload.count("persist-credentials: false"), 2)
        self.assertIn("--group dev --group test", workload)
        self.assertIn("branches: [ryoko/stable-0.22.6]", WORKFLOW)
        self.assertIn("types: [opened, synchronize, reopened, ready_for_review]", WORKFLOW)
        self.assertNotIn("workflow_dispatch:", WORKFLOW)
        self.assertNotIn("pull_request_target:", WORKFLOW)
        self.assertIn("retention-days: 7", workload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
