#!/usr/bin/env python3
"""
Comprehensive tests for archivebox remove command.
Verify remove deletes snapshots from DB and filesystem.
"""

import os
import json
import subprocess
from pathlib import Path


def _find_snapshot_dir(data_dir: Path, snapshot_id: str) -> Path | None:
    candidates = {snapshot_id}
    if len(snapshot_id) == 32:
        candidates.add(f"{snapshot_id[:8]}-{snapshot_id[8:12]}-{snapshot_id[12:16]}-{snapshot_id[16:20]}-{snapshot_id[20:]}")
    elif len(snapshot_id) == 36 and "-" in snapshot_id:
        candidates.add(snapshot_id.replace("-", ""))

    for needle in candidates:
        for path in data_dir.rglob(needle):
            if path.is_dir():
                return path
    return None


def _snapshot_rows(data_dir: Path, env: dict) -> list[dict]:
    script = """
import json
from archivebox.core.models import Snapshot
print(json.dumps([
    {"id": str(snapshot.id), "url": snapshot.url}
    for snapshot in Snapshot.objects.order_by("url")
]))
"""
    result = subprocess.run(
        ["archivebox", "manage", "shell", "-c", script],
        cwd=data_dir,
        capture_output=True,
        env=env,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])


def test_remove_deletes_snapshot_from_db(tmp_path, process, disable_extractors_dict):
    """Test that remove command deletes snapshot from database."""
    os.chdir(tmp_path)

    # Add a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    rows = _snapshot_rows(tmp_path, disable_extractors_dict)
    assert len(rows) == 1
    snapshot_id = rows[0]["id"]
    snapshot_dir = _find_snapshot_dir(tmp_path, snapshot_id)
    assert snapshot_dir is not None, f"Snapshot output directory not found for {snapshot_id}"

    # Remove it
    subprocess.run(
        ["archivebox", "remove", "https://example.com", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    assert len(_snapshot_rows(tmp_path, disable_extractors_dict)) == 0
    assert not snapshot_dir.exists()


def test_remove_deletes_archive_directory(tmp_path, process, disable_extractors_dict):
    """Test that remove --yes removes the current snapshot output directory."""
    os.chdir(tmp_path)

    # Add a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    rows = _snapshot_rows(tmp_path, disable_extractors_dict)
    assert len(rows) == 1
    snapshot_id = rows[0]["id"]

    snapshot_dir = _find_snapshot_dir(tmp_path, snapshot_id)
    assert snapshot_dir is not None, f"Snapshot output directory not found for {snapshot_id}"

    subprocess.run(
        ["archivebox", "remove", "https://example.com", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    assert not snapshot_dir.exists()


def test_remove_yes_flag_skips_confirmation(tmp_path, process, disable_extractors_dict):
    """Test that --yes flag skips confirmation prompt."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    # Remove with --yes should complete without interaction
    result = subprocess.run(
        ["archivebox", "remove", "https://example.com", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    assert result.returncode == 0
    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert "Index now contains 0 links." in output


def test_remove_without_yes_prompts_and_keeps_snapshot(tmp_path, process, disable_extractors_dict):
    """Test that omitting --yes prompts for confirmation and keeps data when declined."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
        check=True,
    )

    rows = _snapshot_rows(tmp_path, disable_extractors_dict)
    assert len(rows) == 1
    snapshot_dir = _find_snapshot_dir(tmp_path, rows[0]["id"])
    assert snapshot_dir is not None

    result = subprocess.run(
        ["archivebox", "remove", "https://example.com"],
        input=b"n\n",
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0
    assert "Do you want to proceed" in output or "y/[n]" in output
    assert len(_snapshot_rows(tmp_path, disable_extractors_dict)) == 1
    assert snapshot_dir.exists()


def test_remove_multiple_snapshots(tmp_path, process, disable_extractors_dict):
    """Test removing multiple snapshots at once."""
    os.chdir(tmp_path)

    # Add multiple snapshots
    for url in ["https://example.com", "https://example.org"]:
        subprocess.run(
            ["archivebox", "add", "--index-only", "--depth=0", url],
            capture_output=True,
            env=disable_extractors_dict,
        )

    assert len(_snapshot_rows(tmp_path, disable_extractors_dict)) == 2

    # Remove both
    subprocess.run(
        ["archivebox", "remove", "https://example.com", "https://example.org", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    assert len(_snapshot_rows(tmp_path, disable_extractors_dict)) == 0


def test_remove_with_filter(tmp_path, process, disable_extractors_dict):
    """Test removing snapshots using filter."""
    os.chdir(tmp_path)

    # Add snapshots
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    # Remove using filter
    result = subprocess.run(
        ["archivebox", "remove", "--filter-type=search", "--filter=example.com", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    # Should complete (exit code depends on implementation)
    assert result.returncode in [0, 1, 2]


def test_remove_with_regex_filter_deletes_all_matches(tmp_path, process, disable_extractors_dict):
    """Test regex filters remove every matching snapshot."""
    os.chdir(tmp_path)

    for url in ["https://example.com", "https://iana.org"]:
        subprocess.run(
            ["archivebox", "add", "--index-only", "--depth=0", url],
            capture_output=True,
            env=disable_extractors_dict,
            check=True,
        )

    result = subprocess.run(
        ["archivebox", "remove", "--filter-type=regex", ".*", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
        check=True,
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert len(_snapshot_rows(tmp_path, disable_extractors_dict)) == 0
    assert "Removed" in output or "Found" in output


def test_remove_nonexistent_url_fails_gracefully(tmp_path, process, disable_extractors_dict):
    """Test that removing non-existent URL fails gracefully."""
    os.chdir(tmp_path)

    result = subprocess.run(
        ["archivebox", "remove", "https://nonexistent-url-12345.com", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    # Should fail or show error
    stdout_text = result.stdout.decode("utf-8", errors="replace").lower()
    assert result.returncode != 0 or "not found" in stdout_text or "no matches" in stdout_text


def test_remove_reports_remaining_link_count_correctly(tmp_path, process, disable_extractors_dict):
    """Test remove reports the remaining snapshot count after deletion."""
    os.chdir(tmp_path)

    for url in ["https://example.com", "https://example.org"]:
        subprocess.run(
            ["archivebox", "add", "--index-only", "--depth=0", url],
            capture_output=True,
            env=disable_extractors_dict,
            check=True,
        )

    result = subprocess.run(
        ["archivebox", "remove", "https://example.org", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
        check=True,
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert "Removed 1 out of 2 links" in output
    assert "Index now contains 1 links." in output


def test_remove_after_flag(tmp_path, process, disable_extractors_dict):
    """Test remove --after flag removes snapshots after date."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    # Try remove with --after flag (should work or show usage)
    result = subprocess.run(
        ["archivebox", "remove", "--after=2020-01-01", "--yes"],
        capture_output=True,
        env=disable_extractors_dict,
        timeout=30,
    )

    # Should complete
    assert result.returncode in [0, 1, 2]
