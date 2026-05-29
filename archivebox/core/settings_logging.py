__package__ = "archivebox.core"

import re
import os
import tempfile
import logging
from pathlib import Path


from archivebox.config import CONSTANTS


IGNORABLE_URL_PATTERNS = [
    re.compile(r"/.*/?apple-touch-icon.*\.png"),
    re.compile(r"/.*/?favicon\.ico"),
    re.compile(r"/.*/?robots\.txt"),
    re.compile(r"/.*/?.*\.(css|js)\.map"),
    re.compile(r"/.*/?.*\.(css|js)\.map"),
    re.compile(r"/static/.*"),
    re.compile(r"/admin/jsi18n/"),
]


SENSITIVE_QUERY_PARAM_RE = re.compile(r"(?i)([?&](?:api_key|token|access_token|password|secret)=)([^&#\s]+)")
WEBREQUEST_RE = re.compile(r"<WebRequest\b[^>]*\bmethod=(?P<method>[A-Z]+)\s+uri=(?P<uri>\S+)")
RUNNING_AT_RE = re.compile(r"running at\s+([^>]+:\d+)")


def _redact_url(url: str) -> str:
    return SENSITIVE_QUERY_PARAM_RE.sub(r"\1[REDACTED]", url)


def _short_code_path(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except (OSError, ValueError):
        parts = Path(path).parts
        return "/".join(parts[-4:]) if len(parts) > 4 else path


def _resolve_view_name(url: str) -> str:
    try:
        from django.urls import resolve

        match = resolve(url.split("?", 1)[0])
        if match.view_name:
            return match.view_name
        view_func = match.func
        view_class = getattr(view_func, "view_class", None)
        if view_class is not None:
            return f"{view_class.__module__}.{view_class.__name__}"
        return f"{view_func.__module__}.{view_func.__name__}"
    except Exception:
        return "unknown"


class NoisyRequestsFilter(logging.Filter):
    def filter(self, record) -> bool:
        logline = record.getMessage()
        # '"GET /api/v1/docs HTTP/1.1" 200 1023'
        # '"GET /static/admin/js/SelectFilter2.js HTTP/1.1" 200 15502'
        # '"GET /static/admin/js/SelectBox.js HTTP/1.1" 304 0'
        # '"GET /admin/jsi18n/ HTTP/1.1" 200 3352'
        # '"GET /admin/api/apitoken/0191bbf8-fd5e-0b8c-83a8-0f32f048a0af/change/ HTTP/1.1" 200 28778'

        # ignore harmless 404s for the patterns in IGNORABLE_URL_PATTERNS
        for pattern in IGNORABLE_URL_PATTERNS:
            ignorable_GET_request = re.compile(f'"GET {pattern.pattern} HTTP/.*" (2..|30.|404) .+$', re.I | re.M)
            if ignorable_GET_request.match(logline):
                return False

            ignorable_404_pattern = re.compile(f"Not Found: {pattern.pattern}", re.I | re.M)
            if ignorable_404_pattern.match(logline):
                return False

        return True


class DaphneCloseTimeoutFilter(logging.Filter):
    def filter(self, record) -> bool:
        if record.name != "daphne.server":
            return True
        logline = record.getMessage()
        if not (
            "Application instance" in logline
            and "for connection <WebRequest" in logline
            and "took too long to shut down" in logline
            and "was killed" in logline
        ):
            return True

        match = WEBREQUEST_RE.search(logline)
        method = match.group("method") if match else "-"
        uri = _redact_url(match.group("uri")) if match else "-"
        view = _resolve_view_name(uri) if uri != "-" else "unknown"
        code_paths = [_short_code_path(path) for path in RUNNING_AT_RE.findall(logline)]
        code = code_paths[-1] if code_paths else "unknown"
        record.msg = f"Daphne killed slow response after client disconnect: {method} {uri} view={view} code={code}"
        record.args = ()
        return True


class CustomOutboundWebhookLogFormatter(logging.Formatter):
    def format(self, record):
        result = super().format(record)
        return result.replace("HTTP Request: ", "OutboundWebhook: ")


class StripANSIColorCodesFilter(logging.Filter):
    _ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    _bare_re = re.compile(r"\[[0-9;]*m")

    def filter(self, record) -> bool:
        msg = record.getMessage()
        if isinstance(msg, str) and ("\x1b[" in msg or "[m" in msg):
            msg = self._ansi_re.sub("", msg)
            msg = self._bare_re.sub("", msg)
            record.msg = msg
            record.args = ()
        return True


ERROR_LOG = tempfile.NamedTemporaryFile().name

LOGS_DIR = CONSTANTS.LOGS_DIR

if os.access(LOGS_DIR, os.W_OK) and LOGS_DIR.is_dir():
    ERROR_LOG = LOGS_DIR / "errors.log"
else:
    # historically too many edge cases here around creating log dir w/ correct permissions early on
    # if there's an issue on startup, we trash the log and let user figure it out via stdout/stderr
    # print(f'[!] WARNING: data/logs dir does not exist. Logging to temp file: {ERROR_LOG}')
    pass

LOG_LEVEL_DATABASE = "WARNING"  # change to DEBUG to log all SQL queries
LOG_LEVEL_REQUEST = "WARNING"  # if DEBUG else 'WARNING'

if LOG_LEVEL_DATABASE == "DEBUG":
    db_logger = logging.getLogger("django.db.backends")
    db_logger.setLevel(logging.DEBUG)
    db_logger.addHandler(logging.StreamHandler())


SETTINGS_LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "rich": {
            "datefmt": "[%Y-%m-%d %H:%M:%S]",
            "format": "%(name)s %(message)s",
        },
        "outbound_webhooks": {
            "()": CustomOutboundWebhookLogFormatter,
            "datefmt": "[%Y-%m-%d %H:%M:%S]",
        },
    },
    "filters": {
        "noisyrequestsfilter": {
            "()": NoisyRequestsFilter,
        },
        "daphneclosetimeout": {
            "()": DaphneCloseTimeoutFilter,
        },
        "stripansi": {
            "()": StripANSIColorCodesFilter,
        },
        "require_debug_false": {
            "()": "django.utils.log.RequireDebugFalse",
        },
        "require_debug_true": {
            "()": "django.utils.log.RequireDebugTrue",
        },
    },
    "handlers": {
        "default": {
            "class": "rich.logging.RichHandler",
            "formatter": "rich",
            "level": "DEBUG",
            "markup": False,
            "rich_tracebacks": False,  # Use standard Python tracebacks (no frame/box)
            "filters": ["noisyrequestsfilter", "daphneclosetimeout", "stripansi"],
        },
        "logfile": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": ERROR_LOG,
            "maxBytes": 1024 * 1024 * 25,  # 25 MB
            "backupCount": 10,
            "formatter": "rich",
            "filters": ["noisyrequestsfilter", "daphneclosetimeout", "stripansi"],
        },
        "outbound_webhooks": {
            "class": "rich.logging.RichHandler",
            "markup": False,
            "rich_tracebacks": False,  # Use standard Python tracebacks (no frame/box)
            "formatter": "outbound_webhooks",
        },
        # "mail_admins": {
        #     "level": "ERROR",
        #     "filters": ["require_debug_false"],
        #     "class": "django.utils.log.AdminEmailHandler",
        # },
        "null": {
            "class": "logging.NullHandler",
        },
    },
    "root": {
        "handlers": ["default", "logfile"],
        "level": "INFO",
        "formatter": "rich",
    },
    "loggers": {
        "api": {
            "handlers": ["default", "logfile"],
            "level": "DEBUG",
            "propagate": False,
        },
        "checks": {
            "handlers": ["default", "logfile"],
            "level": "DEBUG",
            "propagate": False,
        },
        "core": {
            "handlers": ["default", "logfile"],
            "level": "DEBUG",
            "propagate": False,
        },
        "httpx": {
            "handlers": ["outbound_webhooks"],
            "level": "INFO",
            "formatter": "outbound_webhooks",
            "propagate": False,
        },
        "django": {
            "handlers": ["default", "logfile"],
            "level": "INFO",
            "filters": ["noisyrequestsfilter"],
            "propagate": False,
        },
        "django.utils.autoreload": {
            "propagate": False,
            "handlers": [],
            "level": "ERROR",
        },
        "django.channels.server": {
            # see archivebox.misc.monkey_patches.ModifiedAccessLogGenerator for dedicated daphne server logging settings
            "propagate": False,
            "handlers": ["default", "logfile"],
            "level": "INFO",
            "filters": ["noisyrequestsfilter"],
        },
        "django.server": {  # logs all requests (2xx, 3xx, 4xx)
            "propagate": False,
            "handlers": ["default", "logfile"],
            "level": "INFO",
            "filters": ["noisyrequestsfilter"],
        },
        "django.request": {  # only logs 4xx and 5xx errors
            "propagate": False,
            "handlers": ["default", "logfile"],
            "level": "ERROR",
            "filters": ["noisyrequestsfilter"],
        },
        "django.db.backends": {
            "propagate": False,
            "handlers": ["default"],
            "level": LOG_LEVEL_DATABASE,
        },
    },
}
