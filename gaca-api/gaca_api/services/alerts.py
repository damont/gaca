"""Alert business logic. No FastAPI, no HTTP — just orchestration."""
from __future__ import annotations

import re

from ..db import alerts as alerts_db
from ..db import users as users_db
from ..db import artifacts as artifacts_db
from ..db import sessions as sessions_db
from ..models.alert import PERSON_CLOSE_REASONS, AlertDoc, AlertLink, AlertRun
from ..models.base import utcnow
from ..models.user import UserDoc
from ..schemas.alert import AlertCreate, AlertOut, render_value
from ..schemas.event import GacaEvent, RunCompleted, RunFailed
from . import artifact_refs
from ..db import cloud as cloud_db
from . import auth as auth_service
from . import push as push_service
from . import runs as run_service
from . import sessions as session_service
from .exceptions import Conflict, InvalidRequest, InvalidTransition, NotFound

# An alert can be closed, or have an action run on it, only while it is open.
OPEN = "new"
RUN_SUMMARY_MAX = 300
MAX_RUN_LINKS = 5


def is_open(alert: AlertDoc) -> bool:
    return alert.status == OPEN


async def create(user_id: str, payload: AlertCreate, allow_cloud: bool = True) -> tuple[AlertDoc, bool]:
    """Create an alert, or fold it onto the one it dedupes with.

    Returns (alert, created) so the route can answer 201 or 200.

    A re-fired condition resets the status to `new`. That is deliberate: if you
    dismissed "disk low" yesterday and the disk is still filling today, the
    alert should come back rather than stay silently dismissed. Ownership,
    identity and creation time are preserved — it is the same alert, updated.

    THE UPDATE IS A FULL REPLACE OF THE CONTENT FIELDS, NOT A MERGE. Anything
    the caller omits reverts to its default — omit `suggested_actions` and the
    old ones are cleared, omit `source` and it goes back to "unknown". This is
    intentional, not an oversight:

      * An alert describes a condition as it stands *now*. A `suggested_action`
        carries a `prompt` that gets handed to an agent to execute; a stale one
        surviving a re-post means tapping a plausible-looking button dispatches
        work based on an hour-old picture. Losing buttons is a visible,
        harmless failure; keeping wrong ones is neither.
      * It makes the endpoint genuinely idempotent — the same body twice gives
        the same row regardless of history, which is what you want from
        something a five-minute cron hits.

    The cost is that a terse re-post silently strips detail, and the poster is
    an LLM that may not resend unchanged fields. That is a judgment rule rather
    than a schema rule, so the Hermes skill has to say "a re-post is a complete
    restatement, not a delta". If we ever want merge semantics instead, it is
    `payload.model_dump(exclude_unset=True)` here — but then the result depends
    on history, which makes a noisy poster much harder to debug.
    """
    if payload.artifact_id and await artifacts_db.find(user_id, payload.artifact_id) is None:
        # A cloud gacabit the caller can see (a pointer) is a valid thing to attach too.
        if not (allow_cloud and await cloud_db.get_pointer(user_id, payload.artifact_id)):
            raise NotFound("artifact", payload.artifact_id)
    if payload.dedupe_key:
        existing = await alerts_db.find_by_dedupe_key(user_id, payload.dedupe_key)
        if existing is not None:
            updated = AlertDoc.model_validate(
                {
                    **existing.model_dump(),
                    **payload.model_dump(),
                    "status": "new",
                    "status_changed_at": utcnow(),
                    # Clear anything the previous triage left behind, including runs: they
                    # answered an earlier picture of the condition.
                    "session_id": None,
                    "runs": [],
                    "resolution_note": None,
                }
            )
            await alerts_db.replace(updated)
            await push_service.notify(
                user_id,
                title=updated.title,
                body=updated.body or "",
                alert_id=updated.id,
                severity=updated.severity,
            )
            return updated, False

    alert = AlertDoc(user_id=user_id, **payload.model_dump())
    await alerts_db.insert(alert)
    await push_service.notify(
        user_id,
        title=alert.title,
        body=alert.body or "",
        alert_id=alert.id,
        severity=alert.severity,
    )
    return alert, True


async def to_out(user_id: str, alerts: list[AlertDoc]) -> list[AlertOut]:
    """Alerts in the client shape: effective status derived, and each `artifact_id`
    resolved to its chip — one artifact query for the whole list."""
    refs = await artifact_refs.resolve(user_id, {a.artifact_id: None for a in alerts if a.artifact_id})
    owner = await users_db.find(user_id)
    removed = auth_service.removed_crew_names(owner) if owner else set()
    return [
        AlertOut(
            **a.model_dump(),
            effective_status=a.status,
            crew_removed=a.crew in removed,
            artifact=refs.get(a.artifact_id) if a.artifact_id else None,
        )
        for a in alerts
    ]


