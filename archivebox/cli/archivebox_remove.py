#!/usr/bin/env python3

__package__ = "archivebox.cli"
__command__ = "archivebox remove"

import time
from pathlib import Path
from collections.abc import Iterable

import rich_click as click

from django.db import OperationalError
from django.db.models import QuerySet

from archivebox.config import CONSTANTS
from archivebox.config.django import setup_django
from archivebox.misc.util import enforce_types, docstring
from archivebox.misc.checks import check_data_folder
from archivebox.misc.logging_util import (
    log_list_started,
    log_list_finished,
    log_removal_started,
    log_removal_finished,
    TimedProgress,
)


@enforce_types
def remove(
    filter_patterns: Iterable[str] = (),
    filter_type: str = "exact",
    snapshots: QuerySet | None = None,
    after: float | None = None,
    before: float | None = None,
    yes: bool = False,
    out_dir: Path = CONSTANTS.DATA_DIR,
) -> QuerySet:
    """Remove the specified URLs from the archive"""

    setup_django()
    check_data_folder()

    from archivebox.cli.archivebox_search import get_snapshots

    pattern_list = list(filter_patterns)

    log_list_started(pattern_list or None, filter_type)
    timer = TimedProgress(360, prefix="      ")
    try:
        snapshots = get_snapshots(
            snapshots=snapshots,
            filter_patterns=pattern_list or None,
            filter_type=filter_type,
            after=after,
            before=before,
        )
    finally:
        timer.end()

    if not snapshots.exists():
        log_removal_finished(0, 0)
        raise SystemExit(1)

    log_list_finished(snapshots)
    log_removal_started(snapshots, yes=yes)

    from archivebox.core.models import Snapshot
    from archivebox.search.query import flush_search_index

    # Freeze the target set up-front so a concurrent daemon writing new
    # snapshots can't extend the deletion under us, and so the cursor isn't
    # held open across the per-row deletes below.
    snapshot_pks = list(snapshots.values_list("pk", flat=True))
    to_remove = len(snapshot_pks)

    # Search-index flush touches a separate backend (FTS / sonic), not the
    # main index.sqlite3 writer lock, so it's safe to do once up front.
    flush_search_index(snapshots=Snapshot.objects.filter(pk__in=snapshot_pks))

    # Delete one snapshot at a time. Each ``.delete()`` is its own short
    # Django-atomic block, so the writer lock is released between rows and
    # an in-flight daemon transaction can interleave instead of deadlocking.
    # Filesystem cleanup for each row is scheduled via ``transaction.on_commit``
    # in ``base_models/models.py`` and runs AFTER its row's tx commits — so
    # rmtree doesn't hold the lock either.
    #
    # The SQLite retry wrapper in core/sqlite_backend/base.py re-raises lock
    # errors when called inside an atomic block (because it can't safely
    # release+reacquire a transaction), so we wrap each row's delete in our
    # own retry loop at this outer (non-atomic) level. Each attempt is a
    # fresh atomic; an exception cleanly rolls it back before we sleep.
    retry_interval = 1.0
    retry_timeout = 60.0
    for pk in snapshot_pks:
        deadline = time.monotonic() + retry_timeout
        while True:
            try:
                Snapshot.objects.filter(pk=pk).delete()
                break
            except OperationalError as err:
                if "database is locked" not in str(err) or time.monotonic() >= deadline:
                    raise
                time.sleep(retry_interval)

    all_snapshots = Snapshot.objects.all()
    log_removal_finished(all_snapshots.count(), to_remove)

    return all_snapshots


@click.command()
@click.option("--yes", is_flag=True, help="Remove links instantly without prompting to confirm")
@click.option("--before", type=float, help="Remove only URLs bookmarked before timestamp")
@click.option("--after", type=float, help="Remove only URLs bookmarked after timestamp")
@click.option(
    "--filter-type",
    "-f",
    type=click.Choice(("exact", "substring", "domain", "regex", "tag")),
    default="exact",
    help="Type of pattern matching to use when filtering URLs",
)
@click.argument("filter_patterns", nargs=-1)
@docstring(remove.__doc__)
def main(**kwargs):
    """Remove the specified URLs from the archive"""
    remove(**kwargs)


if __name__ == "__main__":
    main()
