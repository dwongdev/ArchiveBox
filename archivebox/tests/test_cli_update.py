#!/usr/bin/env python3
"""
Comprehensive tests for archivebox update command.
Verify update drains old dirs, reconciles DB, and queues snapshots.
"""

import os
import subprocess

import pytest

from archivebox.core.models import Snapshot
from archivebox.tests.conftest import run_queued_crawls
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def test_update_runs_successfully_on_empty_archive(tmp_path, process):
    """Test that update runs without error on empty archive."""
    os.chdir(tmp_path)
    result = subprocess.run(
        ["archivebox", "update"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Should complete successfully even with no snapshots
    assert result.returncode == 0


def test_update_reconciles_existing_snapshots(tmp_path, process, disable_extractors_dict):
    """Test that update command reconciles existing snapshots."""
    os.chdir(tmp_path)

    # Add a snapshot (index-only for faster test)
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    # Run update - should reconcile and queue
    result = subprocess.run(
        ["archivebox", "update"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    assert result.returncode == 0


def test_update_specific_snapshot_by_filter(tmp_path, process, disable_extractors_dict):
    """Test updating specific snapshot using filter."""
    os.chdir(tmp_path)

    # Add multiple snapshots
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=90,
    )
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.org"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=90,
    )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    # Update with filter pattern (uses filter_patterns argument)
    result = subprocess.run(
        ["archivebox", "update", "--filter-type=substring", "example.com"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    # Should complete successfully
    assert result.returncode == 0


def test_update_preserves_snapshot_count(tmp_path, process, disable_extractors_dict):
    """Test that update doesn't change snapshot count."""
    os.chdir(tmp_path)

    # Add snapshots
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=90,
    )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    # Count before update
    with use_archivebox_db(tmp_path):
        count_before = Snapshot.objects.count()

    assert count_before == 1

    # Run update (should reconcile + queue, not create new snapshots)
    subprocess.run(
        ["archivebox", "update"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    # Count after update
    with use_archivebox_db(tmp_path):
        count_after = Snapshot.objects.count()

    # Snapshot count should remain the same
    assert count_after == count_before


def test_update_seals_migrated_snapshots(tmp_path, process, disable_extractors_dict):
    """Test that full update reconciles migrated snapshots without re-queuing them."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=90,
    )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    # Run update
    result = subprocess.run(
        ["archivebox", "update"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    assert result.returncode == 0

    # Check that snapshot remains archived instead of being queued for a full re-crawl.
    with use_archivebox_db(tmp_path):
        status = Snapshot.objects.values_list("status", flat=True).get()

    assert status == "sealed"
