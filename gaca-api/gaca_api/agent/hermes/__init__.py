"""The Hermes engine adapter.

`translate.py` maps Hermes' raw SSE events onto neutral GacaEvents; `adapter.py`
(the AgentBackend implementation) drives Hermes' HTTP API. This subpackage is the
quarantine zone: Hermes' vocabulary — event strings, endpoint paths, header
names — is allowed here and nowhere else in gaca-api.
"""
