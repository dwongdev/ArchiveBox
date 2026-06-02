__package__ = "archivebox.plugins"

import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, TypedDict

from abx_plugins import get_plugins_dir
from django.utils.safestring import mark_safe

from archivebox.config.constants import CONSTANTS


class ConfigLookup(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def items(self) -> Iterable[tuple[str, Any]]: ...


class PluginSpecialConfig(TypedDict):
    enabled: bool
    timeout: int
    binary: str


BUILTIN_PLUGINS_DIR = Path(get_plugins_dir()).resolve()
USER_PLUGINS_DIR = CONSTANTS.USER_PLUGINS_DIR


def iter_plugin_dirs() -> list[Path]:
    """Iterate over all built-in and user plugin directories."""
    plugin_dirs: list[Path] = []

    for base_dir in (BUILTIN_PLUGINS_DIR, USER_PLUGINS_DIR):
        if not base_dir.exists():
            continue

        for plugin_dir in base_dir.iterdir():
            if plugin_dir.is_dir() and not plugin_dir.name.startswith("_"):
                plugin_dirs.append(plugin_dir)

    return plugin_dirs


@lru_cache(maxsize=1)
def get_plugins() -> list[str]:
    """
    Get list of available plugins by discovering plugin directories.

    Returns plugin directory names for any plugin that exposes hooks, config.json,
    or a standardized templates/icon.html asset. This includes non-extractor
    plugins such as binary providers and shared base plugins.
    """
    plugins = []

    for plugin_dir in iter_plugin_dirs():
        has_hooks = any(plugin_dir.glob("on_*__*.*"))
        has_config = (plugin_dir / "config.json").exists()
        has_icon = (plugin_dir / "templates" / "icon.html").exists()
        if has_hooks or has_config or has_icon:
            plugins.append(plugin_dir.name)

    return sorted(set(plugins))


def get_plugin_name(plugin: str) -> str:
    """
    Get the base plugin name without numeric prefix.

    Examples:
        '10_title' -> 'title'
        '26_readability' -> 'readability'
        '50_parse_html_urls' -> 'parse_html_urls'
    """
    parts = plugin.split("_", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1]
    return plugin


def get_enabled_plugins(config: ConfigLookup | None = None, **config_kwargs: Any) -> list[str]:
    """
    Get the list of enabled plugins based on config and available hooks.

    Filters plugins by USE_/SAVE_ flags. Only returns plugins that are enabled.
    """
    if config is None:
        from archivebox.config.common import get_config

        config = get_config(**config_kwargs)

    def normalize_enabled_plugins(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(plugin).strip() for plugin in parsed if str(plugin).strip()]
            return [plugin.strip() for plugin in raw.split(",") if plugin.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(plugin).strip() for plugin in value if str(plugin).strip()]
        return [str(value).strip()] if str(value).strip() else []

    plugins_override = config.get("PLUGINS")
    if plugins_override:
        return normalize_enabled_plugins(plugins_override)

    enabled = []
    for plugin in get_plugins():
        plugin_config = get_plugin_special_config(plugin, config)
        if plugin_config["enabled"]:
            enabled.append(plugin)

    return enabled


def discover_plugins_that_provide_interface(
    module_name: str,
    required_attrs: list[str],
    plugin_prefix: str | None = None,
) -> dict[str, Any]:
    """
    Discover plugins that provide a specific Python module with required interface.

    This enables dynamic plugin discovery for features like search backends,
    storage backends, etc. without hardcoding imports.
    """
    import importlib.util

    backends = {}

    for base_dir in (BUILTIN_PLUGINS_DIR, USER_PLUGINS_DIR):
        if not base_dir.exists():
            continue

        for plugin_dir in base_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            plugin_name = plugin_dir.name
            if plugin_prefix and not plugin_name.startswith(plugin_prefix):
                continue

            module_path = plugin_dir / f"{module_name}.py"
            if not module_path.exists():
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"archivebox.dynamic_plugins.{plugin_name}.{module_name}",
                    module_path,
                )
                if spec is None or spec.loader is None:
                    continue

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                if not all(attr in vars(module) for attr in required_attrs):
                    continue

                if plugin_prefix:
                    backend_name = plugin_name[len(plugin_prefix) :]
                else:
                    backend_name = plugin_name

                backends[backend_name] = module

            except Exception:
                continue

    return backends


def get_search_backends() -> dict[str, Any]:
    """
    Discover all available search backend plugins.

    Search backends must provide a search.py module with:
        - search(query: str) -> List[str]  (returns snapshot IDs)
        - flush(snapshot_ids: Iterable[str]) -> None
    """
    return discover_plugins_that_provide_interface(
        module_name="search",
        required_attrs=["search", "flush"],
        plugin_prefix="search_backend_",
    )


@lru_cache(maxsize=1)
def discover_plugin_configs() -> dict[str, dict[str, Any]]:
    """
    Discover all plugin config.json schemas.

    Each plugin can define a config.json file with JSONSchema defining
    its configuration options. This is intentionally cached because these
    schemas are plugin package metadata, not live user config; runtime values
    still come from env/db config at each callsite.
    """
    configs = {}

    for plugin_dir in iter_plugin_dirs():
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            continue

        try:
            with open(config_path) as f:
                schema = json.load(f)

            if not isinstance(schema, dict):
                continue
            if schema.get("type") != "object":
                continue
            if "properties" not in schema:
                continue

            configs[plugin_dir.name] = schema

        except (json.JSONDecodeError, OSError) as e:
            import sys

            print(f"Warning: Failed to load config.json from {plugin_dir.name}: {e}", file=sys.stderr)
            continue

    return configs


