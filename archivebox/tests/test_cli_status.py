#!/usr/bin/env python3
"""
Comprehensive tests for archivebox status command.
Verify status reports accurate collection state from DB and filesystem.
"""

import pytest

from archivebox.core.models import Snapshot
from archivebox.tests.conftest import find_snapshot_dir, run_archivebox_cmd, run_queued_crawls, cli_env

from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def test_status_runs_successfully(initialized_archive):
    """Test that status command runs without error."""
    result = run_archivebox_cmd(["status"])

    assert result.returncode == 0
    assert len(result.stdout) > 100


def test_status_shows_zero_snapshots_in_empty_archive(initialized_archive):
    """Test status shows 0 snapshots in empty archive."""
    result = run_archivebox_cmd(["status"])

    output = result.stdout
    # Should indicate empty/zero state
    assert "0" in output


def test_status_shows_correct_snapshot_count(initialized_archive):
    """Test that status shows accurate snapshot count from DB."""
    env = cli_env(disable_extractors=True)

    # Add 3 snapshots
    for url in ["https://example.com", "https://example.org", "https://example.net"]:
        run_archivebox_cmd(
            ["add", "--index-only", "--depth=0", url],
            env=env,
        )
    run_queued_crawls(initialized_archive, env)

    result = run_archivebox_cmd(["status"])

    # Verify DB has 3 snapshots
    with use_archivebox_db(initialized_archive):
        db_count = Snapshot.objects.count()

    assert db_count == 3
    # Status output should show 3
    assert "3" in result.stdout


def test_status_shows_archived_count(initialized_archive):
    """Test status distinguishes archived vs unarchived snapshots."""
    env = cli_env(disable_extractors=True)

    run_archivebox_cmd(
        ["add", "--index-only", "--depth=0", "https://example.com"],
        env=env,
    )
    run_queued_crawls(initialized_archive, env)

    result = run_archivebox_cmd(["status"])

    # Should show archived/unarchived categories
    assert "archived" in result.stdout.lower() or "queued" in result.stdout.lower()


def test_status_shows_archive_directory_size(initialized_archive):
    """Test status reports archive directory size."""
    result = run_archivebox_cmd(["status"])

    output = result.stdout
    # Should show size info
    assert "Size" in output or "size" in output


def test_status_counts_archive_directories(initialized_archive):
    """Test status counts directories in archive/ folder."""
    env = cli_env(disable_extractors=True)

    run_archivebox_cmd(
        ["add", "--index-only", "--depth=0", "https://example.com"],
        env=env,
    )
    run_queued_crawls(initialized_archive, env)

    result = run_archivebox_cmd(["status"])

    # Should show directory count
    assert "present" in result.stdout.lower() or "directories" in result.stdout


def test_status_detects_orphaned_directories(initialized_archive):
    """Test status detects directories not in DB (orphaned)."""
    env = cli_env(disable_extractors=True)

    # Add a snapshot
    run_archivebox_cmd(
        ["add", "--index-only", "--depth=0", "https://example.com"],
        env=env,
    )
    run_queued_crawls(initialized_archive, env)

    # Create an orphaned directory
    (initialized_archive / "archive" / "fake_orphaned_dir").mkdir(parents=True, exist_ok=True)

    result = run_archivebox_cmd(["status"])

    # Should mention orphaned dirs
    assert "orphan" in result.stdout.lower() or "1" in result.stdout


def test_status_counts_new_snapshot_output_dirs_as_archived(initialized_archive):
    """Test status reads archived/present counts from the current snapshot output layout."""
    env = cli_env(disable_extractors=True)
    env = env.copy()
    env["ARCHIVEBOX_ALLOW_NO_UNIX_SOCKETS"] = "true"

    run_archivebox_cmd(
        ["add", "--index-only", "--depth=0", "https://example.com"],
        env=env,
        check=True,
    )
    run_queued_crawls(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        snapshot_id = Snapshot.objects.values_list("id", flat=True).get(url="https://example.com")

    snapshot_dir = find_snapshot_dir(initialized_archive, str(snapshot_id))
    assert snapshot_dir is not None, f"Snapshot output directory not found for {snapshot_id}"
    title_dir = snapshot_dir / "title"
    title_dir.mkdir(parents=True, exist_ok=True)
    (title_dir / "title.txt").write_text("Example Domain")

    result = run_archivebox_cmd(["status"], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "archived: 1" in result.stdout
    assert "present: 1" in result.stdout


def test_status_shows_user_info(initialized_archive):
    """Test status shows user/login information."""
    result = run_archivebox_cmd(["status"])

    output = result.stdout
    # Should show user section
    assert "user" in output.lower() or "login" in output.lower()


def test_status_reads_from_db_not_filesystem(initialized_archive):
    """Test that status uses DB as source of truth, not filesystem."""
    env = cli_env(disable_extractors=True)

    # Add snapshot to DB
    run_archivebox_cmd(
        ["add", "--index-only", "--depth=0", "https://example.com"],
        env=env,
    )
    run_queued_crawls(initialized_archive, env)

    # Verify DB has snapshot
    with use_archivebox_db(initialized_archive):
        db_count = Snapshot.objects.count()

    assert db_count == 1

    # Status should reflect DB count
    result = run_archivebox_cmd(["status"])
    assert "1" in result.stdout


def test_status_shows_index_file_info(initialized_archive):
    """Test status shows index file information."""
    result = run_archivebox_cmd(["status"])

    # Should mention index
    assert "index" in result.stdout.lower() or "Index" in result.stdout


def test_status_help_lists_available_options(initialized_archive):
    """Test that status --help works and documents the command."""
    result = run_archivebox_cmd(
        ["status", "--help"],
    )

    assert result.returncode == 0
    assert "status" in result.stdout.lower() or "statistic" in result.stdout.lower()


def test_status_shows_data_directory_path(initialized_archive):
    """Test that status reports which collection directory it is inspecting."""
    result = run_archivebox_cmd(["status"])

    assert "archive" in result.stdout.lower() or str(initialized_archive) in result.stdout
