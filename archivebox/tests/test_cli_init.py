#!/usr/bin/env python3
"""
Comprehensive tests for archivebox init command.
Verify init creates correct database schema, filesystem structure, and config.
"""

import os
import subprocess

import pytest
from django.utils import timezone
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder

from archivebox.config.common import get_config
from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl
from archivebox.machine.models import Machine
from archivebox.tests.conftest import run_queued_crawls
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


DIR_PERMISSIONS = get_config().OUTPUT_PERMISSIONS.replace("6", "7").replace("4", "5")


def test_init_creates_database_file(tmp_path):
    """Test that init creates index.sqlite3 database file."""
    os.chdir(tmp_path)
    result = subprocess.run(["archivebox", "init"], capture_output=True)

    assert result.returncode == 0
    db_path = tmp_path / "index.sqlite3"
    assert db_path.exists()
    assert db_path.is_file()


def test_init_creates_archive_directory(tmp_path):
    """Test that init creates archive directory."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    archive_dir = tmp_path / "archive"
    assert archive_dir.exists()
    assert archive_dir.is_dir()


def test_init_uses_cwd_archive_and_users_dirs(tmp_path):
    """Test that init creates archive/users storage roots under cwd."""
    os.chdir(tmp_path)

    result = subprocess.run(["archivebox", "init"], capture_output=True)

    assert result.returncode == 0
    assert (tmp_path / "archive").is_dir()
    assert (tmp_path / "archive" / "users").is_dir()


def test_init_creates_sources_directory(tmp_path):
    """Test that init creates sources directory."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    sources_dir = tmp_path / "sources"
    assert sources_dir.exists()
    assert sources_dir.is_dir()


def test_init_creates_logs_directory(tmp_path):
    """Test that init creates logs directory."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    logs_dir = tmp_path / "logs"
    assert logs_dir.exists()
    assert logs_dir.is_dir()


def test_init_creates_config_file(tmp_path):
    """Test that init creates ArchiveBox.conf config file."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    config_file = tmp_path / "ArchiveBox.conf"
    assert config_file.exists()
    assert config_file.is_file()


def test_init_runs_migrations(tmp_path):
    """Test that init runs Django migrations and creates core tables."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    with use_archivebox_db(tmp_path):
        migration_count = MigrationRecorder.Migration.objects.count()

    assert migration_count > 0


def test_init_creates_core_snapshot_table(tmp_path):
    """Test that init creates core_snapshot table."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    assert Snapshot._meta.db_table == "core_snapshot"
    with use_archivebox_db(tmp_path):
        assert Snapshot.objects.count() == 0


def test_init_creates_crawls_crawl_table(tmp_path):
    """Test that init creates crawls_crawl table."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    assert Crawl._meta.db_table == "crawls_crawl"
    with use_archivebox_db(tmp_path):
        assert Crawl.objects.count() == 0


def test_init_creates_core_archiveresult_table(tmp_path):
    """Test that init creates core_archiveresult table."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    assert ArchiveResult._meta.db_table == "core_archiveresult"
    with use_archivebox_db(tmp_path):
        assert ArchiveResult.objects.count() == 0


