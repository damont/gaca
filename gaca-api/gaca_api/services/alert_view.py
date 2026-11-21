"""How an alert's actions stand and how its screen should lay out. Pure functions, no I/O.

This is where the alert workflow lives, so every app behaves the same and none of them decides
anything: which actions can be tapped, what each reads as, whether the alert closes when one
finishes, which buttons close it, and what the status line says. An app renders `AlertOut.actions`
and `AlertOut.workflow` and posts the person's taps back.
"""
from __future__ import annotations

from datetime import date

from ..models.alert import AlertDoc, AlertRun, SuggestedAction
from ..schemas.alert import (
    ActionResultOut,
    AlertActionOut,
    AlertDraftOut,
    AlertWorkflow,
    ChatEntry,
    CloseOption,
)

OPEN = "new"

# What the progress line says is "done", by the kind of thing the actions do.
_VERBS = {"calendar.add": "added", "list.add": "added", "task.add": "added"}
# The heading over a list of actions that haven't been tapped yet.
_HEADINGS = {
    "calendar.add": ("Hermes can add this", "Hermes can add these"),
    "list.add": ("Hermes can add this", "Hermes can add these"),
    "email.draft": ("Hermes can draft a reply", "Hermes can draft a reply"),
}


_CTAS = {"calendar.add": "Add", "list.add": "Add", "task.add": "Add", "email.draft": "Draft"}


def is_draft_action(action: SuggestedAction) -> bool:
    return (action.kind or "").startswith("email.draft")


def closes(action: SuggestedAction) -> bool:
    """Does finishing this action finish the alert? Drafting an email doesn't: the person still
    has to send it, and the alert must not disappear while a draft sits unsent."""
    if action.closes is not None:
        return action.closes
    return not is_draft_action(action)


def _valid_covers(alert: AlertDoc, action: SuggestedAction) -> list[int]:
    n = len(alert.suggested_actions)
    return sorted({i for i in action.covers if 0 <= i < n})


def done_indexes(alert: AlertDoc) -> set[int]:
    """Actions that have succeeded, directly or because an "Add all" that covers them did."""
    done: set[int] = set()
    for run in alert.runs:
        if run.state == "done" and run.action_index is not None and run.action_index < len(alert.suggested_actions):
            done.add(run.action_index)
            done.update(_valid_covers(alert, alert.suggested_actions[run.action_index]))
    return done


def running(alert: AlertDoc) -> bool:
    return any(r.state == "running" for r in alert.runs)


def _latest(alert: AlertDoc, index: int, state: str | None = None) -> AlertRun | None:
    for run in reversed(alert.runs):
        if run.action_index == index and (state is None or run.state == state):
            return run
    return None


def _covering_done_run(alert: AlertDoc, index: int) -> AlertRun | None:
    for run in reversed(alert.runs):
        if run.state == "done" and run.action_index is not None and run.action_index < len(alert.suggested_actions):
            if index in _valid_covers(alert, alert.suggested_actions[run.action_index]):
                return run
    return None


def _remaining(alert: AlertDoc, action: SuggestedAction) -> list[int]:
    done = done_indexes(alert)
    return [i for i in _valid_covers(alert, action) if i not in done]


def _is_partial(alert: AlertDoc, action: SuggestedAction) -> bool:
    covered = _valid_covers(alert, action)
    left = _remaining(alert, action)
    return bool(covered) and 0 < len(left) < len(covered)


def effective_label(alert: AlertDoc, index: int) -> str:
    action = alert.suggested_actions[index]
    if _is_partial(alert, action) and action.remaining_label:
        return action.remaining_label.replace("{n}", str(len(_remaining(alert, action))))
    return action.label


def effective_prompt(alert: AlertDoc, index: int) -> str:
    """The prompt to run. An "Add all" that is part-way is told what is already done."""
    action = alert.suggested_actions[index]
    if not _is_partial(alert, action):
        return action.prompt
    done = done_indexes(alert)
    skip = [alert.suggested_actions[i].label for i in _valid_covers(alert, action) if i in done]
    return (
        f"{action.prompt}\n\nThese are already done, so skip them and do only the rest: "
        + "; ".join(skip)
    )


def _state(alert: AlertDoc, index: int) -> str:
    action = alert.suggested_actions[index]
    covered = _valid_covers(alert, action)
    done = done_indexes(alert)
    if covered and all(i in done for i in covered):
        return "done"
    if index in done and not covered:
        return "done"
    if any(r.state == "running" and r.action_index == index for r in alert.runs):
        return "running"
    if (r := _latest(alert, index)) is not None and r.state == "failed":
        return "failed"
    return "done" if index in done else "idle"


def _detail(action: SuggestedAction) -> str | None:
    """The line under an action's label, made from its `params`: the poster can give `detail`
    outright, or a date (with time, place, person) that is written out here."""
    params = action.params or {}
    if isinstance(params.get("detail"), str) and params["detail"].strip():
        return params["detail"].strip()
    if not params.get("date"):
        return None
    try:
        day = date.fromisoformat(str(params["date"]))
        when = f"{day.strftime('%a %b')} {day.day}"
    except ValueError:
        when = str(params["date"])
    parts = [when, str(params["time"]) if params.get("time") else "all day"]
    parts += [str(params[k]) for k in ("location", "who") if params.get(k)]
    return " · ".join(parts)


def _result(alert: AlertDoc, index: int, state: str) -> ActionResultOut | None:
    if state == "done":
        run = _latest(alert, index, "done") or _covering_done_run(alert, index)
        if run is None:
            return None
        draft = run.draft
        # A revised draft (from the chat) replaces the one the run first wrote.
        if draft is not None and alert.draft is not None:
            draft = alert.draft
        return ActionResultOut(
            summary=run.summary, links=run.links,
            draft=AlertDraftOut(**draft.model_dump()) if draft else None,
        )
    if state == "failed":
        run = _latest(alert, index, "failed")
        return ActionResultOut(error=(run.error if run else None) or "It didn't work.")
    return None


