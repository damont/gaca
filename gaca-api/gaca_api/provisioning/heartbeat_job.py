"""Make sure the Hermes cron job for the gaca-heartbeat skill exists.

The skill (skills/gaca-heartbeat) ships into every profile with the others; this is the other half,
the schedule that wakes it up. It is created on ONE profile (the default one): a heartbeat per
profile would multiply the model calls for no benefit. Safe to run any number of times: a job that
is already there is left alone (so a schedule or prompt edited in Hermes is never overwritten), and
a failure is reported, never raised, because a missing heartbeat must not break an install.

The pre-run: Hermes can run a script before a job and skip the model when that script's last line
is `{"wakeAgent": false}`. GACA puts a small one (`PRERUN_SCRIPT`) into the profile's `scripts/`
folder (Hermes only runs scripts from there) and attaches it to the job. It hands over to the
heartbeat skill's own helper (`heartbeat.py prerun`), so the logic ships with the skill and stays
in step with it. A job made before the pre-run existed gets it added: that is the one change made
to an existing job, and only when the job has no script of its own.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..installer.shell import Shell

NAME = "gaca-heartbeat"
DEFAULT_SCHEDULE = "*/30 * * * *"
PROMPT = "Run the gaca-heartbeat skill. Follow it exactly, including how it says to end your reply."
PRERUN_SCRIPT = "gaca-heartbeat-prerun.py"
_MARKER = "# gaca:heartbeat-prerun"

PRERUN_SOURCE = f'''#!/usr/bin/env python3
{_MARKER} (written by GACA; edits are overwritten by `gaca-api hermes connect`)
"""Pre-run for the gaca-heartbeat cron job. Hermes runs this before waking the model and skips the
model entirely when the last line printed is {{"wakeAgent": false}}. The work is done by the skill's
own helper (`heartbeat.py prerun`). If anything here goes wrong, the model is woken as before."""
import os
import subprocess
import sys
from pathlib import Path

home = Path(__file__).resolve().parent.parent
helper = home / "skills" / "gaca" / "gaca-heartbeat" / "scripts" / "heartbeat.py"
if not helper.is_file():
    print("gaca-heartbeat pre-run: the skill's helper is missing, so run every step yourself.")
    sys.exit(0)
env = dict(os.environ)
env.setdefault("HERMES_HOME", str(home))
# Hermes may not pass the profile's GACA settings to scripts; read them from its own .env then.
if not env.get("GACA_API_TOKEN"):
    try:
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in ("GACA_API_TOKEN", "GACA_BASE_URL") and not env.get(key):
                env[key] = value.strip().strip(chr(34) + chr(39))
    except OSError:
        pass
try:
    done = subprocess.run([sys.executable, str(helper), "prerun"], env=env, capture_output=True, text=True, timeout=150)
except Exception as error:  # noqa: BLE001
    print(f"gaca-heartbeat pre-run failed ({{type(error).__name__}}), so run every step yourself.")
    sys.exit(0)
sys.stdout.write(done.stdout)
if done.returncode != 0:
    print(f"gaca-heartbeat pre-run failed (exit {{done.returncode}}): {{done.stderr[-300:]}}")
    print("Run every step of the skill yourself.")
'''


@dataclass
class Outcome:
    status: str  # "created" | "exists" | "upgraded" | "disabled" | "failed"
    detail: str = ""


def manual_command(profile: str, schedule: str = DEFAULT_SCHEDULE) -> str:
    return (f"hermes --profile {profile} cron create '{schedule}' '{PROMPT}' "
            f"--name {NAME} --skill gaca-heartbeat --script {PRERUN_SCRIPT} --deliver local")


def install_prerun(profile_dir: Path) -> bool:
    """Write the pre-run script into `<profile>/scripts/`. True when it is there and ours. A file of
    the same name that GACA did not write is left alone (False)."""
    scripts = profile_dir / "scripts"
    try:
        if scripts.is_symlink():
            return False
        scripts.mkdir(mode=0o700, exist_ok=True)
        target = scripts / PRERUN_SCRIPT
        if target.is_symlink():
            return False
        if target.exists():
            current = target.read_text(encoding="utf-8", errors="replace")
            if _MARKER not in current:
                return False
            if current == PRERUN_SOURCE:
                return True
        temp = scripts / f".{PRERUN_SCRIPT}.tmp"
        temp.write_text(PRERUN_SOURCE, encoding="utf-8")
        os.chmod(temp, 0o700)
        temp.replace(target)
        return True
    except OSError:
        return False


def _existing_job(profile_dir: Path | None) -> dict | None:
    """The heartbeat job's stored record (`cron/jobs.json`), read-only; None if it can't be read."""
    if profile_dir is None:
        return None
    try:
        data = json.loads((profile_dir / "cron" / "jobs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return next((j for j in jobs if isinstance(j, dict) and j.get("name") == NAME), None) if isinstance(jobs, list) else None


def ensure_job(
    shell: Shell,
    hermes_bin: str,
    profile: str = "default",
    schedule: str = DEFAULT_SCHEDULE,
    profile_dir: Path | None = None,
) -> Outcome:
    """`profile_dir` is that profile's Hermes folder; with it, the pre-run script is installed and
    attached. Without it the job is made the old way (the model does every step)."""
    if not schedule.strip():
        return Outcome("disabled")
    prerun = profile_dir is not None and install_prerun(profile_dir)
    try:
        listing = shell.run([hermes_bin, "--profile", profile, "cron", "list", "--all"], timeout=60)
        if listing.returncode != 0:
            return Outcome("failed", (listing.stderr or listing.stdout or "cron list failed").strip()[:300])
        if NAME in (listing.stdout or ""):
            job = _existing_job(profile_dir) if prerun else None
            if job is None or job.get("script") or not job.get("id"):
                return Outcome("exists")
            edited = shell.run([hermes_bin, "--profile", profile, "cron", "edit", str(job["id"]),
                                "--script", PRERUN_SCRIPT], timeout=60)
            if edited.returncode != 0:
                return Outcome("failed", (edited.stderr or edited.stdout or "cron edit failed").strip()[:300])
            return Outcome("upgraded")
        extra = ["--script", PRERUN_SCRIPT] if prerun else []
        made = shell.run([hermes_bin, "--profile", profile, "cron", "create", schedule.strip(), PROMPT,
                          "--name", NAME, "--skill", "gaca-heartbeat", *extra, "--deliver", "local"], timeout=60)
    except (OSError, TimeoutError) as error:  # no hermes binary, or it hung
        return Outcome("failed", str(error))
    except Exception as error:  # noqa: BLE001 - subprocess.TimeoutExpired and friends
        return Outcome("failed", str(error))
    if made.returncode != 0:
        return Outcome("failed", (made.stderr or made.stdout or "cron create failed").strip()[:300])
    return Outcome("created")


def report(outcome: Outcome, profile: str, schedule: str, say) -> None:
    """Tell the person what happened, with the one command to run if it did not work."""
    if outcome.status == "created":
        say.ok(f"scheduled the heartbeat on Hermes profile {profile!r} ({schedule}); it stays silent unless something is worth your attention")
    elif outcome.status == "upgraded":
        say.ok("gave the heartbeat a pre-run script: runs with nothing new no longer wake the model")
    elif outcome.status == "failed":
        say.warn(f"could not schedule the heartbeat: {outcome.detail}")
        say.note(f"to do it by hand: {manual_command(profile, schedule)}")
    # "exists" and "disabled" say nothing: both are the quiet, expected cases.
