"""Bounded fork Linux qualification and fail-closed commit-status evidence.

Control code comes from reviewed W. Product and plugin commands run only from S/P.
This is not the inherited upstream CI aggregate and never installs into a live runtime.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

SOURCE_SHA = "47909663ea283b2b77d326e456629c18dcf13f7b"
PLUGIN_SHA = "22b832ab9f98ac48e2ff61fe779ed35d6c2e0f1c"
CLI_FILE = "tests/e2e/core/providers/test_openai_responses.py"
PLUGIN_E2E_FILE = "tests/e2e/core/providers/test_zz_codexlb_plugin_e2e.py"
REGRESSION_FILES = (
    "tests/agent/test_relay_llm.py",
    "tests/tools/test_vision_history_budget.py",
    "tests/tools/test_vision_native_fast_path.py",
    "tests/tools/test_vision_region.py",
    "tests/tools/test_vision_scale_disclosure.py",
    "tests/tools/test_vision_tools.py",
    "tests/tui_gateway/test_auto_continue.py",
    "tests/tui_gateway/test_compute_host.py",
    "tests/tui_gateway/test_compute_host_borrowed_lease.py",
    "tests/tui_gateway/test_compute_host_corrections.py",
    "tests/tui_gateway/test_compute_host_late_compress_ack.py",
    "tests/tui_gateway/test_compute_host_pending_model_switch.py",
    "tests/tui_gateway/test_compute_host_phase1.py",
    "tests/tui_gateway/test_compute_host_routed_secret_hydration.py",
    "tests/tui_gateway/test_compute_host_turn_protocol.py",
    "tests/tui_gateway/test_interrupt_agent_loop_stopped_hook.py",
    "tests/tui_gateway/test_interrupt_dead_compute_host.py",
    "tests/tui_gateway/test_interrupt_does_not_wait_for_agent.py",
    "tests/tui_gateway/test_seeded_session_create.py",
    "tests/tui_gateway/test_session_create_model_provider_guard.py",
    "tests/tui_gateway/test_session_interrupt_cancels_background_review.py",
    "tests/tui_gateway/test_session_resume_db_ownership.py",
    "tests/tui_gateway/test_session_resume_materializes_minted_key.py",
    "tests/tui_gateway/test_stream_interrupt_recovery_orphan.py",
    "tests/tui_gateway/test_tui_gateway_server.py",
)
STARTUP_IMPORTS = ("requests", "urllib3", "fastapi", "uvicorn", "starlette",
                   "opentelemetry.trace", "mcp.client.streamable_http")


def verify_native(text: str, files: list[str] | tuple[str, ...], total: int) -> dict:
    """Require exact, unique completed files, no skipped cases, and native totals."""
    rows = re.findall(r"^\[.*?\] ✓ (tests/\S+\.py) \((\d+)✓, [\d.]+s\)$", text, re.MULTILINE)
    actual = [file for file, _ in rows]
    summaries = re.findall(r"^=== Summary: (\d+) files, (\d+) tests passed, (\d+) failed \(100% complete\) in [\d.]+s \(\d+ workers\) ===$", text, re.MULTILINE)
    if (len(actual) != len(files) or set(actual) != set(files) or
            len(set(actual)) != len(actual) or any(int(n) == 0 for _, n in rows) or
            sum(int(n) for _, n in rows) != total or
            summaries != [(str(len(files)), str(total), "0")]):
        raise ValueError("native file completion/count/summary mismatch")
    return {"files": sorted(actual), "passed": total}


def verify_unit(text: str, total: int) -> dict:
    summaries = re.findall(r"^(\d+) passed in [\d.]+s(?: \(.*\))?$", text, re.MULTILINE)
    if summaries != [str(total)]:
        raise ValueError("plugin unit run missing, skipped, or wrong count")
    return {"passed": total}


def accept_report(report: dict, workflow_sha: str) -> bool:
    """A successful exit alone, missing lane or foreign source is never sufficient."""
    try:
        if "error" in report:
            return False
        identities = (report["workflow_sha"] == workflow_sha and
                      re.fullmatch(r"[a-f0-9]{40}", workflow_sha) is not None and
                      report["source_sha"] == SOURCE_SHA and report["plugin_sha"] == PLUGIN_SHA)
        clean = all(report[key] == {"head": sha, "clean": True} for key, sha in (
            ("source_before", SOURCE_SHA), ("source_after", SOURCE_SHA),
            ("plugin_before", PLUGIN_SHA), ("plugin_after", PLUGIN_SHA)))
        expected = {"regressions": (REGRESSION_FILES, 926), "cli": ([CLI_FILE], 1),
                    "plugin_unit": (None, 20), "plugin_e2e": ([PLUGIN_E2E_FILE], 2)}
        if set(report["lanes"]) != set(expected):
            return False
        for name, (files, total) in expected.items():
            lane = report["lanes"][name]
            if lane["exit_code"] != 0 or lane["passed"] != total:
                return False
            if files is not None and sorted(lane["files"]) != sorted(files):
                return False
        runner = report["runner"]
        return bool(identities and clean and runner["name"] and runner["os"] == "Linux" and
                    runner["arch"] in ("X64", "ARM64") and
                    report["startup_imports"] == list(STARTUP_IMPORTS) and report["model_resolver"] is True)
    except (KeyError, TypeError, ValueError):
        return False


def checkout_identity(path: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()
    return {"head": git("rev-parse", "HEAD"), "clean": not git("status", "--porcelain", "--untracked-files=all")}


def require_identity(path: Path, sha: str) -> dict:
    identity = checkout_identity(path)
    if identity != {"head": sha, "clean": True}:
        raise ValueError(f"checkout not clean at approved identity: {path.name}")
    return identity


def run_command(command: list[str], cwd: Path, environment: dict, log: Path) -> tuple[int, str]:
    """Keep output off the terminal until complete; an outer job timeout bounds all lanes."""
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=cwd, env=environment, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=1200, check=False)
    text = log.read_text(encoding="utf-8")
    print(text, end="", flush=True)
    return result.returncode, text


def isolated_environment(python: Path, home: Path, scratch: Path) -> dict:
    """No inherited provider credentials, endpoints, plugin config or activation state."""
    state = home / ".hermes"
    state.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    (state / "config.yaml").write_text("security:\n  allow_lazy_installs: false\n", encoding="utf-8")
    return {"HOME": str(home), "PATH": f"{python.parent}:/usr/bin:/bin",
            "HERMES_HOME": str(state), "HERMES_RUNTIME_DIR": str(home / "runtime"),
            "HERMES_PYTHON": str(python), "TMPDIR": str(scratch),
            "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_TEST_WORKERS": "2",
            "HERMES_TEST_FILE_RETRIES": "0", "PYTHON_CPU_COUNT": "2",
            "TZ": "UTC", "LANG": "C.UTF-8", "PYTHONUTF8": "1"}


def prepare_interpreter(python: Path, scratch: Path) -> None:
    """The canonical runner clears TMPDIR and lazy-install policy with env -i.

    Only a fresh task-owned test venv gets this site hook. It does not add source
    paths or alter product code, and also reaches sanitized CLI/compute children.
    """
    script = (
        "import json,sys,sysconfig; from pathlib import Path; "
        "p=Path(sysconfig.get_path('purelib')); "
        "assert Path(sys.prefix) != Path(sys.base_prefix); "
        "print(json.dumps(str(p)))"
    )
    directory = Path(json.loads(subprocess.check_output([str(python), "-c", script], text=True)))
    if python.absolute().parent.parent not in directory.parents:
        raise ValueError("site hook must stay in the task-owned test venv")
    hook = ("import os; os.environ.setdefault('TMPDIR', " + repr(str(scratch)) +
            "); os.environ.setdefault('HERMES_DISABLE_LAZY_INSTALLS', '1')\n")
    destination = directory / "ryoko_qualification_scratch.pth"
    if destination.exists():
        raise ValueError("refusing to replace an existing task site hook")
    destination.write_text(hook, encoding="utf-8")


def qualify(args) -> int:
    source, plugin, evidence = args.source.resolve(), args.plugin.resolve(), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    environment = isolated_environment(args.python.absolute(), args.home.resolve(), args.scratch.resolve())
    report = {"workflow_sha": args.workflow_sha, "source_sha": SOURCE_SHA, "plugin_sha": PLUGIN_SHA,
              "runner": {"name": os.environ.get("RUNNER_NAME", "local-rehearsal"),
                         "os": os.environ.get("RUNNER_OS", "Linux"), "arch": os.environ.get("RUNNER_ARCH", "X64")},
              "lanes": {}}
    try:
        report["source_before"] = require_identity(source, SOURCE_SHA)
        report["plugin_before"] = require_identity(plugin, PLUGIN_SHA)
        probe = ("import importlib,json; "
                 f"names={STARTUP_IMPORTS!r}; "
                 "[importlib.import_module(name) for name in names]; "
                 "from hermes_cli.model_switch import resolve_startup_model_route; "
                 "assert callable(resolve_startup_model_route); "
                 "print(json.dumps({'startup_imports':list(names),'model_resolver':True}))")
        rc, text = run_command([str(args.python), "-c", probe], source, environment, evidence / "startup.log")
        if rc:
            raise ValueError("startup imports failed")
        report.update(json.loads(text))
        native = ["bash", "scripts/run_tests.sh", "-j", "2", "--file-retries", "0"]
        commands = [
            ("regressions", native + list(REGRESSION_FILES), source, REGRESSION_FILES, 926),
            ("cli", native + [CLI_FILE, "-k", "test_parallel_function_calls_round_trip_once", "--include-integration"], source, [CLI_FILE], 1),
            ("plugin_unit", ["bash", "tests/run.sh"], plugin, None, 20),
            ("plugin_e2e", ["bash", "tests/e2e/run.sh", "-j", "2", "--file-retries", "0"], plugin, [PLUGIN_E2E_FILE], 2),
        ]
        environment["HERMES_SRC"] = str(source)
        for name, command, cwd, files, total in commands:
            rc, text = run_command(command, cwd, environment, evidence / f"{name}.log")
            report["lanes"][name] = {"exit_code": rc}
            if rc:
                raise ValueError(f"{name} exited {rc}")
            report["lanes"][name].update(verify_unit(text, total) if files is None else verify_native(text, files, total))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print(f"Qualification failed: {exc}", file=sys.stderr)
    finally:
        report["source_after"] = checkout_identity(source)
        report["plugin_after"] = checkout_identity(plugin)
        (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    accepted = accept_report(report, args.workflow_sha)
    if accepted and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("report=" + json.dumps(report, separators=(",", ":")) + "\n")
    print("Linux qualification accepted" if accepted else "Linux qualification NOT accepted")
    return 0 if accepted else 1


def status_payload(result: str, report: dict, workflow_sha: str, run_url: str) -> dict:
    accepted = result == "success" and accept_report(report, workflow_sha)
    return {"state": "success" if accepted else "failure", "context": "Ryoko Linux qualification",
            "target_url": run_url,
            "description": "Linux only: 926 regressions, 1 CLI, 20+2 plugin; upstream CI not qualified" if accepted
                           else "Linux qualification failed or incomplete; upstream CI not qualified"}


def publish_status() -> None:
    try:
        report = json.loads(os.environ.get("QUALIFICATION_REPORT", "{}"))
    except ValueError:
        report = {}
    payload = status_payload(os.environ.get("QUALIFICATION_RESULT", "failure"), report,
                             os.environ["APPROVED_WORKFLOW_SHA"], os.environ["QUALIFICATION_RUN_URL"])
    # The fixed endpoint cannot be redirected by event data or a model endpoint.
    request = urllib.request.Request(
        f"https://api.github.com/repos/gcs8/hermes-agent/statuses/{SOURCE_SHA}",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result["state"] != payload["state"] or result["context"] != payload["context"]:
        raise ValueError("commit status response mismatch")
    print(f"Posted {payload['context']}: {payload['state']} on {SOURCE_SHA}")
    if payload["state"] != "success":
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["run", "prepare-interpreter", "status"])
    for name in ("source", "plugin", "python", "home", "scratch", "evidence"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--workflow-sha")
    args = parser.parse_args()
    if args.mode == "status":
        publish_status()
        return 0
    if args.mode == "prepare-interpreter":
        prepare_interpreter(args.python, args.scratch.resolve())
        return 0
    if any(getattr(args, name) is None for name in ("source", "plugin", "python", "home", "scratch", "evidence", "workflow_sha")):
        parser.error("run requires all path options and --workflow-sha")
    return qualify(args)


if __name__ == "__main__":
    raise SystemExit(main())
