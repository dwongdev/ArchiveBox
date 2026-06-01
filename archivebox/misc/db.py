"""
Database utility functions for ArchiveBox.

Post-bootstrap: requires archivebox.config constants and uses Django lazily
(``from django.db import ...`` inside functions). Not safe to import pre-bootstrap.
"""

__package__ = "archivebox.misc"

from io import StringIO
from pathlib import Path
from typing import TextIO
from typing import Any
import fcntl
import time
from collections.abc import Callable
from contextlib import contextmanager
from sqlite3 import OperationalError as SQLiteOperationalError

from archivebox.config import CONSTANTS
from archivebox.misc.util import enforce_types


def run_db_analyze_batch(
    remaining: list[str] | None,
    *,
    max_seconds_per_table: float = 120.0,
) -> list[str]:
    """Advance one step of a batched SQLite ``ANALYZE`` sweep.

    Without periodic ANALYZE the optimizer's table stats go stale as
    snapshot/archiveresult tables grow, causing it to start large joins from
    ``auth_user`` instead of using the indexed url column and blowing snapshot
    detail page render time from ~50ms to ~500ms+.

    The whole sweep is spread across many calls instead of running as one
    blocking ``ANALYZE``: pass ``None`` to start a fresh sweep (this call
    enumerates user tables and runs ``ANALYZE`` on the first one); pass the
    returned list to advance one more table on each subsequent call. An
    empty return value means the sweep is complete (or has been aborted) and
    the next caller should pass ``None`` again. Caller is responsible for
    throttling new sweeps (orchestrator starts at most one per 24hr while
    idle) and enforcing a hard upper bound on total sweep wall time.

    Safety guarantees:

    - **Never raises**: every database call is wrapped; on any failure the
      function returns ``[]`` (abandoning the rest of the sweep) so the
      orchestrator never crashes on maintenance errors.
    - **Bounded per-call wall time**: a SQLite progress handler aborts the
      current ``ANALYZE`` statement once ``max_seconds_per_table`` is
      exceeded, so a single pathological table cannot wedge the call.
    - **Never leaves the db locked**: each ``ANALYZE`` runs as a single
      statement transaction that auto-commits (or rolls back on
      abort/error). The cursor and progress handler are always cleaned up
      in ``finally`` blocks even if Python raises mid-call.
    - Silent no-op on non-SQLite backends.

    WAL journal mode (set in Django settings) keeps readers fully unblocked
    throughout; the writer lock is only held for the brief ``sqlite_stat*``
    flush after each table completes.
    """
    from django.db import connection

    if connection.vendor != "sqlite":
        return []

    if remaining is None:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                )
                remaining = [row[0] for row in cursor.fetchall()]
        except Exception:
            return []

    if not remaining:
        return []

    next_table, *rest = remaining
    raw_conn = connection.connection
    progress_handler_set = False
    if raw_conn is not None and max_seconds_per_table > 0:
        deadline = time.monotonic() + max_seconds_per_table
        try:
            raw_conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
            progress_handler_set = True
        except Exception:
            progress_handler_set = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(f'ANALYZE "{next_table}"')
    except Exception:
        # Aborted by progress handler, locked db, or any other failure — skip
        # this table and continue the sweep. ANALYZE is idempotent so we can
        # retry on the next 24hr sweep.
        pass
    finally:
        if progress_handler_set and raw_conn is not None:
            try:
                raw_conn.set_progress_handler(None, 0)
            except Exception:
                pass
    return rest


def compact_command(cmdline: list[str] | None, fallback: str = "") -> str:
    parts = [str(part) for part in (cmdline or []) if str(part)]
    if not parts:
        return fallback
    for marker in ("archivebox", "daphne", "gunicorn", "uvicorn", "supervisord", "sonic", "node"):
        for idx, part in enumerate(parts):
            if Path(part).name == marker or part == marker:
                return " ".join([Path(parts[idx]).name, *parts[idx + 1 :]])[:220]
    return " ".join([Path(parts[0]).name, *parts[1:]])[:220]


def sqlite_lock_holders(db_path: Path = CONSTANTS.DATABASE_FILE) -> list[str]:
    import psutil

    db_path = db_path.resolve()
    db_sidecars = {
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
        db_path.with_name(f"{db_path.name}-journal"),
    }
    holders: list[str] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "status"]):
        try:
            open_files = proc.open_files()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        for open_file in open_files:
            try:
                open_path = Path(open_file.path).resolve()
            except (OSError, RuntimeError):
                continue
            if open_path in db_sidecars:
                info = proc.info
                cmdline = compact_command(info.get("cmdline"), fallback=info.get("name") or "")
                holders.append(f"pid={info['pid']} ppid={info['ppid']} {info['status']} {cmdline}")
                break
    return holders


