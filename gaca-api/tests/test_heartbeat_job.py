"""The heartbeat's schedule: `hermes connect` and the installer make sure the Hermes cron job exists.

Run against a fake `hermes`, never the real CLI: creating the job is the one step here that would
change a real machine."""
import json
import subprocess

import pytest

from gaca_api import cli
from gaca_api.config import settings
from gaca_api.installer.run import Say
from gaca_api.installer.shell import Shell
from gaca_api.provisioning import heartbeat_job
from tests.conftest import make_owner
from tests.test_hermes_connect import hermes_home, run, server  # noqa: F401  (fixtures)


class FakeHermes(Shell):
    def __init__(self, *, listing="", list_rc=0, create_rc=0):
        self.listing, self.list_rc, self.create_rc, self.calls = listing, list_rc, create_rc, []

    def run(self, argv, *, timeout=120, env=None, inherit=False):
        self.calls.append(list(argv))
        if argv[3:5] == ["cron", "list"]:
            return subprocess.CompletedProcess(argv, self.list_rc, self.listing, "no such profile" if self.list_rc else "")
        return subprocess.CompletedProcess(argv, self.create_rc, "", "boom" if self.create_rc else "")

    def which(self, name):
        return f"/usr/bin/{name}"


def test_a_missing_job_is_created_on_the_default_profile_with_the_skill():
    shell = FakeHermes()
    assert heartbeat_job.ensure_job(shell, "hermes", "default").status == "created"
    create = shell.calls[-1]
    assert create[:5] == ["hermes", "--profile", "default", "cron", "create"] and create[5] == "*/30 * * * *"
    assert create[create.index("--name") + 1] == "gaca-heartbeat" and create[create.index("--skill") + 1] == "gaca-heartbeat"
    assert create[create.index("--deliver") + 1] == "local"


def test_an_existing_job_is_left_alone():
    shell = FakeHermes(listing="gaca-heartbeat  */15 * * * *  enabled\n")  # the user changed it to 15 minutes
    assert heartbeat_job.ensure_job(shell, "hermes").status == "exists"
    assert len(shell.calls) == 1  # only the listing: nothing was created or changed


def test_an_empty_schedule_turns_it_off_without_touching_hermes():
    shell = FakeHermes()
    assert heartbeat_job.ensure_job(shell, "hermes", schedule="  ").status == "disabled" and shell.calls == []


@pytest.mark.parametrize("kwargs", [{"list_rc": 1}, {"create_rc": 1}])
def test_a_hermes_failure_is_reported_not_raised(kwargs):
    outcome = heartbeat_job.ensure_job(FakeHermes(**kwargs), "hermes")
    assert outcome.status == "failed" and outcome.detail


def test_no_hermes_binary_is_a_failure_not_a_crash():
    class Missing(FakeHermes):
        def run(self, argv, **kw):
            raise FileNotFoundError("hermes")
    assert heartbeat_job.ensure_job(Missing(), "hermes").status == "failed"


def test_the_report_gives_the_manual_command_when_it_failed():
    lines = []
    heartbeat_job.report(heartbeat_job.Outcome("failed", "boom"), "default", "*/30 * * * *", Say(lines.append))
    text = "\n".join(lines)
    assert "could not schedule the heartbeat: boom" in text and "cron create '*/30 * * * *'" in text


def test_the_report_is_quiet_when_nothing_changed():
    lines = []
    for status in ("exists", "disabled"):
        heartbeat_job.report(heartbeat_job.Outcome(status), "default", "*/30 * * * *", Say(lines.append))
    assert lines == []


def test_hermes_connect_schedules_it(server, hermes_home, monkeypatch, capsys):  # noqa: F811
    shell = FakeHermes()
    monkeypatch.setattr(settings, "heartbeat_schedule", "*/30 * * * *")
    monkeypatch.setattr("gaca_api.installer.shell.Shell", lambda: shell)
    assert cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)]) == 0
    assert any(call[3:5] == ["cron", "create"] for call in shell.calls)
    assert "scheduled the heartbeat" in capsys.readouterr().out
    capsys.readouterr()
    shell.listing = "gaca-heartbeat enabled"
    cli.main(["hermes", "connect", "--hermes-home", str(hermes_home)])
    assert sum(call[3:5] == ["cron", "create"] for call in shell.calls) == 1  # the second run created nothing


