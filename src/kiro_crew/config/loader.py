"""Configuration loader for KiroCrew.

Config location: ~/.kiro/crew/config.json (overridden by KIROCREW_HOME)
Credentials:    ~/.kiro/crew/.env (overridden by KIROCREW_HOME)

KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving the
kiro-cli backend. This module handles session timeouts, hook rules, and the
dashboard URL via the config file. (The dashboard *port* is set with the
``KIROCREW_PORT`` env var, not a config key.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math  # noqa: F401 - historical loader namespace compatibility
import os
import re as _re
import shutil
import stat as _stat
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit as _urlsplit  # noqa: F401 - compatibility facade

from kiro_crew import __version__, model_registry, platform_compat, windows_acl
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE

# Leaf module (stdlib + platform_compat only) — no import cycle with config.
from kiro_crew.atomic_write import atomic_write, on_event_loop

# Computer-use defaults/ceilings come from the feature's constants module rather
# than being re-spelled here (AGENTS.md: no hardcoded values in business logic).
# ``computer_use.types`` is deliberately dependency-free — it imports nothing from
# ``kiro_crew`` — so this cannot create an import cycle with the loader, and the
# ``computer_use`` package's ``__init__`` pulls in only ``platform_compat`` /
# ``executors`` (both stdlib-only), never ``config``.
from kiro_crew.computer_use.types import DEFAULT_ATTACH_SCREENSHOT as _CU_DEFAULT_ATTACH_SCREENSHOT
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_DEPTH as _CU_DEFAULT_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_NODES as _CU_DEFAULT_MAX_TREE_NODES
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY as _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
)
from kiro_crew.computer_use.types import DEFAULT_SCREENSHOT_MAX_PX as _CU_DEFAULT_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import DEFAULT_TEXT_LIMIT as _CU_DEFAULT_TEXT_LIMIT
from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX as _CU_MAX_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import MAX_TEXT_LIMIT as _CU_MAX_TEXT_LIMIT
from kiro_crew.computer_use.types import MAX_TREE_DEPTH_LIMIT as _CU_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import MAX_TREE_NODES_LIMIT as _CU_MAX_TREE_NODES
from kiro_crew.computer_use.types import MIN_SCREENSHOT_MAX_PX as _CU_MIN_SCREENSHOT_MAX_PX

# Post-split section internals are reached through the module: the name-level
# `from kiro_crew.config.sections import (...)` block below is a FROZEN
# pre-split snapshot (test_config_module_boundaries pins it), so a coercer added
# after the split must not join it.
from kiro_crew.config import sections as _sections

# Pure path primitives live in the leaf module ``config.paths`` (stdlib-only,
# no ``kiro_crew`` imports) so the modules that only need ``config_dir()`` can
# import them from there without transitively pulling in the full loader (DTOs,
# schema validation, the process-global cache, and the provider factory).
# Re-exported here for backward compatibility — existing callers keep importing
# these from ``kiro_crew.config.loader``.
#
# The *dir-derived* helpers (config_path, workspace_root, workspace_dir_for, …)
# stay defined below in this module, not in the leaf, so their ``config_dir()``
# calls resolve in this namespace and remain redirectable via
# ``patch("kiro_crew.config.loader.config_dir", ...)`` (used across the suite).
from kiro_crew.config.paths import (  # noqa: F401, kiro_agents_dir
    _WORKSPACE_DIR_NAME,
    CONFIG_DIR_NAME,
    OUTBOX_DIR_NAME,
    _default_workspace_base,
    _safe_dir_name,
    config_dir,
    config_package_dir,
    data_home,
    ensure_data_home,
    kiro_agents_dir,
)
from kiro_crew.config.resolution import (  # noqa: F401
    _KNOWN_CONFIG_SECTIONS,
    _OBSERVED_DEGRADED_SECTIONS,
    CONFIG_RESERVED_TOP_KEYS,
    DEGRADED_TAILSCALE,
    DEGRADED_WHOLE_CONFIG,
    _coerced_section,
    _deep_merge,
    _fail_closed_project_skills_config,
    _mark_file_degraded,
    _subtract_overlay,
    degraded_config_files,
    reset_degraded_observations,
    tailnet_effective_allowed_logins,
    tailnet_identity_unknown,
)

# Section DTOs and their field-level coercion live in a one-way sibling module.
# Re-export every historical loader name so existing imports keep working while
# KiroCrewConfig remains the compatibility facade and owns read/merge/save.
from kiro_crew.config.sections import (  # noqa: F401
    _BOT_NAME_MAX,
    _BOT_NAME_RE,
    _COLOR_HEX_RE,
    _CONNECT_TIMEOUT_CEILING,
    _DEFAULT_BEACON_ENDPOINT,
    _DEFAULT_CHAT_TURN_TIMEOUT_SECS,
    _GITLAB_HOST_NAME_RE,
    _MANAGED_SERVICE_ENV,
    _MAX_RECOVERY_CEILING,
    _MINT_TIMEOUT_CEILING,
    _MINT_TIMEOUT_FLOOR,
    _RECOVER_BACKOFF_CEILING,
    _RETIRED_STT_PROVIDERS,
    _STT_CATALOG,
    _VALID_ACTIVATIONS,
    _VALID_CHANNEL_PREFIXES,
    _VALID_COMPLETION_KEEP,
    _VALID_JAIL_MODES,
    _VALID_STT_MODELS,
    _VALID_STT_PROVIDERS,
    _WARM_SET_CAP_AUTO,
    _WARNED_RESOURCE_LIMIT_KEYS,
    _WARNED_STT_PROVIDERS,
    _WHATSAPP_GROUP_COOLDOWN_DEFAULT,
    _WHATSAPP_GROUP_MODES,
    _YOLO_DURATION_DEFAULT,
    _YOLO_DURATION_SECS,
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    APPROVAL_TURN_MARGIN_SECS,
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    BACKGROUND_WORKER_AGENTS,
    CHAT_ENTRY_CACHE_BYTES_DEFAULT,
    CHAT_ENTRY_CACHE_BYTES_MAX,
    CHAT_ENTRY_CACHE_BYTES_MIN,
    CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
    CHAT_ENTRY_CACHE_ENTRIES_MAX,
    CHAT_ENTRY_CACHE_ENTRIES_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    COMPLETION_KEEP_CHARS_MAX,
    COMPLETION_KEEP_CHARS_MIN,
    CONTEXT_WARN_MARGIN_PCT,
    DEDUP_EVERY_N_SWEEPS_MAX,
    DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
    DEFAULT_AUTOCOMPACT_PCT,
    DEFAULT_CWD_ALLOWED_ROOTS,
    DEFAULT_MAX_PARALLEL_STEPS,
    DEFAULT_MODEL,
    DEFAULT_POOL_SIZE,
    DEFAULT_SESSION_TIMEOUT,
    EFFORT_LEVELS,
    EMBED_RATE_LIMIT_MAX,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
    FORWARD_DECLARED_ENV_DEFAULT,
    IMESSAGE_SERVICES,
    JAIL_MODE_AUTO,
    JAIL_MODE_OFF,
    JAIL_MODE_ON,
    LOOP_STALL_EXIT_AFTER_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT,
    LOOP_STALL_EXIT_AFTER_MAX,
    LOOP_STALL_EXIT_AFTER_MIN,
    MAX_SUBAGENTS_FIXED_FLOOR,
    MCP_PROBE_TIMEOUT_MAX,
    MCP_PROBE_TIMEOUT_MIN,
    POOL_SIZE_MAX,
    POOL_TTL_SECS_MAX,
    POOL_TTL_SECS_MIN,
    RECENT_TINT_COUNT_MAX,
    RECENT_TINT_COUNT_MIN,
    ROLE_MODEL_KEYS,
    SESSION_FOLDER_NAME_MAX,
    SESSION_START_TIMEOUT_MAX,
    SESSION_START_TIMEOUT_MIN,
    SESSION_TIMEOUT_MAX,
    SESSION_TIMEOUT_MIN,
    SOFT_STOP_BUDGET_MAX,
    SOFT_STOP_BUDGET_MIN,
    STT_PROVIDER_LOCAL,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    SWEEP_CHUNK_BUDGET_MAX,
    TELEGRAM_ACTIVATIONS,
    THRESHOLD_PCT_MAX,
    THRESHOLD_PCT_MIN,
    TOOL_APPROVAL_TIMEOUT_MAX,
    TOOL_APPROVAL_TIMEOUT_MIN,
    YOLO_UNTIL_SHUTDOWN,
    AgentConfig,
    ChannelConfig,
    ComputerUseConfig,
    CronHistoryConfig,
    DashboardConfig,
    DiscordConfig,
    ExternalRegistryConfig,
    FeishuConfig,
    HeartbeatConfig,
    IMessageConfig,
    InstancesConfig,
    JiraAuthEntry,
    KiroCrewAgentConfig,
    KnowledgeConfig,
    McpConfig,
    McpGatewayConfig,
    MemoryConfig,
    MemoryStoreConfig,
    MessagingConfig,
    OrchestratorConfig,
    PublishConfig,
    ResolvedBindings,
    ResourceLimitsConfig,
    SessionConfig,
    SessionSummaryConfig,
    SkillsConfig,
    SlackConfig,
    SttConfig,
    TailscaleConfig,
    TaskRunnerConfig,
    TeamsConfig,
    TelegramAccountConfig,
    TelegramConfig,
    TelemetryConfig,
    TunnelConfig,
    WakaTimeConfig,
    WatchdogConfig,
    WebexConfig,
    WeComConfig,
    WeixinConfig,
    WhatsAppConfig,
    WorkspaceConfig,
    _archive_retention_days,
    _clamp_pct,
    _coerce_embedding_provider,
    _coerce_gitlab_hosts,
    _coerce_int,
    _coerce_int_ids,
    _coerce_jira_hosts,
    _coerce_opaque_str_ids,
    _coerce_session_folder,
    _coerce_str_ids,
    _coerce_whatsapp_groups,
    _limit_int,
    _meta,
    _migrate_workspaces,
    _normalize_acp_backend,
    _normalize_jail,
    _normalize_threshold_pair,
    _normalize_yolo_duration,
    _parse_telegram_accounts,
    _port_or_unset,
    _read_auto_add_documents,
    _read_skip_permissions,
    _resolve_stt_model,
    _resolve_stub_overrides,
    _resolve_stub_roster,
    _resolve_stub_servers,
    _safe_bool,
    _safe_color,
    _safe_dict,
    _safe_float,
    _safe_int,
    _safe_list,
    _safe_nonnegative_int,
    _sanitize_bot_name,
    _tailscale_config_from,
    _threshold_pct,
    _validate_activation,
    _validate_telegram_activation,
    _validate_tracking_channels,
    _validated_completion_keep,
    _validated_stt_model,
    _validated_stt_provider,
    coerce_effort,
    coerce_fallback_model,
    coerce_role_efforts,
    coerce_role_models,
    normalize_agent_model,
    resolve_memory_store_config,
    resolve_selected_backend,
    yolo_duration_to_secs,
)

# Superseded-default reporting (#5244). Leaf module: stdlib only, so importing it
# here creates no cycle.
from kiro_crew.config.superseded_defaults import drift_summary, superseded_default_drift

# Schema validation + the validated-data cache live in ``config.validation``.
# Re-exported here for backward compatibility — callers and tests still
# reference these as ``kiro_crew.config.loader.X`` (e.g. the cache tests patch
# ``kiro_crew.config.loader._validate_config_data``). ``validate_config_data``
# is aliased to the historical private name ``_validate_config_data``. The cache
# fingerprint (``_config_fingerprint``) deliberately stays in this module — see
# its definition below.
from kiro_crew.config.validation import (  # noqa: F401
    _CONFIG_CACHE,
    _CONFIG_CACHE_LOCK,
    _HAS_JSONSCHEMA,
    _actual_type_name,
    _apply_field_default,
    _dot_path_from_json_path,
    _get_help_text,
    _is_deprecated_path,
    _is_sensitive_path,
    _lookup_schema_node,
    _mask_value,
)
from kiro_crew.config.validation import validate_config_data as _validate_config_data  # noqa: F401
from kiro_crew.effort import is_valid_effort, model_supports_effort
from kiro_crew.instances.constants import DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_MINT_TIMEOUT_SECS as _DEFAULT_MINT_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _DEFAULT_PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_RECOVER_BACKOFF_MAX_SECS as _DEFAULT_BACKOFF_MAX
from kiro_crew.instances.constants import DEFAULT_SSH_COMPRESSION as _DEFAULT_SSH_COMPRESSION
from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT as _DEFAULT_TUNNEL_BASE_PORT
from kiro_crew.instances.constants import DEFAULT_WARM_SET_CAP as _DEFAULT_WARM_SET_CAP
from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path

# The speech-to-text defaults and the model catalog come from the package that
# owns them, so the model menu this schema advertises cannot name a model that
# cannot be downloaded, and a tuning knob cannot document a default the session
# does not use. No cycle: the only config dependency anywhere under
# ``kiro_crew.stt`` is the leaf ``config.paths``, never this module.
from kiro_crew.stt.limits import DEFAULT_IDLE_EVICT_SECS as _STT_DEFAULT_IDLE_EVICT_SECS
from kiro_crew.stt.limits import DEFAULT_PARTIAL_INTERVAL_MS as _STT_DEFAULT_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import DEFAULT_SILENCE_MS as _STT_DEFAULT_SILENCE_MS
from kiro_crew.stt.limits import DEFAULT_TIMEOUT_SECS as _STT_DEFAULT_TIMEOUT_SECS
from kiro_crew.stt.limits import MAX_IDLE_EVICT_SECS as _STT_IDLE_EVICT_SECS_MAX
from kiro_crew.stt.limits import MAX_INTERVAL_MS as _STT_INTERVAL_MS_MAX
from kiro_crew.stt.limits import MAX_TIMEOUT_SECS as _STT_MAX_TIMEOUT_SECS
from kiro_crew.stt.limits import MIN_IDLE_EVICT_SECS as _STT_IDLE_EVICT_SECS_MIN
from kiro_crew.stt.limits import MIN_PARTIAL_INTERVAL_MS as _STT_MIN_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import MIN_SILENCE_MS as _STT_MIN_SILENCE_MS
from kiro_crew.stt.limits import MIN_TIMEOUT_SECS as _STT_MIN_TIMEOUT_SECS
from kiro_crew.stt.models import DEFAULT_MODEL as _STT_DEFAULT_MODEL

logger = logging.getLogger(__name__)

# Credential keys loaded from .env / environment
CRED_SLACK_APP_TOKEN = "SLACK_APP_TOKEN"
CRED_SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
CRED_OWNER_ID = "KIROCREW_OWNER_ID"
CRED_WECOM_BOT_ID = "WECOM_BOT_ID"
CRED_WECOM_SECRET = "WECOM_SECRET"
CRED_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
CRED_DISCORD_BOT_TOKEN = "DISCORD_BOT_TOKEN"
CRED_WEBEX_BOT_TOKEN = "WEBEX_BOT_TOKEN"
CRED_MICROSOFT_APP_ID = "MICROSOFT_APP_ID"
CRED_MICROSOFT_APP_PASSWORD = "MICROSOFT_APP_PASSWORD"
CRED_MICROSOFT_APP_TENANT_ID = "MICROSOFT_APP_TENANT_ID"
CRED_WEIXIN_TOKEN = "WEIXIN_TOKEN"  # iLink bot credential from the Settings QR flow
CRED_FEISHU_APP_ID = "FEISHU_APP_ID"  # Feishu custom-app id (developer console)
CRED_FEISHU_APP_SECRET = "FEISHU_APP_SECRET"
CRED_JIRA_API_TOKEN = "JIRA_API_TOKEN"  # Jira Cloud/Server API token (resolved from .env)
# kiro-cli's OWN model credential. Unlike the gateway-owned channel tokens
# above, its rightful consumer is the agent subprocess itself (and the whoami
# identity probe), so it is deliberately NOT in sandbox._AGENT_DENIED_ENV_KEYS:
# the spawn paths re-inject it from the .env file after the Docker entrypoint
# scrubs it out of the gateway's /proc/<pid>/environ.
CRED_KIRO_API_KEY = "KIRO_API_KEY"
_CREDENTIAL_KEYS = (
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_OWNER_ID,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_TELEGRAM_BOT_TOKEN,
    CRED_DISCORD_BOT_TOKEN,
    CRED_WEBEX_BOT_TOKEN,
    CRED_MICROSOFT_APP_ID,
    CRED_MICROSOFT_APP_PASSWORD,
    CRED_MICROSOFT_APP_TENANT_ID,
    CRED_WEIXIN_TOKEN,
    CRED_FEISHU_APP_ID,
    CRED_FEISHU_APP_SECRET,
    CRED_JIRA_API_TOKEN,
    CRED_KIRO_API_KEY,
)

# Per-host Jira tokens use a hex-encoded host suffix: JIRA_TOKEN_<HEX>.
# Only hex chars are valid — restricting the pattern prevents forged key names
# injected via multiline env values from reaching the eval-based value reader
# in the Docker entrypoint.
_JIRA_TOKEN_RE = _re.compile(r"^JIRA_TOKEN_[0-9A-Fa-f]+$")

# Keys from .env that were already warned about (fire once per gateway boot).
_warned_env_keys: set[str] = set()


_DEFAULT_PORT = 5476

# KIROCREW_PORT is validated at CLI entry (cli.py main()).
# By the time loader.py is imported the env var is a valid int or absent.
DASHBOARD_PORT: int = int(os.environ.get("KIROCREW_PORT", _DEFAULT_PORT))


# Dir-derived path helpers (workspace_root, config_path, workspace_dir_for, …)
# build on the pure primitives imported from ``config.paths`` above. They live
# here — not in the leaf — so their ``config_dir()`` / ``_default_workspace_base()``
# lookups resolve in this module's namespace, keeping the
# ``patch("kiro_crew.config.loader.config_dir", ...)`` test seam working.


def _workspace_dir_file() -> Path:
    """Return the path to the saved workspace_dir file, respecting KIROCREW_HOME."""
    return config_dir() / "workspace_dir"


def _resolve_workspace_root(root: Path) -> Path:
    """Realpath-normalize a workspace root after ensuring it exists.

    On hosts with a symlinked ``$HOME``/workspace path (e.g. ``/home/<u> ->
    /local/home/<u>``, ``/home/<u>/workplace -> /workplace/<u>``) the symlink-form
    root and its resolved form name the same directory via different strings. The
    per-session work_dir built from this root is passed as the spawn cwd and
    persisted as ``cwd`` in session_map.json. If the stored cwd is the symlink form
    while the transcript is written under the resolved form, cold resume misses and
    silently falls back to a fresh session.

    Normalizing here, at the single source, makes the SAME resolved path flow into
    spawn cwd and the persisted session_map cwd so write and resume always agree.
    This mirrors the existing ``os.path.realpath`` in ``default_project_dir``.
    """
    root.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(str(root)))


def workspace_root() -> Path:
    """Return the top-level workspace root for LLM sessions and tasks.

    Resolution order:
    1. ``KIROCREW_WORKSPACE`` env var (used as-is, no subdirectory appended)
    2. Saved path in ``config_dir()/workspace_dir`` (written by ``kirocrew setup``)
    3. Platform default with ``kirocrew-workspace`` subdirectory

    The chosen root is realpath-normalized (see ``_resolve_workspace_root``) so
    sessions resume correctly on hosts with a symlinked home/workspace path.
    """
    override = os.environ.get("KIROCREW_WORKSPACE")
    if override:
        return _resolve_workspace_root(Path(override))
    if _workspace_dir_file().is_file():
        try:
            saved = _workspace_dir_file().read_text(encoding="utf-8").strip()
            if saved:
                return _resolve_workspace_root(Path(saved))
        except OSError:
            pass
    base = _default_workspace_base()
    return _resolve_workspace_root(base / _WORKSPACE_DIR_NAME)


def _session_work_dir(session_key: str | None) -> Path:
    """Return a per-session subdirectory under workspace_root()."""
    root = workspace_root()
    if session_key:
        return root / _safe_dir_name(session_key)
    return root / "_default"


def outbox_dir() -> Path:
    """Return the outbox directory for agent-to-user file delivery."""
    d = workspace_root() / OUTBOX_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def config_local_path() -> Path:
    """Return path to config.local.json — user overrides that survive upgrades."""
    return config_dir() / "config.local.json"


def _inside_data_home(path: Path) -> bool:
    """Whether *path* lives in ``config_dir()``, the one directory we own.

    ``load()`` reads whatever ``config_path()`` resolves to, and callers can
    redirect that at a file they own -- tests and embedders point it at a
    ``tempfile`` entry in the shared ``TMPDIR``. Anything the loader drops beside
    such a path is a file nobody collects: the caller unlinks the path it created
    and never learns a sibling appeared. One dev host accumulated 72k orphaned
    ``tmpXXXXXXXX.json.bak`` files this way, 7% of a tmpfs inode budget whose
    exhaustion fails every process on the box.

    Ask about the path the sibling will ACTUALLY be written beside, which is not
    always the one you started from: ``<path>.bak`` lands beside the path as
    given, but ``update_config_locked`` resolves a symlinked config first, so its
    ``<path>.lock`` lands beside the TARGET. A ``config.json`` inside the data
    home that symlinks out of it is contained by one question and foreign by the
    other -- see :func:`_lock_target`.

    Containment is unprovable for a symlink loop or a vanished parent; treat that
    as foreign, since acting on a failed check is the worse error.
    """
    try:
        return path.parent.resolve() == config_dir().resolve()
    except OSError:
        return False


def _lock_target(path: Path) -> Path:
    """The path ``update_config_locked`` would put its lock sidecar beside.

    It resolves a symlinked config before locking, so that -- not *path* -- is
    what the containment question has to be asked about. Returns *path* unchanged
    when it is not a symlink or cannot be resolved, which is the conservative
    answer either way: an unresolvable path fails the containment check.
    """
    try:
        return path.resolve() if path.is_symlink() else path
    except OSError:
        return path


def _write_migration_backup(path: Path) -> None:
    """Copy the pre-migration config aside, but ONLY inside our own data home.

    The copy is gated on :func:`_inside_data_home`, which explains the orphan
    problem the gate exists for. In production the config always lives there
    (``config_path()`` is ``config_dir() / "config.json"``), which keeps the real
    backup exactly where it has always been; for a redirected path we write
    nothing rather than litter a directory belonging to someone else.

    Only the LOCATION decision is contained here. A failing copy still
    propagates, because the caller's ``except`` is what skips the migration
    write -- so a config we could not copy aside is not rewritten either,
    and the migration retries on the next load.
    """
    if not _inside_data_home(path):
        # info, not debug: the migration save that follows rewrites this
        # caller-owned file in place, and that now happens with no backup.
        logger.info("Config migrated; no backup written for %s (outside the data home)", path)
        return
    # NOT with_suffix(".json.bak"): that REPLACES the final suffix, so a
    # config path which is not *.json would be renamed rather than backed up.
    backup = Path(str(path) + ".bak")
    shutil.copy2(path, backup)
    logger.info("Config migrated — backup saved to %s", backup)


#: The write-back migrations a load can find pending, as recorded by
#: :meth:`KiroCrewConfig._load_resolved` and re-checked against the on-disk
#: document by :func:`_apply_document_migrations`.
MIGRATE_WORKSPACES = "workspaces"
MIGRATE_AGENTS = "agents"
MIGRATE_DEFAULT_AGENT = "default_agent"


def _apply_document_migrations(
    data: dict,
    pending: frozenset[str],
    *,
    overlay_kiro_agent: str | None,
    default_kiro_agent: str,
) -> bool:
    """Apply the pending write-back migrations to a raw config document in place.

    *data* is ``config.json`` as read **inside the write lock**, not the merged
    snapshot the calling load parsed. That is the whole point: the migration is
    expressed as a delta against the document that is actually on disk right now,
    so a config write that landed after this load's read survives instead of being
    replaced by a re-serialization of the older snapshot.

    *pending* names the migrations the load decided on, so this never widens what
    the migration writes. The load's decisions are taken against the MERGED
    base+overlay view; the overlay is user-owned and never written back, so a
    migration the merged view did not ask for must not be invented here.

    The seeded agent's kiro agent resolves the same three-way precedence the
    loader itself applies, because it has to be the MERGED effective value (that
    is what the default crew dispatched before the migration) computed against a
    CURRENT base: *overlay_kiro_agent* first, since ``config.local.json`` wins the
    deep-merge and the base can say nothing about it; then the base document's own
    ``agent.default_agent`` as read here, so a ``config set`` that landed after
    the load's read is honored rather than reverted; then *default_kiro_agent*,
    the value the load resolved, which is the only one carrying the dataclass
    default. Taking any single one of the three is wrong in a different direction.

    Every entry is re-checked against *data* before it is applied, which makes the
    function idempotent and makes a concurrent writer that already migrated a
    no-op rather than a second rewrite. Returns True when anything changed; the
    caller skips the write entirely when it returns False.
    """
    changed = False

    # Flat workspace strings -> {"dir": ...}. Per entry, and only for entries the
    # document still holds as a string: a workspace added or rewritten by another
    # writer since this load's read is left exactly as that writer left it.
    if MIGRATE_WORKSPACES in pending:
        raw_workspaces = data.get("workspaces")
        if isinstance(raw_workspaces, dict):
            for name, value in list(raw_workspaces.items()):
                if isinstance(value, str):
                    raw_workspaces[name] = asdict(WorkspaceConfig(dir=value))
                    changed = True

    # Seed the default agent when the document still has none.
    if MIGRATE_AGENTS in pending:
        stored_agents = data.get("agents")
        if not isinstance(stored_agents, dict) or not stored_agents:
            stored_agent_section = data.get("agent")
            stored_kiro = (
                stored_agent_section.get("default_agent")
                if isinstance(stored_agent_section, dict)
                else None
            )
            base_kiro = stored_kiro if isinstance(stored_kiro, str) and stored_kiro else None
            data["agents"] = {
                "default": asdict(
                    KiroCrewAgentConfig(
                        kiro_agent=overlay_kiro_agent or base_kiro or default_kiro_agent,
                        workspace="default",
                        memory_store="default",
                    )
                )
            }
            changed = True

    # Point default_agent at an agent that exists. Resolved against the
    # document's OWN agents (after any seeding above), so a concurrent writer's
    # newly added agent is a valid target rather than something we overwrite.
    if MIGRATE_DEFAULT_AGENT in pending:
        stored_agents = data.get("agents")
        known = stored_agents if isinstance(stored_agents, dict) else {}
        stored_default = data.get("default_agent")
        if not isinstance(stored_default, str) or not stored_default or stored_default not in known:
            if "default" in known:
                data["default_agent"] = "default"
            elif known:
                data["default_agent"] = next(iter(known))
            else:
                data["default_agent"] = "default"
            changed = data["default_agent"] != stored_default or changed

    return changed


def _overlay_kiro_agent() -> str | None:
    """``agent.default_agent`` as ``config.local.json`` states it, if it does.

    The overlay is deep-merged OVER the base at load time, so where it names this
    field the base cannot be the effective value. The seeded agent has to carry
    the effective one -- that is what the default crew dispatched before the
    migration existed -- so the seed consults this first.

    Best-effort by design: an unreadable or malformed overlay means "the overlay
    says nothing here", which lets the base value stand. It must not abort the
    migration, because the loader itself already tolerates a bad overlay (it warns
    and marks the file degraded) and a stricter rule here would make one broken
    user-owned file block a write that is correct without it.
    """
    try:
        local_path = config_local_path()
        if not local_path.is_file():
            return None
        raw = json.loads(local_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    section = raw.get("agent")
    if not isinstance(section, dict):
        return None
    value = section.get("default_agent")
    return value if isinstance(value, str) and value else None


def _persist_config_migration(
    path: Path,
    pending: frozenset[str],
    *,
    default_kiro_agent: str,
) -> bool:
    """Write the pending migrations to *path* as a read-modify-write.

    Replaces the ``cfg.save()`` this used to be. ``save()`` re-serializes the
    WHOLE snapshot the load parsed, and that snapshot was read before this call:
    a dashboard PATCH or ``kirocrew config set`` landing in between was silently
    dropped, because nothing ordered the two (#7793). ``load()`` already runs off
    the event loop in places (``chat_runner``'s stop-hook nudge-cap site awaits
    ``asyncio.to_thread(KiroCrewConfig.load)``), so the interleave is reachable.

    Two parts carry the fix, and they are worth separating because only one of
    them can be applied everywhere:

    * **The write is a delta.** Only the keys *pending* names are touched, and
      each is re-decided against the document as read HERE rather than against
      the load's older snapshot. A concurrent write to any other setting is
      therefore not merely ordered but untouchable -- those bytes are never part
      of our write. This part always applies.
    * **The read and the write are one critical section**, via
      :func:`update_config_locked`, so the migration's own keys are decided from
      current state and cannot interleave with another locked writer.

    The ordering the loader uses next door -- ``publish_autocompact_pct``'s
    monotonic ticket -- cannot close this. A ticket orders load against load, and
    the writer being lost here is not a load: no config writer outside this module
    draws a ticket, so the comparison never sees the PATCH at all. Nor is it
    enough to take a lock around the write alone, since the snapshot was read
    before the lock; the read has to move inside it.

    ``update_config_locked`` takes its advisory lock on a ``<path>.lock`` sidecar,
    which for a config path we do not own is the orphan the backup gate above
    exists to prevent -- and ``TestMigrationBackupContainment`` pins that a
    migrating ``load()`` leaves a caller-owned directory exactly as it found it.
    So the same containment predicate decides the lock, asked about the path the
    lock will actually land beside: ``update_config_locked`` resolves a symlinked
    config first, so a ``config.json`` inside the data home that points OUT of it
    would otherwise be classified as contained and drop its sidecar in a directory
    belonging to someone else. Contained, take the lock; foreign, do the delta off
    an immediate fresh read and skip the sidecar. What the foreign path gives up is
    ordering on the migration's OWN keys against an unlocked writer of those same
    keys -- and a redirected config has no gateway writing it, since the dashboard
    and CLI paths write ``config_path()``.

    The lock is taken WITHOUT waiting. ``load()`` is called from the event loop
    all over the tree, and a POSIX ``flock`` wait there would stall the gateway
    for as long as the holder keeps the lock -- so this asks once and gives up.
    Giving up costs nothing, because a held lock means another writer is mid-write
    and that writer's bytes are precisely what must not be clobbered: declining is
    the correct outcome, not a compromise. The migration is already
    retry-on-next-load by construction (the degraded-sections branch in
    ``_load_resolved`` relies on the same property), so the next uncontended load
    performs it. The remaining file I/O on the loop -- one read, one atomic
    rename -- is what ``cfg.save()`` did here before, unchanged.

    The backup is taken immediately before the write -- inside the lock hold where
    there is one -- so it is the bytes being replaced rather than whatever was
    there when the load started. A failing copy still propagates and aborts the
    write, keeping :func:`_write_migration_backup`'s contract: a config we could
    not copy aside is not rewritten, and the migration retries on the next load.

    Returns True when a write happened.
    """
    wrote = False
    # The overlay is user-owned and never written back, so it is read OUTSIDE the
    # lock: there is no update of ours to lose against it, and a concurrent edit
    # to it is the operator's own action rather than a race. Read here rather than
    # taken from the load's snapshot so the seed reflects the file as it is now.
    overlay_kiro_agent = _overlay_kiro_agent()

    def _mutate(current: dict) -> dict | None:
        nonlocal wrote
        if not _apply_document_migrations(
            current,
            pending,
            overlay_kiro_agent=overlay_kiro_agent,
            default_kiro_agent=default_kiro_agent,
        ):
            # Nothing left to migrate -- another writer got here first. Returning
            # None skips the write, so we do not rewrite a file we agree with.
            return None
        _write_migration_backup(path)
        wrote = True
        return current

    if _inside_data_home(_lock_target(path)):
        try:
            update_config_locked(path, mutate=_mutate, wait_for_lock=False)
        except BlockingIOError:
            # POSIX reports a contended single-shot acquire this way. Not a
            # failure worth a warning: info, because the migration simply moves
            # to the next load and nothing was written or lost.
            logger.info(
                "config: migration deferred -- another writer holds %s.lock; "
                "the next load will migrate",
                path.name,
            )
            return False
    else:
        # read_config_for_update fails CLOSED on an unreadable or non-object
        # file, and that exception reaches load()'s except: the file is left
        # alone and the migration retries. Same contract as the locked path.
        result = _mutate(read_config_for_update(path))
        if result is not None:
            write_config_atomically(path, stamp_config_meta(result))
    if wrote:
        # Same reason save() did: drop the validated-data cache so the next load
        # re-reads this write even where the filesystem mtime resolution is coarse.
        _invalidate_config_cache()
    return wrote


def denied_commands_path() -> Path:
    """Return path to denied_commands.json — the denied-command opt-out state.

    This is a KEYSTONE trust-root file (on ``security._SENSITIVE_HOME_DIRS``):
    it holds ``{disable_all, disabled_ids, user_added}``, the user's opt-out from
    the built-in deny ceiling. It lives OUTSIDE the agent-readable
    ``config.json`` precisely so an auto-approved/YOLO agent shell cannot write
    it (via any shell trick) and disable its own deny ceiling. Only the operator
    edits it out-of-band — through the dashboard ``/api/security/…`` endpoints,
    which do not route through the agent tool gate. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "denied_commands.json"


def computer_use_state_path() -> Path:
    """Return path to computer_use.json — the computer-use primary enable.

    Same KEYSTONE reasoning as :func:`denied_commands_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: enabling computer use
    grants full desktop observation plus input synthesis into the operator's real
    applications, which is a security ceiling, not a preference. Keeping it out
    of the agent-readable ``config.json`` is what makes it un-flippable by a
    prompt-injected agent — ``is_sensitive_path`` blocks the tool path and
    ``is_sensitive_bash_command`` blocks the shell forms (``cat``, ``>``,
    ``tee``, archive extraction into the trust root).

    Holds ``{enabled, allowed_apps, extra_denied_apps}``; every read fails soft
    to DISABLED (see ``computer_use.enable_state``). The only writer is the
    dashboard ``/api/computer-use/config`` PUT, which does not route through the
    agent tool gate. Respects ``KIROCREW_HOME``.

    Note the deliberate asymmetry with the ``computer_use`` section of
    ``config.json``: that section carries display/limit knobs ONLY and has no
    ``enabled`` field, precisely so there is exactly one place the feature can be
    turned on and it is not one the agent can reach.
    """
    return config_dir() / "computer_use.json"


def oauth_endpoints_path() -> Path:
    """Return path to oauth_endpoints.json — the operator OAuth-endpoint extension.

    Same KEYSTONE reasoning as :func:`denied_commands_path` and
    :func:`computer_use_state_path`, and the leaf is on
    ``security._CREW_SECRET_LEAVES`` for the same reason: each listed endpoint
    widens the banner-only OAuth entropy carve-out (``security.py``'s
    ``_OAUTH_AUTHORIZATION_ENDPOINTS``), so an agent that could write this file
    could exempt an attacker-controlled host from the exfiltration heuristics —
    it is a trust boundary, not a preference. ``is_sensitive_path`` blocks the
    tool path and ``is_sensitive_bash_command`` blocks the shell forms.

    Holds ``{"additional_authorization_endpoints": [{"host": …, "path": …}]}``;
    every read fails soft to an EMPTY extension set (see
    ``security._load_operator_oauth_endpoints``). There is no dashboard writer:
    the operator hand-edits the file out-of-band. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "oauth_endpoints.json"


def aws_consent_path() -> Path:
    """Return path to aws_service_consent.json — paid-AWS-service consent.

    Same KEYSTONE reasoning as :func:`computer_use_state_path`, and the leaf is
    on ``security._CREW_SECRET_LEAVES`` for the same reason: a recorded consent
    to call a PAID AWS service is an authorization, not a preference. Storing it
    in ``config.json`` would leave it writable by any auto-approved agent shell,
    so a prompt-injected agent could mint the grant and consent, on the
    operator's behalf, to spending the operator's money in an account it picked.
    ``is_sensitive_path`` blocks the tool path and ``is_sensitive_bash_command``
    blocks the shell forms.

    Holds ``{"<service>": {profile, region, account, arn, granted_at}}``; every
    read fails soft to NO CONSENT (see ``aws_consent.read_grant``). The writers
    are the authenticated dashboard ``/api/aws/consent`` handler and the
    ``kirocrew aws-consent`` CLI, both of which open the path directly rather
    than through this gate. Respects ``KIROCREW_HOME``.
    """
    return config_dir() / "aws_service_consent.json"


def read_local_secret(port: int) -> str:
    """Read the internal-API credential for the gateway on *port*.

    Single home for the secret read that callers (cron scripts, MCP tool bridges,
    CLI) need to authenticate to the gateway's internal API. Returns empty string
    when no credential can be read.

    Resolution is per LISTENER first: ``run/gateway-<port>.secret``, then the
    shared ``.local_secret``. That order is the invariant, and it lives here rather
    than in each reader because the credential identifies ONE gateway generation
    while the shared file has one slot per data home, last-writer-wins. A caller
    that reads the shared file while a different generation owns the port it dials
    gets 403 on every internal call.

    *port* is REQUIRED, and deliberately so: the credential is a function of the
    dial target, so inferring the target here would let a caller dial one gateway
    while authenticating for another -- the exact desync this helper exists to
    close, reintroduced one call site at a time and invisible at the call site. A
    caller with no port must resolve one explicitly and pass it, where the choice
    is reviewable.
    """
    # Function-local: port_resolution imports this module, so a module-level
    # import would be circular.
    from kiro_crew.instances import run_marker

    try:
        per_port = run_marker.read_secret(int(port))
    except Exception:
        per_port = ""
    if per_port:
        return per_port
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except OSError:
        return ""


def _raw_config() -> dict:
    """Load raw config.json as dict (cached per process)."""
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


class ConfigReadError(Exception):
    """``config.json`` exists but could not be read as a config object.

    Raised only by :func:`read_config_for_update`, whose callers are about to
    write the value back. It deliberately does NOT inherit from ``OSError`` or
    ``ValueError`` so an existing broad ``except OSError`` around a write cannot
    swallow it and resume the clobbering path.
    """


def read_config_for_update(path: Path | None = None) -> dict:
    """Read ``config.json`` for a read-modify-write, failing CLOSED.

    Every partial config update (flip one toggle, persist one channel) has to
    read the whole file, mutate one key, and write it all back. The obvious
    ``try: json.loads(...) except Exception: data = {}`` is a **data-loss bug**
    in that shape: the fallback is indistinguishable from "the user has no
    settings", so the write-back replaces a fully populated config with a
    single-key one. Every setting the user ever chose is gone, silently, and
    the endpoint still reports success.

    The read fails for mundane reasons — most commonly a *torn read*: several
    config writers still truncate-then-write, so a concurrent reader can
    observe a half-written file. That window is small, which is exactly what
    makes the resulting loss so hard to reproduce and report.

    So: an **absent** file returns ``{}`` (a genuine empty starting point), and
    an unreadable or non-object file raises :class:`ConfigReadError`. Callers
    must let that abort the update — leaving the existing file untouched is
    always better than overwriting it with defaults.

    Pair this with :func:`kiro_crew.atomic_write.atomic_write` on the way out so
    the write cannot create the torn window for the next reader.
    """
    p = path if path is not None else config_path()
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # UnicodeDecodeError is a ValueError, NOT an OSError, so it needs naming
        # explicitly: a config containing invalid UTF-8 (a truncated multi-byte
        # sequence from a torn write, or a mojibake'd hand edit) would otherwise
        # escape this controlled path and crash the caller instead of returning
        # the clean "config unreadable" refusal.
        raise ConfigReadError(f"could not read config at {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigReadError(f"config at {p} is not a JSON object (got {type(raw).__name__})")
    return raw


def write_config_atomically(path: Path, data: dict, *, fsync: bool = False) -> None:
    """Write a config dict to *path* atomically, PRESERVING its permissions.

    The companion to :func:`read_config_for_update`. Two properties matter:

    * **Atomic** (tmp+rename) so a concurrent reader can never observe a
      half-written file. A truncate-then-write leaves a window in which a reader
      sees invalid JSON; a reader that mistakes that for "no settings" will write
      the emptiness back and destroy the user's config.
    * **Mode-preserving.** Because tmp+rename creates a NEW inode, the umask
      default (typically ``0644``) would silently replace an operator's tightened
      ``0600``. ``config.json`` can hold inline credentials, so a settings write
      must never widen who can read it. An existing file's mode is carried over;
      a newly created one defaults to owner-only.

    ``atomic_write``'s ``mode`` routes through ``fchmod_safe``, which applies the
    mode on POSIX and is a documented no-op on Windows.

    **Windows gets a real owner-only DACL, not just the inert mode.** This used
    to deliberately skip ``platform_compat.restrict_to_owner`` because that helper
    shelled out to ``icacls`` — a blocking subprocess this function could not
    afford, being called from ``async`` request handlers and from
    ``KiroCrewConfig.save()``. That constraint no longer exists: the lockdown is
    applied in-process through ``advapi32`` (measured at 0.24 ms, against 313 ms
    for the subprocess it replaced), so it is safe on the event loop and the
    reason to omit it is gone. Since ``config.json`` can carry inline provider
    tokens and API keys, applying it is the correct default rather than a duty
    pushed onto each caller.

    The two guarantees do not collide, because they apply on different platforms:
    mode preservation is a POSIX concept (Windows has no bits to preserve), and
    the DACL is a Windows concept. Hence the platform branch below rather than
    passing both to ``atomic_write``, which refuses ``restrict_to_owner=True``
    alongside a wider explicit ``mode``.

    **On a network-homed data home the DACL turns on the CALLER, not the volume.**
    The in-process lockdown costs 0.24 ms on a local volume but is bounded only by
    SMB on a UNC or mapped-drive path, which a write running inline on the event
    loop cannot afford. That is a fact about the calling thread, so it is asked as
    one, via :func:`kiro_crew.atomic_write.on_event_loop`. A caller that has
    offloaded this write -- ``dashboard/chat_utils.run_config_write``, any
    ``asyncio.to_thread`` wrapper, and every CLI and startup path, which have no
    loop at all -- blocks only its own thread and therefore gets the owner-only
    DACL on **any** volume. Only a write still inline on the loop falls back to
    classifying the volume and skipping when it is remote.

    **Symlinks are followed, not replaced.** ``os.replace`` renames over the link
    itself, turning a symlinked ``config.json`` into a regular file and orphaning
    its target — whereas the ``write_text`` this replaced followed the link and
    updated the target. Symlinking the config into a dotfiles repo is a normal
    setup, so the target is resolved first to preserve that behavior.
    """
    # Resolve BEFORE stat/write so a symlinked config keeps pointing at its
    # target (and the mode preserved is the target's, not the link's).
    try:
        if path.is_symlink():
            path = path.resolve()
    except OSError:
        pass
    # Decide the Windows lockdown HERE, before the stat and the mkdir below and
    # before anything atomic_write does -- every one of those is a round-trip on a
    # network-homed data home. A DACL write to a UNC or mapped-drive path is an
    # unbounded SMB round-trip, so when it cannot be afforded it has to be ruled
    # out before the work starts rather than part way through.
    #
    # But whether it can be afforded is a question about the CALLING THREAD, not
    # about the volume. The volume was only ever a proxy: this function is
    # synchronous and async dashboard handlers reach it inline, where an unbounded
    # wait stalls the one loop the whole gateway shares. Off the loop there is
    # nothing to stall -- a worker started by ``run_config_write`` /
    # ``asyncio.to_thread``, a CLI invocation, a startup path all block only
    # themselves -- so the same predicate ``atomic_write`` already gates its own
    # unbounded-on-Windows step on decides here too, and a network-homed data home
    # gets the DACL whenever its caller has offloaded the write.
    #
    # This sits just AFTER the symlink resolve rather than at the very top of the
    # function, and deliberately: a config symlinked into a dotfiles repo (which
    # the docstring above calls a normal setup) can point at a DIFFERENT volume
    # than the link, so classifying before resolving would classify the wrong one.
    # The resolve is two stats; the earliest CORRECT point is here.
    lock_down = platform_compat.IS_POSIX
    if not platform_compat.IS_POSIX:
        if not on_event_loop():
            # Nothing to stall, so the volume does not decide -- and is not even
            # classified, because its answer could only weaken the outcome.
            lock_down = True
        else:
            try:
                lock_down = windows_acl.volume_is_local(path)
            except Exception:
                # A descriptor API that cannot be loaded cannot tell us the volume
                # is local, and the lockdown would have failed on this host anyway.
                lock_down = False
            if not lock_down:
                logger.warning(
                    "config write: %s is on a non-local volume and this write is "
                    "running on the event loop, so the owner-only DACL was "
                    "SKIPPED to avoid stalling the loop on SMB; the file may be "
                    "readable by other local users. Offloading the write "
                    "(dashboard/chat_utils.run_config_write) applies the DACL "
                    "here too",
                    path,
                )
    try:
        mode = _stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    except OSError:
        mode = 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    if platform_compat.IS_POSIX:
        atomic_write(path, payload, fsync=fsync, mode=mode)
    elif lock_down:
        # Windows: the mode bits above are inert (fchmod_safe is a documented
        # no-op), so there is nothing to preserve and no conflict with
        # restrict_to_owner's implied 0600. Taking the lockdown here rather than
        # leaving it to callers also closes the window a post-write lockdown
        # would leave: atomic_write applies the DACL to the temp file BEFORE any
        # content reaches it, so an inline credential never exists in a file
        # readable by other local accounts.
        #
        # restrict_on_error="warn", not the default "raise": config.json must not
        # become unwritable because a DACL could not be applied. Same trade-off
        # sel.py and dashboard/refresh_tokens.py already take, and strictly
        # better than the previous behavior, which applied no DACL at all.
        atomic_write(
            path,
            payload,
            fsync=fsync,
            restrict_to_owner=True,
            restrict_on_error="warn",
        )
    else:
        # Reached only by a write still INLINE ON THE LOOP whose volume is not
        # local: exactly the write this branch did before the lockdown was added,
        # so such a data home is no worse off than before. The residual is real and
        # declared -- the file keeps the ACL it inherits from its parent -- but it
        # is now per CALLER rather than per platform: offloading a caller moves it
        # to the branch above and it gets the DACL with no change needed here.
        atomic_write(path, payload, fsync=fsync, mode=mode)


def update_config_locked(
    path: Path | None = None,
    *,
    mutate: Callable[[dict], dict | None],
    fsync: bool = False,
    stamp_meta: bool = True,
    on_corrupt: Literal["fail", "reset"] = "fail",
    wait_for_lock: bool = True,
) -> dict:
    """Perform an atomic read-modify-write of a config file under an advisory lock.

    The locked primitive for every DIRECT
    ``write_config_atomically(config_path())`` caller outside this module, and
    the required path for new ``config.json`` mutations.  **No such caller
    remains** -- the dashboard agents endpoint, ``security.py``, the apps manager
    and the CLI setup wizard were the last of them and are converted (#8032);
    ``memory.py`` was converted earlier and reaches this function through
    ``dashboard/chat_utils.run_config_write``.
    ``TestEveryConfigWriterIsLocked`` in
    ``test/test_config_rmw_preserves_settings.py`` is the ratchet that keeps the
    list from regrowing.

    Read "direct caller outside this module" strictly: it is the exact set the
    ratchet checks, and it is NOT the same as "every writer that reaches
    ``config.json``".  :meth:`KiroCrewConfig.save` calls
    :func:`write_config_atomically` and does NOT come through here -- see the
    second family below.

    A SECOND family of writers still bypasses this lock, and the ratchet does
    NOT reach it -- for two different reasons, neither of which is visible from a
    call site:

    * Writers that reach ``config_path()`` through
      ``kiro_crew.agent._atomic_json_write`` (``messaging.py``'s per-channel
      savers, ``core.py``'s STT PUT, ``mcp.py``'s gateway-enable). The ratchet
      matches calls to :func:`write_config_atomically`, and these make none.
    * Writers that go through :meth:`KiroCrewConfig.save` (``updates.py``'s
      log-level PUT, ``core.py``'s theme PUT, several ``agents.py`` agent CRUD
      endpoints). ``save`` DOES call :func:`write_config_atomically` directly,
      but it does so from inside this module, which the ratchet exempts -- so the
      write is invisible to it at every caller.

    Both rely on the in-process asyncio ``_get_config_lock()`` only, which
    serializes same-loop callers and nothing else, so they can still interleave
    with a holder of this lock.  Converting them is follow-up work; do not read
    the ratchet's green as covering them, and note that an ALIASED import of
    :func:`write_config_atomically` would evade it for the same matching reason
    as the first bullet.

    Contract:

    * **Isolation.** An advisory file lock is held for the entire
      read-modify-write, so two concurrent callers are serialized: neither can
      land between the other's read and write.
    * **Sidecar lockfile.** The lock lives on ``<path>.lock``, NOT on the
      config file's own fd.  ``write_config_atomically`` replaces the inode
      (tmp + rename), so a lock taken on the config file's fd would not
      serialize against the rename — a second opener after the rename gets a
      NEW fd on the NEW inode and takes the lock instantly, defeating the
      purpose.
    * **Fail-closed read (default).** :func:`read_config_for_update` is used
      inside the critical section; with ``on_corrupt="fail"`` (the default), an
      unreadable or malformed config raises :class:`ConfigReadError`, aborts
      the update, and the lockfile is released.  The existing file is never
      overwritten with defaults.
    * **Reset-on-corrupt (opt-in).** With ``on_corrupt="reset"``, a
      :class:`ConfigReadError` inside the critical section is caught WHILE THE
      LOCK IS STILL HELD and the *mutate* callback is invoked with ``{}``.
      The caller's write therefore happens in the same lock hold as the read
      attempt, closing any window for a concurrent writer to land between.
      The resulting file is written with mode ``0o600`` (no existing mode to
      preserve from a corrupt file).
    * **Mode-preserving write.** :func:`write_config_atomically` preserves the
      existing file's permission bits, so a tightened ``0600`` is not widened.
    * **Cross-platform.** Locking goes through
      :func:`platform_compat.file_lock`, which uses ``fcntl.flock`` on POSIX
      and a bounded ``msvcrt.locking`` spin on Windows.
    * **Symlink-safe.** The target path is resolved before locking, so a
      symlinked config is updated in place (matching
      ``write_config_atomically``'s behavior).

    Parameters
    ----------
    path : Path | None
        Config file path; defaults to :func:`config_path`.
    mutate : (dict) -> dict | None
        Called with the current config data (possibly ``{}`` for a new file).
        Must return the updated dict to write, or ``None`` to skip the write
        (useful when the mutate discovers no change is needed).
    fsync : bool
        Passed through to :func:`write_config_atomically`.
    stamp_meta : bool
        If True (default), stamps the ``meta`` block via
        :func:`stamp_config_meta` before writing.
    on_corrupt : "fail" | "reset"
        Behavior when :func:`read_config_for_update` raises
        :class:`ConfigReadError`.  ``"fail"`` (default) re-raises, aborting the
        update.  ``"reset"`` catches the error inside the lock hold and invokes
        *mutate* with ``{}``; the caller's write proceeds in the same critical
        section so no concurrent writer can land between.
    wait_for_lock : bool
        If True (default), block until the advisory lock is free -- correct for
        a caller whose update must happen.  If False, the acquire is single-shot
        and raises :class:`OSError` when another writer holds the lock; for a
        caller whose write is OPTIONAL and retried later, and which may run on
        the event-loop thread, where a POSIX ``flock`` wait would stall the
        gateway for as long as the holder keeps it.  It never relaxes the
        serialization -- a contended acquire declines instead of proceeding.

    Returns
    -------
    dict
        The final config dict (after mutation), whether or not a write occurred.

    Raises
    ------
    ConfigReadError
        If the existing config is unreadable or malformed and
        ``on_corrupt="fail"``.
    OSError
        If the lockfile cannot be opened/created or the lock cannot be acquired
        (including a contended ``wait_for_lock=False`` acquire).
    """
    p = path if path is not None else config_path()
    # Resolve symlinks before locking (same logic as write_config_atomically)
    # so the sidecar sits beside the ACTUAL file, not the symlink.
    try:
        if p.is_symlink():
            p = p.resolve()
    except OSError:
        pass
    lock_path = p.parent / (p.name + ".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(fd, exclusive=True, wait=wait_for_lock):
            try:
                data = read_config_for_update(p)
            except ConfigReadError:
                if on_corrupt == "fail":
                    raise
                # on_corrupt="reset": treat as empty inside the same lock hold.
                data = {}
            result = mutate(data)
            if result is None:
                return data
            if stamp_meta:
                result = stamp_config_meta(result)
            write_config_atomically(p, result, fsync=fsync)
            return result
    finally:
        os.close(fd)


# Keys already warned about in this process. The gateway loads config repeatedly
# and a superseded default is per-install information, not per-load, so it is
# said once; ``doctor`` is the surface that renders it again on demand.
_REPORTED_SUPERSEDED_KEYS: set[str] = set()


def _report_superseded_defaults(base_data: dict) -> None:
    """Warn once when stored base values still hold a superseded default.

    *base_data* is the ``config.json`` document as read, BEFORE the
    ``config.local.json`` overlay is merged over it. Reporting on the base is the
    point: the overlay is a separate user-owned file whose value is the operator's
    live choice, so it neither proves nor disproves what the base has materialized.

    Reads only. This deliberately does NOT correct the value -- for a key that also
    has a documented escape hatch, a stored old default and a deliberate opt-out
    are the same bytes on disk, so a rewrite cannot correct one without overriding
    the other. Telling the operator is the part that can be done without guessing.

    ONE line naming every drifted key, not one line per key. The registry is
    append-only, so a per-key line means the terminal noise on a long-lived install
    grows with every default the project ever changes -- and it lands on every
    short-lived ``kirocrew`` invocation, where the once-per-process guard below
    buys nothing because there the process IS the invocation. The per-key detail
    belongs on the surface the operator asked for: ``kirocrew config defaults``,
    and ``doctor``. It is also emitted at debug here, so a gateway run with
    ``-vv`` still carries the full text in its own log.

    Keys already named in this process are not repeated, so a gateway that loads
    config many times says it once. An acknowledged key is not reported at all --
    ``superseded_default_drift`` filters it -- which is what makes this line
    answerable instead of permanent.
    """
    drifted = [
        e
        for e in superseded_default_drift(base_data)
        if e.dotted_key not in _REPORTED_SUPERSEDED_KEYS
    ]
    if not drifted:
        return
    for entry in drifted:
        _REPORTED_SUPERSEDED_KEYS.add(entry.dotted_key)
        logger.debug("Superseded default in stored config: %s", drift_summary(entry))
    logger.warning(
        "%d stored config value(s) still hold a superseded default: %s. "
        "Run 'kirocrew config defaults' to see each one, '--adopt' to take the "
        "current defaults, or '--keep' to affirm yours and stop this notice.",
        len(drifted),
        ", ".join(e.dotted_key for e in drifted),
    )


def stamp_config_meta(data: dict) -> dict:
    """Return *data* with a freshly stamped ``meta`` block in front.

    ``meta.lastTouchedVersion`` names the build that wrote the bytes now on
    disk, which is the first thing to check when a ``config.json`` looks like
    it came from an older schema. An existing stamp is therefore replaced
    rather than merged.

    Every writer that rebuilds the whole file from a dataclass round-trip has
    to stamp through here: ``to_dict()`` models only the schema, so such a
    write drops any top-level key the dataclass does not carry — ``meta``
    among them. Writers that mutate the raw dict they read keep the block
    without help.

    Only ``config.json`` carries the block. ``config.local.json``, agent
    specs, and the other JSON that shares :func:`write_config_atomically` do
    not, so the stamping is deliberately separate from that function.
    """
    return {
        "meta": {
            "lastTouchedVersion": __version__,
            "lastTouchedAt": datetime.now(timezone.utc).isoformat(),
        },
        **{k: v for k, v in data.items() if k != "meta"},
    }


def refresh_config_meta_stamp() -> bool:
    """Re-stamp ``config.json``'s ``meta`` block when it names another build.

    The stamp is only ever written as a side effect of a config write, so an
    upgrade that never touches ``config.json`` leaves ``lastTouchedVersion``
    naming the *previous* build indefinitely. That contradicts the field's
    documented meaning ("the build that wrote the bytes now on disk") and
    sends anyone debugging a version question chasing a build that is no
    longer installed (#3102). Called once per gateway start, off the boot
    path: a version check on one small file, a rewrite only when it differs.

    Deliberately a plain field refresh, not a migration hook: the stamp is
    replaced, every other key is preserved, and nothing else changes. When
    the stored version already matches, the file is not rewritten at all
    (no mtime churn, no ``lastTouchedAt`` bump).

    The read-modify-write goes through :func:`update_config_locked` — the
    required path for new ``config.json`` mutations — so the refresh holds
    the sidecar advisory lock and can never revert a concurrent settings
    write with its own earlier snapshot. Callers that run while the
    dashboard serves requests must ALSO hold the in-process asyncio config
    lock (``_get_config_lock``) around the call, because the legacy writers
    serialize on that lock alone.

    Best-effort by design — a stale stamp is a diagnostic blemish, never
    worth failing a boot over. Returns ``True`` when a refresh was written,
    ``False`` when nothing needed doing (absent/empty file, current stamp)
    or the file could not be safely read (an unreadable/torn config must
    never be replaced with a stamped-but-empty one).
    """
    path = config_path()
    if not path.exists():
        return False

    wrote = False

    def _stamp_if_stale(data: dict) -> dict | None:
        nonlocal wrote
        if not data:
            # Absent or emptied between the exists() check and the lock hold:
            # there is nothing to refresh, and writing would CREATE a config
            # holding only a meta block.
            return None
        meta = data.get("meta")
        stored = meta.get("lastTouchedVersion") if isinstance(meta, dict) else None
        if stored == __version__:
            return None  # current: skip the write entirely
        wrote = True
        return data  # update_config_locked stamps the meta block itself

    try:
        update_config_locked(path, mutate=_stamp_if_stale)
    except ConfigReadError:
        logger.debug(
            "config meta stamp refresh skipped: %s unreadable; leaving it untouched",
            path,
            exc_info=True,
        )
        return False
    except OSError:
        logger.debug(
            "config meta stamp refresh failed: could not lock or write %s",
            path,
            exc_info=True,
        )
        return False
    if wrote:
        _invalidate_config_cache()
    return wrote


def workspace_dir_for(workspace: str | None = None) -> Path:
    """Resolve a named workspace to its directory path.

    Reads the ``dir`` field from ``WorkspaceConfig`` objects (new structured
    format) or falls back to raw string values (legacy flat format).

    Values starting with ``/`` or ``~`` are treated as absolute paths.
    Otherwise the value is relative to ``config_dir()`` (``~/.kiro/crew/``).
    Unmapped workspace names fall back to ``"workspace"``.
    """
    data = _raw_config()
    ws = workspace or data.get("default_workspace", "default")
    mapping = data.get("workspaces", {})
    raw_value = mapping.get(ws, "workspace")

    # Extract the directory string from either format
    if isinstance(raw_value, dict):
        dirname = raw_value.get("dir", "workspace")
    elif isinstance(raw_value, str):
        dirname = raw_value
    else:
        dirname = "workspace"

    p = Path(dirname).expanduser()
    if p.is_absolute():
        return p
    return config_dir() / dirname


def default_project_dir(workspace: str | None = None) -> str:
    """Resolve the default project directory for a workspace.

    Returns the realpath of ``workspace_dir_for(workspace)`` if it exists and
    is not a sensitive path, otherwise returns ``""``.

    Used by chat_handlers (slot.project fallback) and session.py (pool cwd)
    to avoid duplicating the same resolution + validation logic.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    try:
        ws_dir = os.path.realpath(str(workspace_dir_for(workspace)))
        if os.path.isdir(ws_dir) and not is_sensitive_path(ws_dir):
            return ws_dir
    except Exception:
        pass
    return ""


def env_path() -> Path:
    return config_dir() / ".env"


def read_env_file_credential(key: str, env_file: Path | None = None) -> str:
    """Best-effort read of one ``KEY=VALUE`` entry from the data home's ``.env``.

    Same line format :meth:`KiroCrewConfig.load_credentials` parses (one pair
    per line, ``#`` comments, no quotes required, last occurrence wins).
    Returns ``""`` when the file is absent or unreadable — callers treat the
    credential as unset rather than failing.

    Blocking file IO: call via ``asyncio.to_thread`` from async paths.
    """
    ep = env_file if env_file is not None else env_path()
    try:
        text = ep.read_text()
    except OSError:
        return ""
    value = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                value = v.strip()
    return value


def inject_kiro_cli_api_key(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Ensure *env* carries kiro-cli's own model credential (``KIRO_API_KEY``).

    The Docker entrypoint scrubs :data:`_CREDENTIAL_KEYS` out of the gateway's
    process environment into the data home's ``.env`` (mode 600) so they never
    reside in a long-lived ``/proc/<pid>/environ``. Every other credential is
    consumed in-process from :meth:`KiroCrewConfig.load_credentials`, but this
    one authenticates the kiro-cli CHILD, which reads it from its own
    environment — so kiro-cli spawn paths call this to hand the child exactly
    the one variable it owns, without re-widening the parent's environ. A value
    already present in *env* wins (same precedence as ``load_credentials``);
    outside Docker nothing changes because the variable is still inherited.

    Mutates *env* in place and returns it for convenience. Blocking file IO:
    call via ``asyncio.to_thread`` from async paths.
    """
    if not env.get(CRED_KIRO_API_KEY):
        val = read_env_file_credential(CRED_KIRO_API_KEY)
        if val:
            env[CRED_KIRO_API_KEY] = val
    return env


def strip_kiro_cli_api_key(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove kiro-cli's model credential from a child that does not consume it.

    Counterpart to :func:`inject_kiro_cli_api_key` for every ACP backend other
    than kiro (Claude Code, and KAS): the credential authenticates
    kiro-cli's OWN v2 agent loop, and it is deliberately NOT in
    ``sandbox._AGENT_DENIED_ENV_KEYS``, so without this an inherited copy in the
    raw ``os.environ`` snapshot would ride into an agent process that has no use
    for it.

    "Foreign process" is no longer the right framing for KAS: Crew reaches it
    through kiro-cli's ACP relay, so the child IS a kiro-cli. The strip still
    applies because the v3 engine resolves its tokens from kiro-cli's OIDC store
    (``--auth-method cli``) and never reads this variable — the test is what the
    child's engine consumes, not which binary it is.

    Matches the platform env-key convention (exact on POSIX, case-folded on
    Windows) so a differently-cased Windows spelling cannot slip past. Mutates
    *env* in place and returns it.
    """
    matched = [k for k in env if platform_compat.env_key_allowed(k, _KIRO_API_KEY_ONLY)]
    for k in matched:
        del env[k]
    return env


# Single-key allowlist for strip_kiro_cli_api_key's platform-aware matching.
_KIRO_API_KEY_ONLY = frozenset({CRED_KIRO_API_KEY})


def resolve_agent_config_path() -> Path:
    """Return defaults.json, preferring project-dir override for development.

    All modules that need the agent config path should call this instead
    of reimplementing the resolution chain.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        p = Path(proj) / "agents" / "defaults.json"
        if p.exists():
            return p
    return config_package_dir() / "defaults.json"


# ---------------------------------------------------------------------------
# Validation helpers — used by KiroCrewConfig.load()
# ---------------------------------------------------------------------------

# JSON Schema type → Python type names for log messages
_JSON_TYPE_LABELS: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def resolve_loop_stall_exit_after(
    dashboard_data: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Resolve the launch-class default while preserving explicit config.

    The distinction between an absent key and an explicit value exists only at
    config load. Managed services widen the absent-key default; every explicit
    operator value, including 25 seconds, is retained.
    """
    data = dashboard_data or {}
    if data.get("loop_stall_exit_after_secs") is not None:
        return _safe_int(
            data.get("loop_stall_exit_after_secs"),
            LOOP_STALL_EXIT_AFTER_DEFAULT,
            LOOP_STALL_EXIT_AFTER_MIN,
            LOOP_STALL_EXIT_AFTER_MAX,
        )
    source = os.environ if environ is None else environ
    # The generated service definition is the sole launch-class authority.
    # Inferring from systemd metadata is ambiguous because descendants inherit
    # INVOCATION_ID; old definitions are reported by ``kirocrew doctor`` with
    # the one-time regeneration command instead.
    managed = source.get(_MANAGED_SERVICE_ENV) == "1"
    return LOOP_STALL_EXIT_AFTER_MANAGED_DEFAULT if managed else LOOP_STALL_EXIT_AFTER_DEFAULT


def consume_managed_service_launch_environment(
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove and return the one-shot managed-service launch marker.

    The generated service definition sets this marker for the gateway itself.
    Consuming it before the dashboard starts app backends or child terminals
    prevents those descendants from being misclassified as managed services.
    """
    source = os.environ if environ is None else environ
    value = source.pop(_MANAGED_SERVICE_ENV, None)
    return {} if value is None else {_MANAGED_SERVICE_ENV: value}


def load_loop_stall_exit_after(
    environ: Mapping[str, str] | None = None,
) -> int:
    """Load the effective watchdog budget through the canonical config loader.

    The dataclass keeps an absent/null value as ``None`` rather than
    materializing a launch-specific number, so an unrelated ``save()`` cannot
    turn the managed 90-second default into an explicit desktop 25 seconds (or
    leak 90 seconds into a later desktop launch). The normal validated,
    overlay-aware loader remains the single config reader.
    """
    configured = KiroCrewConfig.load().dashboard.loop_stall_exit_after_secs
    dashboard_data = {} if configured is None else {"loop_stall_exit_after_secs": configured}
    return resolve_loop_stall_exit_after(dashboard_data, environ)


# (section, key, min, max) for each bounded field clamped at load time. The
# mins match the runtime floors: subagent_auto_max has a floor of 3
# (``subagent._LEGACY_DEFAULT_MAX`` — the auto-size minimum), so a value < 3 is
# clamped UP to 3 with a warning, mirroring the > ceiling clamp. max_subagents
# keeps a 0 floor here (0 = auto sentinel) — its 0-or-(>=3) rule is applied as a
# special case after the generic loop. Only out-of-range values are altered.
_SECURITY_BOUNDED_FIELDS: tuple[tuple[str, str, int, int], ...] = (
    ("agent", "subagent_auto_max", 3, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "max_subagents", 0, SUBAGENT_AUTO_MAX_CEILING),
    ("agent", "subagent_max_turns", 1, SUBAGENT_MAX_TURNS_CEILING),
    ("agent", "chat_turn_timeout_secs", CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX),
    (
        "agent",
        "session_start_timeout_secs",
        SESSION_START_TIMEOUT_MIN,
        SESSION_START_TIMEOUT_MAX,
    ),
    (
        "agent",
        "tool_approval_timeout_secs",
        TOOL_APPROVAL_TIMEOUT_MIN,
        TOOL_APPROVAL_TIMEOUT_MAX,
    ),
    (
        "dashboard",
        "loop_stall_exit_after_secs",
        LOOP_STALL_EXIT_AFTER_MIN,
        LOOP_STALL_EXIT_AFTER_MAX,
    ),
    (
        "dashboard",
        "chat_entry_cache_max_entries",
        CHAT_ENTRY_CACHE_ENTRIES_MIN,
        CHAT_ENTRY_CACHE_ENTRIES_MAX,
    ),
    (
        "dashboard",
        "chat_entry_cache_max_bytes",
        CHAT_ENTRY_CACHE_BYTES_MIN,
        CHAT_ENTRY_CACHE_BYTES_MAX,
    ),
    ("session", "pool_size", 0, POOL_SIZE_MAX),
)


def _log_config_clamp_event(field: str, file_value: int, clamped: int, lo: int, hi: int) -> None:
    """Emit a best-effort SEL security event for a clamped (tampered) config value.

    Recorded so tampering is detectable after the fact even though the loader
    self-heals by clamping. Lazily imports the SEL to avoid an import cycle and
    to keep the hot load() path free of SEL cost on the normal (in-range) path —
    this only fires when a value was actually out of range. Wrapped so a SEL
    failure can never make config loading raise.
    """
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="config_bounds_clamped",
                caller_identity="config_loader",
                agent="",
                source="background",
                operation="config.load",
                outcome="clamped",
                resources=field,
                metadata={
                    "file_value": file_value,
                    "clamped_to": clamped,
                    "min": lo,
                    "max": hi,
                },
            )
        )
    except Exception:
        logger.debug("SEL config-clamp event failed", exc_info=True)


def _clamp_security_bounds(data: dict) -> None:
    """Clamp security-relevant bounded integers in *data* in place.

    Applies the same ceilings the dashboard API enforces at write time to the
    values read from disk (see ``_SECURITY_BOUNDED_FIELDS`` and the module-level
    ceiling constants for the rationale). Called once on the actual disk-read
    path (cache miss) BEFORE the validated dict is cached, so:

    * subsequent cache hits already serve clamped values (consistent), and
    * the tamper warning / SEL event fires once per file change — enough to
      detect tampering without spamming the hot load() path.

    Only real integers are clamped; ``bool`` (a JSON ``true``/``false``) and any
    non-int are left untouched for the dataclass construction path to
    coerce/default. A clamp is logged at WARNING and recorded as a SEL security
    event; both are best-effort and never fatal (config loading must not raise).
    """
    for section, key, lo, hi in _SECURITY_BOUNDED_FIELDS:
        sect = data.get(section)
        if not isinstance(sect, dict) or key not in sect:
            continue
        val = sect[key]
        # bool is an int subclass; a JSON true/false is not a real bound value.
        if isinstance(val, bool) or not isinstance(val, int):
            continue
        if val < lo or val > hi:
            clamped = max(lo, min(hi, val))
            sect[key] = clamped
            logger.warning(
                "config %s.%s=%d out of range [%d, %d]; clamped to %d "
                "(possible config tampering — a direct file edit cannot exceed "
                "the API-enforced ceiling)",
                section,
                key,
                val,
                lo,
                hi,
                clamped,
            )
            _log_config_clamp_event(f"{section}.{key}", val, clamped, lo, hi)

    # max_subagents special case: 0 is the auto-size sentinel; any explicit pin
    # must be >= MAX_SUBAGENTS_FIXED_FLOOR. A stray 1/2 silently disables
    # auto-sizing AND runs below today's default, so clamp it UP to the floor
    # (0 is left intact). Runs after the generic [0, ceiling] range clamp above.
    agent = data.get("agent")
    if isinstance(agent, dict):
        ms = agent.get("max_subagents")
        if isinstance(ms, int) and not isinstance(ms, bool) and 0 < ms < MAX_SUBAGENTS_FIXED_FLOOR:
            agent["max_subagents"] = MAX_SUBAGENTS_FIXED_FLOOR
            logger.warning(
                "config agent.max_subagents=%d is below the fixed-pin floor of %d "
                "(0 = auto-size; an explicit pin must be >= %d); clamped UP to %d",
                ms,
                MAX_SUBAGENTS_FIXED_FLOOR,
                MAX_SUBAGENTS_FIXED_FLOOR,
                MAX_SUBAGENTS_FIXED_FLOOR,
            )
            _log_config_clamp_event(
                "agent.max_subagents",
                ms,
                MAX_SUBAGENTS_FIXED_FLOOR,
                MAX_SUBAGENTS_FIXED_FLOOR,
                SUBAGENT_AUTO_MAX_CEILING,
            )

    # tool_approval_timeout_secs cross-field case: the approval window must end
    # inside the turn that opened it. At or above the turn ceiling it can never
    # fire — the turn is cut first, so the user is told "this turn timed out"
    # while the real cause (nobody answered the approval prompt) is never named,
    # and an unattended run burns the entire ceiling on every prompt. Clamp to
    # APPROVAL_TURN_MARGIN_SECS below the ceiling. Runs after the generic range
    # clamp above, so both operands are already inside their declared bounds.
    if isinstance(agent, dict):
        window = agent.get("tool_approval_timeout_secs")
        ceiling = agent.get("chat_turn_timeout_secs", _DEFAULT_CHAT_TURN_TIMEOUT_SECS)
        if not isinstance(ceiling, int) or isinstance(ceiling, bool):
            ceiling = _DEFAULT_CHAT_TURN_TIMEOUT_SECS
        budget = max(TOOL_APPROVAL_TIMEOUT_MIN, ceiling - APPROVAL_TURN_MARGIN_SECS)
        if isinstance(window, int) and not isinstance(window, bool) and window > budget:
            agent["tool_approval_timeout_secs"] = budget
            logger.warning(
                "config agent.tool_approval_timeout_secs=%d leaves less than %ds "
                "under the %ds turn ceiling; clamped to %d. A window that outlives "
                "the turn can never fire: the turn is cut first and reports itself "
                "as a turn timeout, hiding the unanswered approval.",
                window,
                APPROVAL_TURN_MARGIN_SECS,
                ceiling,
                budget,
            )
            _log_config_clamp_event(
                "agent.tool_approval_timeout_secs",
                window,
                budget,
                TOOL_APPROVAL_TIMEOUT_MIN,
                budget,
            )


def _config_fingerprint() -> tuple:
    """Cheap signature of the config files — changes whenever either is edited.

    Uses st_mtime_ns + st_size + st_mode for both config.json and
    config.local.json so any edit, truncation, or replacement busts the cache.
    A missing file contributes a sentinel so create/delete also busts it.
    """
    sig: list = []
    for p in (config_path(), config_local_path()):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size, st.st_mode))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _cached_validated_data(fp: tuple | None = None) -> dict | None:
    """Return a deep copy of the cached validated config dict, or None on miss.

    Thin wrapper over the :class:`~kiro_crew.config.validation.ConfigCache`.
    ``_config_fingerprint`` stays in this module because it reads
    ``config_path()``/``config_local_path()``, which the test suite patches as
    ``kiro_crew.config.loader.config_path``.

    Pass *fp* when the caller has already computed the fingerprint, so one load
    costs a single stat pass instead of one per consumer of it. Omitting it
    stats, which suits a caller that has no fingerprint in hand.
    """
    return _CONFIG_CACHE.get(fp if fp is not None else _config_fingerprint())


def _store_validated_data(data: dict, fp: tuple) -> None:
    """Cache a deep copy of *data* under fingerprint *fp* (see ConfigCache.store)."""
    _CONFIG_CACHE.store(data, fp)


def _invalidate_config_cache() -> None:
    """Drop the cached validated config (called after save()/write-back)."""
    _CONFIG_CACHE.clear()


STT_PROVIDER_LOCAL = "local"

#: The recognisers a user can select. ``local`` runs whisper.cpp in-process,
#: ``apple`` uses macOS 26+ on-device recognition, and ``transcribe`` sends audio
#: to AWS Transcribe (billed, and gated on the AWS consent prompt). All three
#: produce partial results, so streaming is not a per-provider capability.
_VALID_STT_PROVIDERS = (STT_PROVIDER_LOCAL, "apple", "transcribe", "openai")

#: Providers a stored config may still name. Each of these needed an out-of-band
#: install the user had to perform themselves (a whisper CLI on ``PATH``, or an
#: ``mlx``/``faster-whisper`` wheel), which is precisely the cost the resident
#: local engine removes, so a stored value degrades to ``local`` instead of
#: leaving voice input pointing at something that is no longer dispatchable.
_RETIRED_STT_PROVIDERS = ("whisper", "mlx", "parakeet", "faster")

#: Model names accepted for ``stt.model``, derived from the catalog that owns the
#: download and its sha256 pin rather than restated here. Restating it is how the
#: advertised menu comes to offer a model that cannot be fetched.
_VALID_STT_MODELS = tuple(m.name for m in _STT_CATALOG)


_VALID_CHANNEL_PREFIXES = ("C", "D", "G")


def _validated_stt_provider(value: object) -> str:
    """Return *value* if it is selectable, else degrade to ``local`` with a reason.

    Degrades and logs; never raises. This value arrives from ``config.json``, so
    an unusable one must leave voice input working the way
    :func:`_normalize_acp_backend` degrades an unusable persisted backend, rather
    than failing the load that read it.
    """
    if value in _VALID_STT_PROVIDERS:
        return str(value)
    if value in _RETIRED_STT_PROVIDERS:
        logger.warning(
            "STT provider %r is retired; using %r instead. It needed a separate "
            "out-of-band install, which the bundled local engine removes while "
            "recognising the same speech.",
            value,
            STT_PROVIDER_LOCAL,
        )
    else:
        logger.warning(
            "Unknown STT provider %r; using %r instead. Selectable providers: %s",
            value,
            STT_PROVIDER_LOCAL,
            ", ".join(_VALID_STT_PROVIDERS),
        )
    return STT_PROVIDER_LOCAL


def _validated_stt_model(value: object) -> str:
    """Return the catalog name *value* selects, falling back to the default.

    Canonicalized here rather than passed through, so every consumer sees a name
    that names a real catalog entry: the model becomes a filename under the
    models directory, and an arbitrary string must not reach a path. ``resolve``
    also maps the names older configuration used onto their current entries, so a
    stored ``turbo`` keeps the model it asked for instead of silently moving to
    the default.
    """
    if not isinstance(value, str) or not value:
        logger.warning("Non-string STT model %r; using %r", value, _STT_DEFAULT_MODEL)
        return _STT_DEFAULT_MODEL
    return _resolve_stt_model(value).name


_VALID_COMPLETION_KEEP = ("head", "tail", "both")


def _validated_completion_keep(value: object) -> str:
    """Return *value* if it is one of head/tail/both, else raise ValueError."""
    if isinstance(value, str) and value in _VALID_COMPLETION_KEEP:
        return value
    raise ValueError(
        f"agent.completion_keep must be one of {list(_VALID_COMPLETION_KEEP)}, " f"got {value!r}"
    )


_YOLO_DURATION_SECS: dict[str, int] = {
    "30m": 1800,
    "1h": 3600,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
}
_YOLO_DURATION_DEFAULT = "6h"
# Not a timed value: an ad-hoc grant that stays on with no expiry until the
# gateway process stops. In-memory only, so it cannot survive a restart.
YOLO_UNTIL_SHUTDOWN = "until_shutdown"


def _read_skip_permissions(agent_data: dict) -> bool:
    """Read the standing auto-approve declaration, honouring older spellings.

    The key was renamed from ``yolo`` so the config itself warns about what it
    does. Canonical spelling is ``dangerously_skip_permissions`` — snake_case
    like every other key in this file, which is also what ``save()`` writes, so
    a save/load round-trip preserves it.

    Two other spellings are accepted on read, most-specific first:
    ``dangerouslySkipPermissions`` (the camelCase form used by other agent tools,
    so a config copied from one still works) and the legacy ``yolo`` (so no
    existing config silently loses auto-approve on upgrade).

    Requires a REAL ``bool``, not Python truthiness: a stringly-typed value
    from a templated/generated config — ``"false"``, ``"0"``, ``"no"``, or any
    other non-empty string a hand-edit or a config generator might write — is
    truthy in Python, so a bare ``bool(...)`` here would silently turn
    "explicitly disabled" into the standing, unattended tool-auto-approve
    grant this key controls. A non-bool value is never treated as an
    affirmative grant; it falls through to check the next spelling, then to
    the ``False`` default.
    """
    for key in ("dangerously_skip_permissions", "dangerouslySkipPermissions", "yolo"):
        if key in agent_data:
            value = agent_data[key]
            if isinstance(value, bool):
                return value
            logger.warning(
                "agent.%s must be a real boolean, got %r — treating as unset",
                key,
                value,
            )
    return False


def _normalize_yolo_duration(value: object) -> str:
    """Coerce ``agent.yolo_duration`` to a supported ad-hoc duration label.

    Anything unrecognised (typo, removed value, wrong type) falls back to the
    default rather than failing the whole config load — the value only widens or
    narrows an already-bounded ad-hoc grant, and the 24h ceiling on timed values
    is enforced independently in ``SafetyOverride``.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _YOLO_DURATION_SECS or v == YOLO_UNTIL_SHUTDOWN:
            return v
    return _YOLO_DURATION_DEFAULT


def yolo_duration_to_secs(label: str) -> int:
    """Seconds for a ``yolo_duration`` label; 0 means "no timed expiry"."""
    if label == YOLO_UNTIL_SHUTDOWN:
        return 0
    return _YOLO_DURATION_SECS.get(label, _YOLO_DURATION_SECS[_YOLO_DURATION_DEFAULT])


def _normalize_jail(value: object) -> str:
    """Coerce a persisted ``agent.jail`` value to a valid mode, deny-by-default.

    Valid persisted modes are ``auto`` / ``on`` / ``off``.  An unknown or
    non-string value normalizes to ``auto`` (the safe default — let the active
    edition decide; the public edition's jail provider is a no-op regardless).
    ``off`` per-invocation is expressed via ``--no-jail`` / ``KIROCREW_NO_JAIL``,
    not persisted config.
    """
    if isinstance(value, str) and value in _VALID_JAIL_MODES:
        return value
    return JAIL_MODE_AUTO


def _normalize_acp_backend(value: object) -> str:
    """Coerce a persisted ``agent.acp_backend`` to a backend this build can serve.

    Delegates to :func:`kiro_crew.acp_backends.resolve_selected_backend`, which owns
    the selectable registry, so the load path, the dashboard PATCH allowlist and the
    schema endpoint cannot disagree about which harnesses exist.

    The import is at module scope rather than deferred: ``acp_backends`` is a leaf
    that imports nothing from ``kiro_crew.acp``, so it does not reproduce the
    package-init cycle (``kiro_crew.acp.__init__`` -> client + runtime -> this
    module) that the old local import of ``acp.types`` existed to dodge.
    """
    return resolve_selected_backend(value)


def _validate_activation(value: str) -> str:
    """Return *value* if it is a valid activation mode, else ``mention`` (deny-by-default)."""
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_MENTION


#: The activation modes a Telegram forum Topic can express. A subset of
#: ``_VALID_ACTIVATIONS`` on purpose, and the subset is the point rather than an
#: omission: ``observe`` needs a channel-history buffer only Slack populates, and
#: feeding it would put non-owner prose into the prompt unfenced; ``review`` is a
#: whole second rendering mode built on Slack Block Kit ephemerals, which Telegram
#: has no equivalent for. Declaring either here would advertise a mode that
#: silently behaves like a different one.
TELEGRAM_ACTIVATIONS = frozenset({ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OFF})


def _validate_telegram_activation(value: str) -> str:
    """*value* if Telegram can express it, else ``mention``.

    Degrades to the NARROWER mode, matching ``WeixinTransport.authorize``'s
    treatment of an unrecognized ``dm_policy``: a malformed value must not resolve
    to the most permissive reading of itself. ``always`` starts a turn for every
    message in an allow-listed Topic, and a Topic is a SHARED space, so agent
    output lands in front of everyone in it. Widening that because a value failed
    to parse would make a typo grant participation the operator never asked for,
    and it fails silently in the direction nobody audits.

    ``mention`` rather than ``off`` because it is fail-safe without being
    fail-dead: an explicit ``@handle`` is an unambiguous request, so the operator
    can still reach the bot while it is refusing to answer unaddressed messages.

    Reached ONLY for a value that was present and unparseable. An ABSENT key is
    resolved to ``always`` by the caller before this runs, and that stays: taking
    the documented default is not the same act as asking for something specific
    and being misunderstood.
    """
    if value in TELEGRAM_ACTIVATIONS:
        return value
    logger.warning(
        "telegram.forum_activation=%r is not one of %s; using %r (the narrower mode, "
        "so an unreadable value cannot widen who the bot answers).",
        value,
        ", ".join(repr(a) for a in sorted(TELEGRAM_ACTIVATIONS)),
        ACTIVATION_MENTION,
    )
    return ACTIVATION_MENTION


def _validate_tracking_channels(raw: list) -> list[dict]:
    """Validate and coerce tracking_channels entries.

    Accepted formats:
    - ``{"channel_id": "C...", "name": "..."}`` — passed through
    - ``"C..."`` (bare string) — auto-coerced to ``{"channel_id": "C..."}`` with a warning

    Rejects entries that are neither strings starting with C/D/G nor dicts with channel_id.
    """
    if not raw:
        return []
    result: list[dict] = []
    coerced = 0
    rejected = 0
    for entry in raw:
        if isinstance(entry, dict) and entry.get("channel_id"):
            result.append(entry)
        elif isinstance(entry, str) and len(entry) > 1 and entry[0] in _VALID_CHANNEL_PREFIXES:
            result.append({"channel_id": entry})
            coerced += 1
        else:
            rejected += 1
    if coerced:
        logger.warning(
            "Config: slack.tracking_channels has %d bare string(s) — auto-coerced to "
            '{"channel_id": "..."} format. Prefer: [{"channel_id": "C...", "name": "..."}]',
            coerced,
        )
    if rejected:
        logger.warning(
            "Config: slack.tracking_channels has %d invalid entries (expected objects with "
            '"channel_id" field or bare channel ID strings starting with C/D/G). '
            "These entries were ignored.",
            rejected,
        )
    return result


def _migrate_workspaces(raw_workspaces: dict) -> dict[str, WorkspaceConfig]:
    """Auto-migrate workspaces from flat or structured format.

    - String values → WorkspaceConfig(dir=value)
    - Dict values with ``dir`` key → WorkspaceConfig(dir=value["dir"])
    - Non-string/non-dict values → default WorkspaceConfig()
    - Empty input → {"default": WorkspaceConfig(dir="workspace")}
    """
    result: dict[str, WorkspaceConfig] = {}
    for name, value in raw_workspaces.items():
        if isinstance(value, str):
            result[name] = WorkspaceConfig(dir=value)
        elif isinstance(value, dict):
            result[name] = WorkspaceConfig(dir=value.get("dir", "workspace"))
        else:
            result[name] = WorkspaceConfig()
    if not result:
        result["default"] = WorkspaceConfig(dir="workspace")
    return result


def resolve_memory_store_config(
    top_level_memory: dict,
    store_overrides: dict,
) -> dict:
    """Deep-merge store overrides onto top-level memory defaults.

    Merge happens at the raw dict level BEFORE dataclass construction.
    A store that only sets embedding_provider inherits all other memory
    settings from the top-level config, not from MemoryConfig defaults.
    """
    merged = dict(top_level_memory)
    for key, value in store_overrides.items():
        if key == "description":
            continue  # description is store-only metadata, not a memory setting
        if value != "" and value is not None:
            merged[key] = value
    return merged


@dataclass
class ResolvedBindings:
    """Resolved workspace, memory store, and kiro agent for a session."""

    workspace_dir: Path
    memory_store_name: str
    effective_memory_config: dict
    kiro_agent: str
    # The KiroCrew agent's own default model, "" when it pins none. Ranks below
    # a per-session pick and above the bound kiro agent's pin / the global
    # agent.model fallback. Defaulted so existing keyword constructions and
    # test doubles built before this field stay valid.
    model: str = ""
    # Whether the REQUESTED agent name was actually honored. False means the
    # resolver fell back to the default agent, so dispatching these bindings runs
    # a different agent than the caller asked for. Callers that store the
    # requested name (chat slots) must not advertise it when this is False.
    # Defaults True so constructions predating this field keep their meaning.
    requested_resolved: bool = True
    # The KiroCrew ALIAS whose bindings these are ("" when no alias applied). A
    # caller replacing an unhonored request must store THIS, not ``kiro_agent``:
    # the stored value is re-resolved later and an alias is matched first, so a
    # physical kiro agent name that also happens to be an alias key would resolve
    # to that alias's target instead — reintroducing the advertised-vs-answering
    # mismatch. An alias key round-trips to itself.
    resolved_alias: str = ""

    def same_dispatch_binding(self, other: "ResolvedBindings") -> bool:
        """Whether two resolutions name the SAME dispatch target.

        Owned here, next to the field set, so a future dispatch-relevant
        binding field forces the identity question at the layer that defines
        it rather than silently widening a permission check that enumerated
        fields by hand (the dashboard's slot agent-conflict guard uses this to
        decide whether two different NAMES may share a slot). Compares every
        field that changes what answers a turn — the kiro agent, workspace,
        memory store, and model — and deliberately not ``resolved_alias``
        (two names resolving to one alias's target ARE the same binding) or
        ``requested_resolved``/``effective_memory_config`` (the former is
        request metadata the caller checks separately; the latter is derived
        from ``memory_store_name`` plus global config shared by both sides).
        """
        return (
            self.kiro_agent == other.kiro_agent
            and self.workspace_dir == other.workspace_dir
            and self.memory_store_name == other.memory_store_name
            and self.model == other.model
        )


@dataclass
class SttConfig:
    """Speech-to-text configuration.

    Enabled by default. Recognition runs on this machine through the bundled
    engine, so having voice input available costs one model download the first
    time it is used and nothing after that.
    """

    enabled: bool = field(
        default=True,
        metadata=_meta("Enabled", "Turn spoken input into text you can send."),
    )
    provider: str = field(
        default=STT_PROVIDER_LOCAL,
        metadata=_meta(
            "Provider",
            "Where speech is recognised. `local` runs on this machine and needs no "
            "account (it downloads one model the first time you dictate), `apple` "
            "uses the on-device recogniser built into macOS 26 and later, and "
            "`transcribe` sends your audio to AWS Transcribe, which bills your AWS "
            "account.",
            enum=list(_VALID_STT_PROVIDERS),
        ),
    )
    model: str = field(
        default=_STT_DEFAULT_MODEL,
        metadata=_meta(
            "Model",
            "Which speech model the local provider downloads and runs. Bigger is "
            "more accurate and a longer first-time download: `tiny` on a machine "
            "short of memory, `base` for everyone, `small` when accents or jargon "
            "are being misheard, `large-v3-turbo` for the best accuracy available.",
            enum=list(_VALID_STT_MODELS),
        ),
    )
    language_code: str = field(
        default="en-US",
        metadata=_meta(
            "Language Code", "Language for speech recognition (e.g. en-US, fr-FR, es-ES)."
        ),
    )
    streaming: bool = field(
        default=True,
        metadata=_meta(
            "Streaming",
            "Show words in the message box while you are still speaking rather than "
            "only once you stop. Every provider supports it; turning it off spends "
            "less CPU on the local provider and fewer API calls on `transcribe`.",
        ),
    )
    silence_ms: int = field(
        default=_STT_DEFAULT_SILENCE_MS,
        metadata=_meta(
            "End-of-phrase silence",
            "How long a pause has to last, in milliseconds, before what you said is "
            "treated as a finished phrase. Raise it if you are being cut off "
            "mid-sentence; lower it if the text lags behind you.",
        ),
    )
    partial_interval_ms: int = field(
        default=_STT_DEFAULT_PARTIAL_INTERVAL_MS,
        metadata=_meta(
            "Live update interval",
            "How often the live transcript is refreshed while you speak, in "
            "milliseconds. Lower feels more immediate and costs a little more CPU "
            "per second of speech; higher is steadier to read.",
        ),
    )
    idle_evict_secs: int = field(
        default=_STT_DEFAULT_IDLE_EVICT_SECS,
        metadata=_meta(
            "Release model after",
            "How long the local model stays loaded in memory after your last "
            "recording, in seconds. It holds roughly 150 MB at the default model, "
            "and reloading it takes a fraction of a second, so lower this on a "
            "machine short of memory. 0 releases it as soon as you stop speaking.",
        ),
    )
    endpointing: bool = field(
        default=False,
        metadata=_meta(
            "Semantic endpointing",
            "While dictating, run a fast background model on each finished phrase to "
            "detect when you have asked a complete question, then send it without "
            "you pressing anything. Needs streaming; off by default.",
        ),
    )
    dictation_panel: bool = field(
        default=True,
        metadata=_meta(
            "Dictation Panel",
            "Show the animated dictation panel while recording instead of the thin status bar. "
            "Ignored when the browser lacks WebGL2 or the OS requests reduced motion — both "
            "fall back to the status bar.",
        ),
    )
    timeout_secs: int = field(
        default=300,
        metadata=_meta("Timeout", "Transcription timeout in seconds."),
    )
    transcribe_region: str = field(
        default="us-east-1",
        metadata=_meta("Transcribe Region", "AWS region for Transcribe API."),
    )
    transcribe_profile: str = field(
        default="",
        metadata=_meta("Transcribe Profile", "AWS profile for Transcribe API."),
    )
    openai_api_key: str = field(
        default="",
        metadata=_meta("OpenAI API Key", "API key for OpenAI STT provider."),
    )
    openai_model: str = field(
        default="whisper-1",
        metadata=_meta("OpenAI Model", "Model name for OpenAI STT provider."),
    )


@dataclass
class ComputerUseConfig:
    """Computer-use DISPLAY and LIMIT knobs — deliberately no ``enabled`` field.

    The primary enable is NOT here. It lives on the keystone
    ``computer_use.json`` (see :func:`computer_use_state_path`) because turning
    computer use on grants full desktop observation plus input synthesis, which
    is a security ceiling rather than a preference: ``config.json`` is writable
    by an auto-approved agent shell (``is_sensitive_bash_command`` does NOT block
    ``echo … > config.json``), so an enable stored here could be flipped by
    prompt injection. Adding an ``enabled`` field to this dataclass would
    silently re-open that hole — do not.

    Everything modelled here is safe for the agent to read and, at worst,
    annoying for it to change: how many accessibility nodes one walk returns, how
    deep it goes, how much text per node, and the screenshot's size/quality. The
    ceilings (``*_LIMIT`` in ``computer_use.types``) are enforced independently by
    the MCP tool schemas, so a hand-edited config cannot ask for an unbounded
    walk.
    """

    max_tree_nodes: int = field(
        default=_CU_DEFAULT_MAX_TREE_NODES,
        metadata=_meta(
            "Max Tree Nodes",
            "Accessibility nodes one window walk may return before truncating.",
        ),
    )
    max_tree_depth: int = field(
        default=_CU_DEFAULT_MAX_TREE_DEPTH,
        metadata=_meta("Max Tree Depth", "How deep one accessibility walk descends."),
    )
    text_limit: int = field(
        default=_CU_DEFAULT_TEXT_LIMIT,
        metadata=_meta("Text Limit", "Characters kept per element title/value."),
    )
    attach_screenshot: bool = field(
        default=_CU_DEFAULT_ATTACH_SCREENSHOT,
        metadata=_meta(
            "Attach Screenshots",
            "Capture the target window and relay the image path alongside the tree. "
            "The accessibility tree is always the primary channel.",
        ),
    )
    screenshot_max_px: int = field(
        default=_CU_DEFAULT_SCREENSHOT_MAX_PX,
        metadata=_meta(
            "Screenshot Width",
            "Longest edge of the downscaled screenshot, in pixels.",
        ),
    )
    screenshot_jpeg_quality: int = field(
        default=_CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
        metadata=_meta("Screenshot Quality", "JPEG quality 1-100 for the screenshot."),
    )
    cursor_motion: bool = field(
        default=False,
        metadata=_meta(
            "Cursor Motion",
            "Draw a visible cursor gliding to each target before a real-pointer "
            "click, so the operator can see what the agent is doing. macOS only; "
            "purely visual and never a permit — the drawn cursor is not the pointer, "
            "and turning this on grants no new capability.",
        ),
    )


@dataclass
class McpGatewayConfig:
    """Sidecar MCP broker daemon — shares MCP backends across sessions."""

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Share MCP Backends",
            "Let sessions with an identical server configuration share one MCP "
            "server process instead of each getting its own. Off, every session "
            "gets its own backend — the same process topology as running without "
            "the broker. Either this or MCP Apps starts the broker; see "
            "docs/architecture/design-notes/mcp-stub-decoupling.md. "
            "Default False — opt-in.",
        ),
    )
    apps_enabled: bool = field(
        default=True,
        metadata=_meta(
            "MCP Apps (retired, opt-out still honoured)",
            "RETIRED GOING FORWARD, but a stored `false` KEEPS ITS OPT-OUT. Nothing "
            "writes this key any more and MCP Management does not surface it: MCP "
            "Apps capability follows whether a server gets a stub, because the stub "
            "is what carries the render and callback path, so a preference cannot "
            "grant it. It can still WITHHOLD it — a released version treated "
            "`false` here as a trustworthy opt-out, so an operator who turned MCP "
            "Apps off stays off (tightest-wins: it beats KIROCREW_MCP_APPS=1, and an "
            "unreadable config fails closed). Absent defaults True, so 'not "
            "configured' is not an opt-out. To GET server-authored UI, turn on the "
            "server's stub in MCP Management — and clear a stored `false` here if "
            "you have one. The only other MCP Apps preference is where it renders "
            "(dashboard.mcp_app_panel). "
            "See docs/architecture/design-notes/mcp-stub-decoupling.md.",
        ),
    )
    forward_declared_env: bool = field(
        default=True,
        metadata=_meta(
            "Forward Declared Env",
            "Apply a pooled server's declared env (mcpServers.<name>.env) to the "
            "shared backend. Only non-secret keys are forwarded — rotating-secret "
            "and credential-prefixed keys are never applied to a shared backend, "
            "and gatewayd re-hashes the sidecar at spawn and forwards nothing on "
            "mismatch, so every forwarded key is one all co-tenants of that "
            "backend declared identically. Turn it OFF to make an env-declaring "
            "server run unwrapped (no stub, no pooling) instead.",
        ),
    )
    socket_path: str = field(
        default="",
        metadata=_meta(
            "Socket Path",
            "Local endpoint for the broker. Empty -> "
            "$KIROCREW_HOME/mcp-gateway/gateway.sock. A unix socket at this path "
            "on POSIX; on Windows the path is not created, it only derives the "
            "named-pipe name and locates the lock file beside it.",
        ),
    )
    overlay_dir: str = field(
        default="",
        metadata=_meta(
            "Overlay Dir",
            "Directory of rewritten agent JSON. Broker stubs from these specs are "
            "injected into each kiro-cli session via ACP session/new. "
            "Empty -> $KIROCREW_HOME/mcp-gateway/agents.",
        ),
    )
    idle_timeout_secs: int = field(
        default=300,
        metadata=_meta("Idle Timeout", "Seconds a refcount=0 MCP backend is kept before drain."),
    )
    resolve_once_refresh_hours: int = field(
        default=24,
        metadata=_meta(
            "Pre-resolve Refresh",
            "Hours before an UNPINNED npm-launcher MCP server (an npx spec at "
            "@latest, a range, or no version) is re-resolved from the registry. "
            "Pre-resolving lets a launch exec the installed tree directly, so "
            "session start does no dependency resolution and needs no network; "
            "this is how often that resolution is refreshed so such a spec still "
            "tracks upstream. A spec pinned to an exact version ignores this -- "
            "re-asking about an exact version cannot change the answer. 0 "
            "re-resolves on every prefetch pass; a server with no resolution yet "
            "simply launches the way it does today.",
        ),
    )
    max_backends: int = field(
        default=64,
        metadata=_meta(
            "Max Backends",
            "Max concurrent pooled MCP backends before the pool refuses a new one. "
            "Must be >= the number of distinct (agent x server) backends that can be "
            "live at once: each agent keeps its own backend per server, so N concurrent "
            "agents with ~S servers each need N*S slots. Bounded by design: idle "
            "backends drain after idle_timeout_secs, so steady-state RAM tracks real "
            "concurrency, not this ceiling.",
        ),
    )
    stub_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Routed Servers",
            "MCP server names given a stub. The stub interposes a "
            "stub, which is what makes server-authored UI (MCP Apps) and backend "
            "sharing possible for that server — so it is the one per-server "
            "decision. Empty by default: an unstubbed server is launched by the "
            "session itself, the same process topology as running without the "
            "broker, and an empty list means no broker runs at all. Whether "
            "stubbed servers SHARE one backend is the separate global switch "
            "(mcp_gateway.enabled). Managed from MCP Management.",
        ),
    )
    poolable_servers: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Poolable Servers (deprecated)",
            "DEPRECATED alias for stub_servers. Read only when stub_servers "
            "is absent, so a config written before the stub became the per-server "
            "decision keeps working: a server that was pooled already had a stub, "
            "so migrating it to the stub set preserves its behaviour. There is no "
            "per-server sharing switch any more — sharing is global over the "
            "stub set.",
        ),
    )
    pool_identity_env: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Pool Identity Env Keys",
            "Env variable NAMES whose value is part of a shared backend's "
            "identity. Names listed here are folded into the backend's env hash "
            "even when they look like a rotating secret (AWS_SECRET*, "
            "AWS_SESSION*, OAUTH*), which is what makes them safe to apply to a "
            "shared backend: two sessions declaring different values get "
            "different backends instead of colliding onto one. Use it to let a "
            "server that authenticates from such a variable be shared at all — "
            "by default it declares one, so nothing is forwarded and the server "
            "runs unwrapped. The cost is the reason the exclusion exists: "
            "rotating a named value re-partitions that server's pool, so the "
            "next session cold-starts a backend. Exact names, not prefixes. "
            "Names the daemon's own credential scrub removes (AWS_ACCESS*, "
            "AWS_SECRET*, AWS_SESSION*, SSH_AUTH_SOCK*, GNUPGHOME*, "
            "GIT_ASKPASS*) are ignored here — that scrub is a separate, broader "
            "guard this setting does not lift. Empty by default.",
        ),
    )
    prewarm_count: int = field(
        default=0,
        metadata=_meta(
            "Prewarm Count",
            "Number of hottest observed (agent x server x channel) MCP backends "
            "to spawn at gateway startup, before the first session connects. "
            "Removes the cold-start latency on the first new-chat after a "
            "gateway restart or after all backends have idled out — the steady "
            "state already reuses warm backends within the idle timeout. The "
            "hot set is learned from prior registers and persisted beside the "
            "socket; channel_id is a stable id, so a prewarmed backend is "
            "reused by every later new-chat in that channel. 0 (default) "
            "disables prewarming — no hot-key file is read or written.",
        ),
    )
    read_buffer_limit_bytes: int = field(
        default=64 * 1024 * 1024,
        metadata=_meta(
            "Read Buffer Limit",
            "Maximum bytes for a single MCP response line before asyncio drops it. "
            "Default 64 MiB. Responses exceeding this are fast-failed with -32000. "
            "Env override: KIROCREW_MCP_READ_LIMIT.",
        ),
    )
    response_spill_threshold_bytes: int = field(
        default=256 * 1024,
        metadata=_meta(
            "Response Spill Threshold",
            "Tool-call responses larger than this (bytes) have their text content "
            "written to ~/.kiro/crew/mcp_spill/ and truncated inline to 16 KiB + "
            "a file path marker. Default 256 KiB. Set 0 to disable spilling. "
            "Env override: KIROCREW_MCP_SPILL_THRESHOLD.",
        ),
    )


