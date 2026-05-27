__package__ = "archivebox.config"

import os
import socket
import hashlib
import tempfile
import platform
import subprocess
from pathlib import Path
from functools import cache
from datetime import datetime
from typing import TYPE_CHECKING

from benedict import benedict

from .permissions import SudoPermission, IS_ROOT, ARCHIVEBOX_USER, ARCHIVEBOX_GROUP

if TYPE_CHECKING:
    from archivebox.config.common import ArchiveBoxConfig

#############################################################################################

PACKAGE_DIR: Path = Path(__file__).resolve().parent.parent  # archivebox source code dir
DATA_DIR: Path = Path(os.environ.get("DATA_DIR", os.getcwd())).resolve()  # archivebox user data dir


def _env_path(key: str, default: Path) -> Path:
    path = Path(os.environ.get(key) or default).expanduser()
    if not path.is_absolute():
        path = DATA_DIR / path
    return path.resolve()


ARCHIVE_DIR: Path = _env_path("ARCHIVE_DIR", DATA_DIR / "archive")  # archivebox snapshot data dir
USERS_DIR: Path = _env_path("USERS_DIR", ARCHIVE_DIR / "users")  # archivebox user-scoped crawl/snapshot data dir

IN_DOCKER = os.environ.get("IN_DOCKER", False) in ("1", "true", "True", "TRUE", "yes")

DATABASE_FILE = DATA_DIR / "index.sqlite3"

#############################################################################################


def _get_collection_id(DATA_DIR=DATA_DIR, force_create=False) -> str:
    collection_id_file = DATA_DIR / ".archivebox_id"

    try:
        return collection_id_file.read_text().strip()
    except (OSError, FileNotFoundError, PermissionError):
        pass

    # hash the machine_id + collection dir path + creation time to get a unique collection_id
    machine_id = get_machine_id()
    collection_path = DATA_DIR.resolve()
    try:
        creation_date = DATA_DIR.stat().st_ctime
    except Exception:
        creation_date = datetime.now().isoformat()
    collection_id = hashlib.sha256(f"{machine_id}:{collection_path}@{creation_date}".encode()).hexdigest()[:8]

    try:
        # only persist collection_id file if we already have an index.sqlite3 file present
        # otherwise we might be running in a directory that is not a collection, no point creating cruft files
        collection_is_active = os.path.isfile(DATABASE_FILE) and os.path.isdir(ARCHIVE_DIR) and os.access(DATA_DIR, os.W_OK)
        if collection_is_active or force_create:
            collection_id_file.write_text(collection_id)

            # if we're running as root right now, make sure the collection_id file is owned by the archivebox user
            if IS_ROOT:
                with SudoPermission(uid=0):
                    if ARCHIVEBOX_USER == 0:
                        subprocess.run(["chmod", "777", str(collection_id_file)])
                    else:
                        subprocess.run(["chown", str(ARCHIVEBOX_USER), str(collection_id_file)])
    except (OSError, FileNotFoundError, PermissionError):
        pass
    return collection_id


@cache
def get_collection_id(DATA_DIR=DATA_DIR) -> str:
    """Get a short, stable, unique ID for the current collection (e.g. abc45678)"""
    return _get_collection_id(DATA_DIR=DATA_DIR)


@cache
def get_machine_id() -> str:
    """Get a short, stable, unique ID for the current machine (e.g. abc45678)"""

    MACHINE_ID = "unknown"
    try:
        import machineid

        MACHINE_ID = machineid.hashed_id("archivebox")[:8]
    except Exception:
        try:
            import uuid
            import hashlib

            MACHINE_ID = hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()[:8]
        except Exception:
            pass
    return MACHINE_ID


@cache
def get_machine_type() -> str:
    """Get a short, stable, unique type identifier for the current machine (e.g. linux-x86_64-docker)"""

    OS: str = platform.system().lower()  # darwin, linux, etc.
    ARCH: str = platform.machine().lower()  # arm64, x86_64, aarch64, etc.
    LIB_DIR_SCOPE: str = f"{ARCH}-{OS}-docker" if IN_DOCKER else f"{ARCH}-{OS}"
    return LIB_DIR_SCOPE


