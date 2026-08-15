"""Zero-config Cloudflare tunnel for mobile access to the KiroCrew dashboard.

Manages the lifecycle of a background ``cloudflared`` process that exposes the
local dashboard port via a temporary trycloudflare.com URL. The generated URL
requires no Cloudflare account and is ephemeral — it lives only as long as the
tunnel process.

The binary is auto-downloaded to ``~/.kiro/bin/cloudflared`` on first use so no
system-wide install is required.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import stat
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BIN_DIR = Path.home() / ".kiro" / "bin"
_CLOUDFLARED_BIN = _BIN_DIR / "cloudflared"

#: Base URL for official cloudflared releases.
_DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

#: Regex to capture the generated trycloudflare.com URL from process output.
_URL_PATTERN = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")

#: Timeout for waiting for the tunnel URL after process start.
_URL_WAIT_TIMEOUT_SECS = 30.0

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_tunnel_url: str | None = None
_tunnel_process: asyncio.subprocess.Process | None = None


def _download_filename() -> str:
    """Return the platform-specific cloudflared binary filename."""
    system = sys.platform
    machine = platform.machine().lower()

    if system == "darwin":
        if machine == "arm64":
            return "cloudflared-darwin-arm64.tgz"
        return "cloudflared-darwin-amd64.tgz"
    elif system == "win32":
        return "cloudflared-windows-amd64.exe"
    else:
        # Linux
        if machine in ("aarch64", "arm64"):
            return "cloudflared-linux-arm64"
        return "cloudflared-linux-amd64"


def _download_url() -> str:
    """Return the full download URL for the current platform."""
    return f"{_DOWNLOAD_BASE}/{_download_filename()}"


async def ensure_cloudflared() -> Path:
    """Download the ``cloudflared`` binary if it is not already present.

    Returns the path to the binary. Raises :class:`RuntimeError` on failure.
    """
    if _CLOUDFLARED_BIN.exists():
        return _CLOUDFLARED_BIN

    _BIN_DIR.mkdir(parents=True, exist_ok=True)

    url = _download_url()
    filename = _download_filename()
    logger.info("Downloading cloudflared from %s", url)

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Failed to download cloudflared: HTTP {resp.status}"
                    )
                data = await resp.read()
    except ImportError:
        raise RuntimeError(
            "aiohttp is required to download cloudflared; it should be available "
            "in the kiro_crew environment"
        )

    if filename.endswith(".tgz"):
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "cloudflared" or member.name.endswith("/cloudflared"):
                    f = tar.extractfile(member)
                    if f is None:
                        raise RuntimeError("Could not extract cloudflared from archive")
                    _CLOUDFLARED_BIN.write_bytes(f.read())
                    break
            else:
                raise RuntimeError("cloudflared binary not found in archive")
    else:
        _CLOUDFLARED_BIN.write_bytes(data)

    # Make executable
    _CLOUDFLARED_BIN.chmod(_CLOUDFLARED_BIN.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    logger.info("cloudflared installed at %s", _CLOUDFLARED_BIN)
    return _CLOUDFLARED_BIN


async def start_tunnel(port: int) -> str | None:
    """Launch a cloudflared quick-tunnel pointing at the local dashboard port.

    Reads stdout/stderr to capture the generated trycloudflare.com URL and saves
    it to the module-level ``_tunnel_url``. Returns the URL on success or
    ``None`` if the tunnel failed to produce a URL within the timeout.
    """
    global _tunnel_url, _tunnel_process

    # Tear down any existing tunnel first.
    await stop_tunnel()

    try:
        binary = await ensure_cloudflared()
    except RuntimeError as exc:
        logger.error("Cannot start tunnel: %s", exc)
        return None

    cmd = [
        str(binary),
        "tunnel",
        "--url",
        f"http://127.0.0.1:{port}",
    ]

    logger.info("Starting cloudflared tunnel: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _tunnel_process = proc

    # cloudflared prints the URL to stderr.
    url = await _read_tunnel_url(proc)
    if url:
        _tunnel_url = url
        logger.info("Cloudflare tunnel active: %s", _tunnel_url)
    else:
        logger.warning("Cloudflare tunnel did not produce a URL within timeout")
        await stop_tunnel()

    return _tunnel_url


async def _read_tunnel_url(proc: asyncio.subprocess.Process) -> str | None:
    """Read process stderr until a trycloudflare URL appears or timeout."""

    async def _scan_stream(stream: asyncio.StreamReader | None) -> str | None:
        if stream is None:
            return None
        while True:
            line = await stream.readline()
            if not line:
                return None
            decoded = line.decode("utf-8", errors="replace")
            match = _URL_PATTERN.search(decoded)
            if match:
                return match.group(1)

    try:
        # The URL appears on stderr for quick tunnels.
        result = await asyncio.wait_for(
            _scan_stream(proc.stderr),
            timeout=_URL_WAIT_TIMEOUT_SECS,
        )
        if result:
            return result
        # Fallback: try stdout if stderr did not yield a URL.
        result = await asyncio.wait_for(
            _scan_stream(proc.stdout),
            timeout=5.0,
        )
        return result
    except asyncio.TimeoutError:
        return None


async def stop_tunnel() -> None:
    """Terminate the running cloudflared tunnel process, if any."""
    global _tunnel_url, _tunnel_process

    if _tunnel_process is not None:
        try:
            _tunnel_process.terminate()
            await asyncio.wait_for(_tunnel_process.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                _tunnel_process.kill()
            except ProcessLookupError:
                pass
        _tunnel_process = None

    _tunnel_url = None


def get_tunnel_url() -> str | None:
    """Return the current tunnel URL, or ``None`` if no tunnel is active."""
    return _tunnel_url


async def handle_mobile_sync(request: web.Request) -> web.Response:
    """REST handler returning mobile sync connection info as JSON.

    Response shape::

        {
            "local": {"host": "<local_ip>", "port": <int>, "token": "<str>"},
            "tunnel": {"url": "<cloudflare_url>", "token": "<str>"} | null,
            "token": "<current_auth_token>",
            "tunnel_active": true | false
        }
    """
    from kiro_crew.dashboard.token_auth import generate_token

    port = request.app.get("port", 5476)
    token = generate_token("mobile", ttl_seconds=86400)

    local_info: dict[str, Any] = {
        "host": _get_local_ip(),
        "port": port,
        "token": token,
    }

    tunnel_info: dict[str, Any] | None = None
    if _tunnel_url:
        tunnel_info = {
            "url": _tunnel_url,
            "token": token,
        }

    payload: dict[str, Any] = {
        "local": local_info,
        "tunnel": tunnel_info,
        "token": token,
        "tunnel_active": _tunnel_url is not None,
    }

    return web.json_response(payload)


def _get_local_ip() -> str:
    """Best-effort detection of the machine's LAN IP address."""
    import socket

    try:
        # Connect to a public address to determine the preferred outbound IP.
        # No data is actually sent.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
