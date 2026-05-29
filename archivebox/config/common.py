__package__ = "archivebox.config"

import json
import os
import re
import secrets
import sys
import shutil
from functools import lru_cache
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, ClassVar, cast
from pathlib import Path

from rich.console import Console
from pydantic import BaseModel, Field, create_model, field_validator, model_validator
from pydantic_settings import SettingsConfigDict
from abx_plugins.plugins.base.utils import BASE_CONFIG_PATH, build_config_model, resolve_plugin_configs

from archivebox.config.configset import BaseConfigSet
from archivebox.config.configset import COMPUTED_CONFIG_KEYS

from .constants import CONSTANTS
from .ldap import LDAPConfig
from .version import get_COMMIT_HASH, get_BUILD_TIME, VERSION
from .permissions import IN_DOCKER

ConfigOverrides = Mapping[str, object]
ConfigPayload = dict[str, object]
PluginSchemaDocuments = dict[str, dict[str, Any]]

###################### Config ##########################

_STDOUT_CONSOLE = Console()
_STDERR_CONSOLE = Console(stderr=True)
_WARNED_SERVER_SECURITY_MODES: set[str] = set()
_WARNED_ARCHIVING_CONFIGS: set[tuple[int, bool]] = set()


def _legacy_bool(value: object) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def permissions_from_legacy_public_flags(raw_config: Mapping[str, object]) -> str | None:
    if str(raw_config.get("PERMISSIONS") or "").strip():
        return None

    public_snapshots = _legacy_bool(raw_config.get("PUBLIC_SNAPSHOTS"))
    public_index = _legacy_bool(raw_config.get("PUBLIC_INDEX"))
    if public_snapshots is False:
        return "private"
    if public_index is False:
        return "unlisted"
    if public_snapshots is True or public_index is True:
        return "public"
    return None


def rprint(*args, file=None, **kwargs):
    console = _STDERR_CONSOLE if file is sys.stderr else _STDOUT_CONSOLE
    console.print(*args, **kwargs)


class ShellConfig(BaseConfigSet):
    toml_section_header: str = "SHELL_CONFIG"

    DEBUG: bool = Field(default="--debug" in sys.argv)

    IS_TTY: bool = Field(default=sys.stdout.isatty())
    USE_COLOR: bool = Field(default=sys.stdout.isatty())
    SHOW_PROGRESS: bool = Field(default=sys.stdout.isatty())

    IN_DOCKER: bool = Field(default=IN_DOCKER)
    IN_QEMU: bool = Field(default=False)

    ANSI: dict[str, str] = Field(
        default_factory=lambda: CONSTANTS.DEFAULT_CLI_COLORS if sys.stdout.isatty() else CONSTANTS.DISABLED_CLI_COLORS,
    )

    @property
    def TERM_WIDTH(self) -> int:
        if not self.IS_TTY:
            return 200
        return shutil.get_terminal_size((140, 10)).columns

    @property
    def COMMIT_HASH(self) -> str | None:
        return get_COMMIT_HASH()

    @property
    def BUILD_TIME(self) -> str:
        return get_BUILD_TIME()


class StorageConfig(BaseConfigSet):
    toml_section_header: str = "STORAGE_CONFIG"

    # ARCHIVE_DIR / USERS_DIR are resolved dynamically via get_config().
    ARCHIVE_DIR: Path = Field(default=CONSTANTS.ARCHIVE_DIR)
    USERS_DIR: Path = Field(default=CONSTANTS.USERS_DIR)
    PERSONAS_DIR: Path = Field(default=CONSTANTS.PERSONAS_DIR)

    # TMP_DIR must be a local, fast, readable/writable dir by archivebox user,
    # must be a short path due to unix path length restrictions for socket files (<100 chars)
    # must be a local SSD/tmpfs for speed and because bind mounts/network mounts/FUSE dont support unix sockets
    TMP_DIR: Path = Field(default=CONSTANTS.DEFAULT_TMP_DIR)

    # LIB_DIR must be a local, fast, readable/writable dir by archivebox user,
    # must be able to contain executable binaries (up to 5GB size)
    # should not be a remote/network/FUSE mount for speed reasons, otherwise extractors will be slow
    LIB_DIR: Path = Field(default=CONSTANTS.DEFAULT_LIB_DIR)

    # LIB_BIN_DIR is where installed binaries can be symlinked for shared runtime lookup.
    # abxpkg/abx-dl build the executable lookup env at exec time.
    LIB_BIN_DIR: Path = Field(default=CONSTANTS.DEFAULT_LIB_BIN_DIR)

    # CUSTOM_TEMPLATES_DIR allows users to override default templates
    # defaults to DATA_DIR / 'user_templates' but can be configured
    CUSTOM_TEMPLATES_DIR: Path = Field(default=CONSTANTS.CUSTOM_TEMPLATES_DIR)

    OUTPUT_PERMISSIONS: str = Field(default="644")
    RESTRICT_FILE_NAMES: str = Field(default="windows")
    ENFORCE_ATOMIC_WRITES: bool = Field(default=True)
    ALLOW_NO_UNIX_SOCKETS: bool = Field(default=False, alias="ARCHIVEBOX_ALLOW_NO_UNIX_SOCKETS")

    # not supposed to be user settable:
    DIR_OUTPUT_PERMISSIONS: str = Field(default="755")  # computed from OUTPUT_PERMISSIONS


