#!/usr/bin/env python3
"""Integration tests for archivebox snapshot command."""

import os
import subprocess
from archivebox.machine.models import Process

import pytest

from archivebox.core.models import Snapshot, Tag
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def test_snapshot_creates_snapshot_with_correct_url(tmp_path, process, disable_extractors_dict):
    """Test that snapshot stores the exact URL in the database."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "snapshot", "create", "https://example.com"],
        capture_output=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )

    with use_archivebox_db(tmp_path):
        snapshot = Snapshot.objects.select_related("crawl__created_by").get(url="https://example.com")
        username = snapshot.crawl.created_by.username

    # Verify the crawl tree contains a relative symlink to the user-scoped snapshot output.
    snapshots_root = tmp_path / "archive" / "users" / username / "snapshots"
    crawl_root = tmp_path / "archive" / "users" / username / "crawls"
    symlinks = [p for p in crawl_root.rglob("*") if p.is_symlink() and p.resolve().is_dir() and p.resolve().is_relative_to(snapshots_root)]
    assert symlinks, "Snapshot symlink should exist under crawl dir"
    link_path = symlinks[0]

    assert link_path.is_symlink(), "Snapshot symlink should exist under crawl dir"
    link_target = os.readlink(link_path)
    assert not os.path.isabs(link_target), "Symlink should be relative"


def test_snapshot_multiple_urls_creates_multiple_records(tmp_path, process, disable_extractors_dict):
    """Test that multiple URLs each get their own snapshot record."""
    os.chdir(tmp_path)

    subprocess.run(
        [
            "archivebox",
            "snapshot",
            "create",
            "https://example.com",
            "https://iana.org",
        ],
        capture_output=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )

    with use_archivebox_db(tmp_path):
        urls = list(Snapshot.objects.order_by("url").values_list("url", flat=True))

    assert "https://example.com" in urls
    assert "https://iana.org" in urls
    assert len(urls) >= 2


def test_snapshot_tag_creates_tag_and_links_to_snapshot(tmp_path, process, disable_extractors_dict):
    """Test that --tag creates tag record and links it to the snapshot."""
    os.chdir(tmp_path)

    subprocess.run(
        [
            "archivebox",
            "snapshot",
            "create",
            "--tag=mytesttag",
            "https://example.com",
        ],
        capture_output=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )

    with use_archivebox_db(tmp_path):
        tag = Tag.objects.filter(name="mytesttag").first()
        assert tag is not None, "Tag 'mytesttag' should exist in core_tag"
        snapshot = Snapshot.objects.filter(url="https://example.com").first()
        assert snapshot is not None
        assert snapshot.tags.filter(pk=tag.pk).exists(), "Tag should be linked to snapshot via core_snapshot_tags"


def test_snapshot_jsonl_output_has_correct_structure(tmp_path, process, disable_extractors_dict):
    """Test that JSONL output contains required fields with correct types."""
    os.chdir(tmp_path)

    # Pass URL as argument instead of stdin for more reliable behavior
    result = subprocess.run(
        ["archivebox", "snapshot", "create", "https://example.com"],
        capture_output=True,
        text=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )

    # Parse JSONL output lines
    records = Process.parse_records_from_text(result.stdout)
    snapshot_records = [r for r in records if r.get("type") == "Snapshot"]

    assert len(snapshot_records) >= 1, "Should output at least one Snapshot JSONL record"

    record = snapshot_records[0]
    assert record.get("type") == "Snapshot"
    assert "id" in record, "Snapshot record should have 'id' field"
    assert "url" in record, "Snapshot record should have 'url' field"
    assert record["url"] == "https://example.com"


def test_snapshot_with_tag_stores_tag_name(tmp_path, process, disable_extractors_dict):
    """Test that title is stored when provided via tag option."""
    os.chdir(tmp_path)

    # Use command line args instead of stdin
    subprocess.run(
        ["archivebox", "snapshot", "create", "--tag=customtag", "https://example.com"],
        capture_output=True,
        text=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )

    with use_archivebox_db(tmp_path):
        tag = Tag.objects.filter(name="customtag").first()

    assert tag is not None
    assert tag.name == "customtag"


def test_snapshot_with_depth_sets_snapshot_depth(tmp_path, process, disable_extractors_dict):
    """Test that --depth sets snapshot depth when creating snapshots."""
    os.chdir(tmp_path)

    subprocess.run(
        [
            "archivebox",
            "snapshot",
            "create",
            "--depth=1",
            "https://example.com",
        ],
        capture_output=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )

    with use_archivebox_db(tmp_path):
        snapshot = Snapshot.objects.order_by("-created_at").first()

    assert snapshot is not None, "Snapshot should be created when depth is provided"
    assert snapshot.depth == 1, "Snapshot depth should match --depth value"


def test_snapshot_allows_duplicate_urls_across_crawls(tmp_path, process, disable_extractors_dict):
    """Snapshot create auto-creates a crawl per run; same URL can appear multiple times."""
    os.chdir(tmp_path)

    # Add same URL twice
    subprocess.run(
        ["archivebox", "snapshot", "create", "https://example.com"],
        capture_output=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )
    subprocess.run(
        ["archivebox", "snapshot", "create", "https://example.com"],
        capture_output=True,
        env={**disable_extractors_dict, "DATA_DIR": str(tmp_path)},
    )

    with use_archivebox_db(tmp_path):
        count = Snapshot.objects.filter(url="https://example.com").count()

    assert count == 2, "Same URL should create separate snapshots across different crawls"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
