"""The alert workflow the API decides for every app: how each action stands, what finishing one
does to the alert, which buttons close it, and how an email draft is carried."""
import asyncio

import pytest

import gaca_api.agent.runtime as agent_runtime
import gaca_api.services.runs as runs
from gaca_api.config import settings
from gaca_api.db import client
from gaca_api.models.alert import AlertDoc
from gaca_api.models.user import CrewMember, UserDoc
from gaca_api.schemas.alert import AlertCreate, AlertDraftOut
from gaca_api.schemas.event import RunCompleted
from gaca_api.schemas.message import MessageOut
from gaca_api.services import alert_view
from gaca_api.services import alerts as alert_service
from gaca_api.services.exceptions import InvalidRequest
from gaca_api.utils import drafts


def _run(factory, tmp_path):
    settings.db_path = str(tmp_path / "t.db")

    async def main():
        await client.connect()
        try:
            return await factory()
        finally:
            await client.close()

    return asyncio.run(main())


def _user() -> UserDoc:
    return UserDoc(
        id="u1", email="u1@example.com", display_name="u1", agent_host="http://agent.example.com",
        crew=[CrewMember(name="default", port=8642, api_key="k")], default_crew="default",
    )


class _Backend:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.sent: list[str] = []

    async def start_run(self, *, session_id, history, user_message, images=()):
        self.sent.append(user_message)
        return "run_x"

    async def stream_events(self, run_id, *, session_id):
        yield RunCompleted(session_id=session_id, run_id=run_id, output=self.outputs.pop(0))


def _backend(monkeypatch, *outputs) -> _Backend:
    b = _Backend(outputs)
    monkeypatch.setattr(agent_runtime, "backend_for", lambda base_url, api_key: b)
    return b


async def _drain():
    await asyncio.gather(*list(runs._background))


def _events(**extra) -> AlertCreate:
    """Three calendar actions and an "Add all" covering them."""
    actions = [
        {"label": f"Add {t}", "prompt": f"Add {t}", "kind": "calendar.add",
         "params": {"date": d, "time": tm, "location": loc}}
        for t, d, tm, loc in (("Soccer", "2026-11-19", "5:30 PM", "Riverside Park"),
                              ("Dismissal", "2026-11-20", None, None),
                              ("Concert", "2026-11-24", "7:00 PM", None))
    ]
    actions.append({"label": "Add all 3", "prompt": "Add all three", "kind": "calendar.add",
                    "covers": [0, 1, 2], "remaining_label": "Add the other {n}"})
    return AlertCreate(kind="calendar.suggestions", title="3 things", suggested_actions=actions, **extra)


# ── how actions read ────────────────────────────────────────────────

def test_an_untouched_alert_offers_every_action_with_a_detail_line(tmp_path):
    async def go():
        alert, _ = await alert_service.create("u1", _events())
        [out] = await alert_service.to_out("u1", [alert])
        return out

    out = _run(go, tmp_path)
    assert [a.state for a in out.actions] == ["idle"] * 4 and all(a.can_run for a in out.actions)
    assert out.actions[0].detail == "Thu Nov 19 · 5:30 PM · Riverside Park"
    assert out.actions[1].detail == "Fri Nov 20 · all day"
    assert out.actions[3].detail is None
    assert [(a.cta, a.prominent) for a in out.actions] == [("Add", False)] * 3 + [("Add", True)]
    w = out.workflow
    assert (w.phase, w.phase_label, w.actions_heading) == ("open", None, "Hermes can add these")
    assert [(o.reason, o.label, o.primary) for o in w.close_options] == [
        ("done_by_me", "I'll handle it", True), ("noted", "Good to know", False), ("not_useful", "Not useful", False)]
    assert w.close_heading == "Or close it yourself" and "teaches Hermes" in w.close_hint
    assert w.chat.prominent is False


