import os

import pytest
import requests
from asgiref.sync import async_to_sync

from archivebox.tests.conftest import ADMIN_TEST_HOST


pytestmark = pytest.mark.django_db


def test_opencode_disabled_route_does_not_start_server(client, monkeypatch):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": False})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: pytest.fail("opencode should not start when disabled"))

    response = client.get("/admin/agent", HTTP_HOST=ADMIN_TEST_HOST)

    assert response.status_code == 404


def test_opencode_agent_requires_superuser(client, db, monkeypatch, django_user_model):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: pytest.fail("opencode should not start before auth passes"))

    response = client.get("/admin/agent", HTTP_HOST=ADMIN_TEST_HOST)
    assert response.status_code == 302
    assert "/admin/login/" in response.headers["Location"]

    user = django_user_model.objects.create_user(username="regular", password="testpassword")
    client.force_login(user)
    response = client.get("/admin/agent", HTTP_HOST=ADMIN_TEST_HOST)
    assert response.status_code == 403


def test_opencode_agent_superuser_gets_wrapper(admin_client, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: (True, ""))

    response = admin_client.get("/admin/agent", HTTP_HOST=ADMIN_TEST_HOST)

    assert response.status_code == 200
    assert b'<iframe src="/admin/agent/opencode/' in response.content
    assert b'/session"' in response.content
    assert b'id="header"' in response.content
    assert b'id="progress-monitor"' in response.content


def test_opencode_proxy_blocks_cross_origin_mutation(admin_client, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: pytest.fail("opencode should not start before origin check passes"))

    response = admin_client.post(
        "/admin/agent/opencode/session",
        data=b"{}",
        content_type="application/json",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_ORIGIN="https://evil.example",
    )

    assert response.status_code == 403


def test_opencode_proxy_allows_same_origin_fetch_metadata(admin_client, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: (True, ""))

    def fake_request(method, url, **kwargs):
        upstream = requests.Response()
        upstream.status_code = 200
        upstream._content = b"{}"
        upstream.headers["Content-Type"] = "application/json"
        return upstream

    monkeypatch.setattr(views.requests, "request", fake_request)

    response = admin_client.post(
        "/admin/agent/opencode/pty/test/connect-token",
        data=b"{}",
        content_type="application/json",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_SEC_FETCH_SITE="same-origin",
    )

    assert response.status_code == 200


def test_opencode_proxy_allows_pty_connect_token_without_origin(admin_client, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: (True, ""))

    def fake_request(method, url, **kwargs):
        upstream = requests.Response()
        upstream.status_code = 200
        upstream._content = b"{}"
        upstream.headers["Content-Type"] = "application/json"
        return upstream

    monkeypatch.setattr(views.requests, "request", fake_request)

    response = admin_client.post(
        "/admin/agent/opencode/pty/test/connect-token",
        data=b"{}",
        content_type="application/json",
        HTTP_HOST=ADMIN_TEST_HOST,
    )

    assert response.status_code == 200


def test_opencode_proxy_blocks_cross_site_fetch_metadata(admin_client, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(
        views,
        "_ensure_opencode",
        lambda settings: pytest.fail("opencode should not start before fetch metadata check passes"),
    )

    response = admin_client.post(
        "/admin/agent/opencode/session",
        data=b"{}",
        content_type="application/json",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_SEC_FETCH_SITE="cross-site",
    )

    assert response.status_code == 403


def test_opencode_project_current_is_seeded_data_project(admin_client, tmp_path, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    workdir = tmp_path / "data"
    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True, "OPENCODE_WORKDIR": str(workdir)})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: (True, ""))

    def fake_request(method, url, **kwargs):
        assert url == "http://127.0.0.1:4096/project/current"
        assert kwargs["params"] == (("directory", str(workdir)),)
        upstream = requests.Response()
        upstream.status_code = 200
        upstream._content = (f'{{"id":"global","worktree":"{workdir.resolve()}","name":"data"}}').encode()
        upstream.headers["Content-Type"] = "application/json"
        return upstream

    monkeypatch.setattr(views.requests, "request", fake_request)

    response = admin_client.get(
        f"/admin/agent/opencode/project/current?directory={workdir}",
        HTTP_HOST=ADMIN_TEST_HOST,
    )

    assert response.status_code == 200
    assert response.json()["id"] == "global"
    assert response.json()["worktree"] == str(workdir.resolve())
    assert response.json()["name"] == "data"


