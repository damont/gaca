"""Make sure the Hermes cron job for the gaca-heartbeat skill exists.

The skill (skills/gaca-heartbeat) ships into every profile with the others; this is the other half,
the schedule that wakes it up. It is created on ONE profile (the default one): a heartbeat per
profile would multiply the model calls for no benefit. Safe to run any number of times: a job that
is already there is left alone (so a schedule or prompt edited in Hermes is never overwritten), and
a failure is reported, never raised, because a missing heartbeat must not break an install.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..installer.shell import Shell

NAME = "gaca-heartbeat"
DEFAULT_SCHEDULE = "*/30 * * * *"
PROMPT = "Run the gaca-heartbeat skill. Follow it exactly, including how it says to end your reply."


@dataclass
class Outcome:
    status: str  # "created" | "exists" | "disabled" | "failed"
    detail: str = ""


def manual_command(profile: str, schedule: str = DEFAULT_SCHEDULE) -> str:
    return (f"hermes --profile {profile} cron create '{schedule}' '{PROMPT}' "
            f"--name {NAME} --skill gaca-heartbeat --deliver local")


def ensure_job(shell: Shell, hermes_bin: str, profile: str = "default", schedule: str = DEFAULT_SCHEDULE) -> Outcome:
    if not schedule.strip():
        return Outcome("disabled")
    try:
        listing = shell.run([hermes_bin, "--profile", profile, "cron", "list", "--all"], timeout=60)
        if listing.returncode != 0:
            return Outcome("failed", (listing.stderr or listing.stdout or "cron list failed").strip()[:300])
        if NAME in (listing.stdout or ""):
            return Outcome("exists")
        made = shell.run([hermes_bin, "--profile", profile, "cron", "create", schedule.strip(), PROMPT,
                          "--name", NAME, "--skill", "gaca-heartbeat", "--deliver", "local"], timeout=60)
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
    elif outcome.status == "failed":
        say.warn(f"could not schedule the heartbeat: {outcome.detail}")
        say.note(f"to do it by hand: {manual_command(profile, schedule)}")
    # "exists" and "disabled" say nothing: both are the quiet, expected cases.
