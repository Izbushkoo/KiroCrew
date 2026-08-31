"""This fork's own record of Claude backend spend + last-seen rate-limit signal.

Two facts, from two different sources, that the dashboard's account chip needs
when ``agent.acp_backend`` is Claude, neither of which Kiro's usage API can
answer:

* **Accumulated cost** (this module, persisted): the running sum of every
  completed turn's Claude-reported cost (``claude_code``'s own
  session-cumulative ``usage_update.cost.amount``, delta'd per turn — see
  ``chat_runner._attach_turn_stats``). It is an Anthropic-computed ESTIMATE,
  not necessarily an actual dollar charge on a Pro/Max/Team plan, and it is
  scoped to what THIS install has observed since the counter started — not
  the account's full history, which only Anthropic's own console/site has.
* **Rate-limit signal** (``acp.client``, in-memory, not persisted here): the
  claude-agent-acp adapter forwards a ``rate_limit_event`` (when the SDK
  emits one — sparse by design, mostly silent until a warning/reject
  threshold) as ``_meta["_claude/rateLimit"]`` on a ``usage_update``
  notification. That is genuinely real, Anthropic-sourced quota data, unlike
  the cost estimate here — see ``acp.client.get_last_claude_rate_limit``.

Persisted (not in-memory-only) because a KiroCrew install commonly restarts
across a day, and a counter that reset on every gateway restart would
understate spend far more than an unpersisted rate-limit signal does (that
one is inherently transient — the adapter re-reports it on the next
threshold-crossing turn regardless).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, TypedDict

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_USAGE_LEAF = "claude_usage.json"


class ClaudeUsage(TypedDict):
    total_cost_usd: float
    since: float | None


def _usage_path() -> Path:
    """Deferred, like ``admission._policy_default_path`` — never capture
    ``config_dir()`` at import time (it can trigger a one-time data-home
    migration, which must only happen at the CLI's chosen point)."""
    return config_dir() / _USAGE_LEAF


def read_usage() -> ClaudeUsage:
    """Return the persisted totals, or the zero baseline if never written.

    Never raises: a corrupt or unreadable file degrades to the zero baseline
    rather than failing whatever dashboard request asked for it — this is
    informational telemetry, not something callers should have to guard.
    """
    try:
        raw = json.loads(_usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"total_cost_usd": 0.0, "since": None}
    if not isinstance(raw, dict):
        return {"total_cost_usd": 0.0, "since": None}
    total = raw.get("total_cost_usd")
    since = raw.get("since")
    return {
        "total_cost_usd": float(total) if isinstance(total, (int, float)) else 0.0,
        "since": float(since) if isinstance(since, (int, float)) else None,
    }


def record_cost(delta_usd: float) -> None:
    """Add *delta_usd* to the running total. BLOCKING — call under ``asyncio.to_thread``.

    Read-modify-write, not append-only: this is a single small counter file,
    not a log, so there is nothing to replay and no benefit to durability
    beyond "the last write wins." Sets ``since`` on the first-ever write so
    the dashboard can say what window the total covers.
    """
    if delta_usd <= 0:
        return
    current = read_usage()
    updated: dict[str, Any] = {
        "total_cost_usd": round(current["total_cost_usd"] + delta_usd, 6),
        "since": current["since"] if current["since"] is not None else time.time(),
    }
    try:
        _usage_path().write_text(json.dumps(updated), encoding="utf-8")
    except OSError:
        logger.warning("Could not persist Claude usage total", exc_info=True)
