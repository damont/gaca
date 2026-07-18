"""Agent backends: the neutral port plus per-engine adapters.

`base.py` defines the AgentBackend Protocol — the seam the rest of gaca-api
depends on. Each engine gets its own subpackage whose adapter
implements the port and whose translator is the only code that knows that
engine's raw shapes. Swapping engines = a new subpackage, not a core rewrite.
"""
