"""The gaca-efficiency helper (skills/gaca-efficiency/scripts/efficiency.py), run for real against a fake
Hermes home and a fake `hermes` command that records what it was asked to change."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "skills" / "gaca-efficiency" / "scripts" / "efficiency.py"
JOB = "c989484fab1b"


@pytest.fixture
def box(tmp_path):
    root = tmp_path / ".hermes"
    trading = root / "profiles" / "trading"
    (trading / "cron").mkdir(parents=True)
    job = {"id": JOB, "name": "stock-notebook-price-moves", "prompt": "Check prices and alert on big moves.",
           "skills": ["stock-notebook"], "schedule": {"kind": "cron", "expr": "*/30 7-18 * * 1-5"},
           "script": None, "model": None, "provider": None, "no_agent": False, "enabled": True}
    (trading / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job]}))
    log = tmp_path / "hermes-calls.jsonl"
    fake = tmp_path / "hermes"
    fake.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    fake.chmod(0o755)

    def run(*args, check=True):
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GACA_HERMES_ROOT": str(root), "GACA_HERMES_BIN": str(fake)}
        result = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=env, timeout=120)
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return {"run": run, "calls": calls, "root": root, "trading": trading, "tmp": tmp_path}


def gate_file(box, body):
    path = box["tmp"] / "gate.py"
    path.write_text(body)
    return str(path)


SKIP = "print('no move over 3%')\nprint('{\"wakeAgent\": false}')\n"


def test_show_prints_the_job(box):
    out = box["run"]("show", "--profile", "trading", "--job", JOB).stdout
    assert "Check prices" in out and "stock-notebook" in out


def test_attach_tests_backs_up_copies_and_edits_through_hermes(box):
    out = box["run"]("attach", "--profile", "trading", "--job", JOB, "--file", gate_file(box, SKIP)).stdout
    assert "SKIP the model" in out and "Backed the job up" in out
    target = box["trading"] / "scripts" / f"efficiency-gate-{JOB}.py"
    assert target.read_text() == SKIP
    assert box["calls"]() == [["--profile", "trading", "cron", "edit", JOB, "--script", target.name]]
    [backup] = (box["root"] / "gaca-efficiency" / "backups").glob(f"trading-{JOB}-*.json")
    assert json.loads(backup.read_text())["job"]["script"] is None


def test_a_failing_script_is_not_attached(box):
    result = box["run"]("attach", "--profile", "trading", "--job", JOB, "--file", gate_file(box, "raise SystemExit(2)\n"), check=False)
    assert result.returncode != 0 and "exited with 2" in result.stderr
    assert box["calls"]() == [] and not (box["trading"] / "scripts" / f"efficiency-gate-{JOB}.py").exists()


def test_a_job_that_already_has_a_script_needs_replace(box):
    jobs = json.loads((box["trading"] / "cron" / "jobs.json").read_text())
    jobs["jobs"][0]["script"] = "context.py"
    (box["trading"] / "cron" / "jobs.json").write_text(json.dumps(jobs))
    result = box["run"]("attach", "--profile", "trading", "--job", JOB, "--file", gate_file(box, SKIP), check=False)
    assert result.returncode != 0 and "--replace" in result.stderr and box["calls"]() == []
    box["run"]("attach", "--profile", "trading", "--job", JOB, "--file", gate_file(box, SKIP), "--replace")
    assert len(box["calls"]()) == 1


def test_undo_restores_the_backed_up_job(box):
    box["run"]("attach", "--profile", "trading", "--job", JOB, "--file", gate_file(box, SKIP))
    box["run"]("undo", "--profile", "trading", "--job", JOB)
    undo = box["calls"]()[-1]
    assert undo[:5] == ["--profile", "trading", "cron", "edit", JOB]
    assert undo[undo.index("--script") + 1] == "" and undo[undo.index("--schedule") + 1] == "*/30 7-18 * * 1-5"
    assert undo[undo.index("--skill") + 1] == "stock-notebook" and undo[undo.index("--model") + 1] == ""


def test_bad_names_are_refused(box):
    assert box["run"]("show", "--profile", "../x", "--job", JOB, check=False).returncode != 0
    assert box["run"]("show", "--profile", "trading", "--job", "a/b", check=False).returncode != 0
    assert box["run"]("undo", "--profile", "trading", "--job", JOB, check=False).returncode != 0  # no backup yet