class GeneralConfig(BaseConfigSet):
    toml_section_header: str = "GENERAL_CONFIG"

    TAG_SEPARATOR_PATTERN: str = Field(default=r"[,]")


class ServerConfig(BaseConfigSet):
    toml_section_header: str = "SERVER_CONFIG"

    SERVER_SECURITY_MODES: ClassVar[tuple[str, ...]] = (
        "safe-subdomains-fullreplay",
        "safe-onedomain-nojsreplay",
        "unsafe-onedomain-noadmin",
        "danger-onedomain-fullreplay",
    )

    SECRET_KEY: str = Field(default_factory=lambda: "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789_") for _ in range(50)))
    BIND_ADDR: str = Field(default="127.0.0.1:8000")
    BASE_URL: str = Field(default="")
    ALLOWED_HOSTS: str = Field(default="*")
    CSRF_TRUSTED_ORIGINS: str = Field(default="")
    SERVER_SECURITY_MODE: str = Field(default="safe-subdomains-fullreplay")

    SNAPSHOTS_PER_PAGE: int = Field(default=40)
    PREVIEW_ORIGINALS: bool = Field(default=True)
    FOOTER_INFO: str = Field(
        default="Content is hosted for personal archiving purposes only.  Contact server owner for any takedown requests.",
    )
    # CUSTOM_TEMPLATES_DIR: Path          = Field(default=None)  # this is now a constant

    PUBLIC_INDEX: bool = Field(default=True)
    PUBLIC_ADD_VIEW: bool = Field(default=False)

    ADMIN_USERNAME: str | None = Field(default=None)
    ADMIN_PASSWORD: str | None = Field(default=None)

    REVERSE_PROXY_USER_HEADER: str = Field(default="Remote-User")
    REVERSE_PROXY_WHITELIST: str = Field(default="")
    LOGOUT_REDIRECT_URL: str = Field(default="/")

    @field_validator("SERVER_SECURITY_MODE", mode="after")
    def validate_server_security_mode(cls, v: str) -> str:
        mode = (v or "").strip().lower()
        if mode not in cls.SERVER_SECURITY_MODES:
            raise ValueError(f"SERVER_SECURITY_MODE must be one of: {', '.join(cls.SERVER_SECURITY_MODES)}")
        return mode

    @property
    def USES_SUBDOMAIN_ROUTING(self) -> bool:
        return self.SERVER_SECURITY_MODE == "safe-subdomains-fullreplay"

    @property
    def ENABLES_FULL_JS_REPLAY(self) -> bool:
        return self.SERVER_SECURITY_MODE in (
            "safe-subdomains-fullreplay",
            "unsafe-onedomain-noadmin",
            "danger-onedomain-fullreplay",
        )

    @property
    def CONTROL_PLANE_ENABLED(self) -> bool:
        return self.SERVER_SECURITY_MODE != "unsafe-onedomain-noadmin"

    @property
    def BLOCK_UNSAFE_METHODS(self) -> bool:
        return self.SERVER_SECURITY_MODE == "unsafe-onedomain-noadmin"

    @property
    def SHOULD_NEUTER_RISKY_REPLAY(self) -> bool:
        return self.SERVER_SECURITY_MODE == "safe-onedomain-nojsreplay"

    @property
    def IS_UNSAFE_MODE(self) -> bool:
        return self.SERVER_SECURITY_MODE == "unsafe-onedomain-noadmin"

    @property
    def IS_DANGEROUS_MODE(self) -> bool:
        return self.SERVER_SECURITY_MODE == "danger-onedomain-fullreplay"

    @property
    def IS_LOWER_SECURITY_MODE(self) -> bool:
        return self.SERVER_SECURITY_MODE in (
            "unsafe-onedomain-noadmin",
            "danger-onedomain-fullreplay",
        )


