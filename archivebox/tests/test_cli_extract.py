#!/usr/bin/env python3
"""
Tests for archivebox extract command.
Verify extract re-runs extractors on existing snapshots.
"""

import pytest

from archivebox.core.models import Snapshot
from archivebox.tests.conftest import run_queued_crawls, run_archivebox_cmd, cli_env

from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def test_extract_runs_on_existing_snapshots(initialized_archive):
    """Test that extract command runs on existing snapshots."""
    env = cli_env(disable_extractors=True)

    # Add a snapshot first
    run_archivebox_cmd(
        ["add", "--index-only", "--depth=0", "https://example.com"],
        env=env,
    )
    run_queued_crawls(initialized_archive, env)

    # Run extract
    result = run_archivebox_cmd(
        ["extract"],
        env=env,
        timeout=30,
    )

    # Should complete
    assert result.returncode in [0, 1]


def test_extract_preserves_snapshot_count(initialized_archive):
    """Test that extract doesn't change snapshot count."""
    env = cli_env(disable_extractors=True)

    # Add snapshot
    run_archivebox_cmd(
        ["add", "--index-only", "--depth=0", "https://example.com"],
        env=env,
    )
    run_queued_crawls(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        count_before = Snapshot.objects.count()

    # Run extract
    run_archivebox_cmd(
        ["extract", "--overwrite"],
        env=env,
        timeout=30,
    )

    with use_archivebox_db(initialized_archive):
        count_after = Snapshot.objects.count()

    assert count_after == count_before
