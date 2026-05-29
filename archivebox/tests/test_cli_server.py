#!/usr/bin/env python3
"""
Tests for archivebox server command.
Verify server can start (basic smoke tests only, no full server testing).
"""

import os
import asyncio
import json
import signal
import socket
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace


def test_sqlite_connections_use_explicit_30_second_busy_timeout():
    from archivebox.core.settings import SQLITE_CONNECTION_OPTIONS

    assert SQLITE_CONNECTION_OPTIONS["OPTIONS"]["timeout"] == 30
    assert "PRAGMA busy_timeout = 30000;" in SQLITE_CONNECTION_OPTIONS["OPTIONS"]["init_command"]
    assert "PRAGMA journal_mode = WAL;" in SQLITE_CONNECTION_OPTIONS["OPTIONS"]["init_command"]


def test_server_shows_usage_info(tmp_path, process):
    """Test that server command shows usage or starts."""
    os.chdir(tmp_path)

    # Just check that the command is recognized
    # We won't actually start a full server in tests
    result = subprocess.run(
        ["archivebox", "server", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "server" in result.stdout.lower() or "http" in result.stdout.lower()


def test_server_init_flag(tmp_path, process):
    """Test that --init flag runs init before starting server."""
    os.chdir(tmp_path)

    # Check init flag is recognized
    result = subprocess.run(
        ["archivebox", "server", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "--init" in result.stdout or "init" in result.stdout.lower()


def test_runner_worker_uses_current_interpreter():
    """The supervised runner should use the active Python environment, not PATH."""
    from archivebox.workers.supervisord_util import RUNNER_WORKER

    assert RUNNER_WORKER["command"] == f"{sys.executable} -m archivebox run --daemon"


def test_daphne_worker_uses_default_application_close_timeout():
    from archivebox.workers.supervisord_util import SERVER_WORKER

    command = SERVER_WORKER("127.0.0.1", "8000")["command"]

    assert "daphne" in command
    assert "--application-close-timeout=0" not in command


def test_reload_workers_use_current_interpreter_and_supervisord_managed_runner():
    from archivebox.workers.supervisord_util import RUNNER_WATCH_WORKER, RUNSERVER_WORKER

    runserver = RUNSERVER_WORKER("127.0.0.1", "8000", reload=True)
    watcher = RUNNER_WATCH_WORKER("http://127.0.0.1:8000")

    assert runserver["name"] == "worker_runserver"
    assert runserver["command"] == f"{sys.executable} -m archivebox manage runserver 127.0.0.1:8000"
    assert 'ARCHIVEBOX_RUNSERVER="1"' in runserver["environment"]
    assert 'ARCHIVEBOX_AUTORELOAD="1"' in runserver["environment"]
    assert 'ARCHIVEBOX_RUNSERVER_BIND_URL="http://127.0.0.1:8000"' in runserver["environment"]

    assert watcher["name"] == "worker_runner_watch"
    assert watcher["command"] == f"{sys.executable} -m archivebox manage runner_watch --bind-url=http://127.0.0.1:8000"


def _free_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_server_daemon_starts_real_plugin_owned_sonic_worker(archivebox_daemon_server):
    server = archivebox_daemon_server(
        USE_INDEXING_BACKEND="True",
        SEARCH_BACKEND_ENGINE="sqlite",
    )
    state = server.wait_for_workers(("worker_daphne", "worker_sonic", "worker_runner"))

    assert state["worker_daphne"]["statename"] == "RUNNING", state
    assert state["worker_runner"]["statename"] == "RUNNING", state
    assert state["worker_sonic"]["statename"] == "RUNNING", state
    assert "sonic" in state["worker_sonic"]["name"]


def test_sonic_worker_is_disabled_by_real_indexing_config(tmp_path):
    from archivebox.workers.supervisord_util import get_sonic_supervisord_worker_from_plugin

    worker = get_sonic_supervisord_worker_from_plugin(
        SimpleNamespace(
            DATA_DIR=str(tmp_path),
            SEARCH_BACKEND_ENGINE="sqlite",
            USE_INDEXING_BACKEND=False,
            SEARCH_BACKEND_SONIC_HOST_NAME="127.0.0.1",
            SEARCH_BACKEND_SONIC_PORT=_free_port(),
            SEARCH_BACKEND_SONIC_PASSWORD="SecretPassword",
            SONIC_BINARY="sonic",
        ),
    )

    assert worker is None


def test_sonic_daemon_event_handler_accepts_real_running_worker(archivebox_daemon_server):
    from abx_dl.events import ProcessStdoutEvent
    from abx_dl.orchestrator import create_bus
    from archivebox.search.sonic_daemon import register_sonic_daemon_event_handler
    from abx_plugins.plugins.search_backend_sonic.daemon import prepare_sonic_daemon

    sonic_port = _free_port()
    server = archivebox_daemon_server(
        USE_INDEXING_BACKEND="True",
        SEARCH_BACKEND_ENGINE="sqlite",
        SEARCH_BACKEND_SONIC_PORT=str(sonic_port),
    )
    state = server.wait_for_workers(("worker_sonic",))
    assert state["worker_sonic"]["statename"] == "RUNNING", state

    daemon_event = prepare_sonic_daemon(
        SimpleNamespace(
            DATA_DIR=str(server.data_dir),
            SEARCH_BACKEND_ENGINE="sqlite",
            USE_INDEXING_BACKEND=True,
            SEARCH_BACKEND_SONIC_HOST_NAME="127.0.0.1",
            SEARCH_BACKEND_SONIC_PORT=sonic_port,
            SEARCH_BACKEND_SONIC_PASSWORD="SecretPassword",
            SONIC_BINARY="sonic",
        ),
    )

    async def run_test():
        bus = create_bus(name="test_sonic_daemon_event_handler_accepts_real_running_worker")
        try:
            register_sonic_daemon_event_handler(bus)
            event = await bus.emit(
                ProcessStdoutEvent(
                    line=json.dumps(daemon_event.to_record()),
                ),
            ).now()
            await event.event_results_list()
        finally:
            await bus.destroy()

    asyncio.run(run_test())


def test_supervisord_sync_does_not_start_duplicate_sonic_listener(tmp_path, process, db):
    from abx_plugins.plugins.search_backend_sonic.daemon import get_sonic_supervisord_worker
    from archivebox.tests.test_orm_helpers import use_archivebox_db
    from archivebox.workers.supervisord_util import (
        get_or_create_supervisord_process,
        get_worker,
        stop_existing_supervisord_process,
        sync_supervisord_workers,
    )

    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    sonic_port = listener.getsockname()[1]
    worker = get_sonic_supervisord_worker(
        SimpleNamespace(
            DATA_DIR=str(tmp_path),
            SEARCH_BACKEND_ENGINE="sonic",
            USE_INDEXING_BACKEND=True,
            SEARCH_BACKEND_SONIC_HOST_NAME="127.0.0.1",
            SEARCH_BACKEND_SONIC_PORT=sonic_port,
            SEARCH_BACKEND_SONIC_PASSWORD="SecretPassword",
            SONIC_BINARY="sonic",
        ),
    )
    assert worker is not None

    try:
        with use_archivebox_db(tmp_path):
            supervisor = get_or_create_supervisord_process(daemonize=False)
            state = sync_supervisord_workers(supervisor, [(worker, False)], prune=True)
            sonic_state = state["worker_sonic"]
            assert sonic_state["statename"] != "RUNNING", sonic_state
            assert get_worker(supervisor, "worker_sonic")["statename"] != "RUNNING"
    finally:
        listener.close()
        with use_archivebox_db(tmp_path):
            stop_existing_supervisord_process()


def test_supervisord_takeover_stops_all_live_process_rows(tmp_path, process, db):
    import psutil
    from django.utils import timezone

    from archivebox.config import CONSTANTS
    from archivebox.machine.models import Machine, Process
    from archivebox.tests.test_orm_helpers import use_archivebox_db

    assert process.returncode == 0, process.stderr
    env = os.environ.copy()
    env.update({"DATA_DIR": str(tmp_path), "USE_COLOR": "False", "SHOW_PROGRESS": "False"})
    procs = []
    try:
        for _index in range(2):
            proc = subprocess.Popen(
                [sys.executable, "-m", "archivebox", "run", "--daemon"],
                cwd=tmp_path,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            procs.append(proc)
            started_at = datetime.fromtimestamp(psutil.Process(proc.pid).create_time(), tz=timezone.get_current_timezone())
            with use_archivebox_db(tmp_path):
                Process.objects.create(
                    machine=Machine.current(),
                    process_type=Process.TypeChoices.SUPERVISORD,
                    worker_type="supervisord",
                    pwd=str(CONSTANTS.DATA_DIR),
                    cmd=[],
                    pid=proc.pid,
                    started_at=started_at,
                    status=Process.StatusChoices.RUNNING,
                )

        with use_archivebox_db(tmp_path):
            from archivebox.workers.supervisord_util import stop_existing_supervisord_process

            stop_existing_supervisord_process()

        for proc in procs:
            proc.wait(timeout=10)
        with use_archivebox_db(tmp_path):
            assert not Process.objects.filter(
                process_type=Process.TypeChoices.SUPERVISORD,
                status=Process.StatusChoices.RUNNING,
                pwd=str(CONSTANTS.DATA_DIR),
            ).exists()
    finally:
        for proc in procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