async def get(user_id: str, alert_id: str) -> AlertDoc:
    """One alert, if it belongs to this user.

    Someone else's alert raises NotFound rather than a forbidden error: a 403
    would confirm the id exists, which is information the caller has no business
    learning.
    """
    alert = await alerts_db.find(alert_id)
    if alert is None or alert.user_id != user_id:
        raise NotFound("alert", alert_id)
    return alert




async def close(
    user_id: str, alert_id: str, reason: str, note: str | None = None
) -> AlertDoc:
    """The person closes an open alert and says how it ended (`done_by_me`, `noted` or
    `not_useful`). `done_by_gaca` is never chosen by hand — only a successful run or take-to-chat
    sets it.

    The reason is what the heartbeat learns from, so it is required, not defaulted.
    """
    if reason not in PERSON_CLOSE_REASONS:
        raise InvalidRequest(f"reason must be one of {', '.join(PERSON_CLOSE_REASONS)}")
    alert = await get(user_id, alert_id)
    if not is_open(alert):
        raise InvalidTransition("alert", alert_id, alert.status, reason)
    updated = alert.model_copy(
        update={
            "status": reason,
            "status_changed_at": utcnow(),
            "resolution_note": note if note is not None else alert.resolution_note,
        }
    )
    await alerts_db.replace(updated)
    return updated


async def act(user_id: str, alert_id: str, note: str | None = None) -> AlertDoc:
    """Deprecated: "I handled it myself". Same as close(reason="done_by_me")."""
    return await close(user_id, alert_id, "done_by_me", note)


async def dismiss(user_id: str, alert_id: str, note: str | None = None) -> AlertDoc:
    """Deprecated: "ignore this". Same as close(reason="not_useful")."""
    return await close(user_id, alert_id, "not_useful", note)


# Given to the agent with every action run, so the answer is short enough to show under a button.
RUN_CONTRACT = (
    "The person tapped this action in the GACA app. Do it now, then reply in at most three short "
    "lines saying exactly what you did, with no preamble. Put any link to what you made or "
    "changed on its own line as a plain URL. If you could not do it, say why in one line."
)


def _alert_context(alert: AlertDoc) -> str:
    """The alert as plain text for an agent: what it said, what it carries, what was offered and
    what has already been tried. Sent ahead of every turn in a chat tied to the alert, so the
    person never retypes it and the agent never works from a stale copy."""
    lines = [
        "[GACA alert this conversation is about — context, not a message from the person. "
        "Don't ask them to repeat it.]",
        f"Title: {alert.title}",
        f"Kind: {alert.kind}   Severity: {alert.severity}   Status: {alert.status}",
    ]
    if alert.body:
        lines.append(f"Body: {alert.body}")
    for key, value in alert.data.items():
        lines.append(f"{key}: {render_value(value)}")
    if alert.links:
        lines.append("Links:")
        lines += [f"- {link.label or link.url}: {link.url}" for link in alert.links]
    if alert.suggested_actions:
        lines.append("Actions that were offered:")
        lines += [f"- {a.label}" for a in alert.suggested_actions]
    if alert.runs:
        lines.append("Already tried:")
        for run in alert.runs:
            detail = run.summary or run.error or ""
            lines.append(f"- {run.label} — {run.state}{': ' + detail if detail else ''}")
    return "\n".join(lines)


async def chat_context(user_id: str, alert_id: str | None) -> str | None:
    """Context for a chat turn in a session tied to `alert_id`; None if there is none (or the
    alert is gone — the chat then simply carries on without it)."""
    if not alert_id:
        return None
    alert = await alerts_db.find(alert_id)
    if alert is None or alert.user_id != user_id:
        return None
    return _alert_context(alert)


def _action_prompt(alert: AlertDoc, instruction: str | None, action_index: int | None) -> tuple[str, str]:
    """(label for the run, prompt for the agent)."""
    prompt = _delegate_prompt(alert, instruction, action_index)
    if action_index is not None:
        label = alert.suggested_actions[action_index].label
    else:
        label = re.split(r"\r\n|\r|\n", (instruction or alert.title).strip(), maxsplit=1)[0][:_TITLE_MAX]
    return label, prompt