def test_one_of_three_leaves_the_alert_open_and_offers_the_rest(tmp_path, monkeypatch):
    backend = _backend(monkeypatch, "Added Soccer to Thu Nov 19.")

    async def go():
        alert, _ = await alert_service.create("u1", _events())
        await alert_service.run_action(_user(), alert.id, suggested_action_index=0)
        await _drain()
        [out] = await alert_service.to_out("u1", [await alert_service.get("u1", alert.id)])
        return out

    out = _run(go, tmp_path)
    assert out.status == "new" and out.open                      # the other two are still to do
    assert [a.state for a in out.actions] == ["done", "idle", "idle", "idle"]
    assert out.actions[0].result.summary.startswith("Added Soccer") and not out.actions[0].can_run
    assert out.actions[3].label == "Add the other 2"            # "Add all" now means the rest
    assert out.workflow.progress_label == "1 of 3 added"
    assert "closes by itself" in out.workflow.close_hint


def test_add_all_after_one_is_told_what_to_skip_and_then_closes_the_alert(tmp_path, monkeypatch):
    backend = _backend(monkeypatch, "Added Soccer.", "Added the other two.")

    async def go():
        alert, _ = await alert_service.create("u1", _events())
        await alert_service.run_action(_user(), alert.id, suggested_action_index=0)
        await _drain()
        await alert_service.run_action(_user(), alert.id, suggested_action_index=3)
        await _drain()
        return await alert_service.get("u1", alert.id), (await alert_service.to_out("u1", [await alert_service.get("u1", alert.id)]))[0]

    alert, out = _run(go, tmp_path)
    assert "skip them and do only the rest: Add Soccer" in backend.sent[1]
    assert alert.status == "done_by_gaca"
    assert out.workflow.phase == "closed" and out.workflow.phase_label == "Done by Hermes"
    assert out.workflow.actions_heading == "Hermes did this" and out.workflow.close_options == []
    assert "Closed automatically" in out.workflow.closed_note
    # Soccer was done on its own; the other two were done by "Add all", which reports once for both.
    assert [(a.index, a.state) for a in out.actions] == [(0, "done"), (3, "done")]
    assert out.actions[1].result.summary == "Added the other two."


def test_add_all_straight_away_shows_one_result_not_one_per_row(tmp_path, monkeypatch):
    _backend(monkeypatch, "Added all three.")

    async def go():
        alert, _ = await alert_service.create("u1", _events())
        await alert_service.run_action(_user(), alert.id, suggested_action_index=3)
        await _drain()
        return (await alert_service.to_out("u1", [await alert_service.get("u1", alert.id)]))[0]

    out = _run(go, tmp_path)
    assert [(a.index, a.state, a.label) for a in out.actions] == [(3, "done", "Add all 3")]
    assert out.status == "done_by_gaca" and out.actions[0].result.summary == "Added all three."


def test_doing_each_one_by_one_closes_the_alert_on_the_last_and_hides_the_covering_button(tmp_path, monkeypatch):
    _backend(monkeypatch, "a", "b", "c")

    async def go():
        alert, _ = await alert_service.create("u1", _events())
        statuses = []
        for i in (0, 1, 2):
            await alert_service.run_action(_user(), alert.id, suggested_action_index=i)
            await _drain()
            statuses.append((await alert_service.get("u1", alert.id)).status)
        return statuses, (await alert_service.to_out("u1", [await alert_service.get("u1", alert.id)]))[0]

    statuses, out = _run(go, tmp_path)
    assert statuses == ["new", "new", "done_by_gaca"]
    assert [a.index for a in out.actions] == [0, 1, 2]           # the "Add all" has nothing left to say


def test_running_state_locks_every_other_action(tmp_path, monkeypatch):
    _backend(monkeypatch, "ok")

    async def go():
        alert, _ = await alert_service.create("u1", _events())
        started = await alert_service.run_action(_user(), alert.id, suggested_action_index=1)
        [out] = await alert_service.to_out("u1", [started])
        await _drain()
        return out

    out = _run(go, tmp_path)
    assert [a.state for a in out.actions] == ["idle", "running", "idle", "idle"]
    assert not any(a.can_run for a in out.actions)
    assert out.actions[1].note == "Hermes is on it" and out.actions[0].note is None
    assert (out.workflow.phase, out.workflow.phase_label) == ("working", "Working…")


