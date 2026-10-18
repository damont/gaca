"""Hermes jobs + execution rows -> neutral automations."""
from gaca_api.agent.hermes import translate_automations as tau

LONG_PROMPT = "## Prompt\n\n" + ("skill text\n" * 200)


def _job(**kw):
    base = {"id": "aaaaaaaaaaaa", "name": "Daily briefing", "schedule": {"kind": "cron", "expr": "0 7 * * *"},
            "enabled": True, "state": "scheduled", "next_run_at": "2026-10-19T07:00:00-04:00",
            "last_run_at": "2026-10-18T07:00:00-04:00", "last_status": "ok", "no_agent": False}
    return {**base, **kw}


def _row(i, status="completed", at="2026-10-18T07:00:00-04:00", **kw):
    return {"id": f"e{i}", "job_id": "aaaaaaaaaaaa", "status": status, "claimed_at": at, "started_at": at,
            "finished_at": "2026-10-18T07:00:41-04:00", "error": None, **kw}


def test_excerpt_takes_the_end_of_the_response_not_the_prompt():
    text = f"# Cron Job: x\n\n**Run Time:** now\n{LONG_PROMPT}\n**Response Characters:** 9\n## Response\n\nTwo suggestions posted."
    assert tau.excerpt(text) == "Two suggestions posted."
    # a prompt that quotes the header doesn't fool it: the last one wins
    assert tau.excerpt("## Response\nquoted\n## Response\nreal") == "real"
    # script jobs: header, then --- then the script's output
    assert tau.excerpt('# Cron Job: s\n\n**Mode:** no_agent (script)\n\n---\n\n{"stored": 4}') == '{"stored": 4}'
    # only the tail of a long response was read
    assert tau.excerpt("...the middle of a long answer") == "...the middle of a long answer"


def test_excerpt_says_nothing_when_the_run_had_nothing_to_say():
    for out in (None, "", "## Response\n\n[SILENT]", "## Response\n\n   "):
        assert tau.excerpt(out) is None
    long = tau.excerpt("## Response\n" + "word " * 400)
    assert len(long) <= 600 and long.endswith("…")


def test_run_status_mapping():
    assert [tau.run_status(s) for s in ("completed", "failed", "unknown", "claimed", "running", None)] == [
        "ok", "failed", "failed", "running", "running", "failed"]


def test_runs_carry_duration_summary_and_error():
    rows = [
        _row(1, output="## Response\nAll good"),
        _row(2, status="failed", error="Timed out waiting for the Gmail connector. " * 30, output="## Response\nignored"),
        {"id": "e3", "status": "running", "claimed_at": "2026-10-18T07:00:00-04:00"},
        {"no": "id"},
    ]
    ok, bad, running = tau.runs(rows)
    assert (ok.status, ok.duration_seconds, ok.summary, ok.error) == ("ok", 41.0, "All good", None)
    assert bad.status == "failed" and bad.summary is None and len(bad.error) <= 500
    assert running.status == "running" and running.duration_seconds is None


def test_a_list_reads_status_from_the_latest_run_and_orders_recent_oldest_first():
    rows = {"aaaaaaaaaaaa": [_row(3, "failed", "2026-10-18T07:00:00-04:00", error="x"),
                             _row(2, "completed", "2026-10-17T07:00:00-04:00"),
                             _row(1, "completed", "2026-10-16T07:00:00-04:00")]}
    [a] = tau.automations({"jobs": [_job()]}, rows)
    assert (a.status, a.last_status, a.schedule, a.cron_expr) == ("failed", "failed", "Every day at 7:00 AM", "0 7 * * *")
    assert [m.status for m in a.recent] == ["ok", "ok", "failed"]  # oldest to newest
    assert a.next_run is not None and a.kind == "agent"


def test_without_history_the_job_itself_is_enough():
    [a] = tau.automations({"jobs": [_job(last_status="error")]})
    assert a.status == "failed" and a.recent == []
    [a] = tau.automations({"jobs": [_job(last_status=None, last_run_at=None)]})
    assert a.status == "never_run"
    [a] = tau.automations({"jobs": [_job(latest_execution={"status": "running", "claimed_at": "2026-10-18T07:00:00-04:00"})]})
    assert a.status == "running"


def test_paused_and_done_and_the_done_filter():
    paused = _job(id="p" * 12, enabled=False, state="paused", paused_at="2026-10-18T00:00:00-04:00")
    done = _job(id="d" * 12, name="One time", enabled=False, state="completed", next_run_at=None,
                schedule={"kind": "once", "display": "once at 2026-09-22"})
    done_failed = _job(id="f" * 12, enabled=False, state="completed", last_status="error", next_run_at=None)
    shown = {a.id: a for a in tau.automations({"jobs": [paused, done, done_failed]})}
    assert shown["p" * 12].status == "paused" and shown["p" * 12].next_run is None
    assert "d" * 12 not in shown
    assert shown["f" * 12].status == "failed"  # a finished one-time job that failed is not "done"
    both = {a.id: a for a in tau.automations({"jobs": [done]}, include_done=True)}
    assert both["d" * 12].status == "done" and both["d" * 12].schedule == "once at 2026-09-22" and both["d" * 12].cron_expr is None


def test_sorted_soonest_first_and_odd_input_is_skipped():
    soon = _job(id="s" * 12, name="B", next_run_at="2026-10-18T09:00:00-04:00")
    later = _job(id="l" * 12, name="A", next_run_at="2026-10-19T09:00:00-04:00")
    never = _job(id="n" * 12, name="C", next_run_at=None)
    out = tau.automations({"jobs": [never, later, "junk", {"name": "no id"}, soon]})
    assert [a.id for a in out] == ["s" * 12, "l" * 12, "n" * 12]
    assert tau.automations({}) == [] and tau.automations({"jobs": None}) == []