def test_init_sets_correct_file_permissions(tmp_path):
    """Test that init sets correct permissions on created files."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    # Check database permissions
    db_path = tmp_path / "index.sqlite3"
    assert oct(db_path.stat().st_mode)[-3:] in (get_config().OUTPUT_PERMISSIONS, DIR_PERMISSIONS)

    # Check directory permissions
    archive_dir = tmp_path / "archive"
    assert oct(archive_dir.stat().st_mode)[-3:] in (get_config().OUTPUT_PERMISSIONS, DIR_PERMISSIONS)


def test_init_is_idempotent(tmp_path):
    """Test that running init multiple times is safe (idempotent)."""
    os.chdir(tmp_path)

    # First init
    result1 = subprocess.run(["archivebox", "init"], capture_output=True, text=True)
    assert result1.returncode == 0
    assert "Initializing a new ArchiveBox" in result1.stdout

    # Second init should update, not fail
    result2 = subprocess.run(["archivebox", "init"], capture_output=True, text=True)
    assert result2.returncode == 0
    assert "updating existing ArchiveBox" in result2.stdout or "up-to-date" in result2.stdout.lower()

    # Database should still be valid
    with use_archivebox_db(tmp_path):
        count = MigrationRecorder.Migration.objects.count()
    assert count > 0


def test_init_refuses_database_migrated_by_newer_code(tmp_path):
    """A downgraded ArchiveBox build must fail before serving a newer DB schema."""
    os.chdir(tmp_path)
    result = subprocess.run(["archivebox", "init"], capture_output=True, text=True)
    assert result.returncode == 0

    with use_archivebox_db(tmp_path):
        MigrationRecorder.Migration.objects.create(app="crawls", name="9999_future_test", applied=timezone.now())
        connections["default"].commit()

    result = subprocess.run(["archivebox", "init"], capture_output=True, text=True)
    assert result.returncode == 3
    assert "migrated by a newer version of ArchiveBox" in result.stderr
    assert "crawls.9999_future_test" in result.stderr
    assert "archivebox manage migrate crawls " in result.stderr


def test_init_recovers_from_pre_squash_dev_history(tmp_path):
    """Pre-squash dev DBs (rows for migrations now absorbed by ``replaces=``)
    must NOT trip the newer-DB guard — every historical squash would otherwise
    brick beta-tester collections that pre-date the squash commit."""
    os.chdir(tmp_path)
    result = subprocess.run(["archivebox", "init"], capture_output=True, text=True)
    assert result.returncode == 0

    # Sampling — one name per affected app, all listed in the ``replaces=``
    # declarations of the current squash anchors. If any of these get treated
    # as missing-from-code, dev DBs that ran the historical chain pre-squash
    # would refuse to start.
    historical_pre_squash_rows = [
        ("api", "0002_alter_apitoken_options"),
        ("api", "0009_rename_created_apitoken_created_at_and_more"),
        ("core", "0023_alter_archiveresult_options_archiveresult_abid_and_more"),
        ("core", "0074_alter_snapshot_downloaded_at"),
        ("core", "0075_crawl"),
        ("machine", "0002_alter_machine_stats_installedbinary"),
        ("machine", "0004_alter_installedbinary_abspath_and_more"),
    ]
    with use_archivebox_db(tmp_path):
        for app, name in historical_pre_squash_rows:
            MigrationRecorder.Migration.objects.create(app=app, name=name, applied=timezone.now())
        connections["default"].commit()

    result = subprocess.run(["archivebox", "init"], capture_output=True, text=True)
    assert result.returncode == 0, f"init refused to recover pre-squash dev DB.\nstdout={result.stdout}\nstderr={result.stderr}"
    assert "migrated by a newer version of ArchiveBox" not in result.stderr


def test_init_with_existing_data_preserves_snapshots(tmp_path, process, disable_extractors_dict):
    """Test that re-running init preserves existing snapshot data."""
    os.chdir(tmp_path)

    # Add a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    # Check snapshot was created
    with use_archivebox_db(tmp_path):
        count_before = Snapshot.objects.count()
    assert count_before == 1

    # Run init again
    result = subprocess.run(["archivebox", "init"], capture_output=True)
    assert result.returncode == 0

    # Snapshot should still exist
    with use_archivebox_db(tmp_path):
        count_after = Snapshot.objects.count()
    assert count_after == count_before


def test_init_quick_flag_skips_checks(tmp_path):
    """Test that init --quick runs faster by skipping some checks."""
    os.chdir(tmp_path)

    result = subprocess.run(["archivebox", "init", "--quick"], capture_output=True, text=True)

    assert result.returncode == 0
    # Database should still be created
    db_path = tmp_path / "index.sqlite3"
    assert db_path.exists()


def test_init_creates_machine_table(tmp_path):
    """Test that init creates the machine_machine table."""
    os.chdir(tmp_path)
    subprocess.run(["archivebox", "init"], capture_output=True)

    assert Machine._meta.db_table == "machine_machine"
    with use_archivebox_db(tmp_path):
        Machine.objects.count()


def test_init_output_shows_collection_info(tmp_path):
    """Test that init output shows helpful collection information."""
    os.chdir(tmp_path)
    result = subprocess.run(["archivebox", "init"], capture_output=True, text=True)

    output = result.stdout
    # Should show some helpful info about the collection
    assert "ArchiveBox" in output or "collection" in output.lower() or "Initializing" in output


def test_init_ignores_unrecognized_archive_directories(tmp_path, process, disable_extractors_dict):
    """Test that init upgrades existing dirs without choking on extra folders."""
    os.chdir(tmp_path)
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
        check=True,
    )
    run_queued_crawls(tmp_path, disable_extractors_dict)
    (tmp_path / "archive" / "some_random_folder").mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["archivebox", "init"],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    assert result.returncode == 0, result.stdout + result.stderr
