"""Zero-config Cloudflare tunnel for mobile access to the KiroCrew dashboard.

Manages the lifecycle of a background ``cloudflared`` process that exposes the
local dashboard port via a temporary trycloudflare.com URL. The generated URL
requires no Cloudflare account and is ephemeral — it lives only as long as the
tunnel process.

Supports persistent named tunnels: when a Cloudflare Tunnel token file
(``~/.kiro/crew/cloudflare_tunnel_token``) is present, ``cloudflared`` runs
as a named tunnel instead of a quick-tunnel. An optional custom domain file
(``~/.kiro/crew/cloudflare_tunnel_domain``) provides the fixed URL returned
by the sync endpoint.

The binary is auto-downloaded to ``~/.kiro/bin/cloudflared`` on first use so no
system-wide install is required.
"""

from __future__ import annotations

import asyncio
import logging
import os
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

#: TTL for the persistent mobile auth token (1 year).
_PERSISTENT_TOKEN_TTL_SECS = 365 * 24 * 3600

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_tunnel_url: str | None = None
_tunnel_process: asyncio.subprocess.Process | None = None


def _data_home() -> Path:
    """Return the Kiro Crew data home directory."""
    from kiro_crew.config.loader import config_dir

    return config_dir()


def _tunnel_token_path() -> Path:
    """Path to the persistent Cloudflare Tunnel token file."""
    return _data_home() / "cloudflare_tunnel_token"


def _tunnel_domain_path() -> Path:
    """Path to the persistent custom domain file."""
    return _data_home() / "cloudflare_tunnel_domain"


def _persistent_mobile_token_path() -> Path:
    """Path to the persistent mobile auth token file."""
    return _data_home() / "mobile_auth_token"


def _has_persistent_tunnel_token() -> bool:
    """Return True if a persistent Cloudflare Tunnel token file exists."""
    return _tunnel_token_path().is_file()


def _read_persistent_tunnel_token() -> str | None:
    """Read the Cloudflare Tunnel token from disk, or None if absent."""
    path = _tunnel_token_path()
    if not path.is_file():
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
        return token if token else None
    except OSError:
        logger.warning("Could not read tunnel token from %s", path)
        return None


def _read_custom_domain() -> str | None:
    """Read the custom tunnel domain from disk, or None if absent."""
    path = _tunnel_domain_path()
    if not path.is_file():
        return None
    try:
        domain = path.read_text(encoding="utf-8").strip()
        return domain if domain else None
    except OSError:
        logger.warning("Could not read custom domain from %s", path)
        return None


def _get_or_create_persistent_mobile_token() -> str:
    """Return a persistent 1-year mobile auth token, creating one if needed.

    The token is saved to disk so it survives Gateway restarts. If the stored
    token has expired or is invalid, a fresh one is generated and persisted.
    """
    from kiro_crew.dashboard.token_auth import generate_token, validate_token

    path = _persistent_mobile_token_path()

    # Try to load an existing token
    if path.is_file():
        try:
            stored = path.read_text(encoding="utf-8").strip()
            if stored:
                valid, _uid, _reason = validate_token(stored, use_session_exp=True)
                if valid:
                    return stored
                logger.info("Persistent mobile token expired or invalid, regenerating")
        except OSError:
            logger.warning("Could not read persistent mobile token from %s", path)

    # Generate a fresh 1-year token
    token = generate_token("mobile", ttl_seconds=_PERSISTENT_TOKEN_TTL_SECS)

    # Persist to disk
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        os.chmod(path, 0o600)
        logger.info("Persistent mobile auth token saved to %s", path)
    except OSError:
        logger.warning("Could not persist mobile auth token to %s", path)

    return token


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
    """Launch a cloudflared tunnel pointing at the local dashboard port.

    If a persistent tunnel token file exists at
    ``~/.kiro/crew/cloudflare_tunnel_token``, runs ``cloudflared tunnel run``
    with that token (a named tunnel). Otherwise falls back to a quick-tunnel
    (trycloudflare.com). When a custom domain file exists at
    ``~/.kiro/crew/cloudflare_tunnel_domain``, its content is used as the
    tunnel URL instead of parsing stdout.

    Returns the URL on success or ``None`` on failure.
    """
    global _tunnel_url, _tunnel_process

    # Tear down any existing tunnel first.
    await stop_tunnel()

    try:
        binary = await ensure_cloudflared()
    except RuntimeError as exc:
        logger.error("Cannot start tunnel: %s", exc)
        return None

    tunnel_token = _read_persistent_tunnel_token()
    custom_domain = _read_custom_domain()

    if tunnel_token:
        # Named tunnel mode: use the persistent token.
        cmd = [
            str(binary),
            "tunnel",
            "--no-autoupdate",
            "run",
            "--token",
            tunnel_token,
        ]
        logger.info("Starting cloudflared named tunnel (persistent token)")
    else:
        # Quick-tunnel mode (ephemeral trycloudflare.com URL).
        cmd = [
            str(binary),
            "tunnel",
            "--url",
            f"http://127.0.0.1:{port}",
        ]
        logger.info("Starting cloudflared quick-tunnel: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _tunnel_process = proc

    if tunnel_token and custom_domain:
        # For named tunnels with a custom domain, the URL is known in advance.
        _tunnel_url = custom_domain if custom_domain.startswith("https://") else f"https://{custom_domain}"
        logger.info("Cloudflare named tunnel active with custom domain: %s", _tunnel_url)
    else:
        # Parse the URL from cloudflared output (quick-tunnel or named tunnel
        # without a custom domain file).
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
            "tunnel_active": true | false,
            "persistent_tunnel": true | false
        }

    When a persistent mobile auth token is configured (stored at
    ``~/.kiro/crew/mobile_auth_token``), it is reused across restarts.
    ``persistent_tunnel`` indicates whether a persistent Cloudflare Tunnel
    token is configured.
    """
    token = _get_or_create_persistent_mobile_token()

    port = request.app.get("port", 5476)

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
        "persistent_tunnel": _has_persistent_tunnel_token(),
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
