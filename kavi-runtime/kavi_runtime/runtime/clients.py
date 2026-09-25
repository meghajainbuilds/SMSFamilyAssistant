"""Shared client singletons for the runtime.

`_get_clients` returns the process-global `GraphClient`, `ClaudeClient`,
`BlueBubblesClient` singletons. Used by every capability's entry point so
that webhook + scheduler ticks share the same client state (auth tokens,
connection pools, retry state).

Lives in `runtime/` because more than one capability needs it.
"""

from __future__ import annotations

from kavi_runtime.bluebubbles_client import BlueBubblesClient
from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.graph_client import GraphClient

_graph_client: GraphClient | None = None
_claude_client: ClaudeClient | None = None
_bb_client: BlueBubblesClient | None = None


def _get_clients(config: dict) -> tuple[GraphClient, ClaudeClient, BlueBubblesClient]:
    """Return the process-global (graph, claude, bluebubbles) client triple,
    initializing each on first call."""
    global _graph_client, _claude_client, _bb_client
    if _graph_client is None:
        _graph_client = GraphClient(config)
    if _claude_client is None:
        _claude_client = ClaudeClient(config)
    if _bb_client is None:
        _bb_client = BlueBubblesClient(config)
    return _graph_client, _claude_client, _bb_client


def _reset_clients_for_test() -> None:
    """Test-only: clear the cached singletons so a fresh config + new clients
    take effect on the next `_get_clients` call. Prod code never calls this."""
    global _graph_client, _claude_client, _bb_client
    _graph_client = None
    _claude_client = None
    _bb_client = None


__all__ = ["_get_clients", "_reset_clients_for_test"]
