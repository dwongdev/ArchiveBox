import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl


pytestmark = pytest.mark.django_db(transaction=True)


def test_basic_success_case_request(client, tmp_path, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/archiveresult-detail", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/archiveresult-detail", crawl=crawl)
    result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="api-basic",
        hook_name="on_Snapshot__api_basic",
        status=ArchiveResult.StatusChoices.SUCCEEDED,
        output_str="ok",
    )

    response = client.get(f"/api/v1/core/archiveresult/{result.id}", **api_headers)

    assert response.status_code == 200, response.content


def test_archiveresult_patch_upload_finalizes_queued_result(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(
        urls="https://example.com",
        created_by=api_admin_user,
        status=Crawl.StatusChoices.SEALED,
        retry_at=None,
    )
    snapshot = Snapshot.objects.create(
        url="https://example.com/upload-patch",
        crawl=crawl,
        status=Snapshot.StatusChoices.SEALED,
        retry_at=None,
    )
    result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="dom",
        hook_name="on_Snapshot__archivebox_browser_extension_upload",
        status=ArchiveResult.StatusChoices.QUEUED,
    )

    response = client.generic(
        "PATCH",
        f"/api/v1/core/archiveresult/{result.id}",
        encode_multipart(
            BOUNDARY,
            {
                "files": SimpleUploadedFile("output.html", b"<html>uploaded</html>", content_type="text/html"),
                "output_paths": "output.html",
                "output_str": "output.html",
            },
        ),
        content_type=MULTIPART_CONTENT,
        **api_headers,
    )
    assert response.status_code == 200, response.content

    result.refresh_from_db()
    snapshot.refresh_from_db()
    assert result.status == ArchiveResult.StatusChoices.SUCCEEDED
    assert result.output_str == "output.html"
    assert snapshot.status == Snapshot.StatusChoices.SEALED
    assert snapshot.retry_at is not None