# The forwarding default assumed when config omits
# ``mcp_gateway.forward_declared_env``. Read from the dataclass default so the
# field and every parse-site fallback cannot drift apart: this default is read
# in three places (the field, the loader's ``_safe_bool`` fallback, and the
# dashboard stub-batch reader), and a reader disagreeing with the field makes the
# batch skip servers the rewrite pools perfectly well.
FORWARD_DECLARED_ENV_DEFAULT = bool(
    McpGatewayConfig.__dataclass_fields__["forward_declared_env"].default  # type: ignore[arg-type]
)


@dataclass
class McpConfig:
    """MCP server settings that apply whether or not the broker is enabled.

    Distinct from :class:`McpGatewayConfig`, which configures the sharing broker
    itself: these settings govern how MCP servers are FOUND and launched, so
    they matter equally with the broker off.
    """

    extra_path_dirs: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Extra MCP Binary Directories",
            "Additional directories to search for MCP server binaries, ahead of "
            "the built-in locations. Add one when a package manager installs its "
            "MCP launchers somewhere Kiro Crew does not know about: a server "
            "declared by bare name that resolves nowhere never starts, and the "
            "session just comes up short of tools. Each entry must be a single "
            "absolute directory (``~`` is expanded); anything else is ignored "
            "with a warning. These directories are prepended to the search path "
            "used by the MCP probe, the agent-config command resolver, and the "
            "broker's rewriter alike, so a binary found here is found "
            "everywhere. They do NOT join the search for the agent runtime "
            "itself, which must not be shadowable by a configured directory.",
        ),
    )