# ── drafts ──────────────────────────────────────────────────────────

DRAFT = "To: Pat Alvarez <pat@maple.example>\nSubject: Re: Lease renewal terms\n\nHi Pat,\n\nCould we keep it to $50?\n\nDavid"


def _reply_alert() -> AlertCreate:
    return AlertCreate(kind="reply.needed", title="Landlord wants an answer", suggested_actions=[
        {"label": "Ask for less", "prompt": "Draft asking for $50", "kind": "email.draft"},
        {"label": "Accept", "prompt": "Draft accepting", "kind": "email.draft"},
    ])


def test_a_draft_is_stored_whole_does_not_close_the_alert_and_changes_the_buttons(tmp_path, monkeypatch):
    backend = _backend(monkeypatch, DRAFT)

    async def go():
        alert, _ = await alert_service.create("u1", _reply_alert())
        await alert_service.run_action(_user(), alert.id, suggested_action_index=0)
        await _drain()
        stored = await alert_service.get("u1", alert.id)
        return stored, (await alert_service.to_out("u1", [stored]))[0]

    stored, out = _run(go, tmp_path)
    assert "Subject: <subject>" in backend.sent[0] and "not sending anything" in backend.sent[0]
    assert stored.status == "new"                                  # still waiting for the person to send it
    assert stored.draft.subject == "Re: Lease renewal terms" and stored.draft.body.endswith("David")
    assert out.workflow.phase == "waiting_on_you" and out.workflow.phase_label == "Draft ready · waiting for you"
    assert out.workflow.actions_heading == "Hermes drafted this"
    assert [(o.reason, o.label) for o in out.workflow.close_options] == [
        ("done_by_gaca", "I sent it"), ("done_by_me", "I'll write it"), ("not_useful", "No reply needed")]
    first, second = out.actions
    assert first.state == "done" and first.result.draft.to.startswith("Pat Alvarez")
    assert first.result.summary == "Draft ready: Re: Lease renewal terms"
    assert second.state == "idle" and second.can_run and second.cta == "Draft"   # other drafts stay available
    assert out.draft.mailto.startswith("mailto:pat@maple.example?subject=Re%3A%20Lease%20renewal%20terms&body=Hi%20Pat")
    assert out.draft.copy_text.startswith("Subject: Re: Lease renewal terms\n\nHi Pat")


def test_i_sent_it_closes_as_done_by_gaca_but_only_after_something_succeeded(tmp_path, monkeypatch):
    _backend(monkeypatch, DRAFT)

    async def go():
        alert, _ = await alert_service.create("u1", _reply_alert())
        with pytest.raises(InvalidRequest):
            await alert_service.close("u1", alert.id, "done_by_gaca")      # nothing has been done yet
        await alert_service.run_action(_user(), alert.id, suggested_action_index=0)
        await _drain()
        return await alert_service.close("u1", alert.id, "done_by_gaca")

    assert _run(go, tmp_path).status == "done_by_gaca"


def test_a_reply_that_is_not_a_draft_leaves_the_draft_alone_and_one_that_is_replaces_it(tmp_path):
    revised = "Here's a shorter one:\nTo: Pat Alvarez <pat@maple.example>\nSubject: Re: Lease renewal terms\n\nHi Pat, can we do $50? David"

    async def go():
        alert, _ = await alert_service.create("u1", _reply_alert())
        from gaca_api.db import alerts as alerts_db
        await alerts_db.replace(alert.model_copy(update={"draft": drafts.parse_draft(DRAFT)}))
        await alert_service.record_chat_reply(alert.id, RunCompleted(session_id="s", run_id="r", output="Sure, what would you like changed?"))
        unchanged = (await alert_service.get("u1", alert.id)).draft.body
        await alert_service.record_chat_reply(alert.id, RunCompleted(session_id="s", run_id="r", output=revised))
        return unchanged, (await alert_service.get("u1", alert.id)).draft.body

    unchanged, now = _run(go, tmp_path)
    assert unchanged.startswith("Hi Pat,\n\nCould we keep") and now == "Hi Pat, can we do $50? David"