class DatabaseConfig(BaseConfigSet):
    toml_section_header: str = "DATABASE_CONFIG"

    DATABASE_NAME: str = Field(default=str(CONSTANTS.DATABASE_FILE), alias="ARCHIVEBOX_DATABASE_NAME")
    SQLITE_JOURNAL_MODE: str = Field(
        default="WAL",
        alias="ARCHIVEBOX_SQLITE_JOURNAL_MODE",
        pattern=r"(?i)^(DELETE|TRUNCATE|PERSIST|MEMORY|WAL|OFF)$",
    )
    SQLITE_MMAP_SIZE: int = Field(
        default=0 if CONSTANTS.IN_DOCKER else 134217728,
        alias="ARCHIVEBOX_SQLITE_MMAP_SIZE",
        ge=0,
    )
    SQLITE_TIMEOUT: float = Field(default=30.0, alias="ARCHIVEBOX_SQLITE_TIMEOUT", ge=0)
    SQLITE_BUSY_TIMEOUT: int = Field(default=30000, alias="ARCHIVEBOX_SQLITE_BUSY_TIMEOUT", ge=0)
    SQLITE_LOCK_RETRY_TIMEOUT: float = Field(default=60.0, alias="ARCHIVEBOX_SQLITE_LOCK_RETRY_TIMEOUT", ge=0)
    SQLITE_LOCK_RETRY_INTERVAL: float = Field(default=5.0, alias="ARCHIVEBOX_SQLITE_LOCK_RETRY_INTERVAL", gt=0)


def _print_server_security_mode_warning(config: ServerConfig) -> None:
    if not config.IS_LOWER_SECURITY_MODE:
        return
    if config.SERVER_SECURITY_MODE in _WARNED_SERVER_SECURITY_MODES:
        return

    rprint(
        f"[yellow][!] WARNING: ArchiveBox is running with SERVER_SECURITY_MODE={config.SERVER_SECURITY_MODE}[/yellow]",
        file=sys.stderr,
    )
    rprint(
        "[yellow]    Archived pages may share an origin with privileged app routes in this mode.[/yellow]",
        file=sys.stderr,
    )
    rprint(
        "[yellow]    To switch to the safer isolated setup:[/yellow]",
        file=sys.stderr,
    )
    rprint(
        "[yellow]    1. Set SERVER_SECURITY_MODE=safe-subdomains-fullreplay[/yellow]",
        file=sys.stderr,
    )
    rprint(
        "[yellow]    2. Point *.archivebox.localhost (or your chosen base domain) at this server[/yellow]",
        file=sys.stderr,
    )
    rprint(
        "[yellow]    3. Configure wildcard DNS/TLS or your reverse proxy so admin., web., api., and snapshot subdomains resolve[/yellow]",
        file=sys.stderr,
    )
    _WARNED_SERVER_SECURITY_MODES.add(config.SERVER_SECURITY_MODE)