def log_sqlite_lock_holders(console: Any, *, db_path: Path = CONSTANTS.DATABASE_FILE, limit: int = 8) -> None:
    holders = sqlite_lock_holders(db_path)
    if holders:
        console.print("[yellow]    DB holders:[/yellow]")
        for holder in holders[:limit]:
            console.print(f"[yellow]    - {holder}[/yellow]")
        if len(holders) > limit:
            console.print(f"[yellow]    ... {len(holders) - limit} more[/yellow]")
    else:
        console.print("[yellow]    No local process with index.sqlite3 open was visible to this user.[/yellow]")


def sqlite_lock_error(error: BaseException) -> bool:
    from django.db import OperationalError as DjangoOperationalError

    return isinstance(error, (SQLiteOperationalError, DjangoOperationalError)) and "database is locked" in str(error).lower()


def retry_sqlite_locks(action: Callable[[], Any], *, label: str, stderr: TextIO | None = None) -> Any:
    from django.db import OperationalError, connections
    from rich.console import Console

    console = Console(file=stderr or None, stderr=stderr is None)
    while True:
        try:
            return action()
        except OperationalError as err:
            if "database is locked" not in str(err).lower():
                raise
        except SQLiteOperationalError as err:
            if not sqlite_lock_error(err):
                raise

        connections.close_all()
        console.print(f"[yellow][*] SQLite database is locked while {label}; retrying in 5s...[/yellow]")
        log_sqlite_lock_holders(console)
        with console.status("[yellow]Waiting for SQLite database lock to clear...[/yellow]", spinner="dots"):
            time.sleep(5.0)