@dataclass
class InstancesConfig:
    """Multi-instance management (the *Instances* feature).

    Gates and tunes the gateway's ability to manage/switch between several
    remote KiroCrew instances over SSH tunnels. Off by default — opt-in only,
    since enabling it allows the gateway to open SSH ``-L`` forwards and relaxes
    the dashboard CSP ``frame-src`` for the active loopback tunnel ports.

    Numeric transport defaults and bounds live in
    ``kiro_crew.instances.constants`` so their canonical values cannot drift
    from this dataclass.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable multi-instance management — lets this gateway open SSH tunnels "
            "to remote Kiro Crews and embed their dashboards. Default off (opt-in). "
            "Enabling also scopes a CSP frame-src relaxation to active tunnel ports.",
        ),
    )
    warm_set_cap: int = field(
        default=_DEFAULT_WARM_SET_CAP,
        metadata=_meta(
            "Warm Set Cap",
            "Max number of remote instances kept warm (iframe mounted + tunnel live) "
            "at once. Least-recently-used instances beyond this are evicted and "
            "reconnected on demand. Bounds memory/socket use (each warm instance is a "
            "full dashboard SPA).",
        ),
    )
    tunnel_base_port: int = field(
        default=_DEFAULT_TUNNEL_BASE_PORT,
        metadata=_meta(
            "Tunnel Base Port",
            "First local loopback port used for an SSH -L forward. The allocator "
            "increments from here, skipping ports already in use.",
        ),
    )
    ssh_compression: bool = field(
        default=_DEFAULT_SSH_COMPRESSION,
        metadata=_meta(
            "SSH Compression",
            "Enable SSH transport compression (ssh -C) on instance tunnels. The "
            "remote dashboard SPA bundle plus all API/WebSocket traffic travel over "
            "this forwarded stream and are highly compressible; the gateway does not "
            "gzip HTTP responses, so this is the only compression in the path. "
            "Default on (best for a dedicated remote host over a slow link); turn off "
            "on a fast/local link where compression CPU outweighs the bandwidth win.",
        ),
    )
    connect_timeout_secs: float | None = field(
        default=None,
        metadata=_meta(
            "Connect Timeout (secs)",
            "How long to wait for the local forward port to accept connections "
            "before declaring a connect attempt failed. When unset, SSH uses "
            "15s and SSM uses 25s. Fifteen seconds is sufficient for a direct "
            "ssh TCP connect, but hosts behind a "
            "ProxyCommand or jump host routinely need longer (the proxy handshake "
            "runs before ssh begins the forward). Raise this if connecting a "
            "remote instance times out while the same ssh forward succeeds by hand. "
            "An explicit value applies to both transports. Clamped to [1, 120].",
        ),
    )
    mint_timeout_secs: float | None = field(
        default=None,
        metadata=_meta(
            "Mint Timeout (secs)",
            "How long to wait for the remote `kirocrew token` mint to return "
            "before failing a connect. When unset, SSH uses 30s and SSM uses "
            "90s (its dispatch latency is higher). The mint runs over the same "
            "ssh transport as the tunnel, so a host behind a ProxyCommand or "
            "jump host pays the proxy handshake here too. An explicit value "
            "applies to both transports, so size it for the slowest transport "
            "you use. Clamped to [10, 120].",
        ),
    )
    max_recovery_attempts: int = field(
        default=_DEFAULT_MAX_RECOVERY,
        metadata=_meta(
            "Max Recovery Attempts",
            "Consecutive self-heal attempts before a dropped tunnel is left "
            "disconnected. With the capped-exponential backoff, the default 8 spans a "
            "~2 min recovery window, enough to outlast a transient drop (screen lock, "
            "proxy warmup) before giving up.",
        ),
    )
    recover_backoff_max_secs: float = field(
        default=_DEFAULT_BACKOFF_MAX,
        metadata=_meta(
            "Recover Backoff Cap (secs)",
            "Cap on the per-attempt backoff between self-heal attempts. The wait grows "
            "1, 2, 4, 8, 16 then holds at this cap; raising it spaces retries further "
            "across a slow reconnect.",
        ),
    )
    probe_failure_threshold: int = field(
        default=_DEFAULT_PROBE_FAILS,
        metadata=_meta(
            "Probe Failure Threshold",
            "Consecutive health-probe failures before a connected-but-not-forwarding "
            "(zombie) tunnel is torn down to trigger self-heal.",
        ),
    )

    def __post_init__(self) -> None:
        if self.warm_set_cap < 1:
            logger.warning("instances.warm_set_cap %d < 1, using 1", self.warm_set_cap)
            object.__setattr__(self, "warm_set_cap", 1)
        if not (1 <= self.tunnel_base_port <= 65535):
            logger.warning(
                "instances.tunnel_base_port %d out of range [1, 65535], using %d",
                self.tunnel_base_port,
                _DEFAULT_TUNNEL_BASE_PORT,
            )
            object.__setattr__(self, "tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT)
        if self.connect_timeout_secs is not None and self.connect_timeout_secs < 1.0:
            logger.warning(
                "instances.connect_timeout_secs %s < 1, using the transport default",
                self.connect_timeout_secs,
            )
            object.__setattr__(self, "connect_timeout_secs", None)
        elif (
            self.connect_timeout_secs is not None
            and self.connect_timeout_secs > _CONNECT_TIMEOUT_CEILING
        ):
            logger.warning(
                "instances.connect_timeout_secs %s > %s, clamping to %s",
                self.connect_timeout_secs,
                _CONNECT_TIMEOUT_CEILING,
                _CONNECT_TIMEOUT_CEILING,
            )
            object.__setattr__(self, "connect_timeout_secs", _CONNECT_TIMEOUT_CEILING)
        if self.mint_timeout_secs is not None and self.mint_timeout_secs < _MINT_TIMEOUT_FLOOR:
            logger.warning(
                "instances.mint_timeout_secs %s < %s, using the transport default",
                self.mint_timeout_secs,
                _MINT_TIMEOUT_FLOOR,
            )
            object.__setattr__(self, "mint_timeout_secs", None)
        elif self.mint_timeout_secs is not None and self.mint_timeout_secs > _MINT_TIMEOUT_CEILING:
            logger.warning(
                "instances.mint_timeout_secs %s > %s, clamping to %s",
                self.mint_timeout_secs,
                _MINT_TIMEOUT_CEILING,
                _MINT_TIMEOUT_CEILING,
            )
            object.__setattr__(self, "mint_timeout_secs", _MINT_TIMEOUT_CEILING)
        if self.max_recovery_attempts < 1:
            logger.warning(
                "instances.max_recovery_attempts %d < 1, using %d",
                self.max_recovery_attempts,
                _DEFAULT_MAX_RECOVERY,
            )
            object.__setattr__(self, "max_recovery_attempts", _DEFAULT_MAX_RECOVERY)
        elif self.max_recovery_attempts > _MAX_RECOVERY_CEILING:
            logger.warning(
                "instances.max_recovery_attempts %d > %d, clamping to %d "
                "(guards against a near-infinite self-heal loop on a dead connection)",
                self.max_recovery_attempts,
                _MAX_RECOVERY_CEILING,
                _MAX_RECOVERY_CEILING,
            )
            object.__setattr__(self, "max_recovery_attempts", _MAX_RECOVERY_CEILING)
        if self.recover_backoff_max_secs <= 0:
            logger.warning(
                "instances.recover_backoff_max_secs %s <= 0, using %s",
                self.recover_backoff_max_secs,
                _DEFAULT_BACKOFF_MAX,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX)
        elif self.recover_backoff_max_secs > _RECOVER_BACKOFF_CEILING:
            logger.warning(
                "instances.recover_backoff_max_secs %s > %s, clamping to %s "
                "(guards against a multi-day self-heal window on a dead connection)",
                self.recover_backoff_max_secs,
                _RECOVER_BACKOFF_CEILING,
                _RECOVER_BACKOFF_CEILING,
            )
            object.__setattr__(self, "recover_backoff_max_secs", _RECOVER_BACKOFF_CEILING)
        if self.probe_failure_threshold < 1:
            logger.warning(
                "instances.probe_failure_threshold %d < 1, using %d",
                self.probe_failure_threshold,
                _DEFAULT_PROBE_FAILS,
            )
            object.__setattr__(self, "probe_failure_threshold", _DEFAULT_PROBE_FAILS)


@dataclass
class HeartbeatConfig:
    """Heartbeat background task queue (~/.kiro/crew/workspace/HEARTBEAT.md)."""

    default_deliver: str = field(
        default="slack",
        metadata=_meta(
            "Default delivery",
            "Where a heartbeat completion with no inline <!-- deliver:... --> tag is "
            "routed: 'slack' (Slack DM + dashboard bell, the default) or 'dashboard' "
            "(dashboard slot + bell only, no Slack). Per-task deliver tags always "
            "override this.",
        ),
    )


@dataclass
class WatchdogConfig:
    """ACP per-session watchdog / liveness-oracle tuning (acp/session_handle.py).

    Wellness (the liveness oracle) is the primary detector; these windows govern
    only the UNKNOWN-verdict backstop class. A WORKING verdict is never acted on
    at any elapsed time, and every watchdog action is non-lethal (auto-recovery,
    never a silent kill).
    """

    check_after_secs: float = field(
        default=60.0,
        metadata=_meta(
            "Check after (s)",
            "Idle seconds on a turn before the liveness oracle is consulted at all. "
            "Below this, the dispatch loop does no watchdog work.",
        ),
    )
    stale_window_secs: float = field(
        default=300.0,
        metadata=_meta(
            "Stale probe window (s)",
            "Idle seconds before an UNKNOWN-verdict model-wait turn is safe-probed "
            "via session/cancel. Probes are non-lethal: a live turn auto-recovers.",
        ),
    )
    tool_stall_suspect_secs: float = field(
        default=3600.0,
        metadata=_meta(
            "Tool stall suspect (s)",
            "Idle seconds before an UNKNOWN-verdict in-flight tool is cancelled and "
            "the turn routed to tool-stall recovery (continue-nudge, no re-run of "
            "the original message). WORKING tools (e.g. a matched live build child) "
            "are never cancelled regardless of duration. Default 1h: generous enough "
            "for long builds and MCP tools on macOS, where the liveness oracle "
            "degrades (no /proc) and cannot distinguish a live build from a stall, "
            "while still landing inside the turn's own ceiling "
            "(agent.chat_turn_timeout_secs) so recovery is reachable. Enforcement is "
            "at handle construction, not config load: a window past the headroom "
            "fraction of the transport's per-prompt timeout is clamped with a "
            "warning, while one that merely exceeds agent.chat_turn_timeout_secs is "
            "warned about but left as set, because the same handle also serves "
            "callers that pass a larger prompt timeout (review and cron turns).",
        ),
    )
    tool_stall_hard_cap_secs: float = field(
        default=3600.0,
        metadata=_meta(
            "Hard cap (s)",
            "Absolute ceiling for UNKNOWN-verdict forbearance (e.g. the extended "
            "probably-thinking window). Applies ONLY to UNKNOWN verdicts — never "
            "to a WORKING session, which is deferred before this cap is consulted "
            "and is therefore bounded only by the turn's own ceiling. Default 1h, "
            "clamped against the transport's per-prompt timeout like the suspect "
            "window.",
        ),
    )
    model_silent_probe_secs: float = field(
        default=900.0,
        metadata=_meta(
            "Silent-think probe window (s)",
            "Extended probe window for a model-wait with an established backend "
            "connection but flat counters (non-streamed server-side reasoning, "
            "e.g. long xhigh thinks). Probing a live think cancels and regenerates "
            "it, so this window is deliberately generous.",
        ),
    )
    wellness_sample_secs: float = field(
        default=3.0,
        metadata=_meta(
            "Wellness sample interval (s)",
            "Minimum spacing between CPU/IO counter samples used for movement "
            "deltas in the liveness oracle.",
        ),
    )


# Keys whose out-of-domain value has already been reported, so a knob read once
# per spawn warns once per process instead of once per agent launch. Same shape
# as ``_OBSERVED_DEGRADED_SECTIONS``; exposed for tests to reset.
_WARNED_RESOURCE_LIMIT_KEYS: set[str] = set()


def _limit_int(value: object, key: str, *, lo: int, hi: int | None = None) -> int | None:
    """Coerce one ``resource_limits`` value, or ``None`` when it is out of domain.

    ``None`` means "no usable value here" and is deliberately NOT a number: each
    mechanism's fallback is its own documented default (``_RLIMIT_DEFAULTS`` for
    the rlimit path, ``_CGROUP_DEFAULT_*`` for the cgroup paths), and those must
    stay where they are rather than being copied into this dataclass as a third
    default set.

    The coercion rules, and why each one is what it is:

    - ``bool`` is not a number here. ``True`` would otherwise coerce to ``1`` and
      set a one-process / one-MB ceiling, which kills the child it limits.
    - A non-integral float TRUNCATES toward zero (``512.5`` -> ``512``), matching
      what every pre-existing reader did, so tightening the parse cannot loosen
      an operator's ceiling.
    - EXCEPT when it truncates to ``0``, either sign: ``0.5`` is not a request to
      disable the limit, but ``int(0.5)`` is exactly the value that means
      "disabled" on the rlimit path and "use the default" on the cgroup path.
      That silent reinterpretation is the trap in #3474, so it is refused.
    - NaN and +/-Infinity are refused before ``int()`` sees them. ``json.loads``
      accepts both literals, and ``int(inf)`` raises ``OverflowError`` --
      uncaught on the rlimit path, which turned a typo into a failure of every
      spawn.
    - Out of range REFUSES rather than clamps, and is checked on the value AS
      WRITTEN rather than on the truncated result. A clamp would silently move a
      confinement ceiling away from the number the operator can read in their own
      file; checking after truncation would let a value below the floor land back
      inside it (``int(-0.5) == 0`` passes a ``>= 0`` floor and then reads as
      "leave inherited", removing the ceiling entirely).

    Every refusal is logged once per key per process: the value is security
    relevant, so an operator must not have to infer it was dropped.
    """

    def _refuse(reason: str) -> None:
        if key in _WARNED_RESOURCE_LIMIT_KEYS:
            return
        _WARNED_RESOURCE_LIMIT_KEYS.add(key)
        logger.warning(
            "config: resource_limits.%s = %r %s — ignoring it and using the "
            "documented default for that mechanism",
            key,
            value,
            reason,
        )

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("is not a number")
        return None
    if isinstance(value, float) and not math.isfinite(value):
        _refuse("is not a finite number")
        return None
    # Range-check the value AS WRITTEN, before any truncation. Checking the
    # truncated result instead lets a value BELOW the floor land back inside it:
    # ``int(-0.5) == 0`` satisfies a ``>= 0`` floor and then reads as this
    # block's "leave inherited" sentinel, REMOVING the ceiling the operator was
    # trying to set.
    if value < lo or (hi is not None and value > hi):
        _refuse(f"is outside the accepted range [{lo}, {hi if hi is not None else 'unbounded'}]")
        return None
    if isinstance(value, float) and not value.is_integer():
        # A fraction that truncates to zero is refused whatever its sign. Zero
        # is meaningful to every consumer of this block -- "leave inherited",
        # "use the default", "disabled" -- so truncating would silently swap the
        # operator's request for one of those.
        if int(value) == 0:
            _refuse("is a fraction that would truncate to 0, which means something else")
            return None
        logger.debug("config: resource_limits.%s = %r truncated to %d", key, value, int(value))
    return int(value)


@dataclass
class ResourceLimitsConfig:
    """Kernel confinement ceilings for spawned agent processes.

    THREE mechanisms read this one block, and a key shared between two of them
    does NOT mean the same thing on both. That is the whole reason this section
    has a schema (#3474): every consumer used to parse the raw dict itself, so
    the incompatible domains were written down nowhere and drifted apart.

    - ``POSIX rlimits`` (``security.apply_resource_limits``, via ``preexec_fn``
      or the exec shim's ``--rlimits=``). Here ``0`` is a MEANINGFUL, documented
      value: "leave the inherited limit unchanged". Absent falls back to
      ``security._RLIMIT_DEFAULTS``.
    - ``cgroup v2 scope`` (``sandbox.cgroup_scope_argv``, ``TasksMax`` /
      ``MemoryMax`` / ``CPUWeight`` on a transient ``systemd-run --user
      --scope``). Here ``0`` is ILLEGAL -- systemd rejects the property and the
      scope never starts -- so ``0``, absent, or anything out of domain falls
      back to the module default and the ceiling is never left unset. The one
      exception is ``max_cpu_percent``, which is opt-in: unset emits no
      ``CPUQuota`` property at all.
    - ``pytest-xdist worker cap`` (``resource_status``), where ``xdist_auto_cap``
      carries its own three-way sentinel.

    Every field is ``int | None``, and ``None`` means "not configured" -- kept
    distinct from ``0`` precisely because ``0`` is a real value on the rlimit
    path. Values are coerced by :func:`_limit_int`, the ONLY parse site for this
    block; a second one is a defect, and ``test_resource_limits_schema.py``
    fails if one appears.
    """

    max_open_files: int | None = field(
        default=None,
        metadata=_meta(
            "Max open files",
            "RLIMIT_NOFILE: open file descriptors per spawned process. Caps fd "
            "leaks. 0 leaves the inherited limit unchanged; unset uses the "
            "built-in default (1024). Not used by the cgroup path.",
            nullable=True,
        ),
    )
    max_processes: int | None = field(
        default=None,
        metadata=_meta(
            "Max processes",
            "READ BY TWO MECHANISMS with different meanings for 0. As "
            "RLIMIT_NPROC it caps processes for the child's real UID, and 0 "
            "leaves the inherited limit unchanged (the default -- see the "
            "per-UID caveat in security._RLIMIT_DEFAULTS). As the cgroup "
            "TasksMax it counts TASKS (threads) in the scope, where 0 is "
            "rejected by systemd, so 0 or unset means the module default.",
            nullable=True,
        ),
    )
    max_memory_mb: int | None = field(
        default=None,
        metadata=_meta(
            "Max memory (MB)",
            "READ BY TWO MECHANISMS with different meanings for 0. As RLIMIT_AS "
            "it caps virtual address space, and 0 leaves the inherited limit "
            "unchanged (the default -- Node/V8 reserve huge VSZ, see the caveat "
            "in security._RLIMIT_DEFAULTS). As the cgroup MemoryMax it is the "
            "per-scope resident ceiling, where 0 is rejected by systemd, so 0 "
            "or unset means the host-proportional module default.",
            nullable=True,
        ),
    )
    max_cpu_seconds: int | None = field(
        default=None,
        metadata=_meta(
            "Max CPU seconds",
            "RLIMIT_CPU: CPU-seconds per spawned process. 0 leaves the "
            "inherited limit unchanged (the default). Not used by the cgroup "
            "path, which throttles with CPUWeight/CPUQuota instead of killing.",
            nullable=True,
        ),
    )
    cpu_weight: int | None = field(
        default=None,
        metadata=_meta(
            "CPU weight",
            "cgroup CPUWeight for the agent scope: relative CPU share under "
            "contention, not a cap. Accepted range 1-10000; unset or out of "
            "range uses the module default. Emitted only when the cpu "
            "controller is delegated to the user manager.",
            nullable=True,
        ),
    )
    max_cpu_percent: int | None = field(
        default=None,
        metadata=_meta(
            "Max CPU percent",
            "cgroup CPUQuota: a HARD CPU cap, opt-in. Unset or 0 emits no "
            "CPUQuota property at all, because a hard cap slows legitimate "
            "builds. May exceed 100 on a multi-core host (150 = 1.5 cores).",
            nullable=True,
        ),
    )
    max_total_memory_mb: int | None = field(
        default=None,
        metadata=_meta(
            "Max total memory (MB)",
            "cgroup MemoryMax for the whole agents SLICE -- how much every "
            "agent tree may claim together, independent of the per-scope "
            "ceiling. 0 or unset uses the host-proportional module default; "
            "the aggregate ceiling is never left unset.",
            nullable=True,
        ),
    )
    max_total_processes: int | None = field(
        default=None,
        metadata=_meta(
            "Max total processes",
            "cgroup TasksMax for the whole agents SLICE, counting tasks "
            "(threads) across every agent tree. 0 or unset uses the module "
            "default; the aggregate ceiling is never left unset.",
            nullable=True,
        ),
    )
    xdist_auto_cap: int | None = field(
        default=None,
        metadata=_meta(
            "pytest-xdist worker cap",
            "Ceiling for auto-computed pytest-xdist worker counts. -1 (the "
            "default) computes it from available memory, 0 disables the "
            "injection entirely and defers to xdist, and N > 0 pins a fixed "
            "cap.",
            nullable=True,
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "ResourceLimitsConfig":
        """Build from a raw ``resource_limits`` dict -- the ONE parse site.

        Accepts whatever ``json.loads`` produced, including ``None`` and a
        non-dict, because the callers are spawn-path readers that must never
        raise: a malformed config has to degrade to defaults, not stop the agent
        from starting. Consumers keep their own interpretation of ``0`` and of
        ``None``; this method only decides what is a usable integer.
        """
        if not isinstance(section, dict):
            return cls()
        return cls(
            max_open_files=_limit_int(section.get("max_open_files"), "max_open_files", lo=0),
            max_processes=_limit_int(section.get("max_processes"), "max_processes", lo=0),
            max_memory_mb=_limit_int(section.get("max_memory_mb"), "max_memory_mb", lo=0),
            max_cpu_seconds=_limit_int(section.get("max_cpu_seconds"), "max_cpu_seconds", lo=0),
            cpu_weight=_limit_int(section.get("cpu_weight"), "cpu_weight", lo=1, hi=10000),
            max_cpu_percent=_limit_int(section.get("max_cpu_percent"), "max_cpu_percent", lo=0),
            max_total_memory_mb=_limit_int(
                section.get("max_total_memory_mb"), "max_total_memory_mb", lo=0
            ),
            max_total_processes=_limit_int(
                section.get("max_total_processes"), "max_total_processes", lo=0
            ),
            xdist_auto_cap=_limit_int(section.get("xdist_auto_cap"), "xdist_auto_cap", lo=-1),
        )


@dataclass
class TunnelConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta("Enabled", "Enable a tunnel to expose the dashboard for remote access."),
    )
    name_mode: str = field(
        default="username",
        metadata=_meta(
            "Name Mode",
            "Tunnel naming: 'username' uses 'kirocrew', "
            "'hash' uses 'kirocrew-<hostHash>' for multi-host disambiguation.",
            enum=["username", "hash"],
        ),
    )
    name_override: str = field(
        default="",
        metadata=_meta(
            "Name Override",
            "Explicit tunnel name (overrides name_mode). "
            "Note: some tunnel providers prefix your username (e.g. 'foo' becomes '<user>-foo').",
        ),
    )


@dataclass
class WeComConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WeCom channel via WeCom AI-bot. Requires the WECOM_BOT_ID "
            "and WECOM_SECRET credentials to be set.",
            tags=["wecom"],
        ),
    )
    allowed_users: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Users",
            "WeCom users allowed to DM the bot. Each entry: {userid, name}. "
            "The owner is always allowed.",
            tags=["wecom"],
        ),
    )
    allow_all_users: bool = field(
        default=False,
        metadata=_meta(
            "Allow All Users",
            "Let every member of the WeCom organization DM the bot, bypassing "
            "the allow-list. Safe-ish because a WeCom AI bot is reachable only "
            "inside your own org tenant (unlike globally addressable bots), "
            "but it grants agent access to the whole company. Default off.",
            tags=["wecom"],
        ),
    )
    ws_url: str = field(
        default="wss://openws.work.weixin.qq.com",
        metadata=_meta(
            "WebSocket URL",
            "WeCom AI-bot long-connection endpoint.",
            tags=["wecom"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["wecom"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["wecom"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["wecom"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig (e.g. hard=50, soft=95, or an out-of-range value) can't
        # make the soft nudge unreachable -- _maybe_notice checks ``pct >= hard``
        # first.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class FeishuConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Feishu (Lark/飞书) channel. Requires FEISHU_APP_ID and "
            "FEISHU_APP_SECRET environment variables to be set.",
            tags=["feishu"],
        ),
    )
    allowed_open_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Open IDs",
            "Feishu open_ids allowed to DM the bot (deny-by-default: empty list "
            "authorises nobody). Find your open_id via the Feishu developer console.",
            tags=["feishu"],
        ),
    )
    allow_group: bool = field(
        default=False,
        metadata=_meta(
            "Allow Group Chat",
            "Serve messages from group chats whose chat_id is in allowed_group_ids. "
            "The bot must be @-mentioned in a group to receive the message.",
            tags=["feishu"],
        ),
    )
    allowed_group_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Group IDs",
            "Feishu group chat_ids allowed to drive a turn (requires allow_group=true).",
            tags=["feishu"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a conversation's context passes this, prompt the user to /compact "
            "or /new instead of auto-compacting.",
            tags=["feishu"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this so the window never overflows.",
            tags=["feishu"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["feishu"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig. The helper's floor
        # is 1, not 0, because a 0% threshold reads as "always over" and would
        # compact every turn -- a hand-rolled max(0, ...) admits exactly that.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


def _coerce_int_ids(raw: object) -> list[int]:
    """Coerce a config value to a clean ``list[int]``, dropping anything invalid.

    Fail closed against a hand-edited config: a non-list (e.g. the string
    ``"12345"``) yields ``[]`` instead of iterating char-by-char, and any entry
    that isn't a clean base-10 integer (``"--100"``, ``"1.5"``, unicode digits,
    booleans) is skipped rather than raising in ``int()`` and crashing config
    load / gateway startup.
    """
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for u in raw:
        try:
            ids.append(int(str(u)))
        except (TypeError, ValueError):
            continue
    return ids


def _coerce_opaque_str_ids(raw: object) -> list[str]:
    """Coerce a config value to a clean, deduped ``list[str]`` of OPAQUE IDs.

    For channels whose user IDs are not numeric — WeChat/iLink uses forms like
    ``wxid_abc123`` and ``<hex>@im.bot`` — so the digit-only filter in
    :func:`_coerce_str_ids` would silently drop every entry. With a
    deny-by-default ``dm_policy`` that would lock out every intended sender.

    Still fails closed on shape: a non-list yields ``[]``, and blank entries are
    dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for u in raw:
        s = str(u).strip()
        if s and s not in out:
            out.append(s)
    return out


_WHATSAPP_GROUP_MODES = ("mention", "rules", "off")
_WHATSAPP_GROUP_COOLDOWN_DEFAULT = 120


def _coerce_whatsapp_groups(raw: object) -> list[dict]:
    """Coerce the whatsapp ``groups`` config value to sanitized rule entries.

    Each entry needs at least a non-empty ``jid``; everything else gets a safe
    default. Unknown ``mode`` values fall back to ``mention`` (never to an
    unprompted-speech mode), and cooldown is clamped to >= 0. Fails closed on
    shape: a non-list yields ``[]``, malformed entries are dropped, duplicate
    JIDs keep the first entry.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        jid = str(entry.get("jid", "")).strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        mode = str(entry.get("mode", "mention")).strip().lower()
        if mode not in _WHATSAPP_GROUP_MODES:
            mode = "mention"
        try:
            cooldown = int(entry.get("cooldown_s", _WHATSAPP_GROUP_COOLDOWN_DEFAULT))
        except (TypeError, ValueError):
            cooldown = _WHATSAPP_GROUP_COOLDOWN_DEFAULT
        out.append(
            {
                "jid": jid,
                "name": str(entry.get("name", "")).strip(),
                "mode": mode,
                "rules": str(entry.get("rules", "")).strip(),
                "cooldown_s": max(0, cooldown),
            }
        )
    return out


def _coerce_str_ids(raw: object) -> list[str]:
    """Coerce a config value to a clean, deduped ``list[str]`` of digit IDs.

    Used for Discord snowflakes, which exceed 2^53 and therefore stay strings
    (JSON round-trip safe). Fails closed like :func:`_coerce_int_ids`: a
    non-list yields ``[]`` and non-digit entries are dropped.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for u in raw:
        s = str(u).strip()
        if s.isdigit() and s not in out:
            out.append(s)
    return out


_GITLAB_HOST_NAME_RE = _re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def _parse_telegram_accounts(raw: object) -> dict[str, "TelegramAccountConfig"]:
    """Parse the deprecated ``telegram.accounts`` map from raw config JSON.

    Parsing is retained so a config written by an earlier release round-trips
    through :meth:`KiroCrewConfig.save` with its tokens and allow-lists intact;
    no bot is started from the result. Each value is a dict with optional keys
    matching :class:`TelegramAccountConfig`. Invalid entries (non-dict values,
    missing bot_token) are skipped so a hand-edited config never crashes
    gateway startup.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, TelegramAccountConfig] = {}
    for account_id, acct_data in raw.items():
        if not isinstance(account_id, str) or not isinstance(acct_data, dict):
            continue
        # Account IDs are held to the same shape they were accepted under, so a
        # config that round-trips here is byte-comparable to what an earlier
        # release wrote: alphanumeric plus dash and underscore, never empty.
        if not account_id or not account_id.replace("-", "").replace("_", "").isalnum():
            continue
        token = str(acct_data.get("bot_token", "")).strip()
        if not token:
            continue
        out[account_id] = TelegramAccountConfig(
            bot_token=token,
            allowed_user_ids=_coerce_int_ids(acct_data.get("allowed_user_ids")),
            allow_forum=_safe_bool(acct_data.get("allow_forum"), False),
            allowed_forum_chat_ids=_coerce_int_ids(acct_data.get("allowed_forum_chat_ids")),
            soft_threshold_pct=_threshold_pct(acct_data.get("soft_threshold_pct"), 80),
        )
    return out


def _coerce_gitlab_hosts(raw: object) -> list[str]:
    """Coerce the self-hosted GitLab allowlist to clean ``host[:port]`` entries.

    Fails closed: a non-list yields ``[]``, and an entry is dropped unless it is
    a bare lowercase-normalized hostname with an optional numeric port. Anything
    carrying a scheme, userinfo, path, query, or wildcard is rejected rather than
    sanitized, so a hand-edited config cannot smuggle a different target past the
    exact-match check the source-provider handler performs.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if not host or len(host) > 255:
            continue
        # Split the optional port BEFORE stripping trailing dots: an absolute-FQDN
        # entry with a port ("gitlab.example.:8443") keeps its dot in the middle of
        # the string, so stripping the whole entry first would leave it there and
        # the URL API's "gitlab.example:8443" could never match.
        name, sep, port_text = host.rpartition(":")
        if not sep:
            name, port_text = host, ""
        name = name.rstrip(".")
        # Hostname-only pattern here: the permissive one allows a trailing port,
        # so validating `name` with it would let a malformed "host:8443:443"
        # entry (whose last colon is split off as the port) silently authorize
        # "host:8443".
        if not name or not _GITLAB_HOST_NAME_RE.fullmatch(name):
            continue
        if sep:
            # A colon was present, so a port MUST follow and it must be a plain
            # run of ASCII digits. Fail closed on anything else rather than
            # authorize a host the operator never wrote:
            #   * "gitlab.example:"      -> empty port; without this it would
            #     fall through to the portless branch and grant the bare host.
            #   * "gitlab.example:+443"  -> int("+443") == 443 silently coerces.
            #   * "gitlab.example:1_000" -> int("1_000") == 1000 (underscores).
            #   * " 443", fullwidth digits, "0x10" -> also coerce or pass isdigit.
            # str.isdigit() alone accepts non-ASCII digit codepoints, so pair it
            # with isascii(); an empty string returns False for both.
            if not (port_text.isascii() and port_text.isdigit()):
                continue
            port = int(port_text)
            if not 0 < port < 65536:
                continue
            # Rebuild the port canonically: a configured "08443" would otherwise
            # be stored verbatim while both the browser URL API and the backend
            # normalize the URL's port to "8443", so the entry could never match.
            # The default HTTPS port is dropped entirely, matching the URL API.
            host = name if port == 443 else f"{name}:{port}"
        else:
            host = name
        # gitlab.com is always accepted and must not need an allowlist entry.
        if host in {"gitlab.com", "www.gitlab.com"} or host in out:
            continue
        out.append(host)
    return out


def _coerce_jira_hosts(raw: object) -> list[str]:
    """Coerce the self-hosted Jira allowlist — identical rules to GitLab hosts."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if not host or len(host) > 255:
            continue
        name, sep, port_text = host.rpartition(":")
        if not sep:
            name, port_text = host, ""
        name = name.rstrip(".")
        if not name or not _GITLAB_HOST_NAME_RE.fullmatch(name):
            continue
        if sep:
            if not (port_text.isascii() and port_text.isdigit()):
                continue
            port = int(port_text)
            if not 0 < port < 65536:
                continue
            host = name if port == 443 else f"{name}:{port}"
        else:
            host = name
        if host in out:
            continue
        out.append(host)
    return out


def _coerce_int(raw: object, default: int) -> int:
    """Return ``int(raw)`` or *default* if *raw* isn't a clean base-10 integer.

    Fail closed against a hand-edited non-numeric config value (e.g. ``"abc"``)
    that would otherwise raise in ``int()`` and crash config load.
    """
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


#: Longest accepted channel session-folder name — matches the 100-char cap the
#: folder CRUD endpoint applies, so a name that round-trips through config can
#: never be longer than one created in the sidebar.
SESSION_FOLDER_NAME_MAX = 100


def _coerce_session_folder(raw: object) -> str:
    """Coerce a channel's ``session_folder`` value to a usable folder name.

    Empty string means the feature is off (the default) — sessions from the
    channel stay unfiled. Anything else is the name of the sidebar folder they
    are filed into. Non-strings, control characters, path separators, and
    over-long values all fail closed to off rather than producing a folder the
    user did not ask for: truncating an over-long hand-edited value would file
    conversations into a real folder whose name nobody chose, which is worse
    than leaving them where they already were.
    """
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    if len(name) > SESSION_FOLDER_NAME_MAX:
        return ""
    if any(ch in name for ch in ("/", "\\")) or any(ord(ch) < 0x20 for ch in name):
        return ""
    return name


@dataclass
class TelegramAccountConfig:
    """A single named Telegram bot account, retained only to preserve config.

    Deprecated and inert: nothing starts a bot from this entry. It stays
    parseable and serializable so that loading and saving a config written by an
    earlier release round-trips the operator's tokens and allow-lists instead of
    erasing them. To serve one of these bots, move its token to
    ``telegram.bot_token``.
    """

    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Telegram Bot API token for this account.",
            tags=["telegram"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Numeric Telegram user IDs permitted to DM this bot account.",
            tags=["telegram"],
        ),
    )
    allow_forum: bool = field(
        default=False,
        metadata=_meta(
            "Allow Forum Topics",
            "Serve forum Topics for this account.",
            tags=["telegram"],
        ),
    )
    allowed_forum_chat_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Forum Chat IDs",
            "Supergroup chat_ids permitted for this account.",
            tags=["telegram"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt threshold for this account.",
            tags=["telegram"],
        ),
    )


@dataclass
class TelegramConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Telegram Bot API channel (long-polling). Requires "
            "TELEGRAM_BOT_TOKEN (env/.env) or telegram.bot_token.",
            tags=["telegram"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Telegram Bot API token from @BotFather. Prefer the TELEGRAM_BOT_TOKEN "
            "credential (env/.env) over storing it here.",
            tags=["telegram"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Numeric Telegram user IDs permitted to DM the bot. Empty = deny all "
            "(fail closed): a Telegram bot is globally reachable by @username.",
            tags=["telegram"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["telegram"],
        ),
    )
    show_thinking: bool = field(
        default=False,
        metadata=_meta(
            "Show Thinking",
            "Post the model's reasoning after each answer as a collapsed, "
            "expandable quote. Off by default: Telegram's rate limit is per chat "
            "and shared with the streaming edits the answer already spends, so "
            "reasoning costs an extra message per turn.",
            tags=["telegram"],
        ),
    )
    allow_forum: bool = field(
        default=False,
        metadata=_meta(
            "Allow Forum Topics",
            "Serve Telegram supergroup forum Topics as per-topic sessions "
            "(Slack-thread style). Fail-closed: also requires the supergroup's "
            "chat_id in allowed_forum_chat_ids.",
            tags=["telegram"],
        ),
    )
    allowed_forum_chat_ids: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Forum Chat IDs",
            "Numeric supergroup chat_ids permitted to run forum-topic sessions. "
            "Empty = deny all groups (fail closed).",
            tags=["telegram"],
        ),
    )
    voice_replies: bool = field(
        default=False,
        metadata=_meta(
            "Voice Replies",
            "Speak each answer as a voice/audio message in addition to the text, "
            "using the global voice_reply provider settings. Off by default: it "
            "costs a second message per turn against Telegram's per-chat rate "
            "budget, and TTS may not be configured. Toggle per conversation with "
            "/voice on|off; this is the default for a new conversation.",
            tags=["telegram"],
        ),
    )
    forum_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta(
            "Forum Activation",
            "When the bot answers inside an allow-listed forum Topic: 'always' "
            "(every message), 'mention' (only when its @handle is used or one of "
            "its own messages is replied to), or 'off' (never). Slack's channel "
            "equivalent defaults to 'mention'; this defaults to 'always' so an "
            "existing forum keeps working after an upgrade instead of going quiet. "
            "Does not apply to a 1:1 DM, which is always served.",
            tags=["telegram"],
        ),
    )
    accounts: dict[str, TelegramAccountConfig] = field(
        default_factory=dict,
        metadata=_meta(
            "Accounts",
            "Deprecated and inert: named Telegram bot accounts no longer start a "
            "bot. Multi-bot operation is withdrawn until a bot is a governable "
            "unit (its own enable switch, its own posture ceiling, and honest "
            "audit attribution) rather than a second inbound door that only the "
            "global telegram.enabled can close. The map is still parsed and "
            "written back so an existing config keeps its tokens and allow-lists, "
            "but nothing reads it: move the token you want served to "
            "telegram.bot_token.",
            tags=["telegram"],
            deprecated=True,
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["telegram"],
        ),
    )

    def __post_init__(self) -> None:
        # Telegram carries only the soft nudge threshold; the hard-compaction
        # backstop is the backend autocompactor (session.autocompact_pct).
        self.soft_threshold_pct = _clamp_pct(self.soft_threshold_pct)


@dataclass
class WeixinConfig:
    """Weixin (personal WeChat) channel via Tencent's iLink Bot API.

    Distinct from :class:`WeComConfig` (enterprise WeCom over WebSocket). The
    bot ``token`` + ``account_id`` are obtained through the Settings > Channels
    QR-login flow; prefer the WEIXIN_TOKEN credential over storing the token
    here.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Weixin (iLink personal WeChat) channel (long-polling). "
            "Requires a bot token + account id from the Settings QR flow.",
            tags=["weixin"],
        ),
    )
    token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "iLink bot token (from QR login). Prefer the WEIXIN_TOKEN credential "
            "(env/.env / cred store) over storing it here.",
            tags=["weixin"],
            sensitive=True,
        ),
    )
    account_id: str = field(
        default="",
        metadata=_meta(
            "Account ID",
            "iLink bot account id captured during QR login.",
            tags=["weixin"],
        ),
    )
    base_url: str = field(
        default="https://ilinkai.weixin.qq.com",
        metadata=_meta(
            "iLink Base URL",
            "iLink API base URL (per-account, returned by QR login).",
            tags=["weixin"],
        ),
    )
    dm_policy: str = field(
        default="allowlist",
        metadata=_meta(
            "DM Policy",
            "Who may DM the bot: 'allowlist' (only allowed_user_ids, the default), "
            "'open' (any sender), or 'disabled'. Defaults to allowlist with an empty "
            "list, so a freshly connected bot authorizes NOBODY until you add an id.",
            tags=["weixin"],
        ),
    )
    allowed_user_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Weixin user ids permitted to DM the bot when dm_policy='allowlist'. "
            "Empty = deny all (fail closed).",
            tags=["weixin"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["weixin"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context passes this percentage.",
            tags=["weixin"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["weixin"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class WhatsAppConfig:
    """WhatsApp channel via a QR-linked personal account (WhatsApp Web protocol).

    Pairs as a linked device on the operator's own WhatsApp account — there is
    no bot token. Pairing state lives in a local session database under the
    data home (``whatsapp/session.db``), created by the Settings > Channels QR
    flow. Requires the optional ``whatsapp`` dependency extra
    (``pip install 'kirocrew[whatsapp]'``).

    Uses the unofficial WhatsApp Web protocol; automation on a personal
    account is against WhatsApp's Terms of Service and carries a small risk
    of the linked number being banned. Keep volumes personal-scale.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the WhatsApp channel (QR-linked personal account over the "
            "WhatsApp Web protocol). Pair a device from Settings > Channels; "
            "needs the 'whatsapp' dependency extra installed.",
            tags=["whatsapp"],
        ),
    )
    dm_policy: str = field(
        default="self",
        metadata=_meta(
            "DM Policy",
            "Who may command the agent in direct chats: 'self' (only the linked "
            "account itself — your own messages, the default), 'allowlist' "
            "(yourself plus allowed_wa_ids), 'open' (any sender), or 'disabled'. "
            "Unknown values deny everyone (fail closed).",
            tags=["whatsapp"],
        ),
    )
    allowed_wa_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed WhatsApp IDs",
            "Phone numbers (digits only, country code, no '+') additionally "
            "permitted to DM the agent when dm_policy='allowlist'. Empty adds "
            "nobody beyond the linked account.",
            tags=["whatsapp"],
        ),
    )
    groups: list[dict] = field(
        default_factory=list,
        metadata=_meta(
            "Group Rules",
            "Per-group participation rules. Each entry: {'jid': group JID "
            "(…@g.us), 'name': display label, 'mode': 'mention' (reply only "
            "when @-mentioned or quoted, the default) | 'rules' (also speak "
            "unprompted when the entry's rules say the agent can genuinely "
            "help) | 'off', 'rules': free-text guidance for when to speak, "
            "'cooldown_s': minimum seconds between unprompted replies "
            "(default 120)}. Groups not listed are ignored entirely.",
            tags=["whatsapp"],
        ),
    )
    db_path: str = field(
        default="",
        metadata=_meta(
            "Session DB Path",
            "Read-only. The pairing session database always lives at "
            "<data home>/whatsapp/session.db, because that path is what the "
            "sensitive-path protection matches: it holds the linked-device keys, "
            "and moving it elsewhere would take the credential out from behind "
            "the one control that stops an agent reading it.",
            tags=["whatsapp"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to /compact or /new when context passes this percentage.",
            tags=["whatsapp"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context passes this percentage.",
            tags=["whatsapp"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["whatsapp"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeixinConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class DiscordConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Discord channel (Gateway WebSocket, DMs plus optional "
            "allow-listed server threads). Requires DISCORD_BOT_TOKEN (env/.env) "
            "or discord.bot_token.",
            tags=["discord"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Discord bot token from the Developer Portal (Bot page). Prefer the "
            "DISCORD_BOT_TOKEN credential (env/.env) over storing it here.",
            tags=["discord"],
            sensitive=True,
        ),
    )
    allowed_user_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed User IDs",
            "Discord user IDs (snowflakes) permitted to message the bot. Empty = "
            "deny all (fail closed).",
            tags=["discord"],
        ),
    )
    allowed_thread_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Thread IDs",
            "Discord server thread IDs where approved users may run the agent. "
            "Empty = DMs only. A server channel is denied unless it is listed in "
            "allowed_channel_ids, and a turn there still runs in a thread.",
            tags=["discord"],
        ),
    )
    allowed_channel_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Channel IDs",
            "Discord server channels where approved users may start a new agent thread.",
            tags=["discord"],
        ),
    )
    auto_thread: bool = field(
        default=True,
        metadata=_meta(
            "Auto-create Threads",
            "Create one Discord thread per approved message in an allowed channel.",
            tags=["discord"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "Prompt the user to !compact or !new when context passes this percentage.",
            tags=["discord"],
        ),
    )
    reactions_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Reactions Enabled",
            "Show phase-aware emoji reactions on Discord messages during processing.",
            tags=["discord"],
        ),
    )
    show_thinking: bool = field(
        default=False,
        metadata=_meta(
            "Show Thinking",
            "Post the model's thinking/reasoning as a subtext note in Discord. "
            "Off by default to keep responses concise.",
            tags=["discord"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["discord"],
        ),
    )

    def __post_init__(self) -> None:
        # Discord carries only the soft nudge threshold; the hard-compaction
        # backstop is the backend autocompactor (session.autocompact_pct).
        self.soft_threshold_pct = _clamp_pct(self.soft_threshold_pct)


@dataclass
class WebexConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Webex Messaging channel (device WebSocket, no public "
            "URL needed). Requires WEBEX_BOT_TOKEN (env/.env) or webex.bot_token.",
            tags=["webex"],
        ),
    )
    bot_token: str = field(
        default="",
        metadata=_meta(
            "Bot Token",
            "Webex bot access token from developer.webex.com (My Webex Apps). "
            "Prefer the WEBEX_BOT_TOKEN credential (env/.env) over storing it here.",
            tags=["webex"],
            sensitive=True,
        ),
    )
    allowed_emails: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Emails",
            "Webex account emails permitted to DM the bot. Empty = deny all "
            "(fail closed): anyone in the org can message a Webex bot.",
            tags=["webex"],
        ),
    )
    allow_group_rooms: bool = field(
        default=False,
        metadata=_meta(
            "Allow Group Spaces",
            "Answer in group spaces as well as direct messages. Off by default: a "
            "reply in a space is visible to every member, including people who are "
            "not on the allow-list, so tool output would leave the DM. A Webex bot "
            "only ever sees messages that @mention it in a space.",
            tags=["webex"],
        ),
    )
    allowed_room_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Room IDs",
            "Webex space IDs the bot may answer in when group spaces are enabled. "
            "Empty = deny all (fail closed), so turning the switch on alone grants "
            "nothing; the sender must ALSO be on the email allow-list.",
            tags=["webex"],
        ),
    )
    reply_in_thread: bool = field(
        default=True,
        metadata=_meta(
            "Reply in Thread",
            "Reply under the message's own thread when it has one, keeping a space "
            "readable. Webex threads are flat, so a reply always attaches to the "
            "thread root.",
            tags=["webex"],
        ),
    )
    wdm_base: str = field(
        default="",
        metadata=_meta(
            "Device Manager Base URL",
            "Override the Webex Device Manager host used for the inbound "
            "WebSocket. Empty (the default) discovers the org's own regional host "
            "per token, which is what a non-US-resident org needs; set this only "
            "to pin a REGIONAL WEBEX host for a network that reaches it but not "
            "the service catalog. Must be an https Webex host (*.wbx2.com, "
            "*.webex.com, *.ciscospark.com) — the bot token rides device "
            "registration, so anything else is refused and discovery is used "
            "instead. An outbound proxy belongs in HTTPS_PROXY, not here.",
            tags=["webex"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["webex"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["webex"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["webex"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WeComConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )


@dataclass
class IMessageConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the iMessage channel. macOS only, and the gateway must run "
            "on the Mac that is signed in to Messages. Needs no bot and no "
            "token — it drives Messages.app through the local imsg bridge, so "
            "the transport involves no third party. The turn itself still goes "
            "to the configured model provider, as on any channel.",
            tags=["imessage"],
        ),
    )
    db_path: str = field(
        default="",
        metadata=_meta(
            "Messages Database Path",
            "Override the Messages database location. Empty (the default) lets "
            "the bridge use ~/Library/Messages/chat.db. Reading it needs Full "
            "Disk Access for the process the gateway runs as.",
            tags=["imessage"],
        ),
    )
    allowed_handles: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Handles",
            "Phone numbers or Apple ID emails permitted to message the agent. "
            "Empty = deny all (fail closed): anyone who knows this Mac's handle "
            "can send to it. Formatting is ignored, so '+61 400 000 000' and "
            "'+61400000000' are the same handle.",
            tags=["imessage"],
        ),
    )
    service: str = field(
        default="imessage",
        metadata=_meta(
            "Send Service",
            "Which service outbound replies use: 'imessage' (default), 'sms', "
            "or 'auto' to let the bridge fall back to SMS when iMessage is "
            "unavailable. Inbound is unaffected — the channel answers on "
            "whichever service the message arrived over.",
            tags=["imessage"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a conversation's context passes this, prompt the user to "
            "/compact or /new instead of auto-compacting.",
            tags=["imessage"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["imessage"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["imessage"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable -- _maybe_notice
        # checks ``pct >= hard`` first. Mirrors WebexConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )
        # An unrecognized service would be forwarded to the bridge and rejected
        # per send, turning a typo into a channel that accepts messages and
        # never answers. Fall back to the safe default instead.
        service = (self.service or "").strip().lower()
        self.service = service if service in IMESSAGE_SERVICES else "imessage"


@dataclass
class TeamsConfig:
    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Enabled",
            "Enable the Microsoft Teams channel (self-hosted inbound HTTPS "
            "webhook via the Bot Framework). Requires a public HTTPS endpoint "
            "pointing at /api/messaging/teams plus MICROSOFT_APP_ID and "
            "MICROSOFT_APP_PASSWORD (env/.env) or teams.app_id/app_password.",
            tags=["teams"],
        ),
    )
    app_id: str = field(
        default="",
        metadata=_meta(
            "App ID",
            "Microsoft App (Client) ID of the Azure Bot registration. Prefer "
            "the MICROSOFT_APP_ID credential (env/.env) over storing it here.",
            tags=["teams"],
        ),
    )
    app_password: str = field(
        default="",
        metadata=_meta(
            "App Password",
            "Azure Bot client secret. Set ONLY via the MICROSOFT_APP_PASSWORD "
            "credential (env/.env); it is deliberately NOT read from config.json "
            "so the agent-readable config never holds the secret.",
            tags=["teams"],
            sensitive=True,
        ),
    )
    tenant_id: str = field(
        default="",
        metadata=_meta(
            "Tenant ID",
            "Azure AD tenant id for a single-tenant bot. Leave empty for a "
            "multi-tenant bot (uses the botframework.com token authority).",
            tags=["teams"],
        ),
    )
    allowed_emails: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Emails",
            "Azure AD UPNs/emails OR AAD object ids permitted to DM the bot. "
            "Teams activities reliably carry the sender's object id (email is "
            "often absent), so listing object ids works out of the box; emails "
            "are matched when Teams supplies them. Empty = deny all (fail "
            "closed): a Teams bot is reachable by anyone in the org.",
            tags=["teams"],
        ),
    )
    soft_threshold_pct: int = field(
        default=80,
        metadata=_meta(
            "Soft Context Threshold %",
            "When a DM's context passes this, prompt the user to /compact or /new "
            "instead of auto-compacting.",
            tags=["teams"],
        ),
    )
    hard_threshold_pct: int = field(
        default=95,
        metadata=_meta(
            "Hard Context Threshold %",
            "Force a compaction when context reaches this, even without a user "
            "decision, so the window never overflows.",
            tags=["teams"],
        ),
    )
    session_folder: str = field(
        default="",
        metadata=_meta(
            "Session Folder",
            "Optional sidebar folder for sessions that start on this channel. "
            "Empty (the default) leaves them unfiled; any other value is the "
            "folder name, created when these settings are saved and marked with "
            "the channel's brand mark. A configured folder that no longer exists "
            "leaves conversations unfiled until the next save recreates it.",
            tags=["teams"],
        ),
    )

    def __post_init__(self) -> None:
        # Shared normalization: clamp both thresholds and guarantee soft <= hard
        # so a misconfig can't make the soft nudge unreachable. Mirrors
        # WebexConfig.
        self.soft_threshold_pct, self.hard_threshold_pct = _normalize_threshold_pair(
            self.soft_threshold_pct, self.hard_threshold_pct
        )
=======
# Compatibility facade: section DTOs remain importable from this module.
>>>>>>> upstream/main


@dataclass
class KiroCrewConfig:
    agent: AgentConfig = field(
        default_factory=AgentConfig,
        metadata=_meta("Agent", "Agent runtime configuration."),
    )
    session: SessionConfig = field(
        default_factory=SessionConfig,
        metadata=_meta("Session", "Session management settings."),
    )
    taskrunner: TaskRunnerConfig = field(
        default_factory=TaskRunnerConfig,
        metadata=_meta("Task Runner", "Task runner configuration."),
    )
    orchestrator: OrchestratorConfig = field(
        default_factory=OrchestratorConfig,
        metadata=_meta("Orchestrator", "Autopilot/orchestrator settings."),
    )
    messaging: MessagingConfig = field(
        default_factory=MessagingConfig,
        metadata=_meta("Messaging", "Channel-neutral messaging transport settings."),
    )
    cron_history: CronHistoryConfig = field(
        default_factory=CronHistoryConfig,
        metadata=_meta("Cron History", "Cron execution history storage limits."),
    )
    memory: MemoryConfig = field(
        default_factory=MemoryConfig,
        metadata=_meta("Memory", "Memory and embedding configuration."),
    )
    knowledge: KnowledgeConfig = field(
        default_factory=KnowledgeConfig,
        metadata=_meta("Knowledge", "Knowledge Library ingestion settings."),
    )
    skills: SkillsConfig = field(
        default_factory=SkillsConfig,
        metadata=_meta("Skills", "Skill loading and matching configuration."),
    )
    session_summary: SessionSummaryConfig = field(
        default_factory=SessionSummaryConfig,
        metadata=_meta(
            "Session Summary",
            "Intent-level session summaries for the chat right panel. Off by default.",
        ),
    )
    telemetry: TelemetryConfig = field(
        default_factory=TelemetryConfig,
        metadata=_meta(
            "Telemetry",
            "Metrics telemetry (local-first JSONL sink). Off by default.",
        ),
    )
    stt: SttConfig = field(
        default_factory=SttConfig,
        metadata=_meta("STT", "Speech-to-text transcription settings."),
    )
    computer_use: ComputerUseConfig = field(
        default_factory=ComputerUseConfig,
        metadata=_meta(
            "Computer Use",
            "Desktop automation tree/screenshot budgets. The primary enable is NOT "
            "here — it lives on the keystone computer_use.json.",
        ),
    )
    mcp_gateway: McpGatewayConfig = field(
        default_factory=McpGatewayConfig,
        metadata=_meta("MCP Gateway", "Sidecar MCP broker that shares backends across sessions."),
    )
    mcp: McpConfig = field(
        default_factory=McpConfig,
        metadata=_meta(
            "MCP",
            "How MCP servers are found and launched — applies with the broker off too.",
        ),
    )
    instances: InstancesConfig = field(
        default_factory=InstancesConfig,
        metadata=_meta(
            "Instances", "Multi-instance management — manage/switch remote Kiro Crews over SSH."
        ),
    )
    heartbeat: HeartbeatConfig = field(
        default_factory=HeartbeatConfig,
        metadata=_meta("Heartbeat", "Heartbeat background task queue delivery defaults."),
    )
    watchdog: WatchdogConfig = field(
        default_factory=WatchdogConfig,
        metadata=_meta("Watchdog", "ACP per-session watchdog / liveness-oracle windows."),
    )
    resource_limits: ResourceLimitsConfig = field(
        default_factory=ResourceLimitsConfig,
        metadata=_meta(
            "Resource Limits",
            "Kernel confinement ceilings for spawned agents (POSIX rlimits and "
            "cgroup v2 scope properties). Shared keys mean different things to "
            "the two mechanisms -- see the per-field help.",
        ),
    )

    slack: SlackConfig = field(
        default_factory=SlackConfig,
        metadata=_meta("Slack", "Slack integration settings.", tags=["slack"]),
    )
    publish: PublishConfig = field(
        default_factory=PublishConfig,
        metadata=_meta(
            "Publish", "Artifact publishing controls (destinations allowlist).", tags=["publish"]
        ),
    )
    wecom: WeComConfig = field(
        default_factory=WeComConfig,
        metadata=_meta("WeCom", "WeCom (企业微信) AI-bot integration settings.", tags=["wecom"]),
    )
    telegram: TelegramConfig = field(
        default_factory=TelegramConfig,
        metadata=_meta("Telegram", "Telegram Bot API integration settings.", tags=["telegram"]),
    )
    weixin: WeixinConfig = field(
        default_factory=WeixinConfig,
        metadata=_meta(
            "WeChat", "Weixin (iLink personal WeChat) integration settings.", tags=["weixin"]
        ),
    )
    whatsapp: WhatsAppConfig = field(
        default_factory=WhatsAppConfig,
        metadata=_meta(
            "WhatsApp",
            "WhatsApp (QR-linked personal account) integration settings.",
            tags=["whatsapp"],
        ),
    )
    feishu: FeishuConfig = field(
        default_factory=FeishuConfig,
        metadata=_meta(
            "Feishu",
            "Feishu (Lark/飞书) channel configuration.",
            tags=["feishu"],
        ),
    )
    discord: DiscordConfig = field(
        default_factory=DiscordConfig,
        metadata=_meta("Discord", "Discord bot integration settings.", tags=["discord"]),
    )
    webex: WebexConfig = field(
        default_factory=WebexConfig,
        metadata=_meta("Webex", "Webex Messaging integration settings.", tags=["webex"]),
    )
    wakatime: WakaTimeConfig = field(
        default_factory=WakaTimeConfig,
        metadata=_meta(
            "WakaTime", "WakaTime dev-time tracking integration settings.", tags=["wakatime"]
        ),
    )
    teams: TeamsConfig = field(
        default_factory=TeamsConfig,
        metadata=_meta("Teams", "Microsoft Teams integration settings.", tags=["teams"]),
    )
    imessage: IMessageConfig = field(
        default_factory=IMessageConfig,
        metadata=_meta(
            "iMessage",
            "iMessage integration settings (macOS only, local bridge, no bot token).",
            tags=["imessage"],
        ),
    )
    dashboard: DashboardConfig = field(
        default_factory=DashboardConfig,
        metadata=_meta("Dashboard", "Dashboard UI settings."),
    )
    tunnel: TunnelConfig = field(
        default_factory=TunnelConfig,
        metadata=_meta("Tunnel", "AEA tunnel settings for remote dashboard access."),
    )
    hooks: dict = field(
        default_factory=dict,
        metadata=_meta("Hooks", "Script hook definitions keyed by hook ID."),
    )
    slack_channels: dict[str, ChannelConfig] = field(
        default_factory=dict,
        metadata=_meta("Slack Channels", "Per-channel activation config."),
    )
    slack_dm_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta("Slack DM Activation", "Default activation mode for DMs."),
    )
    observe_max_messages: int = field(
        default=200,
        metadata=_meta("Observe Max Messages", "Max messages per observe-mode channel."),
    )
    observe_ttl_hours: float = field(
        default=168.0,
        metadata=_meta("Observe TTL Hours", "Hours to keep observe history."),
    )
    agents: dict[str, KiroCrewAgentConfig] = field(
        default_factory=dict,
        metadata=_meta("Agents", "Named Kiro Crew agent definitions."),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Active Kiro Crew agent name from the agents section."),
    )
    workspaces: dict[str, WorkspaceConfig] = field(
        default_factory=dict,
        metadata=_meta("Workspaces", "Named workspace definitions."),
    )
    default_workspace: str = field(
        default="default",
        metadata=_meta("Default Workspace", "Active workspace name."),
    )
    memory_stores: dict[str, MemoryStoreConfig] = field(
        default_factory=dict,
        metadata=_meta("Memory Stores", "Named memory store definitions."),
    )
    default_memory_store: str = field(
        default="default",
        metadata=_meta("Default Memory Store", "Fallback memory store name."),
    )
    auto_update: bool = field(
        default=True,
        metadata=_meta("Auto Update", "Enable automatic update checks."),
    )
    #: Opt-in for the Connections gallery, which is merged but held for a later
    #: release. A real field rather than an unmodelled top-level key because the
    #: browser is the consumer: the frontend reads this flag live off ``GET
    #: /api/config/kirocrew`` (``useConnectionsUi.ts``), and that response drops
    #: every key the core does not model — ``_masked_config_dict`` cannot tell
    #: whether an unmodelled value is a secret, so it strips them all rather
    #: than leak one. Being schema-known is therefore what makes the flag
    #: reachable at all; it also stops ``validation`` reporting the operator's
    #: own documented setting as an "unrecognized top-level key".
    #:
    #: Parsed through ``_safe_bool`` and defaulting False: a value Kiro Crew
    #: cannot read must mean "keep the held surface hidden", never the reverse
    #: (same posture as ``computer_use.cursor_motion``). The frontend predicate
    #: is a strict ``=== true``, so coercing e.g. the string ``"true"`` would
    #: only make the two ends disagree about what was configured.
    connections_ui: bool = field(
        default=False,
        metadata=_meta(
            "Connections UI",
            "Show the Connections gallery (held for a later release).",
        ),
    )
    #: Top-level sections that were PRESENT on disk but not a JSON object, and
    #: were therefore coerced to defaults by :meth:`load`.
    #:
    #: The loader's whole contract is to degrade rather than raise, which is
    #: right for an ordinary consumer and dangerous for one reading a SECURITY
    #: value out of a section: a coerced-away section is indistinguishable from
    #: "the operator configured nothing", so a narrowing silently becomes
    #: allow-all (#4057, and the same shape as #3945).
    #:
    #: A consumer cannot recover this by re-reading the file, which is why the
    #: signal has to live here: ``load()`` runs a migration that REWRITES
    #: ``config.json`` in normalized form, so by the time any gate looks, the
    #: malformed section is gone from disk. The evidence only exists during the
    #: parse that discarded it.
    #:
    #: Excluded from serialization (``repr=False``, and the config writers work
    #: from explicit field lists) — it describes THIS read, not the operator's
    #: settings, and must never be written back into their config. The leading
    #: underscore keeps it out of the config schema/baseline machinery, which
    #: skips private fields (same convention as ``_extra_sections``); consumers
    #: read the :attr:`degraded_sections` property.
    _degraded_sections: frozenset[str] = field(
        default_factory=frozenset,
        repr=False,
        compare=False,
    )

    @property
    def degraded_sections(self) -> frozenset[str]:
        """Sections this load discarded (see ``_degraded_sections``)."""
        return self._degraded_sections

    timezone: str = field(
        default="",
        metadata=_meta(
            "Timezone",
            "IANA timezone name (e.g. 'America/Los_Angeles'). "
            "Used to display cron schedules in local time.",
        ),
    )
    snapshot_dir: str = field(
        default="",
        metadata=_meta(
            "Snapshot Directory",
            "Directory for kirocrew snapshot output. "
            "Defaults to ~/.kiro/crew/snapshots if empty.",
        ),
    )
    registries: list[ExternalRegistryConfig] = field(
        default_factory=list,
        metadata=_meta(
            "Registries",
            "External app registries (org-owned repos). " "Each entry: {name, repo, branch}.",
        ),
    )
    # Unknown top-level config.json sections captured verbatim at load() and
    # re-emitted by to_dict() so a section this core does not model (e.g. an
    # edition-contributed section written by a companion) is NOT silently
    # dropped on the first save()/PATCH round-trip. Excluded from the JSON
    # schema by the leading underscore (build_json_schema skips private fields);
    # populated only from disk. This is the data-preservation half of the
    # ConfigSchemaContributor seam — a companion writes its section, the core
    # round-trips it untouched.
    _extra_sections: dict = field(default_factory=dict)

    def channel_config(self, channel_id: str) -> ChannelConfig:
        """Return the config for *channel_id*, falling back to defaults.

        DMs (channel IDs starting with ``D``) use ``slack_dm_activation``.
        Group channels use ``mention`` unless overridden in ``slack_channels``.
        """
        if channel_id in self.slack_channels:
            return self.slack_channels[channel_id]
        if channel_id.startswith("D"):
            return ChannelConfig(activation=self.slack_dm_activation)
        return ChannelConfig(activation=ACTIVATION_MENTION)

    @property
    def slack_enterprise_ids(self) -> set[str]:
        """Extra allowed enterprise IDs from ``slack.allowed_enterprise_ids``."""
        return set(self.slack.allowed_enterprise_ids)

    @classmethod
    def load(cls) -> KiroCrewConfig:
        """Load config from ~/.kiro/crew/config.json, falling back to defaults.

        If ``config.local.json`` exists alongside ``config.json``, it is
        deep-merged on top. User overrides in the local file survive
        upgrades that regenerate ``config.json``.

        The overlay is applied at load time but NOT persisted back by
        ``save()`` — only the base config is written to ``config.json``.
        """
        # The ordering ticket comes back from the resolve step, drawn BEFORE the
        # read, so a concurrent newer load cannot be overwritten by this one
        # finishing later (see publish_autocompact_pct) and this method adds no
        # filesystem I/O of its own on the event loop.
        cfg, _autocompact_ticket = cls._load_resolved()
        # Push the MCP search-path setting to its consumer. It is PUSHED rather
        # than read there because kiro_crew.env.mcp_search_path is reached from
        # the event loop by every MCP probe and by the agent-config resolver, so
        # a config read on that side would stat/read/validate config.json on the
        # loop. Done here rather than inside _load_resolved so EVERY return path
        # publishes -- including the defaults path taken when neither config file
        # could be read, which must CLEAR a previously published snapshot rather
        # than leave a deleted directory resolving commands. Lazy import: env
        # must stay off this module's import graph.
        try:
            from kiro_crew.env import publish_config_path_dirs

            publish_config_path_dirs(cfg.mcp.extra_path_dirs)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; the
            # search path simply keeps its previous (or empty) contribution.
            logger.warning("Publishing mcp.extra_path_dirs failed: %s", e)
        # Publish the alias table for the same reason and in the same place: the
        # display-side resolver (:func:`resolve_effective_agent`) runs on the
        # event loop for every slots frame, so it must never reach for
        # config.json itself. Here rather than in _load_resolved so EVERY return
        # path publishes -- including the degraded-defaults path, which must
        # overwrite a richer previous snapshot rather than leave the resolver
        # honoring aliases that no longer load.
        try:
            publish_agent_alias_snapshot(cfg)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; the
            # resolver simply keeps reporting no divergence.
            logger.warning("Publishing agent alias snapshot failed: %s", e)
        # Same placement and same reason again: the compaction gate reads this
        # after every turn on the event loop, and publishing on EVERY return path
        # is what lets a CLI write reach a gateway that is already running.
        try:
            publish_autocompact_pct(cfg, _autocompact_ticket)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; the gate
            # keeps using the threshold it already had.
            logger.warning("Publishing autocompact threshold failed: %s", e)
        # Same placement and same reason once more: cron resolves this on the
        # event loop on every timer tick (see publish_config_timezone), and
        # publishing on EVERY return path is what lets a settings change reach a
        # gateway that is already running. Shares the autocompact ticket
        # deliberately -- both describe the same read of the same files, so they
        # must order identically against a concurrent load.
        try:
            publish_config_timezone(cfg, _autocompact_ticket)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; cron
            # keeps using the zone it already had.
            logger.warning("Publishing config timezone failed: %s", e)
        return cfg

    @classmethod
    def _load_resolved(cls) -> tuple[KiroCrewConfig, int]:
        """Resolve the config from disk (or defaults). See :meth:`load`.

        Split out so :meth:`load` owns the post-resolution publication on every
        return path; this method may return from more than one place.

        Returns the config PLUS the ordering ticket drawn before the read, which
        is what lets :meth:`load` publish the compaction threshold in the correct
        order relative to a concurrent load without any filesystem I/O of its own
        on the event loop.
        """
        # Drawn BEFORE any read below, so it records when this load began
        # observing the files rather than when it finished. See
        # next_config_load_ticket and publish_autocompact_pct.
        ticket = next_config_load_ticket()
        path = config_path()

        # Hot-path cache: reuse the validated, merged dict when neither config
        # file has changed since the last load. Skips read + json.loads +
        # _deep_merge + the full jsonschema.validate. A deep copy is returned so
        # in-place mutation by callers (and the write-back migration below) can
        # never corrupt the cached original.
        #
        # ONE stat pass serves both consumers of it below: the cache lookup and
        # the pre-read TOCTOU fingerprint. load() runs on the event loop, so a
        # second pass would be filesystem I/O there for information already in
        # hand.
        fp = _config_fingerprint()
        cached_data = _cached_validated_data(fp)
        if cached_data is not None:
            data = cached_data
        else:
            # fp was captured BEFORE reading, so a write landing during the read
            # is detected: we cache under it, it won't match the post-write
            # on-disk stat, and the next load() re-reads instead of serving
            # content read mid-write (read->store TOCTOU).
            # _store_validated_data documents this contract.
            pre_read_fp = fp
            data = {}
            loaded_base = False
            config_source_unreadable = False
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = raw
                        loaded_base = True
                    else:
                        config_source_unreadable = True
                        logger.warning("Config is not a JSON object, using defaults")
                        _mark_file_degraded(path)
                except (json.JSONDecodeError, OSError) as e:
                    config_source_unreadable = True
                    logger.warning("Failed to load config from %s: %s", path, e)
                    _mark_file_degraded(path)

            # Report -- never correct -- a stored BASE value that still holds a
            # superseded default (issue #5244), before the overlay merge below:
            # the overlay is the operator's live choice and says nothing about
            # what the base materialized. Read-only by design; a key with a
            # documented escape hatch cannot be corrected automatically, because
            # a stale default and a deliberate opt-out are the same bytes.
            # Skipped when no base file loaded -- nothing is stored to report on.
            if loaded_base:
                _report_superseded_defaults(data)

            # Deep-merge config.local.json overlay (user-owned, never touched by setup)
            local_data: dict = {}
            local_path = config_local_path()
            if local_path.is_file():
                try:
                    st_mode = local_path.stat().st_mode
                    if st_mode & 0o002:
                        logger.warning(
                            "config.local.json is world-writable (%o); "
                            "consider running: chmod 600 %s",
                            st_mode & 0o777,
                            local_path,
                        )
                    raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                    if isinstance(raw_local, dict):
                        local_data = raw_local
                    else:
                        config_source_unreadable = True
                        logger.warning("config.local.json is not a JSON object, ignoring")
                        _mark_file_degraded(local_path)
                except (json.JSONDecodeError, OSError) as e:
                    config_source_unreadable = True
                    logger.warning("Failed to load config.local.json: %s", e)
                    _mark_file_degraded(local_path)

            if local_data:
                data = _deep_merge(data, local_data)

            # A present source that cannot be read or parsed may contain the
            # operator's hard-off switch. Preserve that unknown as disabled
            # before either the defaults return or schema normalization can
            # turn it into the enabled-by-default missing-field case.
            _fail_closed_project_skills_config(
                data, config_source_unreadable=config_source_unreadable
            )

            # Return defaults only if neither file was successfully loaded. Seed
            # the default "kirocrew" agent in-memory (matching the on-disk
            # migration below) so a never-setup home still lists the default
            # agent — but do NOT persist: a plain read (e.g. `agent list`) must
            # not create config files as a side effect. Not cached — there's no
            # file to invalidate against, and the path is already cheap
            # (existence checks only, no read/parse/validate).
            if not loaded_base and not local_data:
                # An UNREADABLE file reaches this same "no config" branch as a
                # genuinely absent one, and the two are opposite claims for a
                # security gate: "the operator configured nothing" versus "we
                # could not read what they configured". Carry the observation
                # through so the caller can tell them apart (#4057).
                cfg = cls(_degraded_sections=frozenset(_OBSERVED_DEGRADED_SECTIONS))
                cfg.skills.project_skills_enabled = (
                    data.get("skills", {}).get("project_skills_enabled", True) is True
                )
                kiro = cfg.agent.default_agent or "kirocrew"
                cfg.agents["default"] = KiroCrewAgentConfig(
                    kiro_agent=kiro,
                    workspace="default",
                    memory_store="default",
                )
                cfg.default_agent = "default"
                return cfg, ticket

            # Preserve fail-closed security semantics before advisory schema
            # validation can replace malformed input with a missing-field default.
            # Normalize resource_limits FIRST, for exactly that reason. Its
            # fields are declared ``int | None``, so jsonschema reads a
            # hand-edited ``512.5`` as a type violation and
            # ``_apply_field_default`` POPS the key -- deleting a ceiling the
            # parse rule would have accepted, since it truncates. That deletion
            # is not neutral: the rlimit path's fallback for a missing value is
            # ``0``, which means "leave inherited", so a 512 MB ceiling becomes
            # NO ceiling, and ``to_dict`` then persists ``null`` over what the
            # operator wrote. Normalizing here means validation sees the same
            # integers ``from_raw`` would produce; it is idempotent, so the
            # section build below agrees by construction.
            if isinstance(data.get("resource_limits"), dict):
                data["resource_limits"] = asdict(
                    ResourceLimitsConfig.from_raw(data["resource_limits"])
                )
            # Validate against JSON Schema (advisory — never fatal)
            _validate_config_data(data)
            # Clamp security-relevant resource-limit knobs to their API ceilings
            # BEFORE caching, so a hand-edited/prompt-injected config.json that
            # exceeds a ceiling cannot drive resource exhaustion (DoS). Runs only
            # on the disk-read path; cache hits below already serve clamped values.
            _clamp_security_bounds(data)
            # Cache the validated, merged dict under the PRE-read fingerprint so
            # a mid-read write self-heals (next load misses and re-reads).
            _store_validated_data(data, pre_read_fp)

        # Collected during the parse that discards them — the only moment the
        # evidence exists, since the migration below rewrites config.json in
        # normalized form (see KiroCrewConfig.degraded_sections).
        _degraded: set[str] = set()
        agent_data = _coerced_section(data, "agent", _degraded)
        session_data = _coerced_section(data, "session", _degraded)
        taskrunner_data = _coerced_section(data, "taskrunner", _degraded)
        cron_history_data = _coerced_section(data, "cron_history", _degraded)
        memory_data = _coerced_section(data, "memory", _degraded)
        knowledge_data = _coerced_section(data, "knowledge", _degraded)
        telegram_data = _coerced_section(data, "telegram", _degraded)
        weixin_data = _coerced_section(data, "weixin", _degraded)
        whatsapp_data = _coerced_section(data, "whatsapp", _degraded)
        feishu_data = _coerced_section(data, "feishu", _degraded)
        discord_data = _coerced_section(data, "discord", _degraded)
        webex_data = _coerced_section(data, "webex", _degraded)
        wakatime_data = _coerced_section(data, "wakatime", _degraded)
        teams_data = _coerced_section(data, "teams", _degraded)
        imessage_data = _coerced_section(data, "imessage", _degraded)
        slack_data = _coerced_section(data, "slack", _degraded)
        publish_data = _coerced_section(data, "publish", _degraded)
        # A malformed allowed_destinations is the same class as a malformed
        # section one level down (#4057), in two shapes. A non-LIST value:
        # iterating it either crashes load() with a TypeError (a scalar — a
        # config typo must not abort gateway startup) or yields garbage (a
        # dict iterates as its keys, a string as its characters). A list with
        # non-string/empty ENTRIES: the parse filter drops them, so an
        # all-invalid narrowing like [1, 2] parses to [] — indistinguishable
        # from "no restriction configured", the exact silent widening this fix
        # exists to stop. Both shapes record the degradation so the publish
        # gate denies, and parse from what safely remains. Validation cannot
        # repair these values (publish.allowed_destinations is fail-closed
        # there — repairing an OPEN default silently widens), so the loader
        # must be the layer that survives them.
        _dests_raw = publish_data.get("allowed_destinations", [])
        if not isinstance(_dests_raw, list):
            _degraded.add("publish")
            _OBSERVED_DEGRADED_SECTIONS.add("publish")
            logger.warning(
                "config: 'publish.allowed_destinations' is not a list (got %s) "
                "— treating the publish section as degraded; publishing is "
                "denied until the file is fixed and the gateway restarted",
                type(_dests_raw).__name__,
            )
            _dests_raw = []
        elif any(not (isinstance(_d, str) and _d) for _d in _dests_raw):
            _degraded.add("publish")
            _OBSERVED_DEGRADED_SECTIONS.add("publish")
            logger.warning(
                "config: 'publish.allowed_destinations' carries entr(y/ies) "
                "that are not non-empty strings — treating the publish section "
                "as degraded; publishing is denied until the file is fixed and "
                "the gateway restarted",
            )
            _dests_raw = []
        # Back-compat: this channel's config section was renamed
        # "wechat" -> "wecom". Fall back to the legacy key so existing
        # installs keep their WeCom settings on upgrade (read-only alias;
        # no broader migration machinery).
        # Alias-aware: record under whichever key the operator actually used, so
        # the warning names the section they can go and fix.
        _wecom_key = "wecom" if "wecom" in data else "wechat"
        wecom_data = _coerced_section(data, _wecom_key, _degraded)
        dashboard_data = _coerced_section(data, "dashboard", _degraded)
        stt_data = _coerced_section(data, "stt", _degraded)
        computer_use_data = _coerced_section(data, "computer_use", _degraded)
        instances_data = _coerced_section(data, "instances", _degraded)
        connect_timeout_raw = instances_data.get("connect_timeout_secs")
        mint_timeout_raw = instances_data.get("mint_timeout_secs")
        mcp_gateway_data = _coerced_section(data, "mcp_gateway", _degraded)
        mcp_data = _coerced_section(data, "mcp", _degraded)
        heartbeat_data = _coerced_section(data, "heartbeat", _degraded)
        heartbeat_default_deliver = (
            str(heartbeat_data.get("default_deliver", "slack")).strip().lower()
        )
        if heartbeat_default_deliver not in ("slack", "dashboard"):
            heartbeat_default_deliver = "slack"
        tunnel_data = _coerced_section(data, "tunnel", _degraded)
        skills_data = _coerced_section(data, "skills", _degraded)
        session_summary_data = _coerced_section(data, "session_summary", _degraded)
        messaging_data = _coerced_section(data, "messaging", _degraded)
        telemetry_data = _coerced_section(data, "telemetry", _degraded)
        orchestrator_data = _coerced_section(data, "orchestrator", _degraded)
        watchdog_data = _coerced_section(data, "watchdog", _degraded)
        resource_limits_data = _coerced_section(data, "resource_limits", _degraded)

        # Parse agents section into dict[str, KiroCrewAgentConfig]
        raw_agents = data.get("agents", {})
        agents: dict[str, KiroCrewAgentConfig] = {}
        if isinstance(raw_agents, dict):
            for name, entry in raw_agents.items():
                if isinstance(entry, dict):
                    # config.json is hand-editable (and agent-writable), so a
                    # non-string model (e.g. `model: 123`) must not survive the
                    # load — it would reach normalize_agent_model().strip() and
                    # raise AttributeError from the resolver instead of simply
                    # being ignored.
                    raw_model = entry.get("model", "")
                    # Same guard as model: a non-string triggers (e.g. `1`) must
                    # not survive load — select_crew's roster calls .strip() on it.
                    raw_triggers = entry.get("triggers", "")
                    agents[name] = KiroCrewAgentConfig(
                        kiro_agent=entry.get("kiro_agent", ""),
                        workspace=entry.get("workspace", "default"),
                        memory_store=entry.get("memory_store", "default"),
                        model=raw_model if isinstance(raw_model, str) else "",
                        # Same hand-editable-config guard: an unknown level must
                        # collapse to "" (inherit) rather than travel to the
                        # provider, where kiro-cli rejects the whole overlay.
                        reasoning_effort=coerce_effort(entry.get("reasoning_effort", "")),
                        description=entry.get("description", ""),
                        triggers=raw_triggers if isinstance(raw_triggers, str) else "",
                        source=entry.get("source", "kirocrew"),
                        # Same guard family as model/triggers: config.json is
                        # hand-editable, so a junk value must collapse to 0
                        # (inherit the global window), never crash the load.
                        # lo=0 keeps a negative override from arming an
                        # instant-cancel window.
                        watchdog_tool_stall_suspect_secs=_safe_float(
                            entry.get("watchdog_tool_stall_suspect_secs", 0.0), 0.0, lo=0.0
                        ),
                        watchdog_tool_stall_hard_cap_secs=_safe_float(
                            entry.get("watchdog_tool_stall_hard_cap_secs", 0.0), 0.0, lo=0.0
                        ),
                        telegram_account=entry.get("telegram_account", ""),
                        session_color=_safe_color(entry.get("session_color", "")),
                        # Module-qualified on purpose: the facade's `from
                        # sections import` list is a frozen pre-split snapshot
                        # (test_config_module_boundaries), and post-split
                        # internals are reached through the module, not
                        # re-exported from here.
                        avatar=_sections._safe_avatar(entry.get("avatar")),
                    )

        # Migrate workspaces from flat or structured format
        raw_workspaces = data.get("workspaces", {})
        if not isinstance(raw_workspaces, dict):
            raw_workspaces = {}
        workspaces = _migrate_workspaces(raw_workspaces)

        # Parse memory_stores; synthesize default if missing
        raw_stores = data.get("memory_stores", {})
        memory_stores: dict[str, MemoryStoreConfig] = {}
        if isinstance(raw_stores, dict) and raw_stores:
            for name, entry in raw_stores.items():
                if isinstance(entry, dict):
                    memory_stores[name] = MemoryStoreConfig(
                        description=entry.get("description", ""),
                        embedding_provider=entry.get("embedding_provider", ""),
                    )
        if not memory_stores:
            memory_stores["default"] = MemoryStoreConfig()

        # Parse top-level default_agent and default_memory_store
        default_agent_val = data.get("default_agent", "")
        if not isinstance(default_agent_val, str):
            default_agent_val = ""
        default_memory_store_val = data.get("default_memory_store", "default")
        if not isinstance(default_memory_store_val, str):
            default_memory_store_val = "default"

        # Capture unknown top-level sections verbatim so a section this core does
        # not model (e.g. an edition-contributed section written by a companion)
        # survives the load()->to_dict()->save() round-trip instead of being
        # silently dropped. ``meta`` is stamped by save() itself, so it is never
        # treated as an unknown section to preserve.
        extra_sections = {
            k: v
            for k, v in data.items()
            if k not in _KNOWN_CONFIG_SECTIONS and k not in CONFIG_RESERVED_TOP_KEYS
        }

        cfg = cls(
            agent=AgentConfig(
                approval_mode=agent_data.get("approval_mode", "auto"),
                streaming=agent_data.get("streaming", True),
                model=agent_data.get("model", DEFAULT_MODEL),
                role_models=coerce_role_models(agent_data.get("role_models")),
                role_efforts=coerce_role_efforts(agent_data.get("role_efforts")),
                fallback_model=coerce_fallback_model(agent_data.get("fallback_model", "auto")),
                reasoning_effort=agent_data.get("reasoning_effort", ""),
                provider=agent_data.get("provider", "acp"),
                mcp_registry_mode=_safe_bool(agent_data.get("mcp_registry_mode", False), False),
                mcp_quarantine_after_failures=_safe_int(
                    agent_data.get("mcp_quarantine_after_failures", 3), 3
                ),
                acp_backend=_normalize_acp_backend(agent_data.get("acp_backend")),
                member_acp_backend=_normalize_acp_backend(
                    agent_data.get("member_acp_backend", "kas")
                ),
                default_agent=agent_data.get("default_agent", ""),
                sweep_agents_backups=_safe_bool(
                    agent_data.get("sweep_agents_backups", False), False
                ),
                sandbox=agent_data.get("sandbox", "auto"),
                sandbox_allow_no_isolation=bool(
                    agent_data.get("sandbox_allow_no_isolation", False)
                ),
                sandbox_allow_unsandboxed_exec=bool(
                    agent_data.get("sandbox_allow_unsandboxed_exec", False)
                ),
                apps_allow_third_party=_safe_bool(
                    agent_data.get("apps_allow_third_party", False), False
                ),
                apps_trusted=(
                    [a for a in _trusted if isinstance(a, str) and a]
                    if isinstance(_trusted := agent_data.get("apps_trusted"), list)
                    else []
                ),
                apps_trusted_local=(
                    [a for a in _trusted_local if isinstance(a, str) and a]
                    if isinstance(_trusted_local := agent_data.get("apps_trusted_local"), list)
                    else []
                ),
                apps_trusted_repositories=(
                    {
                        name: repository
                        for name, repository in _trusted_repositories.items()
                        if isinstance(name, str)
                        and isinstance(repository, str)
                        and name
                        and repository
                    }
                    if isinstance(
                        _trusted_repositories := agent_data.get("apps_trusted_repositories"),
                        dict,
                    )
                    else {}
                ),
                jail=_normalize_jail(agent_data.get("jail", "auto")),
                dangerously_skip_permissions=_read_skip_permissions(agent_data),
                yolo_duration=_normalize_yolo_duration(agent_data.get("yolo_duration")),
                notify_override_expiry=agent_data.get("notify_override_expiry", True),
                conductor_skill=agent_data.get("conductor_skill", False),
                tool_search=bool(agent_data.get("tool_search", True)),
                tool_search_min_pct=_safe_int(agent_data.get("tool_search_min_pct", 5), 5),
                tool_search_min_tokens=_safe_int(
                    agent_data.get("tool_search_min_tokens", 50000), 50000
                ),
                session_sharing=bool(agent_data.get("session_sharing", True)),
                max_subagents=_safe_int(
                    agent_data.get("max_subagents", 0), 0, 0, SUBAGENT_AUTO_MAX_CEILING
                ),
                max_stop_hook_nudges=_safe_int(agent_data.get("max_stop_hook_nudges", 100), 100, 0),
                subagent_mem_buffer_pct=_safe_int(
                    agent_data.get("subagent_mem_buffer_pct", 20), 20
                ),
                chat_turn_timeout_secs=_safe_int(
                    agent_data.get("chat_turn_timeout_secs", 7200),
                    7200,
                    CHAT_TURN_TIMEOUT_MIN,
                    CHAT_TURN_TIMEOUT_MAX,
                ),
                session_start_timeout_secs=_safe_int(
                    agent_data.get("session_start_timeout_secs", 90),
                    90,
                    SESSION_START_TIMEOUT_MIN,
                    SESSION_START_TIMEOUT_MAX,
                ),
                tool_approval_timeout_secs=_safe_int(
                    agent_data.get("tool_approval_timeout_secs", 600),
                    600,
                    TOOL_APPROVAL_TIMEOUT_MIN,
                    TOOL_APPROVAL_TIMEOUT_MAX,
                ),
                # Absent means ON. The grant that decides who may reach a peer
                # session is the AGENT CONFIG, not this switch: the tools come
                # from the `kirocrew-dashboard` MCP server, so an agent that does
                # not mount it never has them -- the same rule as every other MCP
                # server. This stays as a single withdrawal for an operator who
                # wants the capability gone from every agent at once without
                # editing each spec, so an EXPLICIT `false` must still disable it:
                # `bool("false")` is `True`, and `_safe_bool` is what keeps a
                # quoted opt-out from loading as enabled.
                session_control=_safe_bool(agent_data.get("session_control", True), True),
                subagent_cost_gb=_safe_float(agent_data.get("subagent_cost_gb", 0.5), 0.5),
                subagent_cpu_cost_cores=_safe_float(
                    agent_data.get("subagent_cpu_cost_cores", 1.0), 1.0
                ),
                subagent_auto_max=_safe_int(
                    agent_data.get("subagent_auto_max", 32), 32, 3, SUBAGENT_AUTO_MAX_CEILING
                ),
                subagent_spawn_stagger_secs=_safe_float(
                    agent_data.get("subagent_spawn_stagger_secs", 2.0), 2.0
                ),
                spawn_min_memory_gb=_safe_float(agent_data.get("spawn_min_memory_gb", 4.0), 4.0),
                resource_pressure_gb=_safe_float(agent_data.get("resource_pressure_gb", 4.0), 4.0),
                resource_critical_gb=_safe_float(agent_data.get("resource_critical_gb", 2.0), 2.0),
                admission_gate=_safe_bool(agent_data.get("admission_gate"), True),
                subagent_max_turns=_safe_int(
                    agent_data.get("subagent_max_turns", 100), 100, 1, SUBAGENT_MAX_TURNS_CEILING
                ),
                subagent_timeout_secs=agent_data.get("subagent_timeout_secs", 1800),
                subagent_stall_idle_secs=_safe_int(
                    agent_data.get("subagent_stall_idle_secs", 120), 120
                ),
                completion_keep=_validated_completion_keep(
                    agent_data.get("completion_keep", "head")
                ),
                completion_keep_chars=_safe_int(
                    agent_data.get("completion_keep_chars", 3000),
                    3000,
                    COMPLETION_KEEP_CHARS_MIN,
                    COMPLETION_KEEP_CHARS_MAX,
                ),
                subagent_result_ttl_secs=_safe_int(
                    agent_data.get("subagent_result_ttl_secs", 3600), 3600
                ),
                workflow_run_timeout_secs=_safe_int(
                    agent_data.get("workflow_run_timeout_secs", 3600), 3600
                ),
                subagent_cwd_allowed_roots=(
                    [r for r in _roots if isinstance(r, str)]
                    if isinstance(_roots := agent_data.get("subagent_cwd_allowed_roots"), list)
                    else list(DEFAULT_CWD_ALLOWED_ROOTS)
                ),
                log_level=(
                    lvl.upper()
                    if isinstance(lvl := agent_data.get("log_level", "WARNING"), str)
                    else "WARNING"
                ),
                bot_name=_sanitize_bot_name(agent_data.get("bot_name", "")),
                max_channels=agent_data.get("max_channels", 1),
                max_channel_agents=agent_data.get("max_channel_agents", 3),
                soft_stop_budget_secs=max(
                    SOFT_STOP_BUDGET_MIN,
                    min(
                        SOFT_STOP_BUDGET_MAX,
                        _safe_float(agent_data.get("soft_stop_budget_secs", 10.0), 10.0),
                    ),
                ),
            ),
            session=SessionConfig(
                # The only field in this group whose site had no `_safe_int` at all, so
                # it is added here for consistency -- but NOT because the type was
                # unhandled. Verified: on the base revision a hand-edited `"abc"` or
                # `true` already loaded as the 3600 default, because
                # `_validate_config_data` runs over the raw dict before section
                # extraction and owns type handling. What was missing for this field, as
                # for the other ten, is the RANGE: an int of 999999999 loaded verbatim.
                timeout_secs=_safe_int(
                    session_data.get("timeout_secs", DEFAULT_SESSION_TIMEOUT),
                    DEFAULT_SESSION_TIMEOUT,
                    SESSION_TIMEOUT_MIN,
                    SESSION_TIMEOUT_MAX,
                ),
                empty_response_auto_continue=bool(
                    session_data.get("empty_response_auto_continue", True)
                ),
                autocompact_pct=_safe_float(
                    session_data.get("autocompact_pct", DEFAULT_AUTOCOMPACT_PCT),
                    DEFAULT_AUTOCOMPACT_PCT,
                    lo=AUTOCOMPACT_PCT_MIN,
                    hi=AUTOCOMPACT_PCT_MAX,
                ),
                pool_size=_safe_int(
                    session_data.get("pool_size", DEFAULT_POOL_SIZE),
                    DEFAULT_POOL_SIZE,
                    0,
                    POOL_SIZE_MAX,
                ),
                pool_agent=str(session_data.get("pool_agent", "")),
                pool_ttl_secs=_safe_int(
                    session_data.get("pool_ttl_secs", 1800),
                    1800,
                    POOL_TTL_SECS_MIN,
                    POOL_TTL_SECS_MAX,
                ),
                eager_spawn=bool(session_data.get("eager_spawn", True)),
                archive_retention_days=_archive_retention_days(session_data),
                watchdog_rss_max_mb=_safe_int(session_data.get("watchdog_rss_max_mb", 0), 0),
            ),
            taskrunner=TaskRunnerConfig(
                max_parallel_steps=taskrunner_data.get(
                    "max_parallel_steps", DEFAULT_MAX_PARALLEL_STEPS
                ),
                workspace_dir=str(taskrunner_data.get("workspace_dir", "")),
            ),
            cron_history=CronHistoryConfig(
                cron_summary_cap=_safe_int(cron_history_data.get("cron_summary_cap", 200), 200),
                cron_trace_cap_kb=_safe_int(cron_history_data.get("cron_trace_cap_kb", 50), 50),
                cron_max_records_per_job=_safe_int(
                    cron_history_data.get("cron_max_records_per_job", 100), 100
                ),
                cron_max_index_records=_safe_int(
                    cron_history_data.get("cron_max_index_records", 2000), 2000
                ),
            ),
            messaging=MessagingConfig(
                use_transport=bool(messaging_data.get("use_transport", True)),
                dm_scope=str(messaging_data.get("dm_scope", "per-channel-peer")),
                idle_reset_minutes=_coerce_int(messaging_data.get("idle_reset_minutes"), 0),
                daily_reset_hour=_coerce_int(messaging_data.get("daily_reset_hour"), -1),
                queue_mode=str(messaging_data.get("queue_mode", "steer")),
            ),
            # orchestrator/watchdog are advertised in config-baseline.json,
            # served by /api/config/schema, and read by real consumers
            # (acp/session_handle.py, dashboard/chat_orchestrator.py), so load()
            # passes these kwargs — without them config.json values would be
            # silently ignored and the dataclass defaults would always win.
            orchestrator=OrchestratorConfig(
                stage_timeout_seconds=_safe_int(
                    orchestrator_data.get("stage_timeout_seconds", 1800), 1800
                ),
            ),
            watchdog=WatchdogConfig(
                check_after_secs=_safe_float(watchdog_data.get("check_after_secs", 60.0), 60.0),
                stale_window_secs=_safe_float(watchdog_data.get("stale_window_secs", 300.0), 300.0),
                tool_stall_suspect_secs=_safe_float(
                    watchdog_data.get("tool_stall_suspect_secs", 3600.0), 3600.0
                ),
                tool_stall_hard_cap_secs=_safe_float(
                    watchdog_data.get("tool_stall_hard_cap_secs", 3600.0), 3600.0
                ),
                model_silent_probe_secs=_safe_float(
                    watchdog_data.get("model_silent_probe_secs", 900.0), 900.0
                ),
                wellness_sample_secs=_safe_float(
                    watchdog_data.get("wellness_sample_secs", 3.0), 3.0
                ),
            ),
            resource_limits=ResourceLimitsConfig.from_raw(resource_limits_data),
            telemetry=TelemetryConfig(
                enabled=bool(telemetry_data.get("enabled", False)),
                local_dir=str(telemetry_data.get("local_dir", "")),
                export_interval_seconds=_safe_int(
                    telemetry_data.get("export_interval_seconds", 60), 60
                ),
                retention_days=_safe_int(telemetry_data.get("retention_days", 0), 0),
                max_total_mb=_safe_int(telemetry_data.get("max_total_mb", 0), 0),
                otlp_endpoint=str(telemetry_data.get("otlp_endpoint", "")),
                beacon_enabled=bool(telemetry_data.get("beacon_enabled", True)),
                beacon_endpoint=str(
                    telemetry_data.get("beacon_endpoint", _DEFAULT_BEACON_ENDPOINT)
                ),
            ),
            memory=MemoryConfig(
                embedding_provider=_coerce_embedding_provider(
                    memory_data.get("embedding_provider", "llama_cpp")
                ),
                embedding_dim=memory_data.get("embedding_dim", 1024),
                embedding_threads=_safe_int(memory_data.get("embedding_threads", 4), 4, 1, 256),
                # 0 is the documented "inherit embedding_threads" sentinel, so the
                # floor is 0 rather than 1 — clamping it to 1 would erase a
                # deliberate opt-in to the interactive pool.
                embedding_bulk_threads=_safe_int(
                    memory_data.get("embedding_bulk_threads", 1), 1, 0, 256
                ),
                embedding_bulk_duty=_safe_float(
                    memory_data.get("embedding_bulk_duty", 0.2), 0.2, 0.05, 1.0
                ),
                embed_model_url=memory_data.get("embed_model_url", ""),
                embed_model_path=memory_data.get("embed_model_path", ""),
                embed_model_id=memory_data.get("embed_model_id", ""),
                semantic_confidence_threshold=memory_data.get("semantic_confidence_threshold", 0.8),
                episodic_dedup_threshold=memory_data.get("episodic_dedup_threshold", 0.88),
                episodic_max_results=memory_data.get("episodic_max_results", 8),
                episodic_max_count=memory_data.get("episodic_max_count", 10_000),
                decay_rates=(
                    dr if isinstance(dr := memory_data.get("decay_rates", {}), dict) else {}
                ),
                semantic_keys=memory_data.get("semantic_keys", []),
                history_idle_hours=memory_data.get("history_idle_hours", 3.0),
                history_max_days=_safe_nonnegative_int(
                    memory_data.get("history_max_days", 365), 365
                ),
                migrated=memory_data.get("migrated", False),
            ),
            knowledge=KnowledgeConfig(
                auto_ingest_artifacts=bool(knowledge_data.get("auto_ingest_artifacts", False)),
                auto_ingest_artifact_kinds=[
                    k
                    for k in knowledge_data.get(
                        "auto_ingest_artifact_kinds",
                        DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
                    )
                    if isinstance(k, str)
                ],
                max_ingest_file_mb=(
                    float(mb)
                    if isinstance(
                        (mb := knowledge_data.get("max_ingest_file_mb", 100.0)),
                        (int, float),
                    )
                    and not isinstance(mb, bool)
                    and mb >= 0
                    else 100.0
                ),
                embed_timeout_secs=_safe_float(
                    knowledge_data.get("embed_timeout_secs", 10.0), 10.0
                ),
                embed_content_budget=_safe_int(knowledge_data.get("embed_content_budget", 0), 0),
                pool_idle_ttl_secs=_safe_nonnegative_int(
                    knowledge_data.get("pool_idle_ttl_secs", 300),
                    300,
                ),
                auto_add_documents=_read_auto_add_documents(knowledge_data),
                folder_ingest_chunk_budget=_safe_nonnegative_int(
                    knowledge_data.get("folder_ingest_chunk_budget", 300),
                    300,
                    FOLDER_INGEST_CHUNK_BUDGET_MAX,
                ),
                dedup_every_n_sweeps=_safe_nonnegative_int(
                    knowledge_data.get("dedup_every_n_sweeps", 12),
                    12,
                    DEDUP_EVERY_N_SWEEPS_MAX,
                ),
                doc_ingest_hosts=[
                    str(h)
                    for h in knowledge_data.get("doc_ingest_hosts", [])
                    if isinstance(h, str) and h.strip()
                ],
                sweep_chunk_budget=_safe_nonnegative_int(
                    knowledge_data.get("sweep_chunk_budget", 500),
                    500,
                    SWEEP_CHUNK_BUDGET_MAX,
                ),
                embed_rate_limit=_safe_nonnegative_int(
                    knowledge_data.get("embed_rate_limit", 120), 120, EMBED_RATE_LIMIT_MAX
                ),
                extraction_model=str(knowledge_data.get("extraction_model", "")).strip(),
                extraction_pool_size=max(
                    EXTRACTION_POOL_SIZE_MIN,
                    min(
                        EXTRACTION_POOL_SIZE_MAX,
                        _safe_nonnegative_int(knowledge_data.get("extraction_pool_size", 3), 3),
                    ),
                ),
            ),
            telegram=TelegramConfig(
                session_folder=_coerce_session_folder(telegram_data.get("session_folder")),
                enabled=bool(telegram_data.get("enabled", False)),
                bot_token=str(telegram_data.get("bot_token", "")),
                allowed_user_ids=_coerce_int_ids(telegram_data.get("allowed_user_ids")),
                soft_threshold_pct=_threshold_pct(telegram_data.get("soft_threshold_pct"), 80),
                show_thinking=bool(telegram_data.get("show_thinking", False)),
                allow_forum=bool(telegram_data.get("allow_forum", False)),
                voice_replies=bool(telegram_data.get("voice_replies", False)),
                forum_activation=_validate_telegram_activation(
                    str(telegram_data.get("forum_activation", "") or ACTIVATION_ALWAYS)
                ),
                allowed_forum_chat_ids=_coerce_int_ids(telegram_data.get("allowed_forum_chat_ids")),
                accounts=_parse_telegram_accounts(telegram_data.get("accounts")),
            ),
            weixin=WeixinConfig(
                session_folder=_coerce_session_folder(weixin_data.get("session_folder")),
                enabled=bool(weixin_data.get("enabled", False)),
                token=str(weixin_data.get("token", "")),
                account_id=str(weixin_data.get("account_id", "")),
                base_url=str(weixin_data.get("base_url", "") or "https://ilinkai.weixin.qq.com"),
                dm_policy=str(weixin_data.get("dm_policy", "allowlist") or "allowlist"),
                allowed_user_ids=_coerce_opaque_str_ids(weixin_data.get("allowed_user_ids")),
                soft_threshold_pct=_threshold_pct(weixin_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(weixin_data.get("hard_threshold_pct"), 95),
            ),
            whatsapp=WhatsAppConfig(
                session_folder=_coerce_session_folder(whatsapp_data.get("session_folder")),
                enabled=bool(whatsapp_data.get("enabled", False)),
                dm_policy=str(whatsapp_data.get("dm_policy", "self") or "self"),
                allowed_wa_ids=_coerce_str_ids(whatsapp_data.get("allowed_wa_ids")),
                groups=_coerce_whatsapp_groups(whatsapp_data.get("groups")),
                db_path=str(whatsapp_data.get("db_path", "")),
                soft_threshold_pct=_threshold_pct(whatsapp_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(whatsapp_data.get("hard_threshold_pct"), 95),
            ),
            discord=DiscordConfig(
                session_folder=_coerce_session_folder(discord_data.get("session_folder")),
                enabled=bool(discord_data.get("enabled", False)),
                bot_token=str(discord_data.get("bot_token", "")),
                # Discord user IDs are numeric snowflakes that exceed 2^53 —
                # keep them as strings (JSON round-trip safe, matches the
                # transport's string comparison).
                allowed_user_ids=_coerce_str_ids(discord_data.get("allowed_user_ids")),
                allowed_thread_ids=_coerce_str_ids(discord_data.get("allowed_thread_ids")),
                allowed_channel_ids=_coerce_str_ids(discord_data.get("allowed_channel_ids")),
                auto_thread=bool(discord_data.get("auto_thread", True)),
                soft_threshold_pct=_threshold_pct(discord_data.get("soft_threshold_pct"), 80),
                reactions_enabled=bool(discord_data.get("reactions_enabled", True)),
                show_thinking=bool(discord_data.get("show_thinking", False)),
            ),
            webex=WebexConfig(
                session_folder=_coerce_session_folder(webex_data.get("session_folder")),
                enabled=bool(webex_data.get("enabled", False)),
                bot_token=str(webex_data.get("bot_token", "")),
                allowed_emails=(
                    [e for e in webex_data.get("allowed_emails", []) if isinstance(e, str) and e]
                    if isinstance(webex_data.get("allowed_emails", []), list)
                    else []
                ),
                # Group spaces are a SECURITY decision, so the read is as explicit
                # as the write: a field the loader forgets is not merely lost, it
                # silently reverts to the safe default on the next restart while
                # the settings panel keeps showing the saved value it read from
                # config.json — the operator sees an enabled space allow-list and
                # the gateway answers nobody.
                allow_group_rooms=bool(webex_data.get("allow_group_rooms", False)),
                allowed_room_ids=[
                    r
                    for r in _safe_list(webex_data.get("allowed_room_ids"))
                    if isinstance(r, str) and r
                ],
                reply_in_thread=bool(webex_data.get("reply_in_thread", True)),
                wdm_base=str(webex_data.get("wdm_base", "") or ""),
                soft_threshold_pct=_threshold_pct(webex_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(webex_data.get("hard_threshold_pct"), 95),
            ),
            wakatime=WakaTimeConfig(
                enabled=bool(wakatime_data.get("enabled", False)),
                api_base_url=str(wakatime_data.get("api_base_url", "") or ""),
            ),
            imessage=IMessageConfig(
                session_folder=_coerce_session_folder(imessage_data.get("session_folder")),
                enabled=bool(imessage_data.get("enabled", False)),
                db_path=str(imessage_data.get("db_path", "")),
                allowed_handles=[
                    h
                    for h in _safe_list(imessage_data.get("allowed_handles"))
                    if isinstance(h, str) and h
                ],
                service=str(imessage_data.get("service", "") or "imessage"),
                soft_threshold_pct=_threshold_pct(imessage_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(imessage_data.get("hard_threshold_pct"), 95),
            ),
            teams=TeamsConfig(
                session_folder=_coerce_session_folder(teams_data.get("session_folder")),
                enabled=bool(teams_data.get("enabled", False)),
                app_id=str(teams_data.get("app_id", "")),
                # Secret is env-only (MICROSOFT_APP_PASSWORD). Never sourced from
                # config.json, which the agent can read — keeps the Azure Bot
                # credential out of any agent-readable file.
                app_password="",
                tenant_id=str(teams_data.get("tenant_id", "")),
                allowed_emails=(
                    [e for e in teams_data.get("allowed_emails", []) if isinstance(e, str) and e]
                    if isinstance(teams_data.get("allowed_emails", []), list)
                    else []
                ),
                soft_threshold_pct=_threshold_pct(teams_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(teams_data.get("hard_threshold_pct"), 95),
            ),
            slack=SlackConfig(
                session_folder=_coerce_session_folder(slack_data.get("session_folder")),
                allowed_users=[
                    u
                    for u in slack_data.get("allowed_users", [])
                    if isinstance(u, dict) and u.get("slack_id")
                ],
                tracking_channels=_validate_tracking_channels(
                    slack_data.get("tracking_channels", [])
                ),
                open_channels=[
                    c for c in slack_data.get("open_channels", []) if isinstance(c, str)
                ],
                command=slack_data.get("command", "kirocrew"),
                forward_to_agent_callback=str(
                    slack_data.get("forward_to_agent_callback") or ""
                ).strip(),
                trusted_bot_ids={
                    b for b in _safe_list(slack_data.get("trusted_bot_ids")) if isinstance(b, str)
                },
                trusted_bot_turn_limit=_safe_int(
                    slack_data.get("trusted_bot_turn_limit", 5), 5, lo=1
                ),
                allowed_enterprise_ids=[
                    e
                    for e in slack_data.get("allowed_enterprise_ids", [])
                    if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
                ],
                reactions={
                    k: v
                    for k, v in _safe_dict(slack_data.get("reactions")).items()
                    if isinstance(k, str) and (v is None or (isinstance(v, str) and v))
                },
                reactions_enabled=bool(slack_data.get("reactions_enabled", True)),
                use_tunnel_url=bool(slack_data.get("use_tunnel_url", False)),
                show_thinking=bool(slack_data.get("show_thinking", True)),
                home_tab_sessions_per_kind=_safe_int(
                    slack_data.get("home_tab_sessions_per_kind", 5), 5
                ),
            ),
            publish=PublishConfig(
                allowed_destinations=[d for d in _dests_raw if isinstance(d, str) and d],
                relocate_roots=[
                    r
                    for r in publish_data.get("relocate_roots", [])
                    if isinstance(r, str) and r.strip()
                ],
            ),
            wecom=WeComConfig(
                session_folder=_coerce_session_folder(wecom_data.get("session_folder")),
                # _safe_bool, not bool(): `bool("false")` is True, so a JSON string
                # would read the operator's "off" as "on" -- enabling a channel,
                # or opening it to every org member, from a config value that says the
                # opposite. A non-bool must read as the default, not as truthy.
                enabled=_safe_bool(wecom_data.get("enabled"), False),
                allowed_users=[
                    u
                    for u in _safe_list(wecom_data.get("allowed_users"))
                    if isinstance(u, dict) and u.get("userid")
                ],
                allow_all_users=_safe_bool(wecom_data.get("allow_all_users"), False),
                ws_url=str(wecom_data.get("ws_url", "wss://openws.work.weixin.qq.com")),
                soft_threshold_pct=_threshold_pct(wecom_data.get("soft_threshold_pct"), 80),
                hard_threshold_pct=_threshold_pct(wecom_data.get("hard_threshold_pct"), 95),
            ),
            feishu=FeishuConfig(
                enabled=_safe_bool(feishu_data.get("enabled"), False),
                allowed_open_ids=_coerce_opaque_str_ids(feishu_data.get("allowed_open_ids")),
                # Shape-safe coercion rather than bool() / a raw comprehension:
                # the schema type check already substitutes the default for a
                # wrong-typed value, and these helpers keep the guarantee local
                # to the parse (and dedupe + strip the opaque ou_/oc_ ids).
                allow_group=_safe_bool(feishu_data.get("allow_group"), False),
                allowed_group_ids=_coerce_opaque_str_ids(feishu_data.get("allowed_group_ids")),
                soft_threshold_pct=_safe_int(feishu_data.get("soft_threshold_pct", 80), 80),
                hard_threshold_pct=_safe_int(feishu_data.get("hard_threshold_pct", 95), 95),
                session_folder=_coerce_session_folder(feishu_data.get("session_folder")),
            ),
            dashboard=DashboardConfig(
                url=dashboard_data.get("url", ""),
                tailscale=_tailscale_config_from(
                    dashboard_data.get("tailscale"),
                    _degraded,
                    key_present="tailscale" in dashboard_data,
                ),
                restore_sessions=dashboard_data.get("restore_sessions", False),
                qr_session_until_restart=_safe_bool(
                    dashboard_data.get("qr_session_until_restart"), True
                ),
                qr_session_persist_across_restart=_safe_bool(
                    dashboard_data.get("qr_session_persist_across_restart"), False
                ),
                restore_window_minutes=dashboard_data.get("restore_window_minutes", 30),
                surface_channel_sessions=dashboard_data.get("surface_channel_sessions", True),
                bot_name=dashboard_data.get("bot_name", ""),
                avatar=dashboard_data.get("avatar", ""),
                merge_queued_messages=dashboard_data.get("merge_queued_messages", False),
                mcp_probe_timeout_secs=_safe_int(
                    dashboard_data.get("mcp_probe_timeout_secs", 15),
                    15,
                    MCP_PROBE_TIMEOUT_MIN,
                    MCP_PROBE_TIMEOUT_MAX,
                ),
                loop_stall_exit_after_secs=(
                    None
                    if dashboard_data.get("loop_stall_exit_after_secs") is None
                    else _safe_int(
                        dashboard_data.get("loop_stall_exit_after_secs"),
                        LOOP_STALL_EXIT_AFTER_DEFAULT,
                        LOOP_STALL_EXIT_AFTER_MIN,
                        LOOP_STALL_EXIT_AFTER_MAX,
                    )
                ),
                chat_entry_cache_max_entries=_safe_int(
                    dashboard_data.get(
                        "chat_entry_cache_max_entries", CHAT_ENTRY_CACHE_ENTRIES_DEFAULT
                    ),
                    CHAT_ENTRY_CACHE_ENTRIES_DEFAULT,
                    CHAT_ENTRY_CACHE_ENTRIES_MIN,
                    CHAT_ENTRY_CACHE_ENTRIES_MAX,
                ),
                chat_entry_cache_max_bytes=_safe_int(
                    dashboard_data.get(
                        "chat_entry_cache_max_bytes", CHAT_ENTRY_CACHE_BYTES_DEFAULT
                    ),
                    CHAT_ENTRY_CACHE_BYTES_DEFAULT,
                    CHAT_ENTRY_CACHE_BYTES_MIN,
                    CHAT_ENTRY_CACHE_BYTES_MAX,
                ),
                cautious_boot=_safe_bool(dashboard_data.get("cautious_boot"), True),
                auto_open_browser=dashboard_data.get("auto_open_browser", True),
                prevent_sleep=_safe_bool(dashboard_data.get("prevent_sleep"), False),
                quick_send=dashboard_data.get("quick_send", False),
                session_grid=dashboard_data.get("session_grid", False),
                mcp_app_panel=dashboard_data.get("mcp_app_panel", False),
                auto_open_git_panel=_safe_bool(dashboard_data.get("auto_open_git_panel"), False),
                session_card_source_links=_safe_bool(
                    dashboard_data.get("session_card_source_links"), True
                ),
                widget_density=dashboard_data.get("widget_density", "more"),
                use_builtin_browser=_safe_bool(dashboard_data.get("use_builtin_browser"), True),
                browser_view_port=_port_or_unset(dashboard_data.get("browser_view_port", 0)),
                verbosity=dashboard_data.get("verbosity", "default"),
                link_previews=_safe_bool(dashboard_data.get("link_previews"), False),
                usage_text_scrape_enabled=_safe_bool(
                    dashboard_data.get("usage_text_scrape_enabled"), False
                ),
                tail_fork_enabled=dashboard_data.get("tail_fork_enabled", False),
                terminal=dashboard_data.get("terminal", {"enabled": True}),
                default_project=dashboard_data.get("default_project", ""),
                theme_mode=dashboard_data.get("theme_mode", ""),
                sso_login_flags=str(dashboard_data.get("sso_login_flags", "")),
                theme_color=dashboard_data.get("theme_color", ""),
                language=str(dashboard_data.get("language", "")),
                recent_tint_count=_safe_int(
                    dashboard_data.get("recent_tint_count", 0),
                    0,
                    RECENT_TINT_COUNT_MIN,
                    RECENT_TINT_COUNT_MAX,
                ),
                update_nudge=(
                    dashboard_data.get("update_nudge", {})
                    if isinstance(dashboard_data.get("update_nudge"), dict)
                    else {}
                ),
                onboarded=bool(dashboard_data.get("onboarded", False)),
                import_onboarded=_safe_bool(
                    dashboard_data.get("import_onboarded"),
                    _safe_bool(dashboard_data.get("onboarded"), False),
                ),
                # Falls back to `onboarded`: a user who finished first run before
                # this chapter existed has already reached the product, and
                # re-gating their heartbeat on a screen they will never be shown
                # would suppress it forever.
                privacy_acked=_safe_bool(
                    dashboard_data.get("privacy_acked"),
                    _safe_bool(dashboard_data.get("onboarded"), False),
                ),
                user_role=str(dashboard_data.get("user_role", "")),
                user_role_other=str(dashboard_data.get("user_role_other", "")),
                user_technical_level=str(dashboard_data.get("user_technical_level", "")),
                tips_enabled=bool(dashboard_data.get("tips_enabled", True)),
                folder_suggestions_enabled=bool(
                    dashboard_data.get("folder_suggestions_enabled", True)
                ),
                tips_cadence_hours=_safe_float(
                    dashboard_data.get("tips_cadence_hours", 6.0), 6.0, lo=0.0
                ),
                tips_snooze_hours=_safe_float(
                    dashboard_data.get("tips_snooze_hours", 48.0), 48.0, lo=0.0
                ),
                tips_recency_decay=_safe_float(
                    dashboard_data.get("tips_recency_decay", 0.6), 0.6, lo=0.0, hi=1.0
                ),
                tips_model=str(dashboard_data.get("tips_model", "auto")),
                tips_explore_ratio=_safe_float(
                    dashboard_data.get("tips_explore_ratio", 0.2), 0.2, lo=0.0, hi=1.0
                ),
                gitlab_hosts=_coerce_gitlab_hosts(dashboard_data.get("gitlab_hosts")),
                jira_hosts=_coerce_jira_hosts(dashboard_data.get("jira_hosts")),
                jira_auth=[
                    JiraAuthEntry(
                        host=str(entry.get("host", "")),
                        email=str(entry.get("email", "")),
                    )
                    for entry in (dashboard_data.get("jira_auth") or [])
                    if isinstance(entry, dict) and entry.get("host")
                ],
            ),
            tunnel=TunnelConfig(
                enabled=bool(tunnel_data.get("enabled", False)),
                name_mode=str(tunnel_data.get("name_mode", "username")),
                name_override=str(tunnel_data.get("name_override", "")),
            ),
            hooks=data.get("hooks", {}),
            agents=agents,
            default_agent=default_agent_val,
            workspaces=workspaces,
            default_workspace=data.get("default_workspace", "default"),
            memory_stores=memory_stores,
            default_memory_store=default_memory_store_val,
            # Every default below restates its dataclass default, and the two must
            # stay equal: the branch above returns bare dataclass defaults when
            # neither config file exists, so a disagreement gives one field two
            # different defaults depending on whether a config.json is present, and
            # the schema, the docs and the doctor can only describe one of them.
            stt=SttConfig(
                enabled=_safe_bool(stt_data.get("enabled"), True),
                provider=_validated_stt_provider(stt_data.get("provider", STT_PROVIDER_LOCAL)),
                model=_validated_stt_model(stt_data.get("model", _STT_DEFAULT_MODEL)),
                language_code=stt_data.get("language_code", "en-US"),
                streaming=_safe_bool(stt_data.get("streaming"), True),
                silence_ms=_safe_int(
                    stt_data.get("silence_ms"),
                    _STT_DEFAULT_SILENCE_MS,
                    lo=_STT_MIN_SILENCE_MS,
                    hi=_STT_INTERVAL_MS_MAX,
                ),
                partial_interval_ms=_safe_int(
                    stt_data.get("partial_interval_ms"),
                    _STT_DEFAULT_PARTIAL_INTERVAL_MS,
                    lo=_STT_MIN_PARTIAL_INTERVAL_MS,
                    hi=_STT_INTERVAL_MS_MAX,
                ),
                idle_evict_secs=_safe_int(
                    stt_data.get("idle_evict_secs"),
                    _STT_DEFAULT_IDLE_EVICT_SECS,
                    lo=_STT_IDLE_EVICT_SECS_MIN,
                    hi=_STT_IDLE_EVICT_SECS_MAX,
                ),
                endpointing=_safe_bool(stt_data.get("endpointing"), False),
                dictation_panel=_safe_bool(stt_data.get("dictation_panel"), True),
                openai_api_key=stt_data.get("openai_api_key", ""),
                openai_model=stt_data.get("openai_model", "whisper-1"),
                timeout_secs=_safe_int(
                    stt_data.get("timeout_secs"),
                    _STT_DEFAULT_TIMEOUT_SECS,
                    lo=_STT_MIN_TIMEOUT_SECS,
                    hi=_STT_MAX_TIMEOUT_SECS,
                ),
                transcribe_region=stt_data.get("transcribe_region", "us-east-1"),
                transcribe_profile=stt_data.get("transcribe_profile", ""),
            ),
            # Every numeric knob is clamped to the same ceiling the MCP tool
            # schemas enforce, so a hand-edited config.json cannot ask for an
            # unbounded accessibility walk or a full-resolution screenshot.
            # There is deliberately NO ``enabled`` key read here — see
            # ComputerUseConfig's docstring and computer_use_state_path().
            computer_use=ComputerUseConfig(
                max_tree_nodes=min(
                    _CU_MAX_TREE_NODES,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get("max_tree_nodes", _CU_DEFAULT_MAX_TREE_NODES),
                            _CU_DEFAULT_MAX_TREE_NODES,
                        ),
                    ),
                ),
                max_tree_depth=min(
                    _CU_MAX_TREE_DEPTH,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get("max_tree_depth", _CU_DEFAULT_MAX_TREE_DEPTH),
                            _CU_DEFAULT_MAX_TREE_DEPTH,
                        ),
                    ),
                ),
                text_limit=min(
                    _CU_MAX_TEXT_LIMIT,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get("text_limit", _CU_DEFAULT_TEXT_LIMIT),
                            _CU_DEFAULT_TEXT_LIMIT,
                        ),
                    ),
                ),
                attach_screenshot=_safe_bool(
                    computer_use_data.get("attach_screenshot", _CU_DEFAULT_ATTACH_SCREENSHOT),
                    _CU_DEFAULT_ATTACH_SCREENSHOT,
                ),
                screenshot_max_px=min(
                    _CU_MAX_SCREENSHOT_MAX_PX,
                    max(
                        _CU_MIN_SCREENSHOT_MAX_PX,
                        _safe_int(
                            computer_use_data.get(
                                "screenshot_max_px", _CU_DEFAULT_SCREENSHOT_MAX_PX
                            ),
                            _CU_DEFAULT_SCREENSHOT_MAX_PX,
                        ),
                    ),
                ),
                screenshot_jpeg_quality=min(
                    100,
                    max(
                        1,
                        _safe_int(
                            computer_use_data.get(
                                "screenshot_jpeg_quality", _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY
                            ),
                            _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
                        ),
                    ),
                ),
                # Default False: a missing or unparseable value must mean "do not
                # draw on the operator's screen", never the reverse.
                cursor_motion=_safe_bool(computer_use_data.get("cursor_motion", False), False),
            ),
            auto_update=data.get("auto_update", True),
            connections_ui=_safe_bool(data.get("connections_ui", False), False),
            _degraded_sections=frozenset(_degraded | _OBSERVED_DEGRADED_SECTIONS),
            timezone=data.get("timezone", ""),
            snapshot_dir=data.get("snapshot_dir", ""),
            registries=[
                ExternalRegistryConfig(
                    name=str(r.get("name", "")),
                    repo=str(r.get("repo", "")),
                    # Backward-compat: an entry that OMITS ``branch`` is a legacy
                    # config written before URL registries defaulted new entries
                    # to ``main`` (the registries PUT API now always persists an
                    # explicit branch). Such an entry relied on the historical
                    # ``mainline`` default, so preserve it here — silently
                    # retargeting it to ``main`` on upgrade would break any
                    # registry whose content still lives on ``mainline``.
                    branch=str(r.get("branch", "mainline")),
                    # A credential-posture decision, so it is read back verbatim
                    # and validated downstream rather than here: an unrecognised
                    # value must resolve to the restrictive tier, which
                    # ``registry._registry_trust_tier`` does. Absent -> "index",
                    # so a config written before the field existed keeps the
                    # credential-free posture it had.
                    trust=str(r.get("trust", "index")),
                )
                for r in (data.get("registries") or [])
                if isinstance(r, dict) and r.get("repo")
            ],
            mcp_gateway=McpGatewayConfig(
                enabled=bool(mcp_gateway_data.get("enabled", False)),
                # Absent -> True so installs that never configured this keep
                # rendering. A malformed value cannot be distinguished here: the
                # schema validator REMOVES an invalid value before the loader
                # parses (see config/validation.py ``_apply_field_default``), so a
                # hand-edited ``"false"`` arrives as absent and resolves to True,
                # with a warning logged naming the field. ``_safe_bool`` is
                # belt-and-braces for a schema gap, not the acting guard — the
                # acting guard against a truthy string is the validator, since
                # ``bool("false")`` is True. The write path is where an opt-out is
                # actually enforced: the endpoint rejects any non-boolean body.
                apps_enabled=_safe_bool(mcp_gateway_data.get("apps_enabled", True), True),
                # ON by default. The forwarded set is a strict subset of the
                # hashed set and gatewayd re-hashes the sidecar at spawn,
                # forwarding nothing on mismatch, so a forwarded key is one every
                # co-tenant of that backend declared identically. With it off, one
                # ordinary declared key costs the whole server its pooling.
                #
                # Both arguments are True on purpose. A malformed value never
                # reaches this call: ``config.validation`` type-checks first and
                # ``_apply_field_default`` strips a non-boolean so the dataclass
                # default applies, which is why the log says "using default". The
                # fallback here is defence in depth for a bypassed validator, and
                # giving it a different answer than the schema would only put two
                # disagreeing defaults in the file.
                forward_declared_env=_safe_bool(
                    mcp_gateway_data.get("forward_declared_env", FORWARD_DECLARED_ENV_DEFAULT),
                    FORWARD_DECLARED_ENV_DEFAULT,
                ),
                socket_path=str(mcp_gateway_data.get("socket_path", "")),
                overlay_dir=str(mcp_gateway_data.get("overlay_dir", "")),
                idle_timeout_secs=max(
                    10, _safe_int(mcp_gateway_data.get("idle_timeout_secs", 300), 300)
                ),
                # 0 is meaningful (re-resolve every pass), so the floor is 0 and
                # not the usual "at least something" clamp.
                resolve_once_refresh_hours=max(
                    0, _safe_int(mcp_gateway_data.get("resolve_once_refresh_hours", 24), 24)
                ),
                max_backends=max(1, _safe_int(mcp_gateway_data.get("max_backends", 64), 64)),
                poolable_servers=[
                    s for s in mcp_gateway_data.get("poolable_servers", []) if isinstance(s, str)
                ],
                stub_servers=_resolve_stub_servers(mcp_gateway_data),
                # The operator's deviations, kept ALONGSIDE the resolved set above
                # rather than folded away: ``stub_servers`` here is already the
                # effective answer, so a writer that wants to record a new
                # decision needs to see which ones are decisions and which came
                # from the roster. Shares the resolver with the runtime so a
                # non-bool value is dropped in exactly one place.
                stub_overrides=_resolve_stub_overrides(mcp_gateway_data),
                # The file's own roster, carried so ``save()`` can put it back
                # instead of flattening it to the effective set above. See the
                # field's own comment for why that flattening is a data loss.
                _stub_roster=_resolve_stub_roster(mcp_gateway_data),
                # Hand-editable list of env NAMES; keep only strings and drop
                # blanks so a stray null or nested object cannot reach the
                # hashing layer as a key. Not deduplicated here — every consumer
                # builds a frozenset from it.
                pool_identity_env=[
                    s.strip()
                    for s in mcp_gateway_data.get("pool_identity_env", [])
                    if isinstance(s, str) and s.strip()
                ],
                prewarm_count=max(0, _safe_int(mcp_gateway_data.get("prewarm_count", 0), 0)),
                read_buffer_limit_bytes=max(
                    1024,
                    _safe_int(
                        mcp_gateway_data.get("read_buffer_limit_bytes", 64 * 1024 * 1024),
                        64 * 1024 * 1024,
                    ),
                ),
                response_spill_threshold_bytes=max(
                    0,
                    _safe_int(
                        mcp_gateway_data.get("response_spill_threshold_bytes", 256 * 1024),
                        256 * 1024,
                    ),
                ),
            ),
            mcp=McpConfig(
                # Kept as authored strings — validation (absolute-only, ``~``
                # expansion, dedup) belongs to the consumer,
                # kiro_crew.env.augmented_path, so the ONE gate the built-in
                # directories already pass applies to these too instead of a
                # second rule drifting here. Non-strings ARE dropped now: the
                # field is typed list[str] and to_dict() round-trips it verbatim
                # into the saved config.
                extra_path_dirs=[
                    d for d in _safe_list(mcp_data.get("extra_path_dirs", [])) if isinstance(d, str)
                ],
            ),
            instances=InstancesConfig(
                enabled=bool(instances_data.get("enabled", False)),
                warm_set_cap=_safe_int(
                    instances_data.get("warm_set_cap", _DEFAULT_WARM_SET_CAP), _DEFAULT_WARM_SET_CAP
                ),
                tunnel_base_port=_safe_int(
                    instances_data.get("tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT),
                    _DEFAULT_TUNNEL_BASE_PORT,
                ),
                ssh_compression=bool(
                    instances_data.get("ssh_compression", _DEFAULT_SSH_COMPRESSION)
                ),
                connect_timeout_secs=(
                    _safe_float(connect_timeout_raw, _DEFAULT_CONNECT_TIMEOUT)
                    if connect_timeout_raw is not None
                    else None
                ),
                mint_timeout_secs=(
                    _safe_float(mint_timeout_raw, _DEFAULT_MINT_TIMEOUT)
                    if mint_timeout_raw is not None
                    else None
                ),
                max_recovery_attempts=_safe_int(
                    instances_data.get("max_recovery_attempts", _DEFAULT_MAX_RECOVERY),
                    _DEFAULT_MAX_RECOVERY,
                ),
                recover_backoff_max_secs=_safe_float(
                    instances_data.get("recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX),
                    _DEFAULT_BACKOFF_MAX,
                ),
                probe_failure_threshold=_safe_int(
                    instances_data.get("probe_failure_threshold", _DEFAULT_PROBE_FAILS),
                    _DEFAULT_PROBE_FAILS,
                ),
            ),
            heartbeat=HeartbeatConfig(default_deliver=heartbeat_default_deliver),
            skills=SkillsConfig(
                max_triggered=_safe_int(skills_data.get("max_triggered", 0), 0),
                lazy_load=bool(skills_data.get("lazy_load", False)),
                auto_create_from_sessions=bool(skills_data.get("auto_create_from_sessions", False)),
                auto_refine_on_deviation=bool(skills_data.get("auto_refine_on_deviation", False)),
                auto_min_tool_calls=_safe_int(skills_data.get("auto_min_tool_calls", 5), 5),
                auto_similarity_threshold=_safe_float(
                    skills_data.get("auto_similarity_threshold", 0.85), 0.85
                ),
                approval_required=bool(skills_data.get("approval_required", True)),
                max_auto_skills=_safe_int(skills_data.get("max_auto_skills", 100), 100),
                stale_after_days=_safe_int(skills_data.get("stale_after_days", 30), 30),
                archive_after_days=_safe_int(skills_data.get("archive_after_days", 90), 90),
                pending_ttl_days=_safe_int(skills_data.get("pending_ttl_days", 30), 30),
                generate_scripts=bool(skills_data.get("generate_scripts", True)),
                judge_model=str(skills_data.get("judge_model", "auto") or "auto"),
                extra_paths=[
                    p for p in _safe_list(skills_data.get("extra_paths")) if isinstance(p, str)
                ],
                # Security off-switch: malformed values must not become truthy
                # through Python coercion (for example, the string "false").
                project_skills_enabled=(skills_data.get("project_skills_enabled", True) is True),
            ),
            session_summary=SessionSummaryConfig(
                enabled=bool(session_summary_data.get("enabled", False)),
                min_user_turns=_safe_int(session_summary_data.get("min_user_turns", 2), 2),
                regenerate_after_turns=_safe_int(
                    session_summary_data.get("regenerate_after_turns", 1), 1
                ),
                max_intents=_safe_int(session_summary_data.get("max_intents", 50), 50),
                max_constraints=_safe_int(session_summary_data.get("max_constraints", 50), 50),
                assistant_excerpt_chars=_safe_int(
                    session_summary_data.get("assistant_excerpt_chars", 400), 400
                ),
            ),
            slack_channels={
                ch_id: ChannelConfig.from_dict(ch_data)
                for ch_id, ch_data in (
                    slack_data.get("channels", {})
                    if isinstance(slack_data.get("channels"), dict)
                    else {}
                ).items()
                if isinstance(ch_data, dict)
            },
            slack_dm_activation=_validate_activation(
                slack_data.get("dm_activation", ACTIVATION_ALWAYS)
            ),
            observe_max_messages=max(
                1, _safe_int(slack_data.get("observe_max_messages", 200), 200)
            ),
            observe_ttl_hours=max(
                0.0, _safe_float(slack_data.get("observe_ttl_hours", 168.0), 168.0)
            ),
            _extra_sections=extra_sections,
        )

        # Write-back migration: if the on-disk config has legacy format
        # (flat workspace strings, missing sections), back up the original
        # and save the migrated version.  One-shot — subsequent loads see
        # the canonical format and skip.
        #
        # The in-memory half below mutates `cfg` and RECORDS which migrations it
        # decided on; the on-disk half re-reads config.json inside the write lock
        # and applies exactly those as a delta (see _persist_config_migration).
        # It used to be `cfg.save()`, which re-serialized this load's whole
        # snapshot and so dropped any config write that landed after this load's
        # read (#7793).
        try:
            pending: set[str] = set()
            # Flat workspace strings → need migration to {"dir": ...}
            for v in raw_workspaces.values():
                if isinstance(v, str):
                    pending.add(MIGRATE_WORKSPACES)
                    break

            # One-time migration: create default agent when none exists
            if not cfg.agents:
                kiro = cfg.agent.default_agent or "kirocrew"
                cfg.agents["default"] = KiroCrewAgentConfig(
                    kiro_agent=kiro,
                    workspace="default",
                    memory_store="default",
                )
                pending.add(MIGRATE_AGENTS)
            if not cfg.default_agent or cfg.default_agent not in cfg.agents:
                # Prefer "default" if it exists, otherwise use first available agent
                if "default" in cfg.agents:
                    cfg.default_agent = "default"
                elif cfg.agents:
                    cfg.default_agent = next(iter(cfg.agents))
                else:
                    cfg.default_agent = "default"
                pending.add(MIGRATE_DEFAULT_AGENT)

            needs_migration = bool(pending)

            if needs_migration and not cfg._degraded_sections:
                _persist_config_migration(
                    path,
                    frozenset(pending),
                    default_kiro_agent=cfg.agent.default_agent or "kirocrew",
                )
            elif needs_migration:
                # This load DISCARDED something (a malformed section, an
                # unreadable file). The write-back serializes only the parsed
                # fields, so writing back here would replace the operator's
                # malformed narrowing with clean defaults — erasing the only
                # on-disk evidence and turning the denial into silent
                # allow-all at the next restart (#4057). Keep the malformed
                # bytes; every future process re-observes and re-denies until
                # the operator actually fixes the file. Migration re-runs on
                # the first clean load.
                logger.warning(
                    "config: skipping write-back migration — this load "
                    "degraded section(s) %s and writing back would erase the "
                    "evidence; fix the file to clear",
                    sorted(cfg._degraded_sections),
                )
        except Exception as e:
            # Migration write-back is best-effort; never block startup.
            logger.warning("Config write-back failed: %s", e)

        return cfg, ticket

    def to_dict(self) -> dict:
        """Serialize config to the JSON structure used by config.json."""
        from dataclasses import asdict

        d: dict = {
            "agent": asdict(self.agent),
            "session": asdict(self.session),
            "memory": asdict(self.memory),
            "slack": asdict(self.slack),
            "publish": asdict(self.publish),
            "telegram": asdict(self.telegram),
            "discord": asdict(self.discord),
            "webex": asdict(self.webex),
            "wakatime": asdict(self.wakatime),
            "wecom": asdict(self.wecom),
            "weixin": asdict(self.weixin),
            "whatsapp": asdict(self.whatsapp),
            "feishu": asdict(self.feishu),
            "teams": asdict(self.teams),
            "imessage": asdict(self.imessage),
            "dashboard": asdict(self.dashboard),
            "tunnel": asdict(self.tunnel),
            "hooks": self.hooks,
            "agents": {name: asdict(agent_cfg) for name, agent_cfg in self.agents.items()},
            "default_agent": self.default_agent,
            "workspaces": {name: asdict(ws_cfg) for name, ws_cfg in self.workspaces.items()},
            "default_workspace": self.default_workspace,
            "memory_stores": {name: asdict(ms_cfg) for name, ms_cfg in self.memory_stores.items()},
            "default_memory_store": self.default_memory_store,
            "stt": asdict(self.stt),
            "computer_use": asdict(self.computer_use),
            "instances": asdict(self.instances),
            "mcp_gateway": asdict(self.mcp_gateway),
            "mcp": asdict(self.mcp),
            "taskrunner": asdict(self.taskrunner),
            "orchestrator": asdict(self.orchestrator),
            "watchdog": asdict(self.watchdog),
            "resource_limits": asdict(self.resource_limits),
            "messaging": asdict(self.messaging),
            "cron_history": asdict(self.cron_history),
            "knowledge": asdict(self.knowledge),
            "heartbeat": asdict(self.heartbeat),
            "skills": asdict(self.skills),
            "session_summary": asdict(self.session_summary),
            "telemetry": asdict(self.telemetry),
            "snapshot_dir": self.snapshot_dir,
            "timezone": self.timezone,
            "auto_update": self.auto_update,
            # Emitted unconditionally, like every other modelled top-level
            # value: _KNOWN_CONFIG_SECTIONS must equal the key set to_dict()
            # writes (test_known_sections_equals_emitted_sections), and a key
            # listed there but not emitted would be excluded from
            # _extra_sections capture AND dropped here — losing the operator's
            # opt-in on the first save().
            "connections_ui": self.connections_ui,
        }
        # External registries (always serialized so save() round-trips the field)
        d["registries"] = [asdict(r) for r in self.registries]
        # ``mcp_gateway.stub_servers`` is the ROSTER in the file but the EFFECTIVE
        # set on the dataclass, so a straight ``asdict`` round-trip would rewrite
        # the file without the servers the operator opted out of -- turning a
        # reversible deviation into a permanent deletion, on any unrelated save().
        # Emit the roster the load actually read, and drop the private carrier so
        # it never appears as a config key.
        _gw_section = d.get("mcp_gateway")
        if isinstance(_gw_section, dict):
            _gw_section["stub_servers"] = list(self.mcp_gateway.stub_roster)
            _gw_section.pop("_stub_roster", None)
        # Re-emit unknown/edition-contributed top-level sections captured at
        # load() so save()/PATCH does not silently drop them. A known section
        # never appears here (only keys absent from d are restored), so this can
        # never clobber a core section with a stale captured copy.
        for _k, _v in self._extra_sections.items():
            if _k not in d:
                d[_k] = _v
        # Preserve per-channel activation settings on round-trip
        slack_section = d.setdefault("slack", {})
        if self.slack_channels:
            slack_section["channels"] = {
                ch_id: asdict(cfg) for ch_id, cfg in self.slack_channels.items()
            }
        if self.slack_dm_activation != ACTIVATION_ALWAYS:
            slack_section["dm_activation"] = self.slack_dm_activation
        slack_section["observe_max_messages"] = self.observe_max_messages
        if self.slack.trusted_bot_ids:
            slack_section["trusted_bot_ids"] = sorted(self.slack.trusted_bot_ids)
        else:
            slack_section.pop("trusted_bot_ids", None)
        slack_section["observe_ttl_hours"] = self.observe_ttl_hours
        return d

    def save(self) -> None:
        """Write current config to ~/.kiro/crew/config.json.

        Stamps a ``meta`` block with the current version and timestamp
        so we can tell which build last touched the file.

        Values that exist in ``config.local.json`` are stripped from the
        output to prevent overlay settings from leaking into the base file.
        """

        d = self.to_dict()

        # Strip overlay-owned values so they don't leak into config.json
        local_path = config_local_path()
        if local_path.is_file():
            try:
                raw_local = json.loads(local_path.read_text(encoding="utf-8"))
                if isinstance(raw_local, dict):
                    # Compare CANONICAL values for resource_limits.
                    # _subtract_overlay recognises an overlay-owned leaf only when
                    # the emitted value EQUALS the raw overlay value, and this
                    # section is normalized on load (512.5 -> 512, a refused value
                    # -> None). A raw comparison therefore stops matching and
                    # copies an overlay-owned limit into the base file, which is
                    # the leak the subtraction exists to prevent. Only the keys the
                    # overlay actually names are canonicalized: feeding the whole
                    # dataclass would add eight `None` leaves the operator never
                    # wrote and invite deletions they did not ask for.
                    rl_overlay = raw_local.get("resource_limits")
                    if isinstance(rl_overlay, dict):
                        canonical = asdict(ResourceLimitsConfig.from_raw(rl_overlay))
                        raw_local = {
                            **raw_local,
                            "resource_limits": {
                                k: canonical[k] for k in rl_overlay if k in canonical
                            },
                        }
                    d = _subtract_overlay(d, raw_local)
            except (json.JSONDecodeError, OSError):
                pass

        # Atomic + mode-preserving: a concurrent reader must never observe a
        # half-written config, and the write must not widen who can read a file
        # that may hold inline credentials. See write_config_atomically.
        write_config_atomically(config_path(), stamp_config_meta(d))
        # Drop the validated-data cache so the next load() re-reads this write.
        # mtime-keying already detects the change; this makes it immediate even
        # if the filesystem mtime resolution is coarse.
        _invalidate_config_cache()

    @staticmethod
    def _resolve_agent_model() -> str:
        """Read model from installed agent config, falling back to bundled defaults.

        The installed spec is read through
        ``agent_discovery._read_agent_spec`` — the one hardened reader for
        agent configs — not a bare ``read_text``: the agents directory is
        user-writable and shared with other tools, so an oversized file must
        be refused at the read cap instead of slurped onto whatever surface
        asked for its effective model, and a symlink resolving into a
        sensitive path must not donate its target's JSON here.
        """
        agent_json = kiro_agents_dir() / "kirocrew.json"
        if agent_json.is_file():
            data = _read_hardened_agent_spec(agent_json)
            if data:
                model = data.get("model", "")
                if model:
                    return model
        # Bundled defaults.json
        bundled = config_package_dir() / "defaults.json"
        if bundled.is_file():
            try:
                bundled_data = json.loads(bundled.read_text(encoding="utf-8"))
                model = bundled_data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_MODEL

    def acp_effective_model(
        self,
        agent: str | None,
        model_override: str | None,
        global_model: str | None = None,
    ) -> str:
        """The model id the ACP factory selects — what its effort gate keys on.

        This IS the factory's selection, extracted so the spawn-side effort
        verdict (``kiro_crew.subagent._spawn_effective_model``) shares the code
        instead of mirroring it — a mirror that drifts reports a false
        ``effort_applied``/``effort_dropped`` receipt, worse than silence.

        Precedence: ``model_override`` (an explicit caller model or the value
        the session layer resolved) > a named agent's own kiro ``model`` pin
        (``kirocrew`` itself and the no-agent case use the global directly) >
        the collapsed global. ``global_model`` lets the factory pass its
        build-time collapsed ``agent.model``; when omitted it is recomputed
        the same way (``agent.model``, collapsed through
        :meth:`_resolve_agent_model` when it is the ``auto`` sentinel).

        The result is translated into the namespace of the backend that will
        actually be asked to run it: ``to_provider_id(…, "claude_code")`` for
        the claude backend, ``model_registry.to_acp_id`` otherwise (canonical
        keys become kiro ids). ``auto`` collapses to ``""`` either way.

        Keying the translation on the backend is what the warm-pool model-switch
        path already does (``session_allocation``, via ``is_claude_backend``).
        Hardcoding ``to_acp_id`` here meant a COLD start handed the claude
        adapter a kiro-namespaced id — which its ``set_config_option`` rejects,
        and which nothing withheld, because the pre-wire availability guard is
        deliberately kiro-only (the two backends advertise in different
        namespaces, so that check cannot be widened — see
        ``acp.client.model_is_unusable``). A warm claim of the same pinned model
        translated correctly, so the failure depended on whether a pooled
        process happened to exist. Not reachable from the default config:
        ``auto`` pins nothing, and the client skips the send entirely.

        ``to_acp_id``, NOT ``to_provider_id``, is the non-claude choice because
        kiro serves the registry aliases as distinct real models — see its
        docstring. ``""`` means nothing is pinned anywhere: the backend resolves
        the model itself and the effort overlay cannot be keyed.
        """
        if global_model is None:
            global_model = self.agent.model
            if global_model == DEFAULT_MODEL:
                global_model = self._resolve_agent_model()
        if model_override:
            m: str = model_override
        elif not agent or agent == "kirocrew":
            m = global_model
        else:
            m = self._resolve_named_agent_model(agent) or global_model
        if not m:
            return ""
        if self.agent.acp_backend == ACP_BACKEND_CLAUDE:
            return model_registry.to_provider_id(m, "claude_code")
        return model_registry.to_acp_id(m)

    def crew_pinned_effort(self, agent: str | None, crew_agent: str | None = None) -> str:
        """The reasoning effort THIS CREW pins, or ``""`` when it pins none.

        The tier between an explicit per-session override and the configured
        default: a crew that pins nothing resolves ``""`` and therefore inherits
        exactly what it inherited before this field existed.

        Keyed on :func:`resolve_crew_identity` — the same canonical
        ``config.agents`` key the provider factory and the warm-pool claim
        already agree on — so ONE lookup covers every surface that can start a
        session (dashboard, cron, Slack, webhook, spawn). That matters more here
        than for ``model``, whose crew pin arrives as the caller's
        ``model_override``: a schedule- or webhook-woken crew has no dashboard
        slot to carry an override, so a per-caller resolution would leave those
        crews — the ones a pin is most useful for — on the global default.
        """
        crew = self._crew_record(agent, crew_agent)
        return coerce_effort(crew.reasoning_effort) if crew is not None else ""

    def _crew_record(
        self, agent: str | None, crew_agent: str | None
    ) -> "KiroCrewAgentConfig | None":
        """The ``config.agents`` record this session runs as, or ``None``."""
        key = resolve_crew_identity(self, agent, crew_agent)
        return self.agents.get(key) if key else None

    def resolve_session_effort(self, agent: str | None, crew_agent: str | None = None) -> str:
        """The effort a NEW session resolves to, short of an explicit override.

        The crew's own pin first, then the role-aware default: a background
        worker agent (``kirocrew-lite`` / ``kirocrew-heartbeat``) takes the
        ``background`` role effort, everything else the chat default. A pin the
        operator typed on the crew therefore outranks BOTH defaults, including
        the role one — the pin is a choice, the role effort is a built-in.

        Shared with the crews API's readout deliberately. The pane's job is to
        say what a session will actually run at, and a second copy of this chain
        would drift from the one the factory applies — a crew bound to a
        background agent would be reported at the chat default while running at
        the role effort.

        The role check keys on the crew's BOUND ``kiro_agent``, not on ``agent``,
        because that parameter carries different things on different surfaces:
        the dashboard passes a kiro agent name, while Slack threads, cron jobs and
        spawned agents pass a CREW name (the convention
        :func:`resolve_crew_identity` documents). Keying on the raw value made an
        unpinned crew bound to a background worker run at the chat default
        whenever the surface named the crew — the same class of drift in the other
        direction. With no crew record to read, ``agent`` IS the agent name (the
        background/heartbeat session keys pass it directly) and is used as-is.
        """
        crew = self._crew_record(agent, crew_agent)
        if crew is not None:
            pinned = coerce_effort(crew.reasoning_effort)
            if pinned:
                return pinned
        template = crew.kiro_agent if crew is not None else (agent or "")
        if template in BACKGROUND_WORKER_AGENTS:
            return self.agent.resolve_effort("background")
        return self.agent.reasoning_effort

    @staticmethod
    def _resolve_named_agent_model(agent: str, agents_dir: Path | None = None) -> str:
        """Return a named agent's own kiro ``model`` field, or ``""`` if none.

        Used by :meth:`SessionManager.get_or_create` so an explicit global
        ``agent.model`` does not override an agent that pins its own model — the
        global default must rank *below* a per-agent pin. Returns the kiro
        ``model`` slot only; ``""`` when the agent declares none, so the caller
        falls back to the global. ``agents_dir`` overrides the lookup directory
        (a dependency-injection seam for tests); defaults to ``kiro_agents_dir()``.
        """
        if not agent:
            return ""
        base = agents_dir if agents_dir is not None else kiro_agents_dir()
        for af in base.glob("*.json"):
            ad = _read_hardened_agent_spec(af)
            if ad is None:
                continue
            # Skip stray non-object JSON a user may have dropped in the dir.
            if isinstance(ad, dict) and (ad.get("name") == agent or af.stem == agent):
                return ad.get("model") or ""
        return ""

    def load_credentials(self) -> dict[str, str]:
        """Load credentials from ~/.kiro/crew/.env and environment variables.

        .env format: KEY=VALUE (one per line, # comments, no quotes required).
        Environment variables override .env values.
        """
        creds: dict[str, str] = {}
        ep = env_path()
        if ep.exists():
            # Enforce restrictive permissions on the credential file. POSIX
            # only: on Windows mode bits are meaningless (a chmod there
            # toggles the read-only attribute and succeeds without narrowing
            # who can read), and the real owner-only lockdown --
            # ``platform_compat.restrict_to_owner`` -- is not applied on this
            # READ path. It no longer spawns a subprocess, so the reason is no
            # longer cost: it is that a reader has no business rewriting a
            # descriptor it did not create, and doing so here would apply the
            # DACL of whichever process happened to read the file next.
            # Windows enforcement therefore lives where the file is WRITTEN --
            # the setup wizard and the dashboard credential writers all apply
            # ``restrict_to_owner`` at write time.
            try:
                if platform_compat.IS_POSIX and ep.stat().st_mode & 0o077:
                    ep.chmod(0o600)
            except OSError:
                logger.warning("Cannot enforce permissions on %s", ep)
            for line in ep.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()

            # Warn once per boot about keys not in the recognised allowlist.
            # These keys still propagate (operators use them for proxy/feature
            # settings), but the warning makes the behavior visible rather than
            # silently surprising.  The encrypted vault (PR 1+) will provide a
            # proper agent-isolated path for secrets.
            unknown = set(creds) - set(_CREDENTIAL_KEYS) - _warned_env_keys
            if unknown:
                _warned_env_keys.update(unknown)
                for uk in sorted(unknown):
                    logger.warning(
                        "Unknown key %s in .env is not a recognised credential"
                        " -- it will propagate to child processes but is NOT"
                        " agent-isolated. Recognised keys: %s",
                        uk,
                        ", ".join(sorted(_CREDENTIAL_KEYS)),
                    )

        for key in _CREDENTIAL_KEYS:
            val = os.environ.get(key)
            if val:
                creds[key] = val

        # Propagate credentials into the process environment so spawned children
        # (sandboxed agents, MCP servers, cron-fired subprocesses) inherit them
        # via Popen's default env=os.environ.copy() — even when their view of
        # ~/.kiro/crew/.env is a bind-mounted empty file. setdefault() preserves
        # any value the caller already set explicitly.
        #
        # EXCEPTION: when the Docker entrypoint has deliberately scrubbed
        # credentials from the process environ (setting _KIROCREW_CREDS_SCRUBBED=1),
        # re-injecting them here would leak into /proc/<pid>/environ — the exact
        # attack surface the entrypoint closed. The scrub covers only credential
        # keys, so the skip is scoped to _CREDENTIAL_KEYS: every other .env entry
        # (operator-added settings such as proxy or feature variables) still
        # propagates so children behave identically in and out of Docker.
        # Children that need the withheld credentials get them via their own
        # .env read or via an explicit env= kwarg on Popen (the sandbox and ACP
        # spawners already do this).
        scrubbed = bool(os.environ.get("_KIROCREW_CREDS_SCRUBBED"))
        for k, v in creds.items():
            if not v:
                continue
            if scrubbed and (k in _CREDENTIAL_KEYS or _JIRA_TOKEN_RE.match(k)):
                continue
            os.environ.setdefault(k, v)

        return creds

    def create_provider_factory(self) -> Callable:
        """Return a factory that creates LLMProvider instances from config.

        KiroCrew is KiroACP-only: the sole provider is the ACP adapter driving
        the kiro-cli backend. The factory accepts an optional ``session_key`` to
        create a per-session subdirectory under ``workspace_root()``.
        """
        from kiro_crew.providers.acp import (
            AcpProvider,  # circular: acp -> client -> session -> config.loader
        )

        model = self.agent.model
        if model == DEFAULT_MODEL:
            model = self._resolve_agent_model()

        sandbox = self.agent.sandbox
        tool_search = self.agent.tool_search
        tool_search_min_pct = self.agent.tool_search_min_pct
        tool_search_min_tokens = self.agent.tool_search_min_tokens

        # MCP gateway: resolve overlay + socket once, iff some server is stubbed
        # through the gateway. Routing is what puts a stub in the path, and the
        # stub is what carries both the render/callback path and any sharing —
        # so an empty stub set means no stub, no daemon, and no gateway in the
        # path at all (AcpClient falls through to per-session MCP). Sharing
        # (``enabled``) is not consulted here: it decides how a stubbed server's
        # backend is ACQUIRED, and on its own routes nothing.
        _gw = self.mcp_gateway
        if _gw.stub_servers:
            _gw_overlay = _gw.overlay_dir or str(default_overlay_dir())
            _gw_socket = _gw.socket_path or str(default_socket_path())
        else:
            _gw_overlay = None
            _gw_socket = None

        # Effort-drop warnings already emitted by this factory, keyed by
        # (resolved model, level) — see the gate below. Benign under threads:
        # a lost race duplicates one log line, never drops state.
        _effort_drop_warned: set[tuple[str, str]] = set()

        def _acp(
            session_key: str | None = None,
            agent: str | None = None,
            channel_id: str | None = None,
            model_override: str | None = None,
            cwd: str | None = None,
            extra_env: dict[str, str] | None = None,
            reasoning_effort_override: str | None = None,
            crew_agent: str | None = None,
            acp_backend: str | None = None,
            **_kwargs: object,
        ) -> AcpProvider:
            wdir = Path(cwd) if cwd else _session_work_dir(session_key)
            # Canonical crew identity for the session (keys per-agent watchdog
            # windows on the handle) — one shared resolution rule, see
            # resolve_crew_identity.
            crew_agent = resolve_crew_identity(self, agent, crew_agent)
            # Resolve the model, highest tier first:
            #   1. model_override — the caller's explicit pick. The dashboard
            #      passes the slot's own model, else the KiroCrew agent's
            #      configured default (see chat_runner._run_chat).
            #   2. the bound kiro agent's own pinned model, for a named agent.
            #      Custom agents MUST resolve here because the ACP
            #      session/set_mode path switches prompt/tools but not the model,
            #      so an unset model makes kiro fall back to cli.json's
            #      chat.defaultModel. Use _resolve_named_agent_model (the kiro
            #      model slot) to match this backend.
            #   3. ``model`` — the global agent.model default, already collapsed
            #      through _resolve_agent_model() at factory-build time. It
            #      applies to every agent, not just "kirocrew": an agent that
            #      pins nothing inherits the user's configured default instead of
            #      silently falling through to the backend's own choice.
            # "" at the end means nothing is pinned anywhere; AcpClient
            # normalizes "" to DEFAULT_MODEL, same as None.
            # Selection + the per-backend id translation live in
            # acp_effective_model — SHARED with the spawn-side effort verdict
            # (subagent.py) so the reported outcome cannot drift from what this
            # gate actually keys on. (Why the translation is keyed on the
            # backend, and why to_acp_id is the non-claude choice, is documented
            # on that method.)
            m = self.acp_effective_model(agent, model_override, global_model=model)
            # Thread the slot's effort into a per-model override so the kiro
            # cli.json overlay is written from it at spawn — without this, a
            # kiro cold start (or the handler's reset-then-respawn) would only
            # pick up effort already recovered from a pre-existing overlay,
            # never the freshly-set slot value. Mirrors the _claude_code path.
            _eff_per_model: dict[str, str] = {}
            # Everything below an explicit override — the crew's pin, then the
            # role-aware default — resolves in resolve_session_effort, which the
            # crews API also serves its readout from. An explicit override (the
            # dashboard slot's effort, or a sub-agent's resolved "subagent"
            # effort) still wins over all of it.
            _eff = reasoning_effort_override or self.resolve_session_effort(agent, crew_agent)
            if m and _eff and is_valid_effort(_eff) and model_supports_effort(m):
                _eff_per_model[m] = _eff
            elif _eff and is_valid_effort(_eff):
                # Single-authority drop warning: a valid requested effort is
                # being dropped because the resolved model is empty or not
                # effort-capable. Every surface (spawn, dashboard slot, cron)
                # funnels through this factory, so one log at the gate covers
                # them all and cannot drift from the decision it reports on.
                # Reporting-only — the overlay simply stays unwritten, exactly
                # as before. An unresolved model is named "auto" (it IS the
                # DEFAULT_MODEL sentinel the backend resolves itself), matching
                # the spawn-side effort_dropped verdict so one drop event reads
                # as one event across both surfaces.
                #
                # An EXPLICIT override always warns: a caller's own request
                # being dropped is the event this gate exists to surface, and
                # a config-default drop must not burn its dedupe key first
                # (Design review on this PR). Only the static config default
                # (base_effort with no override) dedupes per (model, level) —
                # it is one unchanging configuration fact that would otherwise
                # repeat on every provider construction (warm-pool fills and
                # recycles included); a config change rebuilds the factory and
                # re-arms it.
                _dedupe = not reasoning_effort_override
                if not _dedupe or (m, _eff) not in _effort_drop_warned:
                    if _dedupe:
                        _effort_drop_warned.add((m, _eff))
                    logger.warning(
                        "reasoning effort '%s' will not be applied (session %s) — "
                        "model '%s' does not support effort configuration",
                        _eff,
                        session_key or "?",
                        m or "auto",
                    )
            # Per-session backend selection — ONE call to the selection gate's
            # per-session half (members.select_provider_backend: explicit
            # caller pick > member-DM auto-route > configured default). The
            # factory body carries no branching of its own, so the kiro
            # construction path gains no second check (harness-parity H3/H13);
            # resolve_selected_backend inside the helper applies the same
            # governance/selectability gate as the persisted field, so a
            # denied or unknown value degrades to kiro — the member thread
            # then runs as plain chat and the mount step logs why.
            # circular import: members sits above config in the layering.
            from kiro_crew.members import select_provider_backend

            _backend = select_provider_backend(
                acp_backend,
                session_key,
                self.agent.member_acp_backend,
                self.agent.acp_backend,
            )
            return AcpProvider(
                work_dir=wdir,
                model=m,
                agent=agent,
                crew_agent=crew_agent,
                sandbox_mode=sandbox,
                session_key=session_key,
                channel_id=channel_id,
                extra_env=extra_env,
                acp_backend=_backend,
                effort_per_model=_eff_per_model,
                tool_search=tool_search,
                tool_search_min_pct=tool_search_min_pct,
                tool_search_min_tokens=tool_search_min_tokens,
                mcp_gateway_overlay=_gw_overlay,
                mcp_gateway_socket=_gw_socket,
            )

        return _acp


def build_provider_factory(cfg: "KiroCrewConfig") -> Callable:
    """Return the LLM-provider factory for *cfg*, via the platform seam.

    Routes through ``current_context().providers.create_factory(cfg)`` (the CPP
    ``ProviderRegistry`` extension point) instead of calling
    ``cfg.create_provider_factory()`` directly, so an edition can supply an
    alternate provider factory (e.g. registering an ACP backend the core does not
    ship, through the ``ACP_BACKEND_*`` seam).  The ``Default`` ProviderRegistry
    returns exactly ``cfg.create_provider_factory()``, so the public edition is
    behaviorally identical to calling it directly.

    Fail-closed: a :class:`PlatformCompositionError` (a non-standalone host that
    could not compose its companion) propagates.  Any other transient lookup
    failure degrades to ``cfg.create_provider_factory()`` so an unbooted /
    standalone call site never breaks — it just gets the public factory.

    The fallback is passed as ``fallback_factory`` (a lazy thunk), NOT eagerly:
    ``cfg.create_provider_factory()`` is built ONLY on the degrade path, so the
    standalone happy path builds the factory exactly once (the Default
    ``ProviderRegistry`` already returns ``cfg.create_provider_factory()``, so an
    eager fallback would build it a second time on every session/reload).  A
    failure INSIDE ``cfg.create_provider_factory()`` itself is handled by
    ``safe_context_call`` (which guards the factory call) rather than escaping
    uncaught; with no eager ``fallback`` here there is no usable factory, so a
    composition error propagates (fail-closed) and any other error re-raises —
    a corrupt-config failure surfaces at the factory site, it is not swallowed.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    return safe_context_call(
        lambda: current_context().providers.create_factory(cfg),
        fallback_factory=lambda: cfg.create_provider_factory(),
        log_message="providers.create_factory failed; using cfg.create_provider_factory()",
    )


# ---------------------------------------------------------------------------
# Agent resolver and kiro agent validation
# ---------------------------------------------------------------------------


def _workspace_name_for_dir(config: KiroCrewConfig, ws_dir: Path) -> str:
    """Find the workspace name whose dir matches *ws_dir*."""
    for name, ws_cfg in config.workspaces.items():
        if Path(ws_cfg.dir) == ws_dir:
            return name
    return "default"


_MATERIALIZED_AGENTS: frozenset[str] = frozenset()
_MATERIALIZED_AGENTS_READY = False
# Bumped by every publish. A refresh samples it before scanning and, if it moved
# while the scan was in flight, unions instead of replacing — otherwise a scan
# that globbed the directory BEFORE a registration wrote into it would assign its
# stale view over the just-published names and un-dispatch a freshly enabled app.
_MATERIALIZED_AGENTS_GENERATION = 0
# Monotonic refresh sequencing. A refresh takes a ticket when it STARTS and, on
# completion, discards its result if a refresh that started later already applied:
# two scans race by completion order, not by start order, so an older scan
# finishing second would otherwise overwrite a newer one and resurrect an agent
# that was deleted in between.
_MATERIALIZED_REFRESH_ISSUED = 0
_MATERIALIZED_REFRESH_APPLIED = 0
# Guards the three globals above. Held only for the rebind, never for the scan or
# for a lookup: the read path stays lock-free, which is the whole point of the
# snapshot.
_MATERIALIZED_AGENTS_LOCK = threading.Lock()


def _scan_materialized_agents(agents_dir: Path) -> frozenset[str]:
    """Every agent name declared by the kiro agent configs in *agents_dir*.

    Both spellings are emitted: the config's ``name`` field and the filename stem
    (mirroring :meth:`_resolve_named_agent_model`), since an app's agent is
    registered under a namespaced filename while its config keeps the app's bare
    name. Unreadable or non-object entries are skipped. Performs the glob and the
    per-file reads, so callers must invoke it OFF the event loop.
    """
    names: set[str] = set()
    # Deferred import: `hooks` reaches back into this module for config paths, so
    # the edge must resolve lazily. A failure here propagates to
    # refresh_materialized_agents, which logs and leaves the snapshot untouched —
    # fail-closed, rather than falling back to an unguarded read.
    from kiro_crew.hooks import safe_read_file

    try:
        candidates = sorted(agents_dir.glob("*.json"))
    except OSError:
        return frozenset()
    for af in candidates:
        try:
            # Through the sensitive-path gate, not a bare read: this directory is
            # user-writable, so a symlink planted there (`evil.json` ->
            # `~/.aws/credentials`) would otherwise be read verbatim by a boot
            # refresh. safe_read_file re-checks the RESOLVED target and raises
            # PermissionError for a refused path — an OSError subclass, so a
            # refused entry is skipped by the same handler as an unreadable one.
            data = json.loads(safe_read_file(str(af)))
        except (ValueError, OSError):
            continue
        # Skip stray non-object JSON a user may have dropped in the dir. The
        # filename stem is only trusted AFTER the file parses as an agent config:
        # naming an unparseable file dispatchable would hand kiro-cli a name it
        # cannot load, and it would fall back to its own default silently — the
        # same invisible mismatch this whole change removes.
        if not isinstance(data, dict):
            continue
        # Trust the config's DECLARED `name`, not the filename. `kiro-cli agent
        # list` enumerates agents by their declared name — an app agent written to
        # `mochi--mochi.json` with `"name": "mochi"` is listed as `mochi`, and
        # `mochi--mochi` is not listed at all. Treating the stem as dispatchable
        # would hand kiro-cli a name it does not know, which falls back to its own
        # default silently: the exact invisible mismatch this change removes. The
        # stem is used ONLY when the config declares no name, where it is the only
        # identifier available.
        declared = data.get("name")
        if isinstance(declared, str) and declared:
            names.add(declared)
        else:
            names.add(af.stem)
    return frozenset(names)


def refresh_materialized_agents() -> None:
    """Rescan the kiro agents directory into the in-memory snapshot.

    MUST be called off the event loop — it globs a directory and reads every
    config in it, which scales with agent count. Callers on the loop must use
    :func:`schedule_materialized_agents_refresh` instead.

    Placing the cost on the WRITER is the point: the read path
    (:func:`_materialized_kiro_agent`, reached from ``_run_chat`` ->
    :func:`resolve_agent_bindings` on every turn of an app-bound session) then
    does zero filesystem work. Never raises.

    Consequence worth stating plainly: editing an existing config IN PLACE — say
    renaming its ``name`` field by hand — refreshes nothing, so that new name
    stays undispatchable until the next registration or gateway boot. Hand-editing
    is not how an app agent is meant to appear (``_register_agents`` is), and the
    alternative is filesystem work on the loop, so the staleness is accepted
    rather than papered over with a per-file stat.
    """
    global _MATERIALIZED_AGENTS, _MATERIALIZED_AGENTS_READY, _MATERIALIZED_REFRESH_ISSUED
    global _MATERIALIZED_REFRESH_APPLIED
    with _MATERIALIZED_AGENTS_LOCK:
        generation_at_start = _MATERIALIZED_AGENTS_GENERATION
        _MATERIALIZED_REFRESH_ISSUED += 1
        my_ticket = _MATERIALIZED_REFRESH_ISSUED
    try:
        snapshot = _scan_materialized_agents(kiro_agents_dir())
    except Exception:  # noqa: BLE001 — a refresh failure only costs a fallback
        logger.debug("Failed to refresh materialized agent names", exc_info=True)
        return
    with _MATERIALIZED_AGENTS_LOCK:
        if my_ticket < _MATERIALIZED_REFRESH_APPLIED:
            # A refresh that started AFTER this one already applied, so this view
            # is older than what is installed. Assigning it would undo the newer
            # scan — resurrecting an agent deleted in between, whose config is gone
            # from disk. Drop it; the newer snapshot already reflects reality.
            logger.debug("Discarding out-of-order materialized agent refresh")
            return
        if _MATERIALIZED_AGENTS_GENERATION != generation_at_start:
            # A registration published while this scan was in flight, so the scan
            # may have globbed the directory before that write landed. Replacing
            # would erase the published names and un-dispatch a freshly enabled
            # app; union instead and let the refresh scheduled by that
            # registration apply the authoritative view (including removals).
            snapshot = frozenset(snapshot | _MATERIALIZED_AGENTS)
        _MATERIALIZED_AGENTS = snapshot
        _MATERIALIZED_AGENTS_READY = True
        _MATERIALIZED_REFRESH_APPLIED = my_ticket
    # An app install/upgrade that rewrote agent JSON just landed in the snapshot;
    # drop the context builder's per-agent includeCrewContext cache so the next
    # build re-reads the flag rather than serving a value cached before the write
    # (otherwise a flipped flag heals only on gateway restart).
    try:
        from kiro_crew.context import invalidate_include_crew_context_cache

        invalidate_include_crew_context_cache()
    except Exception:  # noqa: BLE001 — best-effort; a stale flag is not fatal
        logger.debug("Failed to invalidate includeCrewContext cache", exc_info=True)


def publish_materialized_agents(names: Iterable[str]) -> None:
    """Add *names* to the snapshot immediately, with no filesystem access.

    A pure set union — safe to call from anywhere, including the event loop.
    ``apps.bridges._register_agents`` uses it to publish the agents it just wrote
    BEFORE scheduling the full rescan, because the rescan can be delayed
    arbitrarily when the default executor is saturated, and the window is not
    merely cosmetic: a slot created in it is normalized to the agent that answers
    (the default) and that substitution is STORED, so the slot would stay bound to
    the default agent rather than recovering on the next turn.

    The snapshot is marked ready, which is safe in both contexts: on the loop the
    scheduled rescan fills in everything else moments later, and in a synchronous
    context the scheduler rescans inline, so the union is immediately superseded
    by a complete snapshot.
    """
    global _MATERIALIZED_AGENTS, _MATERIALIZED_AGENTS_READY, _MATERIALIZED_AGENTS_GENERATION
    fresh = {n for n in names if isinstance(n, str) and n}
    if not fresh:
        return
    with _MATERIALIZED_AGENTS_LOCK:
        _MATERIALIZED_AGENTS = frozenset(_MATERIALIZED_AGENTS | fresh)
        _MATERIALIZED_AGENTS_READY = True
        # Signals any in-flight refresh that its view predates this write, so it
        # unions rather than replacing (see refresh_materialized_agents).
        _MATERIALIZED_AGENTS_GENERATION += 1


def schedule_materialized_agents_refresh() -> None:
    """Refresh the snapshot from ANY context without blocking an event loop.

    ``apps.bridges._register_agents`` is the writer that must trigger this. The
    dashboard enable/update handlers dispatch ``register_app`` to an executor
    thread, so from those paths this runs in a synchronous context (no running
    loop) and refreshes inline — the scan is already off the loop, serialized
    inside the awaited registration, so no stale-snapshot window exists there.
    The same inline branch covers the CLI, tests, and the boot warm already on
    an executor. For a caller that does hold a live loop, scanning inline would
    be the same directory-walk-per-agent-file stall the neighbouring prune
    comment warns about, so the scan is handed to the default executor and this
    returns immediately; that offloaded refresh lands a few milliseconds later,
    and a turn dispatched in that window sees the previous snapshot for one
    turn, then self-heals — strictly better than staying stale until the next
    gateway boot. Never raises; the scan itself swallows its errors.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        refresh_materialized_agents()
        return
    try:
        # Fire-and-forget on purpose: nothing awaits this, and
        # refresh_materialized_agents never raises, so the discarded future
        # cannot surface an unretrieved exception.
        loop.run_in_executor(None, refresh_materialized_agents)
    except Exception:  # noqa: BLE001 — a scheduling failure only costs a fallback
        logger.debug("Failed to schedule materialized agent refresh", exc_info=True)


def _materialized_kiro_agent(agent_name: str | None, project_dir: str | None = None) -> str:
    """Return *agent_name* when a materialized kiro agent config declares it.

    An APP's agents are copied into ``~/.kiro/agents/`` by
    ``apps.bridges._register_agents`` under a namespaced FILENAME
    (``<app>--<agent>.json``) while the config inside keeps the app's own bare
    ``name``. Nothing adds them to ``config.agents`` — that mapping is authored
    by setup / the user — so an app agent is resolvable by kiro-cli but is NOT a
    KiroCrew alias. Without this lookup :func:`resolve_agent_bindings` would fall
    all the way back to ``default_agent`` and silently dispatch the DEFAULT kiro
    agent for a session the user explicitly bound to an app's agent: the slot
    still shows the requested name (it is stored verbatim, unvalidated), so the
    UI claims "mochi" while the default agent answers, without the app's MCP
    tools.

    A pure in-memory set membership test — NO filesystem I/O, not even a stat.
    This is reached from ``_run_chat`` -> :func:`resolve_agent_bindings` on EVERY
    turn of an app-bound session (an app agent is never an alias, so it always
    takes this path), and a scan there would stall chat, WebSocket and heartbeat
    processing. The snapshot is refreshed only off-loop, by the gateway at boot
    and by ``_register_agents`` / ``_deregister_agents`` around their writes (see
    :func:`refresh_materialized_agents`).

    CONTRACT, stated deliberately because it is wider than the bug it fixes: this
    honors ANY parseable agent config in the directory, not only app-registered
    ones, and grafts the DEFAULT agent's workspace and memory bindings onto it. An
    agent created by kiro-cli's own flow, or dropped in by hand, therefore becomes
    dispatchable with default bindings — it is not restricted to
    ``bridges._register_agents`` output. That is intentional: the directory is the
    kiro-cli agent registry, every entry in it is a real agent kiro-cli can load,
    and narrowing to app-registered names would mean tracking provenance the
    directory does not record. It is safe inside the single-user trust boundary,
    and reads go through the sensitive-path gate (see
    :func:`_scan_materialized_agents`), but it IS a wider surface than "app agents
    dispatch" and should be read as such.

    When no snapshot exists yet, one is built lazily ONLY in a synchronous
    context (the CLI, tests) — never while an event loop is running, where an
    unwarmed lookup falls back to the default rather than block. Returns ``""``
    for a blank name or when nothing declares it, so a genuinely unknown agent
    still falls back to the default.

    *project_dir* adds the session's own ``<project>/.kiro/agents`` scope, which
    kiro-cli searches BEFORE the user-level directory (it resolves ``--agent``
    against its cwd, and Kiro Crew spawns it with the project dir as cwd). It
    deliberately does NOT use the snapshot: that is one process-wide set, while the
    project scope differs per session, so sharing it would leak one checkout's
    agents into another's.

    The project lookup reads the filesystem, so like the user-level scan it is
    NEVER performed on the event loop — see :func:`_project_declares_agent`. Callers
    that need a project agent resolved must therefore invoke this off the loop;
    ``chat_runner`` and the side-turn handler do so through the discovery pool. An
    on-loop call degrades to the default agent for that turn rather than stalling
    the gateway, which is the same trade the user-level scope already makes.
    """
    if not agent_name:
        return ""
    if _MATERIALIZED_AGENTS_READY and agent_name in _MATERIALIZED_AGENTS:
        return agent_name
    if not _MATERIALIZED_AGENTS_READY:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop on this thread: scanning here blocks nothing.
            refresh_materialized_agents()
            if agent_name in _MATERIALIZED_AGENTS:
                return agent_name
        else:
            # On the event loop with a cold snapshot: never scan. The boot warm
            # normally precedes any turn; falling back for one turn is strictly
            # preferable to stalling the gateway.
            logger.debug("Materialized agent snapshot cold on the event loop; falling back")
    if project_dir and _project_declares_agent(agent_name, project_dir):
        return agent_name
    return ""


# Snapshot of the Kiro Crew agent ALIAS table as ONE immutable
# ``(aliases, default_alias, ready)`` triple — the keys of ``config.agents``, the
# alias a request falls back to, and whether a load has published yet. Refreshed
# by every successful :meth:`KiroCrewConfig.load`, exactly like
# ``_MATERIALIZED_AGENTS`` is refreshed by every scan, and for the same reason:
# the read path (:func:`resolve_effective_agent`, reached from
# ``_ChatSlot.to_dict`` for every slot of every slots frame) must do ZERO
# filesystem work, and ``config.agents`` is otherwise only reachable by
# re-reading and re-validating ``config.json``.
#
# One tuple rather than three globals, and no lock, deliberately: publishing is a
# single rebind of a single name, so a reader either sees the whole previous
# triple or the whole new one. Three separate globals would need a lock to stop a
# reader pairing the new alias set with the old fallback name, and that lock would
# then be acquired once per slot per frame on the event loop. Immutability is what
# removes the need for it — never mutate the tuple or the frozenset in place.
#
# ``ready=False`` reads as "no opinion", not "nothing configured": the resolver
# reports no divergence rather than guessing, because a wrong "your agent was
# substituted" marker is worse than none at all.
_CONFIG_AGENT_ALIAS_SNAPSHOT: tuple[frozenset[str], str, bool] = (frozenset(), "", False)


def publish_agent_alias_snapshot(config: "KiroCrewConfig") -> None:
    """Publish *config*'s alias table for the filesystem-free display resolver.

    Pure in-memory rebind — safe from anywhere, including the event loop. Called
    from :meth:`KiroCrewConfig.load` so every successful load refreshes it,
    including the degraded-defaults path (which must OVERWRITE a richer previous
    snapshot rather than leave a resolver claiming aliases that no longer load).
    """
    global _CONFIG_AGENT_ALIAS_SNAPSHOT
    aliases = frozenset(str(n) for n in config.agents if isinstance(n, str) and n)
    default_alias = config.default_agent if config.default_agent in config.agents else ""
    if not default_alias and aliases:
        # Mirrors resolve_agent_bindings' defensive branch: an unusable
        # ``default_agent`` is answered by the first configured alias.
        default_alias = next(iter(config.agents))
    _CONFIG_AGENT_ALIAS_SNAPSHOT = (aliases, default_alias, True)


def agent_alias_snapshot() -> tuple[frozenset[str], str, bool]:
    """The published alias table as ``(aliases, default_alias, ready)``."""
    return _CONFIG_AGENT_ALIAS_SNAPSHOT


# Snapshot of the auto-compaction threshold, refreshed by every successful
# :meth:`KiroCrewConfig.load`, for the same reason as the two snapshots above:
# the read path (``SessionManager._compaction_gate_decision``) runs on the event
# loop after every turn, so it must never stat/read/validate config.json itself.
#
# What this specifically buys, beyond avoiding that I/O: a config write from ANY
# writer reaches a running gateway. The dashboard PATCH handler and the CLI both
# end at ``update_config_locked``, and only the handler could notify the manager
# it had changed something -- so a ``kirocrew config set`` landed on disk while
# the live threshold kept its startup value until a restart. Publishing on load
# closes that without either writer having to know which live object holds it.
#
# Ordered by a monotonically increasing TICKET drawn before each load's read.
# Loads run concurrently (prompt assembly, background threads), so without
# ordering an older load finishing last would republish the value it read before
# a newer write -- leaving live sessions compacting at an obsolete threshold
# until something loaded again. The two snapshots above carry the same race; the
# consequence there is a display marker, which is why only this one is ordered.
#
# A ticket rather than the files' newest ``st_mtime_ns``, because this ordering
# must be monotonic and an mtime is not. Deleting the newer of the two config
# files LOWERS that maximum, and so does restoring a backup with ``cp -p`` or any
# other writer that preserves timestamps; each one makes the current state of the
# filesystem look like an older read, so the publish that should win is dropped
# and the live gate keeps a threshold the files no longer say. A ticket is
# independent of the filesystem, so a deletion and a timestamp-preserving restore
# both order as what they are: the newest read.
_CONFIG_AUTOCOMPACT_PCT: float = DEFAULT_AUTOCOMPACT_PCT
_CONFIG_AUTOCOMPACT_TICKET: int = 0

#: Highest ticket handed out by :func:`next_config_load_ticket`. Distinct from the
#: PUBLISHED ticket above: a load draws one and can still lose the comparison,
#: which must not move the published mark.
_CONFIG_AUTOCOMPACT_ISSUED: int = 0

#: Serializes the ticket draw and the compare-and-set in
#: :func:`publish_autocompact_pct`. Held ONLY on those two write paths, each of
#: which runs inside ``load()`` and is therefore already doing file I/O and schema
#: validation -- the lock is free by comparison. The READ path
#: (:func:`published_autocompact_pct`) never takes it, which is what keeps the
#: event loop lock-free; that is the objection the alias snapshot above avoids by
#: publishing one immutable tuple, and it does not apply to a write-side lock.
#:
#: Needed because each path is a read followed by a write: without it two
#: concurrent loads can draw the SAME ticket, or both pass the publish comparison
#: and let whichever assigns LAST win, so an older read replaces a newer one and
#: rolls the published ticket backwards with it.
_CONFIG_AUTOCOMPACT_LOCK = threading.Lock()


def next_config_load_ticket() -> int:
    """Draw the next config-load ordering ticket.

    Call this BEFORE the read whose result will be published, so the ticket
    records when this load began observing the files. Two loads whose reads
    interleave are then ordered by ticket rather than by anything on disk: the
    loser's value is at most microseconds stale and the next load corrects it,
    where an unordered publish can leave an obsolete threshold in force
    indefinitely.

    Never returns 0, so 0 means "nothing published yet".
    """
    global _CONFIG_AUTOCOMPACT_ISSUED
    with _CONFIG_AUTOCOMPACT_LOCK:
        _CONFIG_AUTOCOMPACT_ISSUED += 1
        return _CONFIG_AUTOCOMPACT_ISSUED


def publish_autocompact_pct(config: "KiroCrewConfig", ticket: int | None = None) -> None:
    """Publish *config*'s compaction threshold for the filesystem-free read path.

    Pure in-memory rebind -- safe from anywhere, including the event loop, and a
    reader sees either the whole previous value or the whole new one. Called from
    :meth:`KiroCrewConfig.load` so every successful load refreshes it, including
    the degraded-defaults path, which must OVERWRITE a previous snapshot rather
    than leave a stale threshold in force.

    *ticket* orders this publish against concurrent ones. It must come from
    :func:`next_config_load_ticket`, drawn BEFORE the read that produced *config*;
    a ticket lower than the one already published is dropped. Omitting it draws a
    fresh ticket, which therefore always wins -- correct for a caller that has
    just built the config it is publishing (tests), and wrong for one replaying an
    earlier read, which must pass the ticket it drew.

    No ticket value is special-cased. "Neither config file exists" is the current
    truth rather than an older read of the same file, and it arrives here on the
    degraded-defaults path holding a freshly drawn ticket, so it wins by ordinary
    comparison. Being able to state that without a carve-out is the reason the
    ticket is independent of the files: an ordering read off their mtime drops to
    a lower value when a file is removed, and so cannot express it.
    """
    global _CONFIG_AUTOCOMPACT_PCT, _CONFIG_AUTOCOMPACT_TICKET
    # Drawn OUTSIDE the lock: next_config_load_ticket acquires the same
    # non-reentrant lock, so drawing it inside the block below would deadlock.
    if ticket is None:
        ticket = next_config_load_ticket()
    # Compare and BOTH assignments under one lock: they are a single
    # compare-and-set, and splitting them lets two concurrent loads both pass the
    # comparison and race the writes. See _CONFIG_AUTOCOMPACT_LOCK.
    with _CONFIG_AUTOCOMPACT_LOCK:
        if ticket < _CONFIG_AUTOCOMPACT_TICKET:
            return
        _CONFIG_AUTOCOMPACT_TICKET = ticket
        _CONFIG_AUTOCOMPACT_PCT = config.session.autocompact_pct


def published_autocompact_pct() -> float:
    """The published compaction threshold."""
    return _CONFIG_AUTOCOMPACT_PCT


# Snapshot of the global default timezone, refreshed by every successful
# :meth:`KiroCrewConfig.load`, for the same reason as the three snapshots above:
# its read path runs on the event loop. ``kiro_crew.cron._job_tz`` is reached
# from ``CronService._on_timer``'s due-scan for EVERY cron-expression job that
# does not carry its own zone -- so a config stat/read/validate here was a
# per-tick gateway stall (the ``no-blocking-call-on-event-loop`` rule), paid
# again by ``get_local_tz`` on the prompt-assembly and dashboard-handler paths.
#
# Ordered by ticket, like the compaction threshold and unlike the alias table:
# this value decides WHEN a job fires, so an out-of-order publish leaving an
# obsolete zone in force would misfire every schedule depending on it until
# something loaded again. The alias table tolerates that race because its
# consequence is a display marker; a schedule does not.
#
# Defaults to "" -- the dataclass default for :attr:`KiroCrewConfig.timezone` --
# so a read taken BEFORE the first load resolves exactly as a defaults-only load
# would (empty falls through to UTC in both consumers) rather than inventing a
# zone during the boot window.
_CONFIG_TIMEZONE: str = ""
_CONFIG_TIMEZONE_TICKET: int = 0

#: Serializes the compare-and-set in :func:`publish_config_timezone`, for the
#: reasons given on :data:`_CONFIG_AUTOCOMPACT_LOCK`: each publish is a read
#: followed by a write, so without it two concurrent loads can both pass the
#: comparison and let whichever assigns last win, rolling the published ticket
#: backwards. A SEPARATE lock rather than reusing the autocompact one, which
#: :func:`next_config_load_ticket` already holds -- drawing a ticket while
#: holding this one is therefore safe, and the reverse nesting must not appear.
#: The READ path (:func:`published_config_timezone`) never takes it, which is
#: what keeps the event loop lock-free.
_CONFIG_TIMEZONE_LOCK = threading.Lock()


def publish_config_timezone(config: "KiroCrewConfig", ticket: int | None = None) -> None:
    """Publish *config*'s default timezone for the filesystem-free read path.

    Pure in-memory rebind -- safe from anywhere, including the event loop, and a
    reader sees either the whole previous value or the whole new one. Called from
    :meth:`KiroCrewConfig.load` so every successful load refreshes it, including
    the degraded-defaults path, which must OVERWRITE a previous snapshot rather
    than leave a zone the files no longer name.

    *ticket* orders this publish against concurrent ones and carries the same
    contract as :func:`publish_autocompact_pct`: it must come from
    :func:`next_config_load_ticket`, drawn BEFORE the read that produced
    *config*, and a ticket lower than the one already published is dropped.
    Omitting it draws a fresh ticket, which therefore always wins -- correct for
    a caller publishing a config it just built (tests), wrong for one replaying
    an earlier read.
    """
    global _CONFIG_TIMEZONE, _CONFIG_TIMEZONE_TICKET
    # Drawn OUTSIDE the lock below purely for symmetry with
    # publish_autocompact_pct; next_config_load_ticket takes a DIFFERENT
    # (autocompact) lock, so nesting here would not deadlock as it would there.
    if ticket is None:
        ticket = next_config_load_ticket()
    # Compare and BOTH assignments under one lock: they are a single
    # compare-and-set. See _CONFIG_TIMEZONE_LOCK.
    with _CONFIG_TIMEZONE_LOCK:
        if ticket < _CONFIG_TIMEZONE_TICKET:
            return
        _CONFIG_TIMEZONE_TICKET = ticket
        _CONFIG_TIMEZONE = config.timezone


def published_config_timezone() -> str:
    """The published default timezone name, or ``""`` if none is configured.

    ``""`` is not an error and not a "cold snapshot" signal -- it is what an
    unset :attr:`KiroCrewConfig.timezone` says, and both consumers already
    resolve it to UTC. Callers must NOT treat it as a reason to reach for
    ``config.json`` themselves; that is the I/O this snapshot exists to remove.
    """
    return _CONFIG_TIMEZONE


def resolve_effective_agent(agent_name: str | None, project_dir: str | None = None) -> str:
    """Name the agent that will actually answer *agent_name*, or ``""``.

    A DISPLAY-side companion to :func:`resolve_agent_bindings`, and deliberately
    narrower than it. The empty string means **"nothing to report"** — either the
    requested name is honored, or resolution cannot be settled without touching
    the filesystem. A non-empty return is a positive claim that a DIFFERENT agent
    answers this session, which is what the UI renders as a divergence marker.

    Three properties make it safe to call from ``_ChatSlot.to_dict``, which runs
    on the event loop for every slots frame:

    * **No filesystem access, and no lock.** Only the two in-memory snapshots are
      read (:func:`agent_alias_snapshot` and ``_MATERIALIZED_AGENTS``) plus the
      syscall-free project cache. It never scans, stats, or re-reads
      ``config.json``, so it cannot become a per-frame gateway stall — and because
      the alias snapshot is one immutable tuple, reading it is a single atomic
      name load rather than a mutex acquired once per slot per frame.
    * **Fails closed to "no claim".** A cold alias snapshot, a cold materialized
      snapshot, or a cold project cache all return ``""``. A false
      "your agent was substituted" marker is worse than no marker: the user would
      chase a substitution that never happened, and the honest answer during a
      boot window is silence.
    * **Reads nothing back.** The requested name is never rewritten — see the
      note in ``chat_handlers`` on why storing the resolved name was destructive.
      This function only describes; the stored binding stays verbatim.

    *project_dir* widens the "honored" set to the session's own ``.kiro`` scope
    via the cache-only reader, so a project-declared agent is not mislabelled as
    substituted. A cold cache for that project yields ``""``.
    """
    if not agent_name:
        return ""
    aliases, default_alias, ready = agent_alias_snapshot()
    if not ready or not default_alias:
        return ""
    if agent_name in aliases:
        # A Kiro Crew alias resolves to itself (step 1 of resolve_agent_bindings).
        return ""
    if not _MATERIALIZED_AGENTS_READY:
        # Cold snapshot: a materialized kiro agent may well declare this name and
        # we simply cannot see it yet. Claim nothing.
        return ""
    if agent_name in _MATERIALIZED_AGENTS:
        return ""
    if project_dir and not _project_scope_excludes(agent_name, project_dir):
        return ""
    if default_alias == agent_name:
        return ""
    return default_alias


def _project_scope_excludes(agent_name: str, project_dir: str) -> bool:
    """Whether *project_dir* is KNOWN not to declare *agent_name*.

    The conservative half of :func:`_project_declares_agent`: it answers ``True``
    only from a WARM cache, and makes no syscalls even off the event loop. An
    uncached project is not evidence of absence, so it answers ``False`` and the
    caller reports no divergence.
    """
    try:
        # circular import: agent_discovery imports kiro_crew.hooks (the hardened
        # file-read gate), whose import closure reaches back into
        # kiro_crew.config.loader — the same cycle documented at length on
        # :func:`_project_declares_agent`, which defers this identical import for
        # this identical reason. A module-scope import here would be that cycle.
        from kiro_crew.agent_discovery import cached_project_agent_names

        names = cached_project_agent_names(project_dir)
    except Exception:  # noqa: BLE001 — a lookup failure is "no evidence"
        return False
    if names is None:
        return False
    return agent_name not in names


def _read_hardened_agent_spec(path: Path) -> dict | None:
    """Read one agent spec through ``agent_discovery``'s hardened reader.

    Thin wrapper so the model resolvers get the size cap, sensitive-symlink
    rejection, and non-object filtering without each re-deriving them.

    Deferred import so this module keeps its leaf-level import graph —
    ``agent_discovery`` imports ``kiro_crew.hooks``, whose closure reaches
    back into this module (see :func:`_project_declares_agent`). Any failure
    to import or parse means "no usable spec here", never an exception into
    model resolution.
    """
    try:
        from kiro_crew.agent_discovery import _read_agent_spec

        return _read_agent_spec(path, operation="load_config", source="unknown")
    except Exception:
        return None


def _project_declares_agent(agent_name: str, project_dir: str) -> bool:
    """Whether *project_dir* declares a dispatchable agent called *agent_name*.

    Delegates to ``agent_discovery``, which owns the scan, its sensitive-path guards,
    and the stat-signature cache.

    Splits on whether an event loop is running, because that decides what is safe:

    * **Off the loop** (the CLI, tests, and the discovery-pool thread the dashboard
      call sites use) — scan and revalidate normally.
    * **On the loop** — read the cache and nothing else, via a helper that makes no
      syscalls whatsoever. Even one directory's worth of reads is unbounded in
      LATENCY, and this runs on EVERY turn of a project-agent-bound session, so a
      network or otherwise slow checkout would become a recurring gateway stall the
      loop-stall watchdog blames on chat. A cold cache reports "not declared" and the
      caller falls back, exactly as the user-level cold-snapshot path does.

    The dashboard call sites warm the cache through the discovery pool immediately
    before resolving, so the on-loop read is a hit rather than a fallback. Only the
    WARM is offloaded, never ``resolve_agent_bindings`` itself: that function can
    raise ``StopIteration`` on a malformed config, and ``StopIteration`` cannot be
    delivered through a ``Future`` — asyncio rejects it, and the awaiting caller
    hangs instead of seeing the error.

    Deferred import so this module keeps its leaf-level import graph. Best-effort — a
    lookup failure means "not declared here", never an exception into turn handling.
    """
    try:
        # circular import: agent_discovery imports kiro_crew.hooks (the hardened
        # file-read gate), whose import closure reaches back into config.loader —
        # verified by importing agent_discovery in a fresh interpreter and finding
        # kiro_crew.config.loader in sys.modules. A module-scope import here would
        # therefore be a cycle; the deferral is load-bearing, not stylistic.
        from kiro_crew.agent_discovery import (
            cached_project_agent_names,
            project_agent_names,
        )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return agent_name in project_agent_names(
                project_dir, operation="project_declares_agent", source="unknown"
            )
        names = cached_project_agent_names(project_dir)
        if names is None:
            logger.debug(
                "Project agent cache cold on the event loop for %r; falling back "
                "(warm it off-loop before resolving to dispatch a project agent)",
                agent_name,
            )
            return False
        return agent_name in names
    except Exception:  # noqa: BLE001 — a probe failure only costs a fallback
        logger.debug("Project agent probe failed for %r", agent_name, exc_info=True)
        return False


def resolve_crew_identity(
    config: "KiroCrewConfig", agent: str | None, crew_agent: str | None
) -> str:
    """Canonical Kiro Crew identity (a ``config.agents`` key) for a session.

    One rule shared by every session-granting path (provider factory, warm-pool
    claim) so cold starts and claims can never disagree. An explicit
    ``crew_agent`` wins verbatim — including "" ("no crew"), which is how the
    dashboard, the one kiro-name-passing surface, opts out of the fallback.
    When absent, the surface convention documented on
    :func:`_resolve_model_for_agent` applies: Slack threads, cron jobs and
    spawned agents pass a CREW name as ``agent``, so crew-namespace membership
    makes it canonical — a membership check on names the surface owns, not a
    cross-namespace match.
    """
    if crew_agent is not None:
        return crew_agent
    if agent and agent in config.agents:
        # DEBUG, not INFO: every Slack/cron session resolves here routinely.
        # The line exists so a kiro-template name that collides with a crew
        # key (which would silently inherit that crew's watchdog windows) is
        # diagnosable from logs.
        logger.debug("crew_agent %r resolved by crew-namespace fallback", agent)
        return agent
    return ""


def resolve_agent_bindings(
    config: KiroCrewConfig,
    agent_name: str | None = None,
    project_dir: str | None = None,
) -> ResolvedBindings:
    """Resolve workspace, memory store, and kiro agent for a session.

    Resolution:
    1. If agent_name is given and exists in config.agents → use its bindings
    2. Otherwise use config.default_agent (guaranteed to exist by load()), but
       keep dispatching *agent_name* itself when a materialized kiro agent
       declares it (see :func:`_materialized_kiro_agent`) — an app's agents are
       registered in ``~/.kiro/agents/`` and never added to ``config.agents``, so
       this is the only thing that stops an app-bound session from silently
       running the default agent.

    *project_dir* is the session's active project directory, which widens step 2 to
    that project's own ``.kiro`` scope. It must be the same directory Kiro Crew
    passes as the kiro-cli cwd, so an agent found through it is one the backend
    will genuinely resolve; passing a directory the session does not run in would
    reintroduce the silent-substitution bug this lookup exists to prevent.
    """
    import dataclasses as _dc

    # An app agent is resolvable by kiro-cli but is not a KiroCrew alias, so it
    # takes the default's workspace/memory bindings while still dispatching
    # ITSELF. Computed only when the name is not an alias — the lookup touches
    # the filesystem.
    alias_hit = bool(agent_name) and agent_name in config.agents
    passthrough = "" if alias_hit else _materialized_kiro_agent(agent_name, project_dir)
    # A non-empty name that matched NEITHER an alias nor a materialized config is
    # about to be answered by the default agent. Reported so callers that store
    # the requested name never advertise a binding that is not running.
    requested_resolved = (not agent_name) or alias_hit or bool(passthrough)

    # Step 1: explicit agent_name
    if agent_name and agent_name in config.agents:
        agent_cfg = config.agents[agent_name]
        resolved_alias = agent_name
    elif config.default_agent and config.default_agent in config.agents:
        # Step 2: default_agent (guaranteed valid by load())
        agent_cfg = config.agents[config.default_agent]
        resolved_alias = config.default_agent
    elif config.agents:
        # Defensive: default_agent not in agents, use first available
        first_name = next(iter(config.agents))
        logger.warning(
            "default_agent '%s' not found in agents, using '%s'",
            config.default_agent,
            first_name,
        )
        agent_cfg = config.agents[first_name]
        resolved_alias = first_name
    else:
        # No agents at all — return safe defaults
        logger.warning("No agents configured, using bare defaults")
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=config.default_memory_store,
            effective_memory_config=_dc.asdict(config.memory),
            kiro_agent=passthrough or config.agent.default_agent,
            requested_resolved=requested_resolved,
        )

    # Resolve workspace
    ws_name = agent_cfg.workspace
    if ws_name in config.workspaces:
        ws_dir = Path(config.workspaces[ws_name].dir)
    else:
        logger.warning(
            "Agent workspace '%s' not found, falling back to default_workspace '%s'",
            ws_name,
            config.default_workspace,
        )
        fallback_ws = config.workspaces.get(config.default_workspace)
        ws_dir = Path(fallback_ws.dir) if fallback_ws else Path("workspace")

    # Resolve memory store
    store_name = agent_cfg.memory_store
    if store_name not in config.memory_stores:
        logger.warning(
            "Agent memory_store '%s' not found, falling back to '%s'",
            store_name,
            config.default_memory_store,
        )
        store_name = config.default_memory_store

    kiro_agent = passthrough or agent_cfg.kiro_agent

    # Build effective memory config via dict-level merge
    store_cfg = config.memory_stores.get(store_name)
    store_dict = _dc.asdict(store_cfg) if store_cfg else {}
    top_level_memory = _dc.asdict(config.memory)
    effective_memory = resolve_memory_store_config(top_level_memory, store_dict)

    return ResolvedBindings(
        workspace_dir=ws_dir,
        memory_store_name=store_name,
        effective_memory_config=effective_memory,
        kiro_agent=kiro_agent,
        model=normalize_agent_model(agent_cfg.model),
        requested_resolved=requested_resolved,
        resolved_alias=resolved_alias,
    )


def resolve_effective_model(
    config: KiroCrewConfig,
    agent_name: str | None = None,
) -> str:
    """Return the model a new session on *agent_name* would start with.

    Single source of truth for the default-model precedence, so the display
    path (the dashboard's model chip) and the execution path
    (``create_provider_factory._acp``) cannot drift apart. Tiers, highest first:

    1. the KiroCrew agent's own ``model``
    2. the bound kiro agent's pinned ``model`` (skipped for the built-in
       ``kirocrew`` agent, which tracks the global by design)
    3. the global ``agent.model`` default
    4. the installed ``kirocrew.json`` / bundled ``defaults.json`` model

    A per-session pick outranks all of these and is NOT considered here — the
    caller holds it. Returns ``""`` when every tier defers, meaning the backend
    picks (kiro-cli's own ``chat.defaultModel``).
    """
    bindings = resolve_agent_bindings(config, agent_name)
    if bindings.model:
        return bindings.model

    kiro_agent = bindings.kiro_agent
    if kiro_agent and kiro_agent != "kirocrew":
        pinned = normalize_agent_model(config._resolve_named_agent_model(kiro_agent))
        if pinned:
            return pinned

    configured = normalize_agent_model(config.agent.model)
    if configured:
        return configured
    # agent.model is "auto"/unset: fall through to the installed agent file the
    # factory would read, so the chip shows what will actually be used.
    return normalize_agent_model(config._resolve_agent_model())


def validate_kiro_agent_references(
    config: KiroCrewConfig,
    installed_agents: list[str],
) -> None:
    """Cross-reference kiro_agent values against installed agents.

    Logs warnings for unresolved references. Never raises.
    """
    installed_names = set(installed_agents)
    for mc_name, mc_agent in config.agents.items():
        if mc_agent.kiro_agent and mc_agent.kiro_agent not in installed_names:
            logger.warning(
                "KiroCrew agent '%s' references kiro agent '%s' " "which is not installed",
                mc_name,
                mc_agent.kiro_agent,
            )
