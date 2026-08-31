"""``GET /api/acp-backends`` -- per-backend selectability and machine readiness.

One row per backend id in ``acp_backends.ACP_BACKENDS_KNOWN``, sorted by
``policy_id``, including ids this build cannot serve: the dashboard's backend
switch lists all of them and must be able to say which is which, so an
unservable backend needs a row rather than silent absence.

Two facts per row, from two owners that must not be conflated:

* ``selectable`` -- build capability AND deployment policy, read from
  ``handlers.core._selectable_acp_backends()``. That helper is already the
  single derivation feeding the PATCH allowlist and ``/api/config/schema``, and
  it already applies the ``agent_backend`` governance scope. Re-deriving it here
  from ``acp_backends.selectable_backend_values()`` would restore exactly the
  drift that a literal list in three places once caused: the wire would accept
  a value this endpoint calls unselectable, or the reverse.
* ``installed`` -- whether the harness is on THIS machine, from
  :mod:`kiro_crew.agent_sdk`, which asks through the spawn's own resolvers.
  Reached through the SDK rather than the ACP layer directly: this handler is
  application code, and ``scripts/check_agent_sdk_boundary.py`` is what keeps
  that true.

Owner-only, and the snapshot is offloaded: the Claude probe shells out to mise
and walks the filesystem, and resolving the governance ceiling loads config, so
neither may run on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any, Dict, List

from aiohttp import web

from kiro_crew.dashboard.handlers.kiro_prerequisite import _is_dashboard_owner
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Machine-readable error code, per the dashboard error-code contract
#: (``test/test_error_code_contract.py``): the client branches on this, and the
#: English prose beside it is advisory. Spelled to match the other owner-gated
#: dashboard handlers rather than inventing a per-endpoint code.
_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_OWNER_REQUIRED_MESSAGE = "dashboard owner required"

_AUDIT_OPERATION = "acp_backend_status_access"
_INSTALL_AUDIT_OPERATION = "acp_backend_install"

_CODE_UNSUPPORTED_BACKEND = "acp_backend_install_unsupported"
_CODE_NPM_NOT_FOUND = "acp_backend_install_npm_not_found"
_CODE_INSTALL_FAILED = "acp_backend_install_failed"
_CODE_INSTALL_TIMEOUT = "acp_backend_install_timeout"

#: Generous but bounded: npm resolves and downloads a package tree over the
#: network, which can be slow on a cold cache, but a hung install must not pin
#: the request open forever.
_INSTALL_TIMEOUT_SECS = 120


async def _deny_non_owner(
    request: web.Request, *, operation: str = _AUDIT_OPERATION
) -> web.Response | None:
    """Refuse a non-owner, mirroring the prerequisite handlers' 403 shape.

    Which components are installed on the host is host-configuration state and
    belongs to the same audience as the first-run setup surface it complements,
    so it reuses that module's owner predicate rather than a second one.
    """
    if _is_dashboard_owner(request):
        return None

    caller = str(request.get("user") or "")
    audit_caller = str(request.get("app") or caller or "unknown")

    def _audit() -> None:
        sel().log_api_access(
            caller=audit_caller,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error=_OWNER_REQUIRED_MESSAGE,
        )

    try:
        await asyncio.to_thread(_audit)
    except Exception:
        # An unwritable audit log must not convert a denial into a 500 -- the
        # refusal is the security-relevant half and still has to land.
        logger.debug("Could not audit denied ACP backend status access", exc_info=True)
    return web.json_response(
        {"error": _OWNER_REQUIRED_MESSAGE, "code": _CODE_OWNER_REQUIRED},
        status=403,
    )


def _snapshot() -> List[Dict[str, Any]]:
    """Build the rows. BLOCKING -- run under ``asyncio.to_thread``.

    ``_selectable_acp_backends`` is imported here rather than at module scope:
    ``handlers.core`` is a large sibling in the same package, and a
    module-scope import from a module the package ``__init__`` also imports is
    how a cycle gets introduced later.
    """
    from kiro_crew.agent_sdk import INSTALLED, MISSING, probe_backends
    from kiro_crew.dashboard.handlers.core import _selectable_acp_backends

    selectable = set(_selectable_acp_backends())
    rows: List[Dict[str, Any]] = []
    for state in probe_backends():
        rows.append(
            {
                "id": state.backend,
                "policy_id": state.policy_id,
                "selectable": state.backend in selectable,
                "installed": state.installed,
                # Enforced here, not just by the probes: the contract makes this
                # non-empty ONLY for a MISSING verdict, so an UNKNOWN row can
                # never name a component the check never confirmed was absent.
                "missing_components": (
                    list(state.missing_components) if state.installed == MISSING else []
                ),
                "install_command": state.install_command,
                # Clamped to a MISSING-free verdict for the same reason as
                # ``missing_components`` above: "installed but the running gateway
                # cannot use it yet" is only meaningful once the components are
                # actually there. A MISSING or UNKNOWN row carries False.
                "restart_required": (
                    bool(state.restart_required) if state.installed == INSTALLED else False
                ),
            }
        )
    return rows


async def api_acp_backend_status(request: web.Request) -> web.Response:
    """GET /api/acp-backends -- selectability + install state for every backend."""
    denial = await _deny_non_owner(request)
    if denial is not None:
        return denial
    backends = await asyncio.to_thread(_snapshot)
    return web.json_response({"backends": backends})


def _resolve_npm_bin() -> str | None:
    """Find ``npm`` the same way the spawn resolvers find their binaries.

    BLOCKING (filesystem/PATH search) -- call under ``asyncio.to_thread``. A
    gateway running under systemd/launchd inherits a minimal ``PATH`` that
    routinely omits nvm/mise/volta shims, so a bare ``shutil.which("npm")``
    against the unmodified environment would report "not found" for an
    operator who plainly has npm on an interactive shell.
    """
    from kiro_crew.env import augmented_path

    search_path = augmented_path(os.environ.get("PATH", ""))
    return shutil.which("npm", path=search_path)


async def api_acp_backend_install(request: web.Request) -> web.Response:
    """POST /api/acp-backends/install -- ``body: {"backend": "claude"}``.

    The ONLY action this endpoint may ever automate: a fixed, named npm
    package (``CLAUDE_ACP_NPM_PKG``) resolved from this module's own import,
    never a client-supplied command -- the request selects among a closed set
    of known backends, it does not name what runs. Installing the ``claude``
    CLI itself, or logging it in, stays out of scope on purpose: see
    ``kiro_prerequisite.py``'s "Kiro Crew neither installs nor authenticates
    on the user's behalf" stance for kiro-cli, which applies here for the
    identical reason -- a vendor CLI's installer and login flow is a
    privileged surface this project has already decided not to own.
    """
    denial = await _deny_non_owner(request, operation=_INSTALL_AUDIT_OPERATION)
    if denial is not None:
        return denial

    try:
        body = await request.json()
    except Exception:
        body = {}
    backend = body.get("backend") if isinstance(body, dict) else None

    # Function-local: this module must not import kiro_crew.acp at module
    # scope (it sits on the boot path -- see
    # test_the_boot_path_does_not_import_acp_at_module_scope).
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE

    if backend == ACP_BACKEND_CLAUDE:
        return await _install_claude_adapter()
    return web.json_response(
        {
            "error": f"no automated install for backend {backend!r}",
            "code": _CODE_UNSUPPORTED_BACKEND,
        },
        status=400,
    )


async def _install_claude_adapter() -> web.Response:
    """``npm install --global <CLAUDE_ACP_NPM_PKG>`` -- the one command this
    endpoint ever runs, named by import rather than taken from the request."""
    from kiro_crew.acp.client import CLAUDE_ACP_NPM_PKG

    npm_bin = await asyncio.to_thread(_resolve_npm_bin)
    if not npm_bin:
        return web.json_response(
            {
                "error": "npm was not found on this machine; install Node.js/npm first",
                "code": _CODE_NPM_NOT_FOUND,
            },
            status=500,
        )

    proc = await asyncio.create_subprocess_exec(
        npm_bin,
        "install",
        "--global",
        CLAUDE_ACP_NPM_PKG,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=_INSTALL_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        logger.warning("npm install -g %s timed out", CLAUDE_ACP_NPM_PKG)
        return web.json_response(
            {"error": "npm install timed out", "code": _CODE_INSTALL_TIMEOUT},
            status=500,
        )

    if proc.returncode != 0:
        detail = (err or b"").decode(errors="replace").strip()
        logger.warning(
            "npm install -g %s failed (rc=%s): %s", CLAUDE_ACP_NPM_PKG, proc.returncode, detail
        )
        return web.json_response(
            {"error": detail or "npm install failed", "code": _CODE_INSTALL_FAILED},
            status=500,
        )

    # The running gateway's own spawn-resolution cache and this endpoint's
    # 30s probe cache both predate the install; clear the probe cache so the
    # NEXT GET /api/acp-backends reflects it immediately rather than up to 30s
    # stale. The spawn-resolution cache (AcpClient._claude_acp_argv_cache) is
    # per-process and is exactly what the ``restart_required`` row already
    # discloses -- clearing it here would be a second, racier path to the same
    # fact the payload already states honestly.
    from kiro_crew.agent_sdk.backend_install import clear_probe_cache

    clear_probe_cache()

    return web.json_response({"ok": True})