@contextmanager
def migration_lock(stdout: TextIO | None = None):
    from archivebox.config.paths import get_or_create_working_tmp_dir
    from rich.console import Console

    lock_path = get_or_create_working_tmp_dir(autofix=True, quiet=True) / "migrate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Migrations on large SQLite collections can run for hours. Use a
            # kernel lock with no timeout so parallel ArchiveBox commands queue
            # behind the active migrate process instead of racing it.
            console = Console(file=stdout or None, stderr=stdout is None)
            with console.status("[yellow]Waiting for migration lock...[/yellow]", spinner="dots"):
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(1.0)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# Migration names that previously existed in ArchiveBox's source tree but have
# since been deleted (squashed away, renamed, moved between apps, etc.). DBs
# upgraded incrementally through 0.8.x → 0.9.x dev rcs accumulate rows for
# these in ``django_migrations``; the newer-DB guard added in 65dc2521 would
# otherwise refuse to start with "applied migrations missing from this build"
# and brick beta-tester collections. We deliberately do NOT use Django's
# ``replaces=`` for these because Django's all-or-none replaces semantics
# splits the migration graph when only a *subset* of the replaces list is
# applied (which is exactly what happens for users at different intermediate
# dev branch states). This set is the authoritative compat list — extend it
# when squashing more migrations away. Generated from
# ``git log --diff-filter=D --name-only`` over each app's migrations/ tree.
HISTORICAL_GHOST_MIGRATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # core: 0023→0075 sequence plus every transient dev rename
        ("core", "0002_auto_20190417_0739"),
        ("core", "0006_auto_20200915_2006"),
        ("core", "0023_alter_archiveresult_options_archiveresult_abid_and_more"),
        ("core", "0023_new_schema"),
        ("core", "0024_auto_20240513_1143"),
        ("core", "0024_b_clear_config_fields"),
        ("core", "0024_c_disable_fk_checks"),
        ("core", "0024_d_fix_crawls_config"),
        ("core", "0024_f_add_snapshot_config"),
        ("core", "0024_snapshot_crawl"),
        ("core", "0025_allow_duplicate_urls_per_crawl"),
        ("core", "0025_alter_archiveresult_uuid"),
        ("core", "0025_cleanup_schema"),
        ("core", "0026_archiveresult_created_archiveresult_created_by_and_more"),
        ("core", "0026_final_field_adjustments"),
        ("core", "0026_remove_archiveresult_output_dir_and_more"),
        ("core", "0027_alter_archiveresult_created_by_and_more"),
        ("core", "0027_alter_archiveresult_hook_name_alter_archiveresult_id_and_more"),
        ("core", "0027_update_snapshot_ids"),
        ("core", "0028_alter_archiveresult_uuid"),
        ("core", "0028_snapshot_fs_version"),
        ("core", "0029_alter_archiveresult_id"),
        ("core", "0029_archiveresult_hook_fields"),
        ("core", "0030_alter_archiveresult_uuid"),
        ("core", "0030_migrate_output_field"),
        ("core", "0031_alter_archiveresult_id_alter_archiveresult_uuid_and_more"),
        ("core", "0031_snapshot_parent_snapshot"),
        ("core", "0032_alter_archiveresult_binary_and_more"),
        ("core", "0032_alter_archiveresult_id"),
        ("core", "0033_rename_extractor_add_hook_name"),
        ("core", "0033_rename_id_archiveresult_old_id"),
        ("core", "0034_alter_archiveresult_old_id_alter_archiveresult_uuid"),
        ("core", "0034_snapshot_current_step"),
        ("core", "0035_remove_archiveresult_uuid_archiveresult_id"),
        ("core", "0035_snapshot_crawl_non_nullable_remove_created_by"),
        ("core", "0036_alter_archiveresult_id_alter_archiveresult_old_id"),
        ("core", "0036_remove_archiveresult_created_by"),
        ("core", "0037_remove_archiveresult_output_dir_and_more"),
        ("core", "0037_rename_id_snapshot_old_id"),
        ("core", "0038_fix_missing_columns"),
        ("core", "0038_rename_uuid_snapshot_id"),
        ("core", "0039_fix_num_uses_values"),
        ("core", "0039_rename_snapshot_archiveresult_snapshot_old"),
        ("core", "0040_archiveresult_snapshot"),
        ("core", "0041_alter_archiveresult_snapshot_and_more"),
        ("core", "0042_remove_archiveresult_snapshot_old"),
        ("core", "0043_alter_archiveresult_snapshot_alter_snapshot_id_and_more"),
        ("core", "0044_alter_archiveresult_snapshot_alter_tag_uuid_and_more"),
        ("core", "0045_alter_snapshot_old_id"),
        ("core", "0046_alter_archiveresult_snapshot_alter_snapshot_id_and_more"),
        ("core", "0047_alter_snapshottag_unique_together_and_more"),
        ("core", "0048_alter_archiveresult_snapshot_and_more"),
        ("core", "0049_rename_snapshot_snapshottag_snapshot_old_and_more"),
        ("core", "0050_alter_snapshottag_snapshot_old"),
        ("core", "0051_snapshottag_snapshot_alter_snapshottag_snapshot_old"),
        ("core", "0052_alter_snapshottag_unique_together_and_more"),
        ("core", "0053_remove_snapshottag_snapshot_old"),
        ("core", "0054_alter_snapshot_timestamp"),
        ("core", "0055_alter_tag_slug"),
        ("core", "0056_remove_tag_uuid"),
        ("core", "0057_rename_id_tag_old_id"),
        ("core", "0058_alter_tag_old_id"),
        ("core", "0059_tag_id"),
        ("core", "0060_alter_tag_id"),
        ("core", "0061_rename_tag_snapshottag_old_tag_and_more"),
        ("core", "0062_alter_snapshottag_old_tag"),
        ("core", "0063_snapshottag_tag_alter_snapshottag_old_tag"),
        ("core", "0064_alter_snapshottag_unique_together_and_more"),
        ("core", "0065_remove_snapshottag_old_tag"),
        ("core", "0066_alter_snapshottag_tag_alter_tag_id_alter_tag_old_id"),
        ("core", "0067_alter_snapshottag_tag"),
        ("core", "0068_alter_archiveresult_options"),
        ("core", "0069_alter_archiveresult_created_alter_snapshot_added_and_more"),
        ("core", "0070_alter_archiveresult_created_by_alter_snapshot_added_and_more"),
        ("core", "0071_remove_archiveresult_old_id_remove_snapshot_old_id_and_more"),
        ("core", "0072_rename_added_snapshot_bookmarked_at_and_more"),
        ("core", "0073_rename_created_archiveresult_created_at_and_more"),
        ("core", "0074_alter_snapshot_downloaded_at"),
        ("core", "0075_crawl"),
        # api: pre-squash 0001_squashed plus 0002→0009 chain
        ("api", "0001_squashed"),
        ("api", "0002_alter_apitoken_options"),
        ("api", "0002_alter_outboundwebhook_options_and_more"),
        ("api", "0003_alter_apitoken_created_by_and_more"),
        ("api", "0003_rename_user_apitoken_created_by_apitoken_abid_and_more"),
        ("api", "0004_alter_apitoken_id_alter_apitoken_uuid"),
        ("api", "0004_rename_user_apitoken_created_by_apitoken_modified_and_more"),
        ("api", "0005_remove_apitoken_uuid_remove_outboundwebhook_uuid_and_more"),
        ("api", "0006_remove_outboundwebhook_uuid_apitoken_id_and_more"),
        ("api", "0007_alter_apitoken_created_by"),
        ("api", "0008_alter_apitoken_created_alter_apitoken_created_by_and_more"),
        ("api", "0009_rename_created_apitoken_created_at_and_more"),
        # machine: pre-squash 0001_squashed plus transient 0002→0005 renames
        ("machine", "0001_squashed"),
        ("machine", "0002_alter_dependency_bin_name_and_more"),
        ("machine", "0002_alter_machine_stats_installedbinary"),
        ("machine", "0002_process_parent_and_type"),
        ("machine", "0002_rename_custom_cmds_to_overrides"),
        ("machine", "0003_alter_dependency_id_alter_installedbinary_dependency_and_more"),
        ("machine", "0003_alter_installedbinary_options_and_more"),
        ("machine", "0004_alter_installedbinary_abspath_and_more"),
        ("machine", "0004_drop_dependency_table"),
        ("machine", "0004_rename_installedbinary_to_binary"),
        ("machine", "0005_binary_binproviders_binary_output_dir_and_more"),
        # crawls: transient dev renames around the seed-model removal
        ("crawls", "0002_delete_outlink"),
        ("crawls", "0002_drop_seed_model"),
        ("crawls", "0002_upgrade_to_0_9_0"),
        ("crawls", "0003_alter_crawl_output_dir"),
        ("crawls", "0004_alter_crawl_output_dir"),
        ("crawls", "0005_drop_seed_id_column"),
        ("crawls", "0006_alter_crawl_config_alter_crawl_output_dir_and_more"),
    },
)


