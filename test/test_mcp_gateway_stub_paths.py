"""The stub's path resolution must produce ABSOLUTE paths on every platform.

Both the default socket path and the fallback audit log derive from one data
home. That home used to fall back to ``os.environ["HOME"]``, which is normally
unset on Windows (it uses ``USERPROFILE``), so the expression evaluated to
``Path("")`` and every derived path became relative to the stub's cwd.

That is not cosmetic. The Windows pipe name is a hash of the socket path, so a
daemon and a stub started from different working directories would hash to two
different pipe names and never meet -- silently, with a gateway nothing can
reach. The fallback log landing in an arbitrary cwd also quietly destroys the
main signal for "did pooling actually engage".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.stub import (
    _crew_home,
    _default_socket_path,
    _fallback_log_path,
)


def _clear_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce a Windows environment: no KIROCREW_HOME and no HOME."""
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)


def test_kirocrew_home_wins_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert _crew_home() == tmp_path
    assert _default_socket_path() == str(tmp_path / "kirocrew-mcp-gateway.sock")
    assert _fallback_log_path() == tmp_path / "logs" / "stub_fallback.jsonl"


def test_home_is_absolute_without_any_home_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression. With HOME unset the old expression yielded ``Path("")``,
    making every derived path relative to whatever cwd the stub inherited."""
    _clear_home_env(monkeypatch)
    assert _crew_home().is_absolute(), "data home must never be cwd-relative"


def test_socket_default_is_absolute_without_any_home_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load-bearing on Windows: the pipe name is a hash of this path, so a
    cwd-relative value lets two processes derive different pipe names."""
    _clear_home_env(monkeypatch)
    assert Path(_default_socket_path()).is_absolute()


def test_fallback_log_is_absolute_without_any_home_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_home_env(monkeypatch)
    assert _fallback_log_path().is_absolute()


def test_unresolvable_home_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Path.home()`` raises RuntimeError when no home can be resolved at all,
    and neither caller may raise: the socket value is an argparse default (a
    raise there kills the stub before it can degrade to a per-session exec) and
    the log path is used by ``log_fallback``, whose handler catches ``OSError``
    only -- so a RuntimeError would escape the one function documented as
    never allowed to break the exec that keeps kiro-cli working.
    """
    _clear_home_env(monkeypatch)
    monkeypatch.delenv("USERPROFILE", raising=False)

    def _no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))

    # Must not raise, and must still yield a usable path.
    assert _crew_home().parts, "a degraded home must still be a usable path"
    assert _default_socket_path().endswith("kirocrew-mcp-gateway.sock")
    assert _fallback_log_path().name == "stub_fallback.jsonl"


# --- fallback log rotation + aggregation (issue #3495) ----------------------


def _fallback_args():
    import argparse

    return argparse.Namespace(
        server="srv-a",
        agent="agent-a",
        channel_id="",
        target_command="/usr/bin/srv-a",
    )


def test_log_fallback_rotates_at_the_size_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The log rotated at the cap keeps ONE previous generation, bounding disk
    use — it previously grew without limit (467 KB in 15 h on a degraded
    host)."""
    from kiro_crew.mcp_gateway import stub

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    log = stub._fallback_log_path()
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * stub._FALLBACK_LOG_MAX_BYTES)

    stub.log_fallback("reason-x", "uuid-1", "agent-a:srv-a", _fallback_args())

    rotated = log.with_suffix(".jsonl.1")
    assert rotated.exists(), "previous generation not kept"
    assert rotated.stat().st_size == stub._FALLBACK_LOG_MAX_BYTES
    # The fresh live log holds exactly the new record.
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json

    assert json.loads(lines[0])["reason"] == "reason-x"


def test_fallback_counts_aggregates_live_and_rotated_within_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reader the log never had: per-server counts over a window, spanning
    the rotated generation, ignoring aged-out and torn records."""
    import json
    import time as _time

    from kiro_crew.mcp_gateway import stub

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    log = stub._fallback_log_path()
    log.parent.mkdir(parents=True)
    now = _time.time()

    def _rec(server: str, ts: float, reason: str = "spawn failed") -> str:
        return json.dumps({"ts": ts, "server": server, "reason": reason})

    log.with_suffix(".jsonl.1").write_text(
        _rec("srv-a", now - 60) + "\n" + _rec("srv-a", now - 90) + "\n",
        encoding="utf-8",
    )
    log.write_text(
        _rec("srv-b", now - 30, reason="breaker OPEN") + "\n"
        + _rec("srv-a", now - (25 * 3600)) + "\n"  # aged out of the window
        + '{"torn json\n',  # racing writer's partial line
        encoding="utf-8",
    )

    counts = stub.fallback_counts(window_secs=24 * 3600.0)

    assert counts["total"] == 3
    assert counts["by_server"] == {"srv-a": 2, "srv-b": 1}
    assert counts["by_reason"] == {"spawn failed": 2, "breaker OPEN": 1}


def test_fallback_counts_survives_a_missing_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.mcp_gateway import stub

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    counts = stub.fallback_counts()
    assert counts["total"] == 0
    assert counts["by_server"] == {}


def test_gatewayd_fallback_cache_expires_on_time_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a time bucket in the cache key, records aging out of the 24 h
    window kept being counted forever when nothing appended (the file stat
    never changes). The bucket bounds that staleness."""
    import json
    import time as _time

    from kiro_crew.mcp_gateway import gatewayd, stub

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    log = stub._fallback_log_path()
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"ts": _time.time(), "server": "srv-a", "reason": "x"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gatewayd, "_fallback_stats_cache", None)

    calls = {"n": 0}
    real = gatewayd.stub_fallback_counts

    def _spy(*a: object, **k: object) -> dict:
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(gatewayd, "stub_fallback_counts", _spy)

    first = gatewayd._fallback_counts_cached()
    assert first["total"] == 1 and calls["n"] == 1
    # Same bucket, unchanged file: served from cache.
    gatewayd._fallback_counts_cached()
    assert calls["n"] == 1
    # Bucket rolls over: recomputed even though the file never changed.
    base = _time.time()
    monkeypatch.setattr(
        gatewayd.time, "time",
        lambda: base + gatewayd._FALLBACK_CACHE_BUCKET_SECS + 1,
    )
    gatewayd._fallback_counts_cached()
    assert calls["n"] == 2, "bucket rollover must invalidate the cache"
