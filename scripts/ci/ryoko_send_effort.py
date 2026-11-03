"""Bounded Linux/synthetic qualification of the reviewed backend source (stdlib only)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

SOURCE = "99e93a166d152a83a3f323e7541990213dd92b45"
BASE = "f5078c8c2ca78b366f7114b524298b0f85faecf3"
# Frozen from correction/green-final.log; counts are actual parameterized cases.
FILES = {
    "tests/agent/test_reasoning_effort_wire_translation.py": 3,
    "tests/agent/test_reasoning_effort_module.py": 31,
    "tests/agent/test_switch_model_reasoning_override.py": 1,
    "tests/tui_gateway/test_compute_host_corrections.py": 8,
    "tests/tui_gateway/test_compute_host_borrowed_lease.py": 4,
    "tests/tui_gateway/test_compute_host_late_compress_ack.py": 10,
    "tests/tui_gateway/test_compute_host_pending_model_switch.py": 3,
    "tests/tui_gateway/test_compute_host_routed_secret_hydration.py": 1,
    "tests/tui_gateway/test_compute_host_phase1.py": 9,
    "tests/tui_gateway/test_deferred_model_switch_confirm.py": 16,
    "tests/tui_gateway/test_compute_host_turn_protocol.py": 17,
    "tests/tui_gateway/test_make_agent_provider.py": 3,
    "tests/tui_gateway/test_model_reset.py": 1,
    "tests/tui_gateway/test_model_switch_marker_role.py": 5,
    "tests/tui_gateway/test_model_switch_reasoning_flag.py": 2,
    "tests/tui_gateway/test_reasoning_config_per_model.py": 4,
    "tests/tui_gateway/test_reasoning_session_scope.py": 12,
    "tests/tui_gateway/test_secondary_profile_model_and_key.py": 2,
    "tests/tui_gateway/test_send_reasoning_override.py": 79,
}


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], text=True,
                                   env={"PATH": "/usr/bin:/bin"}, timeout=30).strip()


def provenance(source):
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
    if re.findall(r"^scripts/check: (.+)$", text, re.M) != ["14 checks, ok"]:
        raise ValueError("source checks incomplete")


def run(argv, source, env, log):
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(argv, cwd=source, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                timeout=900, check=False)
    if result.returncode != 0:
        raise ValueError(f"native command failed ({result.returncode}): {log.name}")


def qualify(source, evidence, python):
    source, evidence, python = Path(source).absolute(), Path(evidence).absolute(), Path(python).absolute()
    provenance(source)  # No source command executes before this check.
    evidence.mkdir(parents=True, mode=0o700, exist_ok=True)
    if any(evidence.iterdir()):
        raise ValueError("evidence directory must be empty")
    root = evidence.parent
    for name in ("home", "tmp", "state", "cache"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    env = {"PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(root / "home"),
           "TMPDIR": str(root / "tmp"), "HERMES_HOME": str(root / "state"),
           "HERMES_RUNTIME_DIR": str(root / "state/tools"), "HERMES_PYTHON": str(python),
           "HERMES_DISABLE_LAZY_INSTALLS": "1", "LANG": "C.UTF-8", "TZ": "UTC"}
    if os.environ.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    workflow = os.environ.get("WORKFLOW_SHA") or git(Path(__file__).resolve().parents[2], "rev-parse", "HEAD")
    if not re.fullmatch(r"[a-f0-9]{40}", workflow):
        raise ValueError("wrong workflow SHA")
    seen, records = set(), []
    try:
        for index, (file, expected) in enumerate(FILES.items()):
            xml = evidence / f"{index:02d}.xml"
            run(["bash", "scripts/run_tests.sh", "-j", "1", file, "--file-retries", "0",
                 f"--junitxml={xml}"], source, env, evidence / f"{index:02d}.log")
            count = validate_junit(xml, file, expected, seen)
            records.append({"file": file, "tests": count, "xml": xml.name})
        run([str(python), "scripts/check", "--base", BASE], source, env, evidence / "checks.log")
        validate_checks((evidence / "checks.log").read_text())
    finally:
        provenance(source)
    if len(records) != 19 or len(seen) != 211:
        raise ValueError("incomplete coverage")
    report = {"qualified": True, "source": SOURCE, "base": BASE, "workflow": workflow,
              "runner": os.environ.get("RUNNER_NAME", "local"),
              "run": os.environ.get("GITHUB_RUN_ID", "local"),
              "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
              "files": 19, "tests": 211, "checks": 14, "scope": "Linux synthetic only",
              "coverage": records}
    text = json.dumps(report, separators=(",", ":"))
    (evidence / "qualification.json").write_text(text + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("report=" + text + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    report = qualify(args.source, args.evidence, args.python)
    print(json.dumps({key: value for key, value in report.items() if key != "coverage"}))


if __name__ == "__main__":
    main()