@enforce_types
def migration_state(out_dir: Path = CONSTANTS.DATA_DIR) -> tuple[list[str], list[str], dict[str, str]]:
    """Cheaply compare migration files to django_migrations without invoking migrate."""
    from django.apps import apps
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    def applied_rows() -> set[tuple[str, str]]:
        with connection.cursor() as cursor:
            try:
                cursor.execute("SELECT app, name FROM django_migrations")
            except Exception as err:
                if "no such table" in str(err).lower():
                    return set()
                raise
            return {(str(app), str(name)) for app, name in cursor.fetchall()}

    applied = retry_sqlite_locks(applied_rows, label="checking applied migrations")
    disk_migrations: set[tuple[str, str]] = set()
    # Names that any current migration declares it ``replaces=``. Whether or
    # not we use ``replaces=`` today, supporting it costs nothing and keeps
    # the checker honest if a future migration adopts it.
    squashed_replaced: set[tuple[str, str]] = set()
    app_labels = {app_config.label for app_config in apps.get_app_configs()}
    loader = MigrationLoader(connection=None, ignore_no_migrations=True, load=False)
    loader.load_disk()
    for (app_label, migration_name), migration in loader.disk_migrations.items():
        disk_migrations.add((app_label, migration_name))
        for replaced_app, replaced_name in migration.replaces or ():
            squashed_replaced.add((replaced_app, replaced_name))

    applied = {(app, name) for app, name in applied if app in app_labels}
    pending = [f"{app}.{name}" for app, name in sorted(disk_migrations - applied)]
    missing_pairs = sorted(applied - disk_migrations - squashed_replaced - HISTORICAL_GHOST_MIGRATIONS)
    missing_from_code = [f"{app}.{name}" for app, name in missing_pairs]
    rollback_targets = {
        app: (
            max(name for disk_app, name in disk_migrations if disk_app == app)
            if any(disk_app == app for disk_app, _name in disk_migrations)
            else "zero"
        )
        for app, _name in missing_pairs
    }
    return pending, missing_from_code, rollback_targets


@enforce_types
def pending_migrations(out_dir: Path = CONSTANTS.DATA_DIR) -> list[str]:
    """Return migration files on disk that have not been applied yet."""
    pending, _missing_from_code, _rollback_targets = migration_state(out_dir=out_dir)
    return pending


@enforce_types
def apply_migrations(
    out_dir: Path = CONSTANTS.DATA_DIR,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    verbosity: int = 1,
) -> list[str]:
    """Apply pending Django migrations"""
    from django.core.management import call_command

    with migration_lock(stdout=stderr or stdout):
        if not pending_migrations():
            return []

        if stdout is not None:
            retry_sqlite_locks(
                lambda: call_command("migrate", interactive=False, database="default", stdout=stdout, stderr=stderr, verbosity=verbosity),
                label="applying migrations",
                stderr=stderr,
            )
            return []

        def migrate() -> StringIO:
            out1 = StringIO()
            call_command("migrate", interactive=False, database="default", stdout=out1, verbosity=verbosity)
            out1.seek(0)
            return out1

        out1 = retry_sqlite_locks(migrate, label="applying migrations")

        return [line.strip() for line in out1.readlines() if line.strip()]
