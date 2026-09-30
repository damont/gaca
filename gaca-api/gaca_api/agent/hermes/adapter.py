"""HermesAdapter — the AgentBackend implementation that drives Hermes' HTTP API.

Satisfies the neutral AgentBackend port by talking to a Hermes api_server gateway
(the OpenAI-ish gateway, default :8642):

  * start_run     → POST /v1/runs            (Option A: we replay the transcript)
  * stream_events → GET  /v1/runs/{id}/events (SSE, translated per event)

All Hermes specifics — endpoint paths, request/response field names, the bearer
header, SSE `data:` framing — live here and in translate.py. Nothing past the
port ever sees them.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence

import httpx

from ...config import settings
from ...schemas.event import GacaEvent, RunFailed
from ...schemas.message import MessageOut
from ...schemas.model import ModelConfig, ModelInfo, ProviderCredential
from ...schemas.skill import SkillInfo
from .translate import translate


logger = logging.getLogger(__name__)


class HermesAdapter:
    """Drives a Hermes api_server gateway over HTTP. Structurally an AgentBackend."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        stream_idle_timeout: float = 180.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Omit the header entirely when no key is configured — sending "Bearer "
        # is a malformed header value that httpx rejects locally. No key => no
        # auth (a no-auth loopback gateway works; an authed one returns a clean
        # 401 we can surface, instead of a cryptic LocalProtocolError).
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._stream_idle_timeout = stream_idle_timeout
        # Injectable so tests can hand in a MockTransport client; otherwise we own
        # a long-lived client. The default client has no global timeout; the SSE
        # stream sets its own IDLE (per-event) timeout per call in stream_events.
        self._client = client or httpx.AsyncClient(timeout=None)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Running a turn ────────────────────────────────────────────────
    async def start_run(
        self,
        *,
        session_id: str,
        history: Sequence[MessageOut],
        user_message: str,
    ) -> str:
        """POST /v1/runs and return Hermes' run_id immediately (non-blocking).

        Option A — gaca-api owns the transcript: we send the prior turns as
        `conversation_history` and the new turn as `input`. Hermes holds no
        durable memory of its own; it gets the full context each run.
        """
        payload = {
            "session_id": session_id,
            "conversation_history": [
                {"role": m.role, "content": m.content} for m in history
            ],
            "input": user_message,
        }
        resp = await self._client.post(
            f"{self._base_url}/v1/runs", headers=self._headers, json=payload
        )
        resp.raise_for_status()
        return resp.json()["run_id"]

    async def stream_events(
        self, run_id: str, *, session_id: str
    ) -> AsyncIterator[GacaEvent]:
        """GET /v1/runs/{run_id}/events — consume the SSE, yield GacaEvents.

        Reads the `data: {json}` SSE frames, feeds each raw event through
        translate(), and yields the non-None results (unknown events are dropped
        in the translator, never passed through raw). Ends when the stream closes
        after run.completed / run.failed.
        """
        url = f"{self._base_url}/v1/runs/{run_id}/events"
        # Idle (per-read) timeout: it resets on every chunk, so a legitimately
        # long but ACTIVE run is fine — only a stream that goes silent for this
        # long trips it. That's why it's a read timeout, not a total-duration cap.
        timeout = httpx.Timeout(self._stream_idle_timeout, connect=10.0)
        try:
            async with self._client.stream(
                "GET", url, headers=self._headers, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    # SSE frames we care about are `data: {json}`. Everything else
                    # — blank separators, `: stream closed` comments, `event:`/
                    # `id:` lines — is skipped.
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    raw = json.loads(data)
                    event = translate(raw, session_id=session_id, run_id=run_id)
                    if event is not None:
                        yield event
        except httpx.TimeoutException:
            # Stream went silent past the idle window — surface a neutral failure
            # so read_run finalizes and the client sees why it stopped. Ask the
            # engine what it thinks the run is doing, for the log and the error.
            detail = await self._peek_status(run_id)
            error = f"engine event stream idle for over {self._stream_idle_timeout:.0f}s"
            if detail:
                error += f" ({detail})"
            logger.warning("engine stream idle: run=%s %s", run_id, error)
            yield RunFailed(session_id=session_id, run_id=run_id, error=error)
        except httpx.HTTPError as exc:
            logger.warning("engine stream error: run=%s %s", run_id, exc)
            yield RunFailed(
                session_id=session_id, run_id=run_id, error=f"engine stream error: {exc}"
            )

    async def _peek_status(self, run_id: str) -> str | None:
        """Best-effort GET /v1/runs/{id}: a short "status=…, last_event=…" string.

        Purely diagnostic — never raises, and the caller fails the run either way
        (the events stream can't be resumed once it has dropped).
        """
        try:
            resp = await self._client.get(
                f"{self._base_url}/v1/runs/{run_id}",
                headers=self._headers,
                timeout=httpx.Timeout(5.0),
            )
            if resp.status_code != 200:
                return f"engine status lookup returned {resp.status_code}"
            body = resp.json()
            return f"engine reports status={body.get('status')}, last_event={body.get('last_event')}"
        except (httpx.HTTPError, ValueError):
            return None

    # ── Reserved — not wired in this slice ────────────────────────────
    async def submit_approval(self, *, run_id: str, decision: str) -> None:
        raise NotImplementedError("approvals land in a later slice")

    async def set_model(self, config: ModelConfig) -> None:
        raise NotImplementedError("model config lands in a later slice (ADR 0001)")

    async def set_provider_credential(self, credential: ProviderCredential) -> None:
        raise NotImplementedError("provider credentials land in a later slice (ADR 0001)")

    async def list_models(self) -> list[ModelInfo]:
        raise NotImplementedError("model listing lands in a later slice (ADR 0001)")

    async def get_model_info(self, model_id: str) -> ModelInfo:
        raise NotImplementedError("model info lands in a later slice (ADR 0001)")

    # ── Skills ─────────────────────────────────────────────────────────
    async def list_skills(self) -> list[SkillInfo]:
        """GET /v1/skills on this crew member's own Hermes gateway.

        Live every time — never cached here — so the app can't show a skill
        Hermes no longer has installed. Hermes returns
        `{"object": "list", "data": [{"name", "description", "category"}, …]}`,
        already sorted by category then name.
        """
        resp = await self._client.get(
            f"{self._base_url}/v1/skills", headers=self._headers
        )
        resp.raise_for_status()
        return [SkillInfo.model_validate(s) for s in resp.json().get("data", [])]


def create(base_url: str, api_key: str) -> HermesAdapter:
    """Composition entrypoint the agent runtime loads by name from
    GACA_AGENT_ENGINE — keeps the core from importing a specific engine.

    base_url/api_key come from the calling user's own UserDoc, not app-wide
    settings — see agent/runtime.py:backend_for. Only the idle timeout is
    still a process-wide setting; it's an operational tuning knob, not a
    per-user credential.
    """
    return HermesAdapter(
        base_url, api_key, stream_idle_timeout=settings.agent_stream_idle_timeout
    )
