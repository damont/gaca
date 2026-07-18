"""Neutral model- and provider-config schemas (ADR 0001).

Model selection and provider credentials are per-deployment *runtime* config,
entered by the operator through the API — not baked into the deploy layer. These
are the neutral shapes the routers/services speak; only an engine's adapter
knows how to apply them to that engine. See ADR 0001 (runtime model &
provider config).

Skeletal on purpose: the fields below are the contract ADR 0001 fixed; we flesh
out validation/behavior when we build the config flow. They exist now so the
AgentBackend port can reserve its model-management methods without a later
contract change.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ModelConfig(BaseModel):
    """The active model selection: which provider, which model, what params."""
    provider: str                       # e.g. "anthropic", "openai"
    model: str                          # the provider's model id
    params: dict[str, Any] = {}         # temperature, max_tokens, …


class ProviderCredential(BaseModel):
    """A provider API key the operator supplies at runtime."""
    provider: str
    api_key: str


class ModelInfo(BaseModel):
    """Metadata about a selectable model, surfaced to the app."""
    id: str
    provider: str
    context_window: int | None = None
    capabilities: list[str] = []