def dir_is_writable(dir_path: Path, uid: int | None = None, gid: int | None = None, fallback=True, chown=True) -> bool:
    """Check if a given directory is writable by a specific user and group (fallback=try as current user is unable to check with provided uid)"""
    current_uid, current_gid = os.geteuid(), os.getegid()
    uid, gid = uid or current_uid, gid or current_gid

    test_file = dir_path / ".permissions_test"
    try:
        with SudoPermission(uid=uid, fallback=fallback):
            test_file.exists()
            test_file.write_text(f"Checking if PUID={uid} PGID={gid} can write to dir")
            test_file.unlink()
            return True
    except (OSError, PermissionError):
        if chown:
            # try fixing it using sudo permissions
            with SudoPermission(uid=uid, fallback=fallback):
                subprocess.run(["chown", f"{uid}:{gid}", str(dir_path)], stderr=subprocess.DEVNULL)
            return dir_is_writable(dir_path, uid=uid, gid=gid, fallback=fallback, chown=False)
    return False


def assert_dir_can_contain_unix_sockets(dir_path: Path) -> bool:
    """Check if a given directory can contain unix sockets (e.g. /tmp/supervisord.sock)"""
    from archivebox.misc.logging_util import pretty_path

    try:
        socket_path = str(dir_path / ".test_socket.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.remove(socket_path)
        except OSError:
            pass
        s.bind(socket_path)
        s.close()
        try:
            os.remove(socket_path)
        except OSError:
            pass
    except Exception as e:
        raise Exception(f"ArchiveBox failed to create a test UNIX socket file in {pretty_path(dir_path, color=False)}") from e

    return True


def create_and_chown_dir(dir_path: Path) -> None:
    """Create a required runtime dir and fix only that dir's ownership when needed."""
    dir_existed = dir_path.exists()
    dir_path.mkdir(parents=True, exist_ok=True)

    try:
        stat = dir_path.stat()
    except OSError:
        return

    if dir_existed and stat.st_uid == ARCHIVEBOX_USER and stat.st_gid == ARCHIVEBOX_GROUP:
        return

    with SudoPermission(uid=0, fallback=True):
        try:
            os.chown(dir_path, ARCHIVEBOX_USER, ARCHIVEBOX_GROUP)
        except (OSError, PermissionError):
            pass


def tmp_dir_socket_path_is_short_enough(dir_path: Path) -> bool:
    socket_file = dir_path.absolute().resolve() / "supervisord.sock"
    return len(f"file://{socket_file}") <= 96


def get_or_create_working_tmp_dir(autofix=True, quiet=True, config: "ArchiveBoxConfig | None" = None, **config_kwargs):
    from archivebox.config.constants import CONSTANTS
    from archivebox.config.common import get_config
    from archivebox.misc.checks import check_tmp_dir

    config = config or get_config(**config_kwargs)
    # try a few potential directories in order of preference
    CANDIDATES = [
        config.TMP_DIR,  # <user-specified>
        CONSTANTS.DEFAULT_TMP_DIR,  # ./data/tmp/<machine_id>
        Path("/var/run/archivebox") / get_collection_id(),  # /var/run/archivebox/abc5d8512
        Path("/tmp") / "archivebox" / get_collection_id(),  # /tmp/archivebox/abc5d8512
        Path("~/.tmp/archivebox").expanduser() / get_collection_id(),  # ~/.tmp/archivebox/abc5d8512
        Path(tempfile.gettempdir())
        / "archivebox"
        / get_collection_id(),  # /var/folders/qy/6tpfrpx100j1t4l312nz683m0000gn/T/archivebox/abc5d8512
        Path(tempfile.gettempdir())
        / "archivebox"
        / get_collection_id()[:4],  # /var/folders/qy/6tpfrpx100j1t4l312nz683m0000gn/T/archivebox/abc5d
        Path(tempfile.gettempdir()) / "abx" / get_collection_id()[:4],  # /var/folders/qy/6tpfrpx100j1t4l312nz683m0000gn/T/abx/abc5
    ]
    fallback_candidate = None
    for candidate in CANDIDATES:
        try:
            create_and_chown_dir(candidate)
        except Exception:
            pass
        if check_tmp_dir(candidate, throw=False, quiet=True, must_exist=True):
            if autofix and config.TMP_DIR != candidate:
                os.environ["TMP_DIR"] = str(candidate)
            return candidate
        try:
            if (
                fallback_candidate is None
                and candidate.exists()
                and dir_is_writable(candidate)
                and tmp_dir_socket_path_is_short_enough(candidate)
            ):
                fallback_candidate = candidate
        except Exception:
            pass

    # Some sandboxed environments disallow AF_UNIX binds entirely.
    # Fall back to the shortest writable path so read-only CLI commands can still run,
    # and let later permission checks surface the missing socket support if needed.
    if fallback_candidate:
        if autofix and config.TMP_DIR != fallback_candidate:
            os.environ["TMP_DIR"] = str(fallback_candidate)
        return fallback_candidate

    if not quiet:
        raise OSError(f"ArchiveBox is unable to find a writable TMP_DIR, tried {CANDIDATES}!")


def get_or_create_working_lib_dir(autofix=True, quiet=False, config: "ArchiveBoxConfig | None" = None, **config_kwargs):
    from archivebox.config.common import get_config
    from archivebox.misc.checks import check_lib_dir

    config = config or get_config(**config_kwargs)

    # LIB_DIR is either the shared platformdirs default or an explicit env/config override.
    CANDIDATES = [config.LIB_DIR]

    for candidate in CANDIDATES:
        try:
            create_and_chown_dir(candidate)
        except Exception:
            pass
        if check_lib_dir(candidate, throw=False, quiet=True, must_exist=True):
            if autofix and config.LIB_DIR != candidate:
                os.environ["LIB_DIR"] = str(candidate)
            return candidate

    if not quiet:
        raise OSError(f"ArchiveBox is unable to find a writable LIB_DIR, tried {CANDIDATES}!")


def get_data_locations(config: "ArchiveBoxConfig | None" = None, **config_kwargs):
    from archivebox.config.constants import CONSTANTS
    from archivebox.config.common import get_config

    config = config or get_config(**config_kwargs)
    try:
        tmp_dir = get_or_create_working_tmp_dir(autofix=True, quiet=True, config=config) or config.TMP_DIR
    except Exception:
        tmp_dir = config.TMP_DIR

    return benedict(
        {
            "DATA_DIR": {
                "path": DATA_DIR.resolve(),
                "enabled": True,
                "is_valid": os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.R_OK) and os.access(DATA_DIR, os.W_OK),
                "is_mount": os.path.ismount(DATA_DIR.resolve()),
            },
            "CONFIG_FILE": {
                "path": CONSTANTS.CONFIG_FILE.resolve(),
                "enabled": True,
                "is_valid": os.path.isfile(CONSTANTS.CONFIG_FILE)
                and os.access(CONSTANTS.CONFIG_FILE, os.R_OK)
                and os.access(CONSTANTS.CONFIG_FILE, os.W_OK),
            },
            "SQL_INDEX": {
                "path": DATABASE_FILE.resolve(),
                "enabled": True,
                "is_valid": os.path.isfile(DATABASE_FILE) and os.access(DATABASE_FILE, os.R_OK) and os.access(DATABASE_FILE, os.W_OK),
                "is_mount": os.path.ismount(DATABASE_FILE.resolve()),
            },
            "ARCHIVE_DIR": {
                "path": config.ARCHIVE_DIR.resolve(),
                "enabled": True,
                "is_valid": os.path.isdir(config.ARCHIVE_DIR)
                and os.access(config.ARCHIVE_DIR, os.R_OK)
                and os.access(config.ARCHIVE_DIR, os.W_OK),
                "is_mount": os.path.ismount(config.ARCHIVE_DIR.resolve()),
            },
            "USERS_DIR": {
                "path": config.USERS_DIR.resolve(),
                "enabled": os.path.isdir(config.USERS_DIR),
                "is_valid": os.path.isdir(config.USERS_DIR)
                and os.access(config.USERS_DIR, os.R_OK)
                and os.access(config.USERS_DIR, os.W_OK),
                "is_mount": os.path.ismount(config.USERS_DIR.resolve()),
            },
            "SOURCES_DIR": {
                "path": CONSTANTS.SOURCES_DIR.resolve(),
                "enabled": True,
                "is_valid": os.path.isdir(CONSTANTS.SOURCES_DIR)
                and os.access(CONSTANTS.SOURCES_DIR, os.R_OK)
                and os.access(CONSTANTS.SOURCES_DIR, os.W_OK),
            },
            "PERSONAS_DIR": {
                "path": CONSTANTS.PERSONAS_DIR.resolve(),
                "enabled": os.path.isdir(CONSTANTS.PERSONAS_DIR),
                "is_valid": os.path.isdir(CONSTANTS.PERSONAS_DIR)
                and os.access(CONSTANTS.PERSONAS_DIR, os.R_OK)
                and os.access(CONSTANTS.PERSONAS_DIR, os.W_OK),  # read + write
            },
            "LOGS_DIR": {
                "path": CONSTANTS.LOGS_DIR.resolve(),
                "enabled": True,
                "is_valid": os.path.isdir(CONSTANTS.LOGS_DIR)
                and os.access(CONSTANTS.LOGS_DIR, os.R_OK)
                and os.access(CONSTANTS.LOGS_DIR, os.W_OK),  # read + write
            },
            "TMP_DIR": {
                "path": tmp_dir.resolve(),
                "enabled": True,
                "is_valid": os.path.isdir(tmp_dir) and os.access(tmp_dir, os.R_OK) and os.access(tmp_dir, os.W_OK),  # read + write
            },
            # "CACHE_DIR": {
            #     "path": CACHE_DIR.resolve(),
            #     "enabled": True,
            #     "is_valid": os.access(CACHE_DIR, os.R_OK) and os.access(CACHE_DIR, os.W_OK),                        # read + write
            # },
        },
    )


