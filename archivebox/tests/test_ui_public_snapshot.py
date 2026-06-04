"""Public snapshot UI tests."""

import re
import time

import pytest
import requests
from django.test import override_settings

from archivebox.tests.conftest import PUBLIC_TEST_HOST, WEB_TEST_HOST
from archivebox.tests.conftest import (
    cli_env,
    create_admin_and_token,
    get_free_port,
    init_archive,
    start_archivebox_server,
    stop_server,
    wait_for_http,
)
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db


def _login_admin_over_full_server(port: int) -> tuple[requests.Session, str]:
    session = requests.Session()
    wait_for_http(port, host=f"admin.archivebox.localhost:{port}", path="/admin/login/")
    login_page = session.get(
        f"http://admin.archivebox.localhost:{port}/admin/login/",
        timeout=10,
    )
    assert login_page.status_code == 200
    csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
    assert csrf_match, login_page.text[:500]
    login_response = session.post(
        f"http://admin.archivebox.localhost:{port}/admin/login/",
        headers={"Referer": f"http://admin.archivebox.localhost:{port}/admin/login/"},
        data={
            "username": "apitestadmin",
            "password": "testpass123",
            "csrfmiddlewaretoken": csrf_match.group(1),
            "next": "/add/",
        },
        timeout=10,
        allow_redirects=False,
    )
    assert login_response.status_code in (302, 303), login_response.text
    add_page = session.get(
        f"http://admin.archivebox.localhost:{port}/add/",
        headers={"Referer": f"http://admin.archivebox.localhost:{port}/admin/login/"},
        timeout=10,
    )
    assert add_page.status_code == 200
    add_csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', add_page.text)
    assert add_csrf_match, add_page.text[:500]
    return session, add_csrf_match.group(1)