def test_opencode_path_reports_data_as_worktree(admin_client, tmp_path, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    workdir = tmp_path / "data"
    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True, "OPENCODE_WORKDIR": str(workdir)})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: (True, ""))

    def fake_request(method, url, **kwargs):
        assert url == "http://127.0.0.1:4096/path"
        assert kwargs["params"] == (("directory", str(workdir)),)
        upstream = requests.Response()
        upstream.status_code = 200
        upstream._content = (f'{{"directory":"{workdir.resolve()}","worktree":"{workdir.resolve()}"}}').encode()
        upstream.headers["Content-Type"] = "application/json"
        return upstream

    monkeypatch.setattr(views.requests, "request", fake_request)

    response = admin_client.get(
        f"/admin/agent/opencode/path?directory={workdir}",
        HTTP_HOST=ADMIN_TEST_HOST,
    )

    assert response.status_code == 200
    assert response.json()["directory"] == str(workdir.resolve())
    assert response.json()["worktree"] == str(workdir.resolve())


def test_opencode_proxy_does_not_use_basic_auth(admin_client, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: (True, ""))

    def fake_request(method, url, **kwargs):
        assert "Authorization" not in kwargs["headers"]
        upstream = requests.Response()
        upstream.status_code = 200
        upstream._content = b"{}"
        upstream.headers["Content-Type"] = "application/json"
        return upstream

    monkeypatch.setattr(views.requests, "request", fake_request)

    response = admin_client.get("/admin/agent/opencode/global/config", HTTP_HOST=ADMIN_TEST_HOST)

    assert response.status_code == 200


def test_opencode_rewrites_vite_preload_assets():
    from abx_plugins.plugins.opencode import views

    body = b'const BL="modulepreload",UL=function(t){return"/"+t};const icon="/assets/sprite.svg#anthropic"'
    rewritten = views._rewrite_text(body, {"origin": "http://127.0.0.1:4096"}).decode()

    assert 'return"/"+t' not in rewritten
    assert 'return"/admin/agent/opencode/"+t' in rewritten
    assert '"/admin/agent/opencode/assets/sprite.svg#anthropic"' in rewritten


def test_opencode_proxy_streams_sse_without_large_buffer(admin_client, db, monkeypatch):
    from abx_plugins.plugins.opencode import views

    async def fake_event_chunks(request, settings, path):
        assert path == "global/event"
        yield b"data: {}\n\n"

    async def collect(response):
        return b"".join([chunk async for chunk in response.streaming_content])

    monkeypatch.setattr(views, "_machine_config", lambda: {"OPENCODE_ENABLED": True})
    monkeypatch.setattr(views, "_ensure_opencode", lambda settings: (True, ""))
    monkeypatch.setattr(views, "_event_chunks", fake_event_chunks)

    response = admin_client.get("/admin/agent/opencode/global/event", HTTP_HOST=ADMIN_TEST_HOST)
    assert async_to_sync(collect)(response) == b"data: {}\n\n"
    assert response.headers["X-Accel-Buffering"] == "no"


def test_opencode_sse_chunks_are_not_rewritten(monkeypatch):
    from abx_plugins.plugins.opencode import views

    class FakeRequest:
        method = "GET"
        GET = {}
        headers = {}

    class FakeUpstream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_raw(self, chunk_size=None):
            assert chunk_size == 512
            yield b'event: message.part.updated\ndata: {"delta":"a\\nb"}\n\n'

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeUpstream()

    monkeypatch.setattr(views.httpx, "AsyncClient", FakeClient)

    async def collect():
        settings = {"timeout": 30, "origin": "http://127.0.0.1:4096"}
        return b"".join([chunk async for chunk in views._event_chunks(FakeRequest(), settings, "global/event")])

    assert async_to_sync(collect)() == b'event: message.part.updated\ndata: {"delta":"a\\nb"}\n\n'