def _delegate_prompt(
    alert: AlertDoc, instruction: str | None, suggested_action_index: int | None
) -> str:
    """The text handed to the agent for an action run.

    A chosen suggested action supplies its own prompt (written to stand alone); a free-text
    `instruction` layers on top of that, or stands alone. With neither, the alert's own
    title/body/links stand in for it.
    """
    parts: list[str] = []
    if suggested_action_index is not None:
        actions = alert.suggested_actions
        if not (0 <= suggested_action_index < len(actions)):
            raise InvalidRequest(
                f"suggested_action_index {suggested_action_index} is out of "
                f"range for alert {alert.id!r} ({len(actions)} action(s))"
            )
        parts.append(actions[suggested_action_index].prompt)
    if instruction:
        parts.append(instruction)
    if not parts:
        parts.append(f"{alert.title}\n\n{alert.body}" if alert.body else alert.title)
        if alert.links:
            parts.append(
                "Links:\n" + "\n".join(f"- {link.label or link.url}: {link.url}" for link in alert.links)
            )
    return "\n\n".join(parts)


async def _alert_session(user: UserDoc, alert: AlertDoc, crew: str | None):
    """The chat tied to this alert, opened on first use. One chat per alert: a retry or a
    follow-up lands in the same conversation, so the agent has what it already tried.

    Returns (session_id, crew_member, created)."""
    if alert.session_id:
        existing = await sessions_db.find(alert.session_id)
        if existing is not None:
            member = auth_service.find_crew_member(user, existing.crew) or auth_service.resolve_crew_for_delegate(
                user, crew, alert.crew
            )
            return existing.id, member, False
    member = auth_service.resolve_crew_for_delegate(user, crew, alert.crew)
    session = await session_service.create(user.id, alert.title, member.name, alert_id=alert.id)
    return session.id, member, True


async def run_action(
    user: UserDoc,
    alert_id: str,
    *,
    instruction: str | None = None,
    suggested_action_index: int | None = None,
    crew: str | None = None,
) -> AlertDoc:
    """Tap an action: record a run on the alert and start an agent on it.

    The alert stays open while the run is in flight and when it fails (so it can be retried).
    A run that succeeds closes the alert as `done_by_gaca` — see _settle_run. Only one run at a
    time per alert, and only on an open alert. The agent is given the alert as context and the
    short-answer contract; the chat keeps just the action's own prompt as what was "said".

    `crew` is an explicit choice (unknown -> 422); otherwise the chat's own crew member, the
    alert's `crew`, then the account default.
    """
    alert = await get(user.id, alert_id)
    if not is_open(alert):
        raise InvalidTransition("alert", alert_id, alert.status, "running")
    if any(r.state == "running" for r in alert.runs):
        raise Conflict("An action is already running on this alert")

    # Validate against the alert's own data before touching a crew member at all.
    label, prompt = _action_prompt(alert, instruction, suggested_action_index)
    session_id, crew_member, _ = await _alert_session(user, alert, crew)
    backend = auth_service.backend_for_crew(user, crew_member)

    run = AlertRun(action_index=suggested_action_index, label=label, session_id=session_id)
    updated = alert.model_copy(update={"session_id": session_id, "runs": [*alert.runs, run]})
    await alerts_db.replace(updated)

    async def _on_settled(event: GacaEvent) -> None:
        await _settle_run(alert_id, run.id, event)

    try:
        await run_service.start_turn(
            backend,
            session_id,
            prompt,
            on_settled=_on_settled,
            agent_context=f"{_alert_context(updated)}\n\n{RUN_CONTRACT}",
        )
    except Exception as error:
        # The run never started, so don't leave it "running" forever.
        await _finish_run(alert_id, run.id, state="failed", error=str(error))
        raise
    return updated


def _links_in(text: str) -> list[AlertLink]:
    """http(s) URLs the agent mentioned, in order, de-duplicated — shown as tappable links."""
    seen: list[str] = []
    for url in re.findall(r"https?://[^\s<>)\]\"']+", text):
        url = url.rstrip(".,;:!?")
        if url not in seen:
            seen.append(url)
    return [AlertLink(url=u) for u in seen[:MAX_RUN_LINKS]]


async def _finish_run(
    alert_id: str, run_id: str, *, state: str, summary: str | None = None,
    links: list[AlertLink] | None = None, error: str | None = None,
) -> AlertDoc | None:
    """Record how a run ended. Reads the row fresh: the alert may have been closed, deduped
    or deleted since the run started, and a settlement must never clobber that. Returns the
    updated alert, or None if there was nothing to settle."""
    current = await alerts_db.find(alert_id)
    if current is None:
        return None
    runs = list(current.runs)
    for i, r in enumerate(runs):
        if r.id == run_id and r.state == "running":
            runs[i] = r.model_copy(
                update={
                    "state": state, "finished_at": utcnow(), "summary": summary,
                    "links": links or [], "error": error,
                }
            )
            break
    else:
        return None
    updated = current.model_copy(update={"runs": runs})
    await alerts_db.replace(updated)
    return updated