def actions_out(alert: AlertDoc) -> list[AlertActionOut]:
    busy = running(alert)
    open_ = alert.status == OPEN
    out = []
    for i, action in enumerate(alert.suggested_actions):
        state = _state(alert, i)
        if _valid_covers(alert, action) and state == "done" and _latest(alert, i, "done") is None:
            continue  # an "Add all" whose items were all done one by one has nothing left to show
        if state == "done" and _latest(alert, i, "done") is None and _covering_done_run(alert, i) is not None:
            continue  # done only because an "Add all" covered it: that one result says it, not each row again
        out.append(
            AlertActionOut(
                index=i, kind=action.kind, label=effective_label(alert, i), params=action.params,
                detail=_detail(action), state=state,
                cta=_CTAS.get(action.kind or "", "Do it"), prominent=bool(_valid_covers(alert, action)),
                note="Hermes is on it" if state == "running" else None,
                can_run=open_ and not busy and state in ("idle", "failed"),
                result=_result(alert, i, state),
            )
        )
    return out


def should_close(alert: AlertDoc, run: AlertRun) -> bool:
    """A run just succeeded: does that finish the alert? A free-text request does. An action that
    doesn't close (a draft) never does. Otherwise it closes once every closing action is done, so
    adding one of three events leaves the other two to tap."""
    if run.action_index is None:
        return True
    if run.action_index >= len(alert.suggested_actions) or not closes(alert.suggested_actions[run.action_index]):
        return False
    done = done_indexes(alert)
    wanted = [i for i, a in enumerate(alert.suggested_actions) if closes(a) and not _valid_covers(alert, a)]
    return all(i in done for i in wanted)


def _progress(alert: AlertDoc) -> tuple[int, int, str] | None:
    singles = [(i, a) for i, a in enumerate(alert.suggested_actions) if closes(a) and not _valid_covers(alert, a)]
    if len(singles) < 2:
        return None
    done = done_indexes(alert)
    count = sum(1 for i, _ in singles if i in done)
    kinds = {a.kind for _, a in singles}
    verb = _VERBS.get(next(iter(kinds)), "done") if len(kinds) == 1 else "done"
    return count, len(singles), verb


def workflow_out(alert: AlertDoc) -> AlertWorkflow:
    actions = alert.suggested_actions
    open_ = alert.status == OPEN
    busy = running(alert)
    waiting = open_ and not busy and alert.draft is not None
    failed = open_ and any(_state(alert, i) == "failed" for i in range(len(actions)))
    progress = _progress(alert) if open_ else None
    part_way = progress is not None and 0 < progress[0] < progress[1]

    if not open_:
        phase = "closed"
        label = {
            "done_by_gaca": "Done by Hermes", "done_by_me": "You handled this",
            "noted": "Glad to know", "not_useful": "Marked not useful",
        }.get(alert.status)
    elif busy:
        phase, label = "working", "Working…"
    elif waiting:
        phase, label = "waiting_on_you", "Draft ready · waiting for you"
    else:
        phase, label = "open", None

    # Heading over the actions.
    heading = None
    if actions:
        if not open_:
            heading = "Hermes did this" if any(r.state == "done" for r in alert.runs) else None
        elif waiting:
            heading = "Hermes drafted this"
        else:
            first = next((a for a in actions if not _valid_covers(alert, a)), actions[0])
            singular, plural = _HEADINGS.get(first.kind or "", ("Hermes can do this", "Hermes can do this"))
            heading = plural if len([a for a in actions if not _valid_covers(alert, a)]) > 1 else singular

    if not open_ and alert.status == "done_by_gaca":
        chat_hint = "Something to change or undo? Hermes knows what it did."
    elif failed:
        chat_hint = "Hermes already has this alert, and what just failed"
    elif alert.draft is not None:
        chat_hint = "Hermes already has this alert and your draft"
    elif open_ and not actions:
        chat_hint = "No quick action for this one. Hermes has the alert and can look into it with you."
    else:
        chat_hint = "Hermes already has this alert"
    chat = ChatEntry(hint=chat_hint, prominent=open_ and not actions)

    close_heading = close_hint = None
    options: list[CloseOption] = []
    if open_:
        if waiting:
            close_heading = "When you're done"
            options = [
                CloseOption(reason="done_by_gaca", label="I sent it", primary=True),
                CloseOption(reason="done_by_me", label="I'll write it"),
                CloseOption(reason="not_useful", label="No reply needed"),
            ]
            close_hint = "The alert stays here until you close it, so a draft you haven't sent doesn't disappear."
        else:
            close_heading = "Or close it yourself"
            options = [
                CloseOption(reason="done_by_me", label="I'll handle it", primary=True),
                CloseOption(reason="noted", label="Glad to know"),
                CloseOption(reason="not_useful", label="Not useful"),
            ]
            close_hint = (
                "The alert closes by itself once all of them are done."
                if part_way else "Your choice teaches Hermes what to send you next time."
            )

    closed_note = None
    if alert.status == "done_by_gaca":
        if alert.resolution_note == "Taken to chat":
            closed_note = "Closed when you took it to chat."
        elif alert.draft is None and any(r.state == "done" for r in alert.runs):
            closed_note = "Closed automatically when the last action finished."

    return AlertWorkflow(
        phase=phase, phase_label=label, actions_heading=heading,
        progress_label=f"{progress[0]} of {progress[1]} {progress[2]}" if part_way else None,
        chat=chat, close_heading=close_heading, close_options=options, close_hint=close_hint,
        closed_note=closed_note,
    )