def get_plugin_special_config(plugin_name: str, config: ConfigLookup, _visited: set[str] | None = None) -> PluginSpecialConfig:
    """
    Extract special config keys for a plugin following naming conventions.

    ArchiveBox recognizes 3 special config key patterns per plugin:
        - {PLUGIN}_ENABLED: Enable/disable toggle (default True)
        - {PLUGIN}_TIMEOUT: Plugin-specific timeout (fallback to TIMEOUT, default 300)
        - {PLUGIN}_BINARY: Primary binary path (default to plugin_name)
    """
    plugin_upper = plugin_name.upper()

    plugins_whitelist = config.get("PLUGINS", "")
    if plugins_whitelist:
        plugin_configs = discover_plugin_configs()
        plugin_names = {p.strip().lower() for p in plugins_whitelist.split(",") if p.strip()}
        pending = list(plugin_names)

        while pending:
            current = pending.pop()
            schema = plugin_configs.get(current, {})
            required_plugins = schema.get("required_plugins", [])
            if not isinstance(required_plugins, list):
                continue

            for required_plugin in required_plugins:
                required_plugin_name = str(required_plugin).strip().lower()
                if not required_plugin_name or required_plugin_name in plugin_names:
                    continue
                plugin_names.add(required_plugin_name)
                pending.append(required_plugin_name)

        if plugin_name.lower() not in plugin_names:
            enabled = False
        else:
            enabled_key = f"{plugin_upper}_ENABLED"
            enabled = config.get(enabled_key)
            if enabled is None:
                enabled = True
            elif isinstance(enabled, str):
                enabled = enabled.lower() not in ("false", "0", "no", "")
    else:
        enabled_key = f"{plugin_upper}_ENABLED"
        enabled = config.get(enabled_key)
        if enabled is None:
            enabled = True
        elif isinstance(enabled, str):
            enabled = enabled.lower() not in ("false", "0", "no", "")

    plugin_configs = discover_plugin_configs()
    plugin_name_lower = plugin_name.lower()

    if enabled:
        visited = _visited or set()
        if plugin_name_lower not in visited:
            next_visited = visited | {plugin_name_lower}
            schema = plugin_configs.get(plugin_name_lower, {})
            required_plugins = schema.get("required_plugins", [])
            if isinstance(required_plugins, list):
                for required_plugin in required_plugins:
                    required_plugin_name = str(required_plugin).strip()
                    if not required_plugin_name:
                        continue
                    required_config = get_plugin_special_config(required_plugin_name, config, _visited=next_visited)
                    if not required_config["enabled"]:
                        enabled = False
                        break

    timeout_key = f"{plugin_upper}_TIMEOUT"
    timeout = config.get(timeout_key) or config.get("TIMEOUT", 300)

    binary_key = f"{plugin_upper}_BINARY"
    binary = config.get(binary_key, plugin_name)

    return {
        "enabled": bool(enabled),
        "timeout": int(timeout),
        "binary": str(binary),
    }


DEFAULT_TEMPLATES = {
    "icon": """
        <span title="{{ plugin }}" style="display:inline-flex; width:20px; height:20px; align-items:center; justify-content:center;">
            {{ icon }}
        </span>
    """,
    "card": """
        <iframe src="{{ output_path }}"
                class="card-img-top"
                style="width: 100%; height: 100%; border: none;"
                sandbox="allow-same-origin allow-scripts allow-forms"
                loading="lazy"
                fetchpriority="low">
        </iframe>
    """,
    "full": """
        <iframe src="{{ output_path }}"
                class="full-page-iframe"
                style="width: 100%; height: 100vh; border: none;"
                sandbox="allow-same-origin allow-scripts allow-forms">
        </iframe>
    """,
}


@lru_cache(maxsize=None)
def get_plugin_template(plugin: str, template_name: str, fallback: bool = True) -> str | None:
    """
    Get a plugin template by plugin name and template type.

    Args:
        plugin: Plugin name (e.g., 'screenshot', '15_singlefile')
        template_name: One of 'icon', 'card', 'full'
        fallback: If True, return default template if plugin template not found
    """
    base_name = get_plugin_name(plugin)
    if base_name in ("yt-dlp", "youtube-dl"):
        base_name = "ytdlp"

    for plugin_dir in iter_plugin_dirs():
        if plugin_dir.name == base_name or plugin_dir.name.endswith(f"_{base_name}"):
            template_path = plugin_dir / "templates" / f"{template_name}.html"
            if template_path.exists():
                return template_path.read_text()

    if fallback:
        return DEFAULT_TEMPLATES.get(template_name, "")

    return None


@lru_cache(maxsize=None)
def get_plugin_icon(plugin: str) -> str:
    """
    Get the icon for a plugin from its icon.html template.
    """
    icon_template = get_plugin_template(plugin, "icon", fallback=False)
    if icon_template:
        return mark_safe(icon_template.strip())

    return mark_safe("📁")