async def _settle_run(alert_id: str, run_id: str, event: GacaEvent) -> None:
    """A run ended. Success records the result and closes an open alert as `done_by_gaca`;
    failure records the error and leaves the alert open so it can be retried. Either way the
    person is told."""
    from ..utils import artifact_links
    from .sessions import tidy

    if isinstance(event, RunCompleted):
        text = artifact_links.strip(event.output or "")
        summary = tidy(text, RUN_SUMMARY_MAX) or "Done."
        updated = await _finish_run(
            alert_id, run_id, state="done", summary=summary, links=_links_in(event.output or "")
        )
        if updated is None:
            return
        if is_open(updated):
            updated = updated.model_copy(
                update={"status": "done_by_gaca", "status_changed_at": utcnow(), "resolution_note": summary}
            )
            await alerts_db.replace(updated)
        await push_service.notify(
            updated.user_id, title=f"Done: {updated.title}", body=summary,
            alert_id=updated.id, severity=updated.severity,
        )
    elif isinstance(event, RunFailed):
        updated = await _finish_run(alert_id, run_id, state="failed", error=event.error)
        if updated is None:
            return
        await push_service.notify(
            updated.user_id, title=f"Couldn't finish: {updated.title}",
            body=event.error or "The action failed. It is still in your inbox.",
            alert_id=updated.id, severity=updated.severity,
        )


MANUAL_DELEGATION_KIND = "manual.delegation"
_TITLE_MAX = 120


async def delegate_new(
    user: UserDoc, instruction: str, *, crew: str | None = None, source: str = "app"
) -> AlertDoc:
    """Raise an alert for something the user asked for (rather than something an agent
    noticed) and run it straight away — the app's "New delegation" screen.

    It's an ordinary alert so the result lands where every run's does. Attributed to the crew
    member handling it, since the inbox's crew filter goes by this field. Who handles it — and
    that they're reachable — is checked before anything is stored, so a bad `crew` never leaves
    an orphan alert behind.
    """
    crew_member = auth_service.resolve_crew_for_delegate(user, crew, None)
    auth_service.backend_for_crew(user, crew_member)  # raises if there's no agent to reach
    title = re.split(r"\r\n|\r|\n", instruction, maxsplit=1)[0][:_TITLE_MAX]
    alert, _ = await create(
        user.id,
        AlertCreate(
            kind=MANUAL_DELEGATION_KIND, title=title, body=instruction,
            crew=crew_member.name, source=source,
        ),
    )
    return await run_action(user, alert.id, instruction=instruction, crew=crew_member.name)


async def take_to_chat(
    user: UserDoc,
    alert_id: str,
    *,
    suggested_action_index: int | None = None,
    crew: str | None = None,
) -> tuple[AlertDoc, str, str | None]:
    """Open the alert's chat WITHOUT sending anything to the agent.

    The chat is tied to the alert and receives it as context on every turn (see chat_context),
    so nothing of the alert is copied into the message box. `draft` is only the one picked
    action's prompt, for the person to read, edit and send; otherwise None.

    Taking an alert to chat counts as the agent helping: an open alert closes as `done_by_gaca`
    with a note saying so. A closed one keeps its status. An alert that already has a chat reuses it.

    Returns (alert, session_id, draft).
    """
    alert = await get(user.id, alert_id)
    draft = None
    if suggested_action_index is not None:
        actions = alert.suggested_actions
        if not (0 <= suggested_action_index < len(actions)):
            raise InvalidRequest(
                f"suggested_action_index {suggested_action_index} is out of "
                f"range for alert {alert.id!r} ({len(actions)} action(s))"
            )
        draft = actions[suggested_action_index].prompt

    session_id, _, _ = await _alert_session(user, alert, crew)
    alert = alert.model_copy(update={"session_id": session_id})
    if is_open(alert):
        alert = alert.model_copy(
            update={
                "status": "done_by_gaca",
                "status_changed_at": utcnow(),
                "resolution_note": alert.resolution_note or "Taken to chat",
            }
        )
    await alerts_db.replace(alert)
    return alert, session_id, draft


async def list_for_user(
    user_id: str,
    *,
    statuses: list[str] | None = None,
    severities: list[str] | None = None,
    crews: list[str] | None = None,
    limit: int = 50,
) -> list[AlertDoc]:
    """A user's alerts, newest first. `statuses` is any of new / done_by_gaca / done_by_me /
    noted / not_useful."""
    alerts = await alerts_db.find_by_user(user_id)
    if statuses:
        wanted = set(statuses)
        alerts = [a for a in alerts if a.status in wanted]
    if severities:
        wanted = set(severities)
        alerts = [a for a in alerts if a.severity in wanted]
    if crews:
        wanted = set(crews)
        alerts = [a for a in alerts if a.crew in wanted]
    return alerts[:limit]