# ── the pre-run script ──

def test_with_a_profile_dir_the_prerun_is_installed_and_attached_to_a_new_job(tmp_path):
    shell = FakeHermes()
    assert heartbeat_job.ensure_job(shell, "hermes", "default", profile_dir=tmp_path).status == "created"
    create = shell.calls[-1]
    assert create[create.index("--script") + 1] == heartbeat_job.PRERUN_SCRIPT
    script = tmp_path / "scripts" / heartbeat_job.PRERUN_SCRIPT
    assert script.read_text() == heartbeat_job.PRERUN_SOURCE and oct(script.stat().st_mode)[-3:] == "700"
    compile(script.read_text(), str(script), "exec")


def _jobs(tmp_path, **job):
    (tmp_path / "cron").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "c85917c3a264", "name": "gaca-heartbeat", **job}]}))


def test_an_existing_job_without_a_script_gets_the_prerun_and_nothing_else(tmp_path):
    _jobs(tmp_path, script=None)
    shell = FakeHermes(listing="gaca-heartbeat  */15 * * * *  enabled\n")
    assert heartbeat_job.ensure_job(shell, "hermes", profile_dir=tmp_path).status == "upgraded"
    assert shell.calls[-1] == ["hermes", "--profile", "default", "cron", "edit", "c85917c3a264",
                               "--script", heartbeat_job.PRERUN_SCRIPT]


def test_an_existing_job_with_its_own_script_is_left_alone(tmp_path):
    _jobs(tmp_path, script="mine.py")
    shell = FakeHermes(listing="gaca-heartbeat\n")
    assert heartbeat_job.ensure_job(shell, "hermes", profile_dir=tmp_path).status == "exists"
    assert len(shell.calls) == 1


def test_a_script_of_the_same_name_that_gaca_did_not_write_is_not_overwritten(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / heartbeat_job.PRERUN_SCRIPT).write_text("print('mine')\n")
    shell = FakeHermes()
    assert heartbeat_job.ensure_job(shell, "hermes", profile_dir=tmp_path).status == "created"
    assert "--script" not in shell.calls[-1]
    assert (tmp_path / "scripts" / heartbeat_job.PRERUN_SCRIPT).read_text() == "print('mine')\n"


def test_an_older_gaca_prerun_is_updated(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / heartbeat_job.PRERUN_SCRIPT).write_text("# gaca:heartbeat-prerun old\n")
    assert heartbeat_job.install_prerun(tmp_path)
    assert (tmp_path / "scripts" / heartbeat_job.PRERUN_SCRIPT).read_text() == heartbeat_job.PRERUN_SOURCE


def test_the_prerun_hands_over_to_the_skill_helper_and_wakes_if_it_is_missing(tmp_path):
    import sys

    assert heartbeat_job.install_prerun(tmp_path)
    script = tmp_path / "scripts" / heartbeat_job.PRERUN_SCRIPT
    missing = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert missing.returncode == 0 and "wakeAgent" not in missing.stdout  # no false line: the model wakes
    helper = tmp_path / "skills" / "gaca" / "gaca-heartbeat" / "scripts" / "heartbeat.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("import sys; print('looked'); print('{\"wakeAgent\": false}')\n")
    asleep = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert asleep.stdout.strip().splitlines()[-1] == '{"wakeAgent": false}'
    helper.write_text("import sys; print('{\"wakeAgent\": false}'); sys.exit(3)\n")
    broken = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert "failed" in broken.stdout.strip().splitlines()[-1] or "yourself" in broken.stdout.strip().splitlines()[-1]


def test_the_prerun_reads_the_token_from_the_profiles_env_when_hermes_does_not_pass_it(tmp_path):
    import os
    import sys

    assert heartbeat_job.install_prerun(tmp_path)
    (tmp_path / ".env").write_text("OTHER=1\nGACA_API_TOKEN='tok-from-env'\n")
    helper = tmp_path / "skills" / "gaca" / "gaca-heartbeat" / "scripts" / "heartbeat.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("import os; print(os.environ.get('GACA_API_TOKEN'))\n")
    env = {k: v for k, v in os.environ.items() if k != "GACA_API_TOKEN"}
    out = subprocess.run([sys.executable, str(tmp_path / "scripts" / heartbeat_job.PRERUN_SCRIPT)],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.stdout.strip() == "tok-from-env"
