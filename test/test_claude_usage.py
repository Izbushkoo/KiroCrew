"""This fork's own install-wide Claude spend counter (claude_usage.py).

Covers ``read_usage`` / ``record_cost``: the persisted running total the
``GET /api/usage/claude`` chip reads, and its zero-baseline / corruption /
concurrency-adjacent behavior. See the module docstring for why this is an
ESTIMATE scoped to this install, never a real remaining-quota reading.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import claude_usage


@pytest.fixture(autouse=True)
def _isolated_usage_file(tmp_path, monkeypatch):
    """Every test gets its own file — this is process-wide persisted state,
    and tests must not see each other's writes."""
    monkeypatch.setattr(claude_usage, "config_dir", lambda: tmp_path)


class TestReadUsage:
    def test_zero_baseline_when_never_written(self):
        assert claude_usage.read_usage() == {"total_cost_usd": 0.0, "since": None}

    def test_missing_file_is_the_zero_baseline_not_an_error(self, tmp_path):
        assert not (tmp_path / "claude_usage.json").exists()
        assert claude_usage.read_usage() == {"total_cost_usd": 0.0, "since": None}

    def test_corrupt_json_degrades_to_zero_baseline(self, tmp_path):
        (tmp_path / "claude_usage.json").write_text("{not json", encoding="utf-8")
        assert claude_usage.read_usage() == {"total_cost_usd": 0.0, "since": None}

    def test_non_dict_json_degrades_to_zero_baseline(self, tmp_path):
        (tmp_path / "claude_usage.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert claude_usage.read_usage() == {"total_cost_usd": 0.0, "since": None}

    def test_malformed_fields_degrade_individually(self, tmp_path):
        # total_cost_usd unusable -> 0.0; since unusable -> None. Neither
        # field's corruption should sink the other.
        (tmp_path / "claude_usage.json").write_text(
            json.dumps({"total_cost_usd": "nope", "since": "also nope"}), encoding="utf-8"
        )
        assert claude_usage.read_usage() == {"total_cost_usd": 0.0, "since": None}


class TestRecordCost:
    def test_first_write_sets_since(self):
        claude_usage.record_cost(0.5)
        usage = claude_usage.read_usage()
        assert usage["total_cost_usd"] == 0.5
        assert usage["since"] is not None

    def test_accumulates_across_calls(self):
        claude_usage.record_cost(0.5)
        claude_usage.record_cost(0.25)
        assert claude_usage.read_usage()["total_cost_usd"] == 0.75

    def test_since_does_not_move_on_a_later_write(self):
        claude_usage.record_cost(0.5)
        since_first = claude_usage.read_usage()["since"]
        claude_usage.record_cost(0.25)
        assert claude_usage.read_usage()["since"] == since_first

    @pytest.mark.parametrize("delta", [0.0, -0.5])
    def test_non_positive_delta_is_a_noop(self, delta):
        claude_usage.record_cost(delta)
        assert claude_usage.read_usage() == {"total_cost_usd": 0.0, "since": None}

    def test_rounds_to_six_decimal_places(self):
        claude_usage.record_cost(0.1)
        claude_usage.record_cost(0.2)
        # Floating point: 0.1 + 0.2 != 0.3 exactly. The stored total must be
        # the rounded value, not a value carrying that error forward forever.
        assert claude_usage.read_usage()["total_cost_usd"] == 0.3

    def test_survives_a_fresh_read_between_writes(self):
        # record_cost is read-modify-write against the FILE, not an in-process
        # cache -- so a second call must see the first call's persisted total
        # even with no shared Python state between them (mirrors what a
        # gateway restart between two turns looks like).
        claude_usage.record_cost(1.0)
        # Force a totally fresh read path.
        assert claude_usage.read_usage()["total_cost_usd"] == 1.0
        claude_usage.record_cost(2.0)
        assert claude_usage.read_usage()["total_cost_usd"] == 3.0