def test_the_agent_is_given_the_current_draft_and_the_format_in_a_chat_about_the_alert(tmp_path, monkeypatch):
    _backend(monkeypatch, DRAFT)

    async def go():
        alert, _ = await alert_service.create("u1", _reply_alert())
        before = await alert_service.chat_context("u1", alert.id)
        await alert_service.run_action(_user(), alert.id, suggested_action_index=0)
        await _drain()
        return before, await alert_service.chat_context("u1", alert.id)

    before, after = _run(go, tmp_path)
    assert "Subject: <subject>" in before and "current email draft" not in before
    assert "The current email draft" in after and "Could we keep it to $50?" in after


# ── reading drafts ──────────────────────────────────────────────────

def test_parsing_a_draft():
    d = drafts.parse_draft(DRAFT, strict=True)
    assert (d.to, d.subject) == ("Pat Alvarez <pat@maple.example>", "Re: Lease renewal terms")
    assert drafts.parse_draft("```\n" + DRAFT + "\n```", strict=True).subject == "Re: Lease renewal terms"
    assert drafts.parse_draft("Sorry, I couldn't find that email.", strict=True) is None      # chat: never guess
    assert drafts.parse_draft("Hi Pat, yes.").body == "Hi Pat, yes."                         # an action: the whole reply
    assert drafts.parse_draft("Subject: x\n\n", strict=True) is None


def test_draft_mailto_handles_no_address_and_special_characters():
    d = AlertDraftOut(to="Pat", subject="A & B?", body="50% off\nnext line")
    assert d.mailto == "mailto:?subject=A%20%26%20B%3F&body=50%25%20off%0Anext%20line"


def test_a_chat_reply_carrying_a_draft_arrives_as_text_plus_a_card():
    from datetime import datetime, timezone
    m = MessageOut(id="m", session_id="s", role="assistant", created_at=datetime.now(timezone.utc),
                   content="Here's a shorter version:\n" + DRAFT)
    assert m.display_content == "Here's a shorter version:" and m.draft.subject == "Re: Lease renewal terms"
    plain = MessageOut(id="m", session_id="s", role="assistant", created_at=datetime.now(timezone.utc), content="Hello")
    assert plain.draft is None and plain.display_content == "Hello"
    mine = MessageOut(id="m", session_id="s", role="user", created_at=datetime.now(timezone.utc), content=DRAFT)
    assert mine.draft is None                                     # only the agent's replies are drafts


# ── the rest of the layout decisions ────────────────────────────────

def test_chat_is_the_main_route_when_there_are_no_actions_and_quiet_otherwise():
    bare = AlertDoc(user_id="u", kind="k", title="t")
    w = alert_view.workflow_out(bare)
    assert w.chat.prominent and "No quick action" in w.chat.hint and w.actions_heading is None


@pytest.mark.parametrize("status,label", [("done_by_me", "You handled this"), ("noted", "Good to know"),
                                          ("not_useful", "Marked not useful")])
def test_a_closed_alert_says_how_it_closed_and_offers_no_buttons(status, label):
    w = alert_view.workflow_out(AlertDoc(user_id="u", kind="k", title="t", status=status))
    assert (w.phase, w.phase_label, w.close_options) == ("closed", label, [])


def test_take_to_chat_says_so_when_the_alert_is_closed_that_way():
    alert = AlertDoc(user_id="u", kind="k", title="t", status="done_by_gaca", resolution_note="Taken to chat")
    assert alert_view.workflow_out(alert).closed_note == "Closed when you took it to chat."