def _create_private_snapshot_over_full_server(data_dir, session: requests.Session, port: int, csrf_token: str, url: str) -> dict[str, str]:
    response = session.post(
        f"http://admin.archivebox.localhost:{port}/add/",
        headers={"Referer": f"http://admin.archivebox.localhost:{port}/add/"},
        data={
            "url": url,
            "depth": "0",
            "max_urls": "1",
            "crawl_max_size": "0",
            "crawl_timeout": "0",
            "snapshot_max_size": "0",
            "crawl_max_concurrent_snapshots": "1",
            "main_plugins": ["wget"],
            "tag": "private-replay-auth",
            "url_filters_allowlist": r"127\.0\.0\.1[:/].*",
            "url_filters_denylist": "",
            "schedule": "",
            "notes": "private replay auth regression fixture",
            "persona": "Default",
            "permissions": "private",
            "start_paused": "",
            "config": "{}",
            "csrfmiddlewaretoken": csrf_token,
        },
        timeout=10,
        allow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text

    deadline = time.time() + 60
    while time.time() < deadline:
        with use_archivebox_db(data_dir):
            from archivebox.core.models import Snapshot

            snapshot = Snapshot.objects.select_related("crawl").filter(url=url).order_by("-created_at").first()
            if snapshot:
                snapshot_id = str(snapshot.id)
                return {
                    "id": snapshot_id,
                    "path": snapshot.url_path,
                    "host": f"snap-{snapshot_id.replace('-', '')[-12:]}.archivebox.localhost:{port}",
                }
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for private Snapshot created from {url}")


def _logout_admin_over_full_server(session: requests.Session, port: int) -> None:
    csrf_token = next((cookie.value for cookie in session.cookies if cookie.name.startswith("archivebox_csrftoken_")), "")
    assert csrf_token
    response = session.post(
        f"http://admin.archivebox.localhost:{port}/admin/logout/",
        headers={"Referer": f"http://admin.archivebox.localhost:{port}/admin/"},
        data={"csrfmiddlewaretoken": csrf_token},
        timeout=10,
        allow_redirects=False,
    )
    assert response.status_code in (200, 302, 303), response.text


def _replay_cookies(session: requests.Session):
    return [cookie for cookie in session.cookies if cookie.name.startswith("archivebox_replay_")]


class TestPublicIndex:
    """Tests for public index visibility and redirects."""

    @pytest.mark.timeout(120)
    @pytest.mark.django_db(transaction=True)
    def test_base_url_redirect_target_is_reachable_over_full_server(self, tmp_path):
        init_archive(tmp_path)
        port = get_free_port()
        env = cli_env(
            port=port,
            server=True,
            BASE_URL=f"http://archivebox.localhost:{port}",
            PUBLIC_INDEX="True",
        )

        try:
            start_archivebox_server(tmp_path, env=env, port=port)
            session = requests.Session()
            session.trust_env = False
            response = session.get(
                f"http://archivebox.localhost:{port}/",
                timeout=10,
                allow_redirects=True,
            )

            assert response.status_code == 200
            assert response.url == f"http://web.archivebox.localhost:{port}/public/"
            assert "ArchiveBox" in response.text
        finally:
            stop_server(tmp_path)

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_lists_only_public_snapshots(self, client, admin_user):
        from archivebox.core.models import Snapshot
        from archivebox.crawls.models import Crawl

        public_crawl = Crawl.objects.create(urls="https://public.example", created_by=admin_user, config={"PERMISSIONS": "public"})
        unlisted_crawl = Crawl.objects.create(urls="https://unlisted.example", created_by=admin_user, config={"PERMISSIONS": "unlisted"})
        private_crawl = Crawl.objects.create(urls="https://private.example", created_by=admin_user, config={"PERMISSIONS": "private"})
        Snapshot.objects.create(
            url="https://public.example",
            title="Public Snapshot",
            crawl=public_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        Snapshot.objects.create(
            url="https://unlisted.example",
            title="Unlisted Snapshot",
            crawl=unlisted_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        Snapshot.objects.create(
            url="https://private.example",
            title="Private Snapshot",
            crawl=private_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )

        response = client.get("/public/", HTTP_HOST=PUBLIC_TEST_HOST)

        assert response.status_code == 200
        assert b"Public Snapshot" in response.content
        assert b"Unlisted Snapshot" not in response.content
        assert b"Private Snapshot" not in response.content

    def test_direct_snapshot_urls_allow_unlisted_but_not_private_for_guests(self, client, admin_user):
        from archivebox.core.models import Snapshot
        from archivebox.crawls.models import Crawl

        unlisted_crawl = Crawl.objects.create(urls="https://unlisted.example", created_by=admin_user, config={"PERMISSIONS": "unlisted"})
        private_crawl = Crawl.objects.create(urls="https://private.example", created_by=admin_user, config={"PERMISSIONS": "private"})
        unlisted_snapshot = Snapshot.objects.create(
            url="https://unlisted.example",
            crawl=unlisted_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        private_snapshot = Snapshot.objects.create(url="https://private.example", crawl=private_crawl, status=Snapshot.StatusChoices.SEALED)

        unlisted_response = client.get(f"/snapshot/{unlisted_snapshot.id}/", HTTP_HOST=WEB_TEST_HOST)
        private_response = client.get(f"/snapshot/{private_snapshot.id}/", HTTP_HOST=WEB_TEST_HOST)

        assert unlisted_response.status_code == 200
        assert private_response.status_code == 302
        assert "/admin/core/snapshot/replay-auth/" in private_response["Location"]

    @pytest.mark.timeout(180)
    @pytest.mark.django_db(transaction=True)
    def test_private_snapshot_bookmark_authorizes_logged_in_admin_with_snap_scoped_replay_cookie(
        self,
        tmp_path,
        recursive_test_site,
    ):
        init_archive(tmp_path)
        create_admin_and_token(tmp_path)
        port = get_free_port()
        env = cli_env(
            port=port,
            server=True,
            SERVER_SECURITY_MODE="safe-subdomains-fullreplay",
            PUBLIC_ADD_VIEW="True",
            PUBLIC_INDEX="True",
        )

        try:
            start_archivebox_server(tmp_path, env=env, port=port)
            session, csrf_token = _login_admin_over_full_server(port)
            snapshot = _create_private_snapshot_over_full_server(tmp_path, session, port, csrf_token, recursive_test_site["root_url"])
            snap_url = f"http://{snapshot['host']}/index.html"

            response = session.get(snap_url, timeout=10, allow_redirects=True)

            assert response.status_code == 200
            assert response.url == snap_url
            assert recursive_test_site["root_url"] in response.text
            assert "/admin/login/" not in response.url
            assert "/admin/login/" not in response.text

            replay_cookies = _replay_cookies(session)
            assert replay_cookies
            assert any(cookie.domain == snapshot["host"].split(":", 1)[0] for cookie in replay_cookies)
            assert not any(cookie.domain in {"archivebox.localhost", ".archivebox.localhost"} for cookie in replay_cookies)
        finally:
            stop_server(tmp_path)

    @pytest.mark.timeout(180)
    @pytest.mark.django_db(transaction=True)
    def test_private_snapshot_replay_cookie_stops_working_after_admin_logout(
        self,
        tmp_path,
        recursive_test_site,
    ):
        init_archive(tmp_path)
        create_admin_and_token(tmp_path)
        port = get_free_port()
        env = cli_env(
            port=port,
            server=True,
            SERVER_SECURITY_MODE="safe-subdomains-fullreplay",
            PUBLIC_ADD_VIEW="True",
            PUBLIC_INDEX="True",
        )

        try:
            start_archivebox_server(tmp_path, env=env, port=port)
            session, csrf_token = _login_admin_over_full_server(port)
            snapshot = _create_private_snapshot_over_full_server(tmp_path, session, port, csrf_token, recursive_test_site["root_url"])
            snap_url = f"http://{snapshot['host']}/index.html"

            authorized = session.get(snap_url, timeout=10, allow_redirects=True)
            assert authorized.status_code == 200
            assert authorized.url == snap_url
            assert _replay_cookies(session)

            _logout_admin_over_full_server(session, port)

            stale_replay = session.get(snap_url, timeout=10, allow_redirects=False)
            assert stale_replay.status_code in (302, 303)
            assert "/admin/core/snapshot/replay-auth/" in stale_replay.headers["Location"]

            logged_out = session.get(snap_url, timeout=10, allow_redirects=True)
            assert "/admin/login/" in logged_out.url
            assert logged_out.url != snap_url
        finally:
            stop_server(tmp_path)

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_redirects_logged_in_users_to_admin_snapshot_list(self, client, admin_user):
        client.force_login(admin_user)

        response = client.get("/public/", HTTP_HOST=PUBLIC_TEST_HOST)

        assert response.status_code == 302
        assert response["Location"] == "/admin/core/snapshot/"
