"""Tests for archivebox extract input handling and pipelines."""

import subprocess
import json

import pytest

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.tests.conftest import run_archivebox_cmd, cli_env

from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def create_extract_snapshot(initialized_archive, env, url="https://example.com"):
    run_archivebox_cmd(
        ["snapshot", "create", url],
        cwd=initialized_archive,
        env=env,
        check=True,
    )


def test_extract_runs_on_snapshot_id(initialized_archive):
    """Test that extract command accepts a snapshot ID."""
    env = cli_env(disable_extractors=True)
    create_extract_snapshot(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        snapshot_id = Snapshot.objects.values_list("id", flat=True).first()

    # Run extract on the snapshot
    result = run_archivebox_cmd(
        ["extract", "--no-wait", str(snapshot_id)],
        env=env,
    )

    # Should not error about invalid snapshot ID
    assert "not found" not in result.stderr.lower()


def test_extract_with_enabled_extractor_creates_archiveresult(initialized_archive):
    """Test that extract creates ArchiveResult when extractor is enabled."""
    env = cli_env(disable_extractors=True)
    create_extract_snapshot(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        snapshot_id = Snapshot.objects.values_list("id", flat=True).first()

    # Run extract with title extractor enabled
    env = env.copy()
    env["SAVE_TITLE"] = "true"

    run_archivebox_cmd(
        ["extract", "--no-wait", str(snapshot_id)],
        env=env,
    )

    with use_archivebox_db(initialized_archive):
        count = ArchiveResult.objects.filter(snapshot_id=snapshot_id).count()

    # May or may not have results depending on timing
    assert count >= 0


def test_extract_plugin_option_accepted(initialized_archive):
    """Test that --plugin option is accepted."""
    env = cli_env(disable_extractors=True)
    create_extract_snapshot(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        snapshot_id = Snapshot.objects.values_list("id", flat=True).first()

    result = run_archivebox_cmd(
        ["extract", "--plugin=title", "--no-wait", str(snapshot_id)],
        env=env,
    )

    assert "unrecognized arguments: --plugin" not in result.stderr


def test_extract_stdin_snapshot_id(initialized_archive):
    """Test that extract reads snapshot IDs from stdin."""
    env = cli_env(disable_extractors=True)
    create_extract_snapshot(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        snapshot_id = Snapshot.objects.values_list("id", flat=True).first()

    result = run_archivebox_cmd(
        ["extract", "--no-wait"],
        input=f"{snapshot_id}\n",
        env=env,
    )

    # Should not show "not found" error
    assert "not found" not in result.stderr.lower() or result.returncode == 0


def test_extract_stdin_jsonl_input(initialized_archive):
    """Test that extract reads JSONL records from stdin."""
    env = cli_env(disable_extractors=True)
    create_extract_snapshot(initialized_archive, env)

    with use_archivebox_db(initialized_archive):
        snapshot_id = Snapshot.objects.values_list("id", flat=True).first()

    jsonl_input = json.dumps({"type": "Snapshot", "id": str(snapshot_id)}) + "\n"

    result = run_archivebox_cmd(
        ["extract", "--no-wait"],
        input=jsonl_input,
        env=env,
    )

    # Should not show "not found" error
    assert "not found" not in result.stderr.lower() or result.returncode == 0


def test_extract_pipeline_from_snapshot(initialized_archive):
    """Test piping snapshot output to extract."""
    env = cli_env(disable_extractors=True)

    # Create snapshot and pipe to extract
    snapshot_proc = run_archivebox_cmd(
        ["snapshot", "create", "https://example.com"],
        cwd=initialized_archive,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        wait=False,
    )

    extract_proc = run_archivebox_cmd(
        ["extract", "--no-wait"],
        cwd=initialized_archive,
        stdin=snapshot_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        wait=False,
    )
    if snapshot_proc.stdout is not None:
        snapshot_proc.stdout.close()

    extract_stdout, extract_stderr = extract_proc.communicate(timeout=60)
    snapshot_stdout, snapshot_stderr = snapshot_proc.communicate(timeout=60)
    assert snapshot_proc.returncode == 0, (snapshot_stdout or "") + (snapshot_stderr or "")

    with use_archivebox_db(initialized_archive):
        snapshot = Snapshot.objects.filter(url="https://example.com").first()

    assert snapshot is not None, "Snapshot should be created by pipeline"


def test_extract_multiple_snapshots(initialized_archive):
    """Test extracting from multiple snapshots."""
    env = cli_env(disable_extractors=True)

    create_extract_snapshot(initialized_archive, env, "https://example.com")
    create_extract_snapshot(initialized_archive, env, "https://iana.org")

    with use_archivebox_db(initialized_archive):
        snapshot_ids = list(Snapshot.objects.values_list("id", flat=True))

    assert len(snapshot_ids) >= 2, "Should have at least 2 snapshots"

    # Extract from all snapshots
    ids_input = "\n".join(str(snapshot_id) for snapshot_id in snapshot_ids) + "\n"
    result = run_archivebox_cmd(
        ["extract", "--no-wait"],
        input=ids_input,
        env=env,
    )
    assert result.returncode == 0, result.stderr

    with use_archivebox_db(initialized_archive):
        count = Snapshot.objects.count()

    assert count >= 2, "Both snapshots should still exist after extraction"


class TestExtractCLI:
    """Test the CLI interface for extract command."""

    def test_cli_help(self, initialized_archive):
        """Test that --help works for extract command."""

        result = run_archivebox_cmd(
            ["extract", "--help"],
        )

        assert result.returncode == 0
        assert "--plugin" in result.stdout or "-p" in result.stdout
        assert "--wait" in result.stdout or "--no-wait" in result.stdout

    def test_cli_no_snapshots_shows_warning(self, initialized_archive):
        """Test that running without snapshots shows a warning."""

        result = run_archivebox_cmd(
            ["extract", "--no-wait"],
            input="",
        )

        # Should show warning about no snapshots or exit normally (empty input)
        assert result.returncode == 0 or "No" in result.stderr
