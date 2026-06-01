import os
import re
import time

import pytest
import requests

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl, CrawlSchedule
from archivebox.tests.test_orm_helpers import use_archivebox_db
from .conftest import (
    build_test_env,
    create_admin_and_token,
    get_depth_counts,
    get_free_port,
    init_archive,
    run_archivebox_cmd_cwd,
    start_server,
    stop_server,
    wait_for_http,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.timeout(180)
def test_add_view_restarts_stopped_supervisord_runner(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(
        port,
        PLUGINS="wget",
        PUBLIC_ADD_VIEW="True",
        PYTEST_CURRENT_TEST="",
    )
    create_admin_and_token(tmp_path)

    try:
        start_server(tmp_path, env=env, port=port)
        _wait_for_worker_state(tmp_path, "worker_runner", "RUNNING")
        _stop_worker(tmp_path, "worker_runner")
        assert _worker_state(tmp_path, "worker_runner") != "RUNNING"

        session, csrf_token = _login_to_add_view(port)
        response = session.post(
            f"http://127.0.0.1:{port}/add/",
            headers={"Host": f"admin.archivebox.localhost:{port}", "Referer": f"http://admin.archivebox.localhost:{port}/add/"},
            data={
                "url": recursive_test_site["root_url"],
                "depth": "0",
                "max_urls": "1",
                "crawl_max_size": "0",
                "snapshot_max_size": "0",
                "main_plugins": ["wget"],
                "tag": "restart-supervised-runner",
                "url_filters_allowlist": r"127\.0\.0\.1[:/].*",
                "url_filters_denylist": "",
                "schedule": "",
                "notes": "restart stopped supervised runner",
                "persona": "Default",
                "permissions": "public",
                "start_paused": "",
                "config": "{}",
                "csrfmiddlewaretoken": csrf_token,
            },
            timeout=10,
            allow_redirects=False,
        )
        assert response.status_code in (302, 303), response.text

        _wait_for_worker_state(tmp_path, "worker_runner", "RUNNING")
        with use_archivebox_db(tmp_path):
            crawl = Crawl.objects.order_by("-created_at").first()
            assert crawl is not None
            assert crawl.tags_str == "restart-supervised-runner"
            assert crawl.urls == recursive_test_site["root_url"]
    finally:
        stop_server(tmp_path)


def _login_to_add_view(port: int) -> tuple[requests.Session, str]:
    session = requests.Session()
    wait_for_http(port, host=f"admin.archivebox.localhost:{port}", path="/admin/login/")
    login_page = session.get(
        f"http://127.0.0.1:{port}/admin/login/",
        headers={"Host": f"admin.archivebox.localhost:{port}"},
        timeout=10,
    )
    assert login_page.status_code == 200
    csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
    assert csrf_match, login_page.text[:500]
    login_response = session.post(
        f"http://127.0.0.1:{port}/admin/login/",
        headers={"Host": f"admin.archivebox.localhost:{port}", "Referer": f"http://admin.archivebox.localhost:{port}/admin/login/"},
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
    add_page = wait_for_http(port, host=f"admin.archivebox.localhost:{port}", path="/add/")
    assert add_page.status_code == 200
    add_csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', add_page.text)
    assert add_csrf_match, add_page.text[:500]
    return session, add_csrf_match.group(1)


def _worker_state(cwd, worker_name: str) -> str | None:
    script = f"""
import json
from archivebox.workers.supervisord_util import get_existing_supervisord_process, get_worker
supervisor = get_existing_supervisord_process()
worker = get_worker(supervisor, {worker_name!r}) if supervisor else None
print(json.dumps(worker))
"""
    stdout, stderr, returncode = run_archivebox_cmd_cwd(["manage", "shell", "-c", script], cwd=cwd, timeout=60)
    assert returncode == 0, stderr or stdout
    import json

    worker = json.loads(stdout.strip().splitlines()[-1])
    return worker.get("statename") if worker else None


def _stop_worker(cwd, worker_name: str) -> None:
    script = f"""
from archivebox.workers.supervisord_util import get_existing_supervisord_process, stop_worker
supervisor = get_existing_supervisord_process()
assert supervisor is not None
stop_worker(supervisor, {worker_name!r})
print("stopped")
"""
    stdout, stderr, returncode = run_archivebox_cmd_cwd(["manage", "shell", "-c", script], cwd=cwd, timeout=60)
    assert returncode == 0, stderr or stdout


def _wait_for_worker_state(cwd, worker_name: str, statename: str, timeout: int = 45) -> None:
    deadline = time.time() + timeout
    state = None
    while time.time() < deadline:
        state = _worker_state(cwd, worker_name)
        if state == statename:
            return
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for {worker_name}={statename}, last state={state}")


@pytest.mark.timeout(180)
def test_add_view_post_creates_schedule_over_server(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(port, PUBLIC_ADD_VIEW="True")
    create_admin_and_token(tmp_path)

    try:
        start_server(tmp_path, env=env, port=port)
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
        wait_for_http(port, host=f"admin.archivebox.localhost:{port}", path="/add/")

        response = session.post(
            f"http://admin.archivebox.localhost:{port}/add/",
            headers={"Referer": f"http://admin.archivebox.localhost:{port}/add/"},
            data={
                "url": recursive_test_site["root_url"],
                "depth": "0",
                "max_urls": "0",
                "crawl_max_size": "0",
                "snapshot_max_size": "0",
                "schedule": "daily",
                "tag": "web-ui",
                "notes": "created from web ui",
                "persona": "Default",
                "permissions": "public",
                "config": "{}",
                "csrfmiddlewaretoken": csrf_match.group(1),
            },
            timeout=10,
            allow_redirects=False,
        )

        assert response.status_code in (302, 303), response.text

        with use_archivebox_db(tmp_path):
            schedule = CrawlSchedule.objects.select_related("template").order_by("-created_at").first()
            row = (schedule.schedule, schedule.template.urls, schedule.template.tags_str) if schedule else None

        assert row == ("daily", recursive_test_site["root_url"], "web-ui")
    finally:
        stop_server(tmp_path)


@pytest.mark.timeout(240)
def test_add_view_depth_two_crawl_renders_outputs_over_server(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(
        port,
        PLUGINS="wget,parse_html_urls",
        PUBLIC_INDEX="True",
        PUBLIC_ADD_VIEW="True",
    )
    create_admin_and_token(tmp_path)

    try:
        start_server(tmp_path, env=env, port=port)
        session = requests.Session()
        wait_for_http(port, host=f"admin.archivebox.localhost:{port}", path="/admin/login/")
        login_page = session.get(
            f"http://127.0.0.1:{port}/admin/login/",
            headers={"Host": f"admin.archivebox.localhost:{port}"},
            timeout=10,
        )
        assert login_page.status_code == 200
        csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
        assert csrf_match, login_page.text[:500]
        login_response = session.post(
            f"http://127.0.0.1:{port}/admin/login/",
            headers={"Host": f"admin.archivebox.localhost:{port}", "Referer": f"http://admin.archivebox.localhost:{port}/admin/login/"},
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
        add_page = wait_for_http(port, host=f"admin.archivebox.localhost:{port}", path="/add/")
        assert add_page.status_code == 200
        assert 'name="depth"' in add_page.text
        assert 'name="url"' in add_page.text

        response = session.post(
            f"http://127.0.0.1:{port}/add/",
            headers={"Host": f"admin.archivebox.localhost:{port}", "Referer": f"http://admin.archivebox.localhost:{port}/add/"},
            data={
                "url": recursive_test_site["root_url"],
                "depth": "2",
                "max_urls": "20",
                "crawl_max_size": "0",
                "snapshot_max_size": "0",
                "main_plugins": ["wget"],
                "postprocessing_plugins": ["parse_html_urls"],
                "tag": "web-depth-two",
                "url_filters_allowlist": r"127\.0\.0\.1[:/].*",
                "url_filters_denylist": "",
                "schedule": "",
                "notes": "created from running-server web ui",
                "persona": "Default",
                "permissions": "public",
                "start_paused": "",
                "config": "{}",
                "csrfmiddlewaretoken": csrf_match.group(1),
            },
            timeout=10,
            allow_redirects=False,
        )
        assert response.status_code in (302, 303), response.text

        deadline = time.time() + 180
        while time.time() < deadline:
            depth_counts = get_depth_counts(tmp_path)
            if (
                depth_counts.get(0, 0) >= 1
                and depth_counts.get(1, 0) >= len(recursive_test_site["child_urls"])
                and depth_counts.get(2, 0) >= len(recursive_test_site["deep_urls"])
            ):
                break
            time.sleep(2)
        else:
            raise AssertionError(f"timed out waiting for depth=2 crawl, got depth counts {get_depth_counts(tmp_path)}")

        with use_archivebox_db(tmp_path):
            depth_counts = get_depth_counts(tmp_path)
            crawl_obj = Crawl.objects.order_by("-created_at").first()
            crawl = (crawl_obj.max_depth, crawl_obj.tags_str, crawl_obj.notes, crawl_obj.config) if crawl_obj else None
            snapshot_rows = list(Snapshot.objects.order_by("depth", "url").values_list("url", "depth", "status", "parent_snapshot_id"))
            archive_results = list(
                ArchiveResult.objects.order_by("plugin", "status").values_list("plugin", "status", "output_files", "output_size"),
            )

        assert crawl[:3] == (2, "web-depth-two", "created from running-server web ui")
        assert (crawl[3] or {})["CRAWL_MAX_URLS"] == 20
        assert depth_counts.get(0, 0) >= 1
        assert depth_counts.get(1, 0) >= len(recursive_test_site["child_urls"])
        assert depth_counts.get(2, 0) >= len(recursive_test_site["deep_urls"])
        assert max(depth_counts) <= 2
        assert set(recursive_test_site["child_urls"]).issubset({url for url, depth, _status, _parent in snapshot_rows if depth == 1})
        assert set(recursive_test_site["deep_urls"]).issubset({url for url, depth, _status, _parent in snapshot_rows if depth == 2})

        result_statuses = [(plugin, status) for plugin, status, _files, _size in archive_results]
        assert ("wget", "succeeded") in result_statuses
        assert any(plugin.endswith("parse_html_urls") and status == "succeeded" for plugin, status in result_statuses)
        assert len([status for _plugin, status, _files, _size in archive_results if status == "failed"]) <= 2
        assert list((tmp_path / "archive/users").rglob("snapshots/**/parse_html_urls/**/urls.jsonl"))
        assert list((tmp_path / "archive/users").rglob("snapshots/**/wget/**/*.html"))

        progress = session.get(
            f"http://127.0.0.1:{port}/progress.json",
            headers={"Host": f"admin.archivebox.localhost:{port}"},
            timeout=10,
        )
        assert progress.status_code == 200
        assert "active_crawls" in progress.json()

        index_page = requests.get(
            f"http://web.archivebox.localhost:{port}/",
            timeout=10,
        )
        assert index_page.status_code == 200
        assert recursive_test_site["root_url"] in index_page.text

        snapshot_admin = session.get(
            f"http://admin.archivebox.localhost:{port}/admin/core/snapshot/",
            timeout=10,
        )
        assert snapshot_admin.status_code == 200
        assert recursive_test_site["root_url"] in snapshot_admin.text
    finally:
        stop_server(tmp_path)
