#!/usr/bin/env python3
"""Tests for archivebox extract command."""

import pytest

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.tests.conftest import cli_env, parse_jsonl_output, run_archivebox_cmd

from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def _create_snapshot(data_dir, env, url="https://example.com"):
    result = run_archivebox_cmd(
        ["snapshot", "create", url],
        cwd=data_dir,
        env=env,
        check=True,
    )
    snapshot = next(record for record in parse_jsonl_output(result.stdout) if record.get("type") == "Snapshot")
    return snapshot


def test_extract_runs_on_existing_snapshots(initialized_archive):
    """Extract queues a requested plugin for an existing snapshot."""
    env = cli_env(disable_extractors=True)

    snapshot = _create_snapshot(initialized_archive, env)
    snapshot_id = snapshot["id"]

    result = run_archivebox_cmd(
        ["extract", "--plugin=title", "--no-wait", snapshot_id],
        cwd=initialized_archive,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr or result.stdout

    with use_archivebox_db(initialized_archive):
        archiveresult = ArchiveResult.objects.get(snapshot_id=snapshot_id, plugin="title")
        extracted_snapshot = Snapshot.objects.get(id=snapshot_id)

    assert archiveresult.status == ArchiveResult.StatusChoices.QUEUED
    assert extracted_snapshot.retry_at is not None


def test_extract_preserves_snapshot_count(initialized_archive):
    """Extract queues work without creating duplicate snapshots."""
    env = cli_env(disable_extractors=True)

    snapshot = _create_snapshot(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        count_before = Snapshot.objects.count()

    result = run_archivebox_cmd(
        ["extract", "--plugin=title", "--no-wait", snapshot["id"]],
        cwd=initialized_archive,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    with use_archivebox_db(initialized_archive):
        count_after = Snapshot.objects.count()

    assert count_after == count_before
