"""The heartbeat's schedule: `hermes connect` and the installer make sure the Hermes cron job exists.

Run against a fake `hermes`, never the real CLI: creating the job is the one step here that would
change a real machine."""
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