def test_opencode_starts_without_opening_browser(tmp_path, monkeypatch):
    from abx_plugins.plugins.opencode import views

    captured_cmd = []
    popen_kwargs = {}
    health_checks = iter([False, False, True])

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured_cmd[:] = cmd
        popen_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(views, "_health", lambda settings: next(health_checks))
    monkeypatch.setattr(
        views,
        "_resolve_binary",
        lambda binary, config: (
            "/opt/archivebox/lib/pnpm/packages/opencode/node_modules/.bin/opencode",
            {
                "PATH": "/opt/archivebox/lib/pnpm/packages/opencode/node_modules/.bin",
                "PNPM_HOME": "/opt/archivebox/lib/pnpm/packages/opencode/node_modules/.bin",
                "NODE_PATH": "/opt/archivebox/lib/pnpm/packages/opencode/node_modules",
            },
        ),
    )
    monkeypatch.setattr(views.subprocess, "Popen", fake_popen)

    settings = views._settings({"OPENCODE_WORKDIR": str(tmp_path), "OPENCODE_BINARY": "opencode"})
    ok, error = views._ensure_opencode(settings)

    assert ok, error
    assert captured_cmd[0] == "/opt/archivebox/lib/pnpm/packages/opencode/node_modules/.bin/opencode"
    assert popen_kwargs["cwd"] == tmp_path.resolve()
    assert popen_kwargs["env"]["BROWSER"] == "false"
    assert popen_kwargs["env"]["GIT_CEILING_DIRECTORIES"] == f"{tmp_path.resolve()}{os.pathsep}{tmp_path.parent.resolve()}"
    assert popen_kwargs["env"]["PNPM_HOME"] == "/opt/archivebox/lib/pnpm/packages/opencode/node_modules/.bin"
    assert popen_kwargs["env"]["NODE_PATH"] == "/opt/archivebox/lib/pnpm/packages/opencode/node_modules"
    assert popen_kwargs["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert popen_kwargs["env"]["XDG_DATA_HOME"] == str(tmp_path / "opencode" / "data")


def test_opencode_state_dir_is_separate_from_workdir(tmp_path):
    from abx_plugins.plugins.opencode import views

    workdir = tmp_path / "data"
    settings = views._settings({"OPENCODE_WORKDIR": str(workdir)})
    views._ensure_project_files(settings)

    assert settings["workdir"] == workdir
    assert settings["opencode_dir"] == workdir / "opencode"
    assert settings["config_home"] == workdir / "opencode" / "config"
    assert settings["data_home"] == workdir / "opencode" / "data"
    assert settings["state_home"] == workdir / "opencode" / "state"
    editable_skill = workdir / "opencode" / "SKILL.md"
    loaded_skill = workdir / "opencode" / "config" / "opencode" / "skills" / "archivebox" / "SKILL.md"
    assert editable_skill.exists()
    assert loaded_skill.is_symlink()
    assert loaded_skill.resolve() == editable_skill.resolve()
    assert f"ArchiveBox collection directory: {workdir.resolve()}" in editable_skill.read_text()


def test_opencode_ensures_default_session_with_public_api(tmp_path, monkeypatch):
    from abx_plugins.plugins.opencode import views

    workdir = tmp_path / "data"
    settings = views._settings({"OPENCODE_WORKDIR": str(workdir)})
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]" if url.endswith("/session") else b"{}"
        return response

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"id":"session"}'
        return response

    monkeypatch.setattr(views.requests, "get", fake_get)
    monkeypatch.setattr(views.requests, "post", fake_post)

    views._ensure_default_session(settings)

    assert calls[0] == (
        "GET",
        "http://127.0.0.1:4096/project/current",
        {"params": {"directory": str(workdir.resolve())}, "timeout": 30},
    )
    assert calls[1][0:2] == ("GET", "http://127.0.0.1:4096/session")
    assert calls[1][2]["params"] == {"directory": str(workdir.resolve()), "roots": "true", "limit": 55}
    assert calls[2][0:2] == ("POST", "http://127.0.0.1:4096/session")
    assert calls[2][2]["params"] == {"directory": str(workdir.resolve())}
    assert calls[2][2]["json"] == {}
