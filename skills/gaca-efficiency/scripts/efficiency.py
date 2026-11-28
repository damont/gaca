#!/usr/bin/env python3
"""Helper for the gaca-efficiency skill: look at a scheduled job, back it up, attach a pre-run
check to it, and undo. Standard library only.

  show   --profile P --job ID                 the job's settings, prompt and any script it already has
  backup --profile P --job ID                 save the job's record; prints the backup's path
  attach --profile P --job ID --file PATH [--replace]
                                              test PATH, copy it into the profile's scripts/ folder and
                                              make it the job's pre-run script (backs the job up first)
  undo   --profile P --job ID [--backup PATH] put the job back as the latest (or given) backup had it
  backups [--profile P] [--job ID]            list the backups

Hermes runs a job's script before the model and skips the model entirely when the script's LAST
line is {"wakeAgent": false}. Any other output is handed to the model as context. Hermes only runs
scripts from the profile's own `scripts/` folder, which is why `attach` copies the file there.

Never edits Hermes' files directly: every change goes through `hermes --profile P cron edit`.
Backups live in ~/.hermes/gaca-efficiency/backups/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
JOB = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_SCRIPT = 64 * 1024
WAKE_FALSE = {"wakeAgent": False}


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def root() -> Path:
    return Path(os.environ.get("GACA_HERMES_ROOT", "~/.hermes")).expanduser()


def profile_dir(name: str) -> Path:
    if name == "default":
        path = root()
    elif PROFILE.fullmatch(name or ""):
        path = root() / "profiles" / name
    else:
        die(f"not a profile name: {name!r}")
    if not path.is_dir() or path.is_symlink():
        die(f"no Hermes profile {name!r} at {path}")
    return path


def hermes() -> str:
    found = os.environ.get("GACA_HERMES_BIN") or shutil.which("hermes")
    if not found:
        local = Path("~/.local/bin/hermes").expanduser()
        found = str(local) if local.exists() else None
    if not found:
        die("can't find the hermes command")
    return found


def load_job(profile: str, job_id: str) -> dict:
    if not JOB.fullmatch(job_id or ""):
        die(f"not a job id: {job_id!r}")
    path = profile_dir(profile) / "cron" / "jobs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        die(f"can't read {path}: {error}")
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    for job in jobs if isinstance(jobs, list) else []:
        if isinstance(job, dict) and job.get("id") == job_id:
            return job
    die(f"no job {job_id} on profile {profile} (list them with: hermes --profile {profile} cron list --all)")


def schedule_of(job: dict) -> str | None:
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        return schedule.get("expr") or schedule.get("display")
    return schedule if isinstance(schedule, str) else None


def backups_dir() -> Path:
    path = root() / "gaca-efficiency" / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def edit(profile: str, job_id: str, *args: str) -> None:
    done = subprocess.run([hermes(), "--profile", profile, "cron", "edit", job_id, *args],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        die(f"hermes cron edit failed: {(done.stderr or done.stdout).strip()[:500]}")
    print((done.stdout or "").strip()[:500])


# ── commands ────────────────────────────────────────────────────────

def cmd_show(args) -> int:
    job = load_job(args.profile, args.job)
    keep = ("id", "name", "prompt", "skills", "skill", "schedule", "script", "no_agent", "monitor_script",
            "monitor_url", "model", "provider", "enabled", "deliver", "context_from", "workdir", "last_status")
    print(json.dumps({k: job.get(k) for k in keep if k in job}, indent=2))
    if job.get("script"):
        path = profile_dir(args.profile) / "scripts" / str(job["script"])
        try:
            print(f"\n--- its script ({path}) ---\n" + path.read_text(encoding="utf-8", errors="replace")[:6000])
        except OSError:
            print(f"\n(its script {path} can't be read)")
    return 0


def backup(profile: str, job_id: str) -> Path:
    job = load_job(profile, job_id)
    path = backups_dir() / f"{profile}-{job_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"profile": profile, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "job": job},
                               indent=2), encoding="utf-8")
    return path


def cmd_backup(args) -> int:
    print(backup(args.profile, args.job))
    return 0


def run_script(path: Path, profile: str) -> tuple[int, str]:
    argv = [sys.executable, str(path)] if path.suffix == ".py" else ["bash", str(path)]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile_dir(profile))
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=90, env=env, cwd=str(path.parent))
    except subprocess.TimeoutExpired:
        return 124, "timed out after 90 seconds (Hermes would wake the model)"
    return done.returncode, (done.stdout or "") + (f"\n[stderr] {done.stderr[-800:]}" if done.stderr else "")


def wakes(output: str) -> bool:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return True
    try:
        gate = json.loads(lines[-1])
    except ValueError:
        return True
    return not isinstance(gate, dict) or gate.get("wakeAgent", True) is not False


def cmd_attach(args) -> int:
    job = load_job(args.profile, args.job)
    source = Path(args.file).expanduser()
    if source.is_symlink() or not source.is_file():
        die(f"{source} is not a file")
    if source.suffix not in (".py", ".sh"):
        die("the script must be a .py or .sh file")
    if source.stat().st_size > MAX_SCRIPT:
        die("the script is too big for a pre-run check; keep it small and cheap")
    if job.get("no_agent"):
        die("this job never wakes the model (no_agent), so a pre-run check saves nothing")
    if job.get("script") and not args.replace:
        die(f"this job already has a script ({job['script']}) whose output the model reads. Write ONE script that "
            "still prints what that one printed and ends with the wake line, then run attach again with --replace.")
    scripts = profile_dir(args.profile) / "scripts"
    scripts.mkdir(exist_ok=True)
    target = scripts / f"efficiency-gate-{args.job}{source.suffix}"

    print("Testing the script once (as Hermes would run it)...")
    temp = scripts / f".test-{target.name}"
    shutil.copyfile(source, temp)
    code, output = run_script(temp, args.profile)
    temp.unlink(missing_ok=True)
    print(output.strip()[-3000:] or "(no output)")
    if code != 0:
        die(f"the script exited with {code}; fix it before attaching (a failing script always wakes the model)")
    print(f"\nThis run it would {'WAKE the model' if wakes(output) else 'SKIP the model'}.")

    saved = backup(args.profile, args.job)
    print(f"Backed the job up to {saved}")
    shutil.copyfile(source, target)
    os.chmod(target, 0o700)
    edit(args.profile, args.job, "--script", target.name)
    print(f"Attached {target.name} as the pre-run script of {job.get('name') or args.job}. "
          f"Undo: python3 {Path(__file__).resolve()} undo --profile {args.profile} --job {args.job}")
    return 0


def latest_backup(profile: str, job_id: str) -> Path | None:
    found = sorted(backups_dir().glob(f"{profile}-{job_id}-*.json"))
    return found[-1] if found else None


def cmd_undo(args) -> int:
    path = Path(args.backup).expanduser() if args.backup else latest_backup(args.profile, args.job)
    if path is None or not path.is_file():
        die(f"no backup for job {args.job} on profile {args.profile}")
    saved = json.loads(path.read_text(encoding="utf-8"))
    job = saved.get("job") or {}
    if saved.get("profile") != args.profile or job.get("id") != args.job:
        die(f"{path} is a backup of a different job")
    edits = ["--script", job.get("script") or "", "--model", job.get("model") or "", "--provider", job.get("provider") or ""]
    if schedule_of(job):
        edits += ["--schedule", schedule_of(job)]
    if job.get("prompt"):
        edits += ["--prompt", job["prompt"]]
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    if skills:
        for skill in skills:
            edits += ["--skill", skill]
    else:
        edits += ["--clear-skills"]
    edit(args.profile, args.job, *edits)
    print(f"Restored {job.get('name') or args.job} from {path}")
    return 0


def cmd_backups(args) -> int:
    pattern = f"{args.profile or '*'}-{args.job or '*'}-*.json"
    for path in sorted(backups_dir().glob(pattern)):
        print(path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up, gate and undo Hermes scheduled jobs for GACA's efficiency review")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "backup", "attach", "undo"):
        p = sub.add_parser(name)
        p.add_argument("--profile", required=True)
        p.add_argument("--job", required=True)
        if name == "attach":
            p.add_argument("--file", required=True)
            p.add_argument("--replace", action="store_true")
        if name == "undo":
            p.add_argument("--backup")
    p = sub.add_parser("backups")
    p.add_argument("--profile")
    p.add_argument("--job")
    args = parser.parse_args()
    return {"show": cmd_show, "backup": cmd_backup, "attach": cmd_attach, "undo": cmd_undo,
            "backups": cmd_backups}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