def get_code_locations(config: "ArchiveBoxConfig | None" = None, **config_kwargs):
    from archivebox.config.constants import CONSTANTS
    from archivebox.config.common import get_config

    config = config or get_config(**config_kwargs)
    try:
        lib_dir = get_or_create_working_lib_dir(autofix=True, quiet=True, config=config) or config.LIB_DIR
    except Exception:
        lib_dir = config.LIB_DIR

    lib_bin_dir = lib_dir / "bin"

    return benedict(
        {
            "PACKAGE_DIR": {
                "path": (PACKAGE_DIR).resolve(),
                "enabled": True,
                "is_valid": os.access(PACKAGE_DIR / "__main__.py", os.X_OK),  # executable
            },
            "TEMPLATES_DIR": {
                "path": CONSTANTS.TEMPLATES_DIR.resolve(),
                "enabled": True,
                "is_valid": os.access(CONSTANTS.STATIC_DIR, os.R_OK) and os.access(CONSTANTS.STATIC_DIR, os.X_OK),  # read + list
            },
            "CUSTOM_TEMPLATES_DIR": {
                "path": config.CUSTOM_TEMPLATES_DIR.resolve(),
                "enabled": os.path.isdir(config.CUSTOM_TEMPLATES_DIR),
                "is_valid": os.path.isdir(config.CUSTOM_TEMPLATES_DIR) and os.access(config.CUSTOM_TEMPLATES_DIR, os.R_OK),  # read
            },
            "USER_PLUGINS_DIR": {
                "path": CONSTANTS.USER_PLUGINS_DIR.resolve(),
                "enabled": os.path.isdir(CONSTANTS.USER_PLUGINS_DIR),
                "is_valid": os.path.isdir(CONSTANTS.USER_PLUGINS_DIR) and os.access(CONSTANTS.USER_PLUGINS_DIR, os.R_OK),  # read
            },
            "LIB_DIR": {
                "path": lib_dir.resolve(),
                "enabled": True,
                "is_valid": os.path.isdir(lib_dir) and os.access(lib_dir, os.R_OK) and os.access(lib_dir, os.W_OK),  # read + write
            },
            "LIB_BIN_DIR": {
                "path": lib_bin_dir.resolve(),
                "enabled": True,
                "is_valid": os.path.isdir(lib_bin_dir)
                and os.access(lib_bin_dir, os.R_OK)
                and os.access(lib_bin_dir, os.W_OK),  # read + write
            },
        },
    )