class ArchivingConfig(BaseConfigSet):
    toml_section_header: str = "ARCHIVING_CONFIG"

    PLUGINS: str = Field(
        default="",
        description="Comma-separated plugin selection for this run. Empty means use enabled plugin defaults.",
    )
    ENABLED_PLUGINS: str = Field(
        default="",
        description="Comma-separated plugin selection override used by the UI and API.",
    )
    ENABLED_EXTRACTORS: str = Field(
        default="",
        description="Legacy comma-separated plugin selection override.",
    )

    ONLY_NEW: bool = Field(default=True)
    OVERWRITE: bool = Field(default=False)

    TIMEOUT: int = Field(default=60)
    MAX_URL_ATTEMPTS: int = Field(default=50)
    MAX_DEPTH: int = Field(default=0)
    CRAWL_MAX_URLS: int = Field(default=0)
    CRAWL_MAX_SIZE: int = Field(default=0)
    CRAWL_TIMEOUT: int = Field(default=0, description="Maximum total crawl runtime in seconds (0 = unlimited).")
    CRAWL_MAX_CONCURRENT_SNAPSHOTS: int = Field(
        default=4,
        description="Maximum number of snapshots to archive concurrently within one crawl.",
    )
    SNAPSHOT_MAX_SIZE: int = Field(default=0)

    RESOLUTION: str = Field(default="1440,2000")
    CHECK_SSL_VALIDITY: bool = Field(default=True)
    USER_AGENT: str = Field(
        default=f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 ArchiveBox/{VERSION} (+https://github.com/ArchiveBox/ArchiveBox/)",
    )
    COOKIES_FILE: Path | None = Field(default=None)

    URL_DENYLIST: str = Field(default=r"\.(css|js|otf|ttf|woff|woff2|gstatic\.com|googleapis\.com/css)(\?.*)?$", alias="URL_BLACKLIST")
    URL_ALLOWLIST: str | None = Field(default=None, alias="URL_WHITELIST")

    SAVE_ALLOWLIST: dict[str, list[str]] = Field(default={})  # mapping of regex patterns to list of archive methods
    SAVE_DENYLIST: dict[str, list[str]] = Field(default={})

    DEFAULT_PERSONA: str = Field(default="Default")
    PERMISSIONS: str = Field(
        default="public",
        description="Snapshot visibility: public lists and serves content, unlisted serves direct links only, private requires admin login.",
    )
    DELETE_AFTER: str = Field(
        default="0",
        description=(
            "Automatically delete Crawl, Snapshot, ArchiveResult, and Process rows after this duration. "
            "Use 0, '', or None to disable. Allowed units: h/hr/hour, d/day, w/week, mo/month, y/year; "
            "minimum non-zero duration is 1h."
        ),
    )

    def warn_if_invalid(self) -> None:
        if int(self.TIMEOUT) < 5:
            rprint(f"[red][!] Warning: TIMEOUT is set too low! (currently set to TIMEOUT={self.TIMEOUT} seconds)[/red]", file=sys.stderr)
            rprint("    You must allow *at least* 5 seconds for indexing and archive methods to run successfully.", file=sys.stderr)
            rprint("    (Setting it to somewhere between 30 and 3000 seconds is recommended)", file=sys.stderr)
            rprint(file=sys.stderr)
            rprint("    If you want to make ArchiveBox run faster, disable specific archive methods instead:", file=sys.stderr)
            rprint("        https://github.com/ArchiveBox/ArchiveBox/wiki/Configuration#archive-method-toggles", file=sys.stderr)
            rprint(file=sys.stderr)

    @field_validator("CHECK_SSL_VALIDITY", mode="after")
    def validate_check_ssl_validity(cls, v):
        """SIDE EFFECT: disable "you really shouldnt disable ssl" warnings emitted by requests"""
        if not v:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return v

    @field_validator("DELETE_AFTER", mode="before")
    @classmethod
    def validate_delete_after(cls, value):
        parse_delete_after(value)
        if value is None:
            return "0"
        return str(value).strip() or "0"

    @field_validator("PERMISSIONS", mode="before")
    @classmethod
    def validate_permissions(cls, value):
        normalized = str(value or "public").strip().lower()
        if normalized not in {"public", "unlisted", "private"}:
            raise ValueError("PERMISSIONS must be one of: public, unlisted, private.")
        return normalized

    @property
    def URL_ALLOWLIST_PTN(self) -> re.Pattern | None:
        return re.compile(self.URL_ALLOWLIST, CONSTANTS.ALLOWDENYLIST_REGEX_FLAGS) if self.URL_ALLOWLIST else None

    @property
    def URL_DENYLIST_PTN(self) -> re.Pattern:
        return re.compile(self.URL_DENYLIST, CONSTANTS.ALLOWDENYLIST_REGEX_FLAGS)

    @property
    def SAVE_ALLOWLIST_PTNS(self) -> dict[re.Pattern, list[str]]:
        return (
            {
                # regexp: methods list
                re.compile(key, CONSTANTS.ALLOWDENYLIST_REGEX_FLAGS): val
                for key, val in self.SAVE_ALLOWLIST.items()
            }
            if self.SAVE_ALLOWLIST
            else {}
        )

    @property
    def SAVE_DENYLIST_PTNS(self) -> dict[re.Pattern, list[str]]:
        return (
            {
                # regexp: methods list
                re.compile(key, CONSTANTS.ALLOWDENYLIST_REGEX_FLAGS): val
                for key, val in self.SAVE_DENYLIST.items()
            }
            if self.SAVE_DENYLIST
            else {}
        )


def parse_delete_after(value) -> timedelta | None:
    if value is None:
        return None

    raw = str(value).strip().lower()
    if raw in ("", "0", "none", "false", "no", "off"):
        return None

    match = re.fullmatch(r"(\d+)\s*(h|hr|hrs|hour|hours|d|day|days|w|week|weeks|mo|month|months|y|yr|yrs|year|years)", raw)
    if not match:
        raise ValueError("DELETE_AFTER must be 0 or a duration like 1h, 7d, 4w, 6mo, or 1y.")

    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        return None
    if unit in ("h", "hr", "hrs", "hour", "hours"):
        duration = timedelta(hours=amount)
    elif unit in ("d", "day", "days"):
        duration = timedelta(days=amount)
    elif unit in ("w", "week", "weeks"):
        duration = timedelta(weeks=amount)
    elif unit in ("mo", "month", "months"):
        duration = timedelta(days=30 * amount)
    else:
        duration = timedelta(days=365 * amount)

    if duration < timedelta(hours=1):
        raise ValueError("DELETE_AFTER must be 0 or at least 1h.")
    return duration


class SearchBackendConfig(BaseConfigSet):
    toml_section_header: str = "SEARCH_BACKEND_CONFIG"

    USE_INDEXING_BACKEND: bool = Field(default=True)
    USE_SEARCHING_BACKEND: bool = Field(default=True)

    SEARCH_BACKEND_ENGINE: str = Field(default="ripgrep")
    SEARCH_PROCESS_HTML: bool = Field(default=True)


def _plugin_user_config_value(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (dict, list, bool, int, float)) or value is None:
        return json.dumps(value)
    return str(value)


def _plugin_user_config(config: Mapping[str, object]) -> dict[str, str]:
    return {key: _plugin_user_config_value(value) for key, value in config.items()}


def _discover_plugin_config_schemas() -> PluginSchemaDocuments:
    from archivebox.hooks import discover_plugin_configs

    schemas: PluginSchemaDocuments = {}
    if BASE_CONFIG_PATH.exists():
        schemas["base"] = {
            "properties": json.loads(BASE_CONFIG_PATH.read_text()).get("properties", {}),
        }
    schemas.update(discover_plugin_configs())
    return schemas


def _plugin_config_properties(plugin_schemas: PluginSchemaDocuments) -> dict[str, dict[str, Any]]:
    properties: dict[str, dict[str, Any]] = {}
    for schema in plugin_schemas.values():
        schema_properties = schema.get("properties") or {}
        if isinstance(schema_properties, dict):
            properties.update(schema_properties)
    return properties


def _plugin_config_model(plugin_schemas: PluginSchemaDocuments) -> type[BaseModel]:
    return build_config_model("ArchiveBoxPluginConfig", _plugin_config_properties(plugin_schemas))


@lru_cache(maxsize=1)
def _archivebox_config_input_names() -> set[str]:
    names = set(ArchiveBoxConfig.model_fields)
    for field in ArchiveBoxConfig.model_fields.values():
        if isinstance(field.alias, str):
            names.add(field.alias)
    return names


class ArchiveBoxBaseConfig(
    ShellConfig,
    StorageConfig,
    GeneralConfig,
    ServerConfig,
    DatabaseConfig,
    ArchivingConfig,
    SearchBackendConfig,
    LDAPConfig,
):
    """Merged, typed ArchiveBox config.

    Core ArchiveBox fields are declared above. Plugin-owned fields are added to
    the concrete ArchiveBoxConfig model from plugin JSONSchema below, so
    ArchiveBox does not hardcode any individual plugin config names.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        validate_default=True,
        use_enum_values=True,
        arbitrary_types_allowed=True,
        populate_by_name=True,
    )

    DATA_DIR: Path = Field(default=CONSTANTS.DATA_DIR)
    ABX_RUNTIME: str = Field(default="archivebox")
    CRAWL_DIR: Path | None = Field(default=None)
    CRAWL_OUTPUT_DIR: Path | None = Field(default=None)
    SNAP_DIR: Path | None = Field(default=None)
    computed_config_keys: ClassVar[tuple[str, ...]] = COMPUTED_CONFIG_KEYS

    @model_validator(mode="after")
    def resolve_runtime_paths(self):
        self.DATA_DIR = self.DATA_DIR.expanduser().resolve()

        archive_dir = self.ARCHIVE_DIR.expanduser()
        if archive_dir == (CONSTANTS.DATA_DIR / CONSTANTS.ARCHIVE_DIR_NAME) and self.DATA_DIR != CONSTANTS.DATA_DIR:
            archive_dir = self.DATA_DIR / CONSTANTS.ARCHIVE_DIR_NAME
        if not archive_dir.is_absolute():
            archive_dir = self.DATA_DIR / archive_dir
        self.ARCHIVE_DIR = archive_dir.resolve()

        users_dir = self.USERS_DIR.expanduser()
        if users_dir == (CONSTANTS.ARCHIVE_DIR / CONSTANTS.USERS_DIR_NAME):
            users_dir = self.ARCHIVE_DIR / CONSTANTS.USERS_DIR_NAME
        if not users_dir.is_absolute():
            users_dir = self.ARCHIVE_DIR / users_dir
        self.USERS_DIR = users_dir.resolve()

        lib_dir = self.LIB_DIR.expanduser()
        if not lib_dir.is_absolute():
            lib_dir = self.DATA_DIR / lib_dir
        self.LIB_DIR = lib_dir.resolve()

        lib_bin_dir = self.LIB_BIN_DIR.expanduser()
        if lib_bin_dir == CONSTANTS.DEFAULT_LIB_BIN_DIR and self.LIB_DIR != CONSTANTS.DEFAULT_LIB_DIR:
            lib_bin_dir = self.LIB_DIR / "bin"
        elif not lib_bin_dir.is_absolute():
            lib_bin_dir = self.DATA_DIR / lib_bin_dir
        self.LIB_BIN_DIR = lib_bin_dir.resolve()

        return self


def _build_archivebox_config_model(plugin_schemas: PluginSchemaDocuments) -> type[ArchiveBoxBaseConfig]:
    core_fields = set(ArchiveBoxBaseConfig.model_fields)
    plugin_fields: dict[str, Any] = {
        key: (field.annotation, field) for key, field in _plugin_config_model(plugin_schemas).model_fields.items() if key not in core_fields
    }
    return cast(
        type[ArchiveBoxBaseConfig],
        create_model(
            "ArchiveBoxConfig",
            __base__=ArchiveBoxBaseConfig,
            __module__=__name__,
            **plugin_fields,
        ),
    )


PLUGIN_CONFIG_SCHEMAS = _discover_plugin_config_schemas()
ArchiveBoxConfig = _build_archivebox_config_model(PLUGIN_CONFIG_SCHEMAS)


def get_config(
    defaults: ConfigOverrides | None = None,
    overrides: ConfigOverrides | None = None,
    base_config: ArchiveBoxBaseConfig | Mapping[str, object] | None = None,
    persona: Any = None,
    user: Any = None,
    crawl: Any = None,
    snapshot: Any = None,
    archiveresult: Any = None,
    machine: Any = None,
    include_machine: bool = True,
    resolve_plugins: bool = True,
) -> ArchiveBoxBaseConfig:
    """
    Get merged config from all sources.

    Priority (highest to lowest):
    1. Explicit overrides
    2. Per-ArchiveResult config
    3. Per-snapshot config and output path
    4. Per-crawl config and output path
    5. Per-user config
    6. Per-persona derived config
    7. Current machine derived config
    8. Environment variables
    9. Config file (ArchiveBox.conf)
    10. Plugin schema defaults
    11. Core config defaults
    """
    if snapshot is None and archiveresult is not None:
        snapshot = archiveresult.snapshot

    if crawl is None and snapshot is not None:
        crawl = snapshot.crawl

    if include_machine and machine is None:
        try:
            from django.apps import apps

            if apps.ready:
                from archivebox.machine.models import Machine

                machine = Machine.current()
        except Exception:
            machine = None

    if persona is None and crawl is not None:
        persona = crawl.resolve_persona()

    config_data: ConfigPayload = dict(defaults or {})
    if base_config is not None:
        if isinstance(base_config, ArchiveBoxBaseConfig):
            config_data.update(base_config.model_dump(mode="json"))
        else:
            config_data.update(dict(base_config))
    else:
        config_data.update(ArchiveBoxConfig().model_dump(mode="json"))
        legacy_permissions = permissions_from_legacy_public_flags({**BaseConfigSet.load_from_file(CONSTANTS.CONFIG_FILE), **os.environ})
        if legacy_permissions:
            config_data["PERMISSIONS"] = legacy_permissions

    scope_overrides: ConfigPayload = {}

    if include_machine and machine is not None and machine.config:
        from archivebox.machine.models import _sanitize_machine_config

        scope_overrides.update(_sanitize_machine_config(machine.config, lib_dir=config_data.get("LIB_DIR")))

    if persona is not None:
        scope_overrides.update(persona.get_derived_config())

    user_config = getattr(user, "config", None)
    if user_config:
        scope_overrides.update(user_config)

    if crawl is not None and crawl.config:
        scope_overrides.update(crawl.config)

    if crawl is not None:
        crawl_output_dir = None
        if not overrides or "CRAWL_OUTPUT_DIR" not in overrides or "CRAWL_DIR" not in overrides:
            crawl_output_dir = crawl.output_dir
        if not overrides or "CRAWL_OUTPUT_DIR" not in overrides:
            scope_overrides["CRAWL_OUTPUT_DIR"] = crawl_output_dir
        if not overrides or "CRAWL_DIR" not in overrides:
            scope_overrides["CRAWL_DIR"] = crawl_output_dir

    if snapshot is not None and snapshot.config:
        scope_overrides.update(snapshot.config)

    if snapshot is not None:
        scope_overrides["SNAP_DIR"] = snapshot.output_dir

    if archiveresult is not None and archiveresult.config:
        scope_overrides.update(archiveresult.config)

    if overrides:
        scope_overrides.update(overrides)

    legacy_scope_permissions = permissions_from_legacy_public_flags(scope_overrides)
    if legacy_scope_permissions:
        scope_overrides["PERMISSIONS"] = legacy_scope_permissions

    archivebox_scope_overrides = {key: value for key, value in scope_overrides.items() if key in _archivebox_config_input_names()}
    config_data.update(archivebox_scope_overrides)

    if resolve_plugins:
        plugin_schemas = {
            plugin_name: schema.get("properties", {}) for plugin_name, schema in PLUGIN_CONFIG_SCHEMAS.items() if isinstance(schema, dict)
        }
        plugin_global_config = {key: str(value) if isinstance(value, Path) else value for key, value in config_data.items()}
        plugin_sections = resolve_plugin_configs(
            plugin_schemas,
            global_config=plugin_global_config,
            user_config={**BaseConfigSet.load_from_file(CONSTANTS.CONFIG_FILE), **_plugin_user_config(scope_overrides)},
        )
        for plugin_config in plugin_sections.values():
            config_data.update(plugin_config)
        config_data.update(archivebox_scope_overrides)

    config_data["ABX_RUNTIME"] = "archivebox"

    config = ArchiveBoxConfig.model_validate(config_data)
    os.environ["LIB_DIR"] = str(config.LIB_DIR)
    os.environ["LIB_BIN_DIR"] = str(config.LIB_BIN_DIR)
    os.environ["ABXPKG_LIB_DIR"] = str(config.LIB_DIR)
    archiving_warning_key = (config.TIMEOUT, config.USE_COLOR)
    if archiving_warning_key not in _WARNED_ARCHIVING_CONFIGS:
        config.warn_if_invalid()
        _WARNED_ARCHIVING_CONFIGS.add(archiving_warning_key)
    _print_server_security_mode_warning(config)
    return config


def get_all_configs() -> dict[str, BaseConfigSet]:
    """Get all config section objects as a dictionary."""
    return {
        "SHELL_CONFIG": ShellConfig(),
        "STORAGE_CONFIG": StorageConfig(),
        "GENERAL_CONFIG": GeneralConfig(),
        "SERVER_CONFIG": ServerConfig(),
        "DATABASE_CONFIG": DatabaseConfig(),
        "ARCHIVING_CONFIG": ArchivingConfig(),
        "SEARCH_BACKEND_CONFIG": SearchBackendConfig(),
        "LDAP_CONFIG": LDAPConfig(),
    }
