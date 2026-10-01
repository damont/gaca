"""The AgentBackend port — the neutral seam every engine implements.

The core (routers, services, the app) depends only on this Protocol, never on a
concrete engine. Each engine ships its own adapter implementing this Protocol; a second
engine is just a second adapter. Because it's a Protocol, matching is *structural*: an object
is an AgentBackend if it has these methods — no explicit subclassing required.

Nothing engine-specific belongs here — this file speaks only neutral schemas
(GacaEvent, MessageOut, ModelConfig, …). Each engine's own shapes live behind
its adapter in `agent/<engine>/`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from ..schemas.crew_profile import ProfileInfo
from ..schemas.event import GacaEvent
from ..schemas.message import ImageInput, MessageOut
from ..schemas.model import ModelConfig, ModelInfo, ProviderCredential
from ..schemas.skill import SkillInfo


@runtime_checkable
class AgentBackend(Protocol):
    """What any agent engine must provide for gaca-api to drive it."""

    # ── Running a turn ────────────────────────────────────────────────
    async def start_run(
        self,
        *,
        session_id: str,
        history: Sequence[MessageOut],
        user_message: str,
        images: Sequence[ImageInput] = (),
    ) -> str:
        """Begin a run; return its engine run_id immediately (non-blocking).

        `images` are photos attached to THIS turn (history carries only text, plus
        a note from the adapter that earlier turns had photos).

        `history` is the prior conversation for this session and `user_message`
        the new user turn — gaca-api owns the transcript and replays it each run
        (Option A). The engine holds no durable memory of its own here; it gets
        the context it needs on every call. The returned run_id addresses the run
        in `stream_events` and `submit_approval`.
        """
        ...

    def stream_events(
        self, run_id: str, *, session_id: str
    ) -> AsyncIterator[GacaEvent]:
        """Consume the engine's live run output and yield neutral GacaEvents.

        An async generator. The caller supplies `session_id` (it started the run
        for a session) so each event can be tagged with the session/run it
        belongs to. Events come out complete except for `seq`, which stays None —
        the core stamps it as it persists and publishes them, because `seq` needs
        per-session monotonic state the adapter doesn't hold. Terminates when the
        run reaches `run.completed` or `run.failed` — there is no separate
        sentinel.
        """
        ...

    # ── Human-in-the-loop (reserved — wired in a later slice) ─────────
    async def submit_approval(self, *, run_id: str, decision: str) -> None:
        """Resolve a pending approval for a run (approve/deny a gated tool).

        Reserved now so adding approvals later is not a contract change; the
        `decision` type gets a neutral enum when we build the approval flow.
        """
        ...

    # ── Model & provider config (reserved — ADR 0001) ─────────────────
    async def set_model(self, config: ModelConfig) -> None:
        """Set/replace the active model (provider + model id + params)."""
        ...

    async def set_provider_credential(self, credential: ProviderCredential) -> None:
        """Store a provider API key the operator supplied at runtime."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """List models the engine can use, with their metadata."""
        ...

    async def get_model_info(self, model_id: str) -> ModelInfo:
        """Return metadata/capabilities for one model."""
        ...

    # ── Skills ──────────────────────────────────────────────────────
    async def list_skills(self) -> list[SkillInfo]:
        """List the skills installed and visible to this crew member.

        Read-only insight, not control — skills load however the engine
        loads them; the app just shows what's there. Never cached by
        gaca-api: each call reflects whatever the engine currently has.
        """
        ...

    # ── Profile (read-only picture of how this crew member is set up) ──
    async def describe_profile(self) -> ProfileInfo:
        """What the engine can tell us about this profile, in GACA's neutral terms
        (schemas/crew_profile.py): status, model, channels, skills, tools, schedule.

        Never raises for an unreachable or partly-broken engine: it returns
        `status="offline"` or lists what it couldn't read in `unavailable`, so a
        client can show the rest instead of an error page.
        """
        ...
