#!/usr/bin/env python3
"""Real user-facing archive flows against live URLs."""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl
from archivebox.machine.models import Process
from archivebox.tests.test_orm_helpers import use_archivebox_db

from .conftest import _find_system_browser

pytestmark = pytest.mark.django_db(transaction=True)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"PID {pid} is still alive")


def _cleanup_process_group(group_pid: int | None, *child_pids: int | None) -> None:
    if group_pid and _pid_is_alive(group_pid):
        try:
            os.killpg(group_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                os.kill(group_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for pid in child_pids:
        if pid and _pid_is_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _live_exit_env(data_dir, *, plugins_root=None, extra=None):
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(data_dir),
            "USE_COLOR": "false",
            "SHOW_PROGRESS": "false",
            "SAVE_ARCHIVEDOTORG": "false",
            "SAVE_FAVICON": "false",
            "SAVE_HEADERS": "false",
            "SAVE_TITLE": "false",
            "SAVE_READABILITY": "false",
            "SAVE_SINGLEFILE": "false",
            "SAVE_MERCURY": "false",
            "SAVE_SCREENSHOT": "false",
            "SAVE_PDF": "false",
            "SAVE_DOM": "false",
            "SAVE_GIT": "false",
            "SAVE_YTDLP": "false",
            "TIMEOUT": "60",
            "WGET_TIMEOUT": "45",
            "CRAWL_MAX_CONCURRENT_SNAPSHOTS": "1",
            "PARSE_HTML_URLS_ENABLED": "true",
            "PARSE_DOM_OUTLINKS_ENABLED": "false",
            "SEARCH_BACKEND_ENGINE": "sqlite",
        },
    )
    if plugins_root is not None:
        env["ABX_PLUGINS_DIR"] = str(plugins_root)
    if extra:
        env.update(extra)
    return env


def _wait_for_port(host: str, port: int, *, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"server did not listen on {host}:{port}")


def _wait_for_log(log_path: Path, text: str, *, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    content = ""
    while time.time() < deadline:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace")
            if text in content:
                return content
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {text!r} in {log_path}:\n{content}")


def _wait_for_log_count(log_path: Path, text: str, count: int, *, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    content = ""
    while time.time() < deadline:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace")
            if content.count(text) >= count:
                return content
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {count} occurrences of {text!r} in {log_path}:\n{content}")


def _wait_for_pid_to_disappear(pid: int, *, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"PID {pid} is still running")


def _wait_for_process(predicate, *, timeout: float = 20.0):
    deadline = time.time() + timeout
    last_seen = []
    while time.time() < deadline:
        last_seen = []
        for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                command = " ".join(cmdline)
                last_seen.append(f"{proc.info.get('pid')} {proc.info.get('ppid')} {command}")
                if predicate(proc, command):
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        time.sleep(0.2)
    raise AssertionError("No matching live process found. Last seen:\n" + "\n".join(last_seen[-50:]))


def _supervisor_pid_from_log(log_path: Path) -> int:
    content = log_path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"Supervisord connected \(pid=(\d+)\)", content)
    assert matches, content
    return int(matches[-1])


def _worker_pid_from_log(log_path: Path, worker_name: str) -> int:
    content = log_path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(rf"Worker {re.escape(worker_name)}: started RUNNING \(pid (\d+),", content)
    assert matches, content
    return int(matches[-1])


def _pgrep_data_dir(data_dir) -> list[str]:
    result = subprocess.run(["pgrep", "-af", str(data_dir)], capture_output=True, text=True, timeout=5)
    lines = [line for line in result.stdout.splitlines() if "pgrep -af" not in line]

    # A foreground ArchiveBox process can be killed with SIGKILL before Python
    # cleanup runs. Supervisord's command line only points at its generated
    # config file, so catch orphaned supervisors by resolving pidfiles whose
    # configs still reference this real test DATA_DIR.
    for runtime_root in (Path("/tmp/archivebox"), Path(data_dir) / "tmp"):
        for config_path in runtime_root.glob("*/supervisord.conf"):
            try:
                config_text = config_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if str(data_dir) not in config_text:
                continue
            pid_path = config_path.with_name("supervisord.pid")
            try:
                pid = int(pid_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if not _pid_is_alive(pid):
                continue
            ps_line = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid=,ppid=,command="],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if ps_line:
                lines.append(ps_line)

    return sorted(set(lines))


def _assert_no_processes_for_data_dir(data_dir, *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    remaining: list[str] = []
    while time.time() < deadline:
        remaining = _pgrep_data_dir(data_dir)
        if not remaining:
            return
        time.sleep(0.25)
    raise AssertionError("processes still reference test DATA_DIR:\n" + "\n".join(remaining))


def _kill_processes_for_data_dir(data_dir) -> None:
    for line in _pgrep_data_dir(data_dir):
        try:
            pid = int(line.split(None, 1)[0])
        except (IndexError, ValueError):
            continue
        if pid != os.getpid():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _start_server(data_dir, *, port: int, log_name: str, env: dict[str, str] | None = None) -> tuple[subprocess.Popen[str], Path]:
    log_path = data_dir / log_name
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "archivebox", "server", f"127.0.0.1:{port}"],
        cwd=data_dir,
        env=env or _live_exit_env(data_dir),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log.close()
    _wait_for_port("127.0.0.1", port)
    _wait_for_log(log_path, "Tailing worker logs", timeout=30.0)
    return proc, log_path


def _stop_process(proc: subprocess.Popen[str], sig=signal.SIGTERM, *, timeout: float = 15.0) -> str:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, OSError):
            try:
                os.kill(proc.pid, sig)
            except ProcessLookupError:
                pass
    try:
        stdout, _stderr = proc.communicate(timeout=timeout)
        return stdout or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout, _stderr = proc.communicate(timeout=5)
        return stdout or ""


def _write_slow_snapshot_plugin(plugins_root, marker_dir):
    plugin_dir = plugins_root / "slow_exit"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    hook = plugin_dir / "on_Snapshot__09_slow_exit.finite.bg.sh"
    hook.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"marker_dir={str(marker_dir)!r}",
                'mkdir -p "$marker_dir"',
                'echo $$ >> "$marker_dir/hook-pids.txt"',
                'touch "$marker_dir/hook-started"',
                "trap 'touch \"$marker_dir/hook-stopped\"; exit 0' TERM INT HUP",
                "while true; do sleep 1; done",
                "",
            ],
        ),
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return plugin_dir


def _wait_for_crawl_state(data_dir, predicate, *, timeout: float = 30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with use_archivebox_db(data_dir):
            last = {
                "crawls": list(Crawl.objects.order_by("created_at").values("id", "status", "retry_at")),
                "snapshots": list(Snapshot.objects.order_by("created_at").values("id", "url", "status", "retry_at")),
                "results": list(ArchiveResult.objects.order_by("created_at").values("id", "plugin", "status")),
                "processes": list(Process.objects.order_by("created_at").values("id", "process_type", "status", "pid", "cmd")),
            }
        if predicate(last):
            return last
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for crawl state, last={last}")


def _wait_for_hook_runs(marker_dir: Path, count: int, *, timeout: float = 45.0) -> list[int]:
    pid_file = marker_dir / "hook-pids.txt"
    deadline = time.time() + timeout
    pids: list[int] = []
    while time.time() < deadline:
        if pid_file.exists():
            pids = [int(line.strip()) for line in pid_file.read_text().splitlines() if line.strip()]
            if len(pids) >= count:
                return pids
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {count} slow hook runs, got {pids}")


def _start_live_add(
    data_dir,
    env,
    *,
    url="https://example.com",
    max_urls="2",
    log_name="archivebox-add.log",
) -> tuple[subprocess.Popen[str], Path]:
    log_path = data_dir / log_name
    log = log_path.open("w", encoding="utf-8")
    urls = [url] if isinstance(url, str) else url
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "archivebox",
            "add",
            "--depth=1",
            f"--max-urls={max_urls}",
            "--crawl-max-size=50mb",
            "--plugins=wget,parse_html_urls,slow_exit",
            *urls,
        ],
        cwd=data_dir,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log.close()
    return proc, log_path


@pytest.mark.timeout(90)
def test_cli_run_signal_cleans_background_hook_process_group(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    plugins_root = tmp_path / "runtime_plugins"
    plugin_dir = plugins_root / "cancel_group"
    plugin_dir.mkdir(parents=True)
    daemon_hook = plugin_dir / "on_CrawlSetup__10_daemon.daemon.bg.sh"
    foreground_hook = plugin_dir / "on_CrawlSetup__20_foreground.sh"
    daemon_hook.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'test_dir="${LEAK_TEST_DIR:?}"',
                "sleep 600 &",
                'echo $$ > "$test_dir/daemon.pid"',
                'echo $! > "$test_dir/daemon-child.pid"',
                'echo ready > "$test_dir/daemon.ready"',
                "trap 'echo cleaned > \"$test_dir/daemon.cleaned\"; exit 0' TERM INT",
                "wait",
                "",
            ],
        ),
    )
    foreground_hook.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'test_dir="${LEAK_TEST_DIR:?}"',
                'echo $$ > "$test_dir/foreground.pid"',
                'echo ready > "$test_dir/foreground.ready"',
                "trap 'echo cleaned > \"$test_dir/foreground.cleaned\"; exit 0' TERM INT",
                "while true; do sleep 1; done",
                "",
            ],
        ),
    )
    daemon_hook.chmod(0o755)
    foreground_hook.chmod(0o755)

    leak_test_dir = tmp_path / "leak-check"
    leak_test_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "ABX_PLUGINS_DIR": str(plugins_root),
            "LEAK_TEST_DIR": str(leak_test_dir),
            "PLUGINS": "cancel_group",
            "TIMEOUT": "30",
            "USE_COLOR": "false",
            "SHOW_PROGRESS": "false",
        },
    )

    create_result = subprocess.run(
        [sys.executable, "-m", "archivebox", "crawl", "create", "https://example.com"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert create_result.returncode == 0, create_result.stderr or create_result.stdout
    crawl_records = [json.loads(line) for line in create_result.stdout.splitlines() if line.strip().startswith("{")]
    crawl_id = next(record["id"] for record in crawl_records if record.get("type") == "Crawl")

    daemon_pid: int | None = None
    daemon_child_pid: int | None = None
    foreground_pid: int | None = None
    run_process = subprocess.Popen(
        [sys.executable, "-m", "archivebox", "run", f"--crawl-id={crawl_id}"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if (leak_test_dir / "daemon.ready").exists() and (leak_test_dir / "foreground.ready").exists():
                break
            if run_process.poll() is not None:
                output = run_process.communicate(timeout=1)[0]
                raise AssertionError(f"archivebox run exited before hooks were ready:\n{output}")
            time.sleep(0.05)
        assert (leak_test_dir / "daemon.ready").exists()
        assert (leak_test_dir / "foreground.ready").exists()

        daemon_pid = int((leak_test_dir / "daemon.pid").read_text().strip())
        daemon_child_pid = int((leak_test_dir / "daemon-child.pid").read_text().strip())
        foreground_pid = int((leak_test_dir / "foreground.pid").read_text().strip())
        assert _pid_is_alive(daemon_pid)
        assert _pid_is_alive(daemon_child_pid)
        assert _pid_is_alive(foreground_pid)

        run_process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        if run_process.poll() is None:
            run_process.send_signal(signal.SIGTERM)
        output = run_process.communicate(timeout=20)[0]
        assert "Runner error" not in output

        _wait_for_pid_exit(daemon_pid)
        _wait_for_pid_exit(daemon_child_pid)
        _wait_for_pid_exit(foreground_pid)
        assert (leak_test_dir / "daemon.cleaned").read_text().strip() == "cleaned"
        assert (leak_test_dir / "foreground.cleaned").read_text().strip() == "cleaned"
    finally:
        if run_process.poll() is None:
            try:
                os.killpg(run_process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            run_process.communicate(timeout=5)
        _cleanup_process_group(daemon_pid, daemon_child_pid)
        _cleanup_process_group(foreground_pid)


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("stop_signal", "expected_notice"),
    [
        (signal.SIGHUP, "Got SIGHUP"),
        (signal.SIGINT, "Got SIGINT"),
        (signal.SIGTERM, "Got SIGTERM"),
        (signal.SIGKILL, None),
    ],
)
def test_live_server_signal_exit_and_resume_uses_existing_supervisor_state(tmp_path, process, stop_signal, expected_notice):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    env = _live_exit_env(tmp_path)
    port = _free_port()
    server = None
    resumed = None
    try:
        server, server_log = _start_server(tmp_path, port=port, log_name=f"server-{stop_signal.name}.log", env=env)

        os.kill(server.pid, stop_signal)
        try:
            server.wait(timeout=20 if stop_signal != signal.SIGKILL else 5)
        except subprocess.TimeoutExpired:
            os.kill(server.pid, signal.SIGKILL)
            server.wait(timeout=5)

        if expected_notice:
            log_text = server_log.read_text(encoding="utf-8", errors="replace")
            assert expected_notice in log_text
            assert "ArchiveBox server shut down gracefully" in log_text
            _assert_no_processes_for_data_dir(tmp_path, timeout=12)

        resumed, resumed_log = _start_server(tmp_path, port=port, log_name=f"server-{stop_signal.name}-resumed.log", env=env)
        status = subprocess.run(
            [sys.executable, "-m", "archivebox", "status"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert status.returncode == 0, status.stderr or status.stdout

        os.kill(resumed.pid, signal.SIGTERM)
        resumed.wait(timeout=20)
        resumed_text = resumed_log.read_text(encoding="utf-8", errors="replace")
        assert "Got SIGTERM" in resumed_text
        assert "ArchiveBox server shut down gracefully" in resumed_text
        _assert_no_processes_for_data_dir(tmp_path, timeout=12)
    finally:
        for proc in (server, resumed):
            if proc is not None and proc.poll() is None:
                _stop_process(proc, signal.SIGKILL)
        _kill_processes_for_data_dir(tmp_path)


@pytest.mark.timeout(180)
def test_live_daemonized_server_keeps_supervisord_owned_by_archivebox_parent(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    env = _live_exit_env(tmp_path)
    port = _free_port()
    bind_url = f"http://127.0.0.1:{port}"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "archivebox", "server", "--daemonize", f"127.0.0.1:{port}"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        _wait_for_port("127.0.0.1", port, timeout=30)

        server_process = _wait_for_process(
            lambda _proc, command: "archivebox" in command and " server " in f" {command} " and bind_url.replace("http://", "") in command,
        )
        supervisord = _wait_for_process(
            lambda proc, command: proc.ppid() == server_process.pid and "supervisord" in command,
        )
        _wait_for_process(
            lambda proc, command: proc.ppid() == supervisord.pid and "supervisord_watchdog" in command,
        )

        os.kill(server_process.pid, signal.SIGKILL)
        _wait_for_pid_to_disappear(server_process.pid, timeout=10)
        _wait_for_pid_to_disappear(supervisord.pid, timeout=20)
        _assert_no_processes_for_data_dir(tmp_path, timeout=12)
    finally:
        _kill_processes_for_data_dir(tmp_path)
        _assert_no_processes_for_data_dir(tmp_path, timeout=12)


@pytest.mark.timeout(240)
def test_live_second_server_takes_over_existing_server_process(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    env = _live_exit_env(tmp_path)
    port = _free_port()
    first = None
    second = None
    try:
        first, first_log = _start_server(tmp_path, port=port, log_name="server-first.log", env=env)
        second, second_log = _start_server(tmp_path, port=port, log_name="server-second.log", env=env)

        assert first.poll() is None
        first_text = first_log.read_text(encoding="utf-8", errors="replace")
        second_text = second_log.read_text(encoding="utf-8", errors="replace")
        assert "A newer archivebox process took over the runtime stack" in first_text
        assert "Starting orchestrator, server" in second_text

        status = subprocess.run(
            [sys.executable, "-m", "archivebox", "status"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert status.returncode == 0, status.stderr or status.stdout

        first_resumes = first_log.read_text(encoding="utf-8", errors="replace").count("Other newer archivebox process")
        _stop_process(second, signal.SIGTERM)
        second = None
        _wait_for_log_count(first_log, "Other newer archivebox process", first_resumes + 1, timeout=35)
        assert first.poll() is None
    finally:
        if second is not None and second.poll() is None:
            _stop_process(second, signal.SIGTERM)
        if first is not None and first.poll() is None:
            _stop_process(first, signal.SIGKILL)
        _kill_processes_for_data_dir(tmp_path)
        _assert_no_processes_for_data_dir(tmp_path, timeout=12)


@pytest.mark.timeout(420)
def test_live_repeated_server_startups_take_over_cleanly(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    env = _live_exit_env(tmp_path)
    port = _free_port()
    servers: list[subprocess.Popen[str]] = []
    server_pids: list[int] = []
    daphne_pids: list[int] = []
    runner_pids: list[int] = []
    try:
        for index in range(5):
            server, log_path = _start_server(tmp_path, port=port, log_name=f"server-chaos-{index}.log", env=env)
            servers.append(server)
            server_pids.append(server.pid)
            daphne_pids.append(_worker_pid_from_log(log_path, "worker_daphne"))
            runner_pids.append(_worker_pid_from_log(log_path, "worker_runner"))

            if index > 0:
                previous_server = servers[index - 1]
                previous_log = (tmp_path / f"server-chaos-{index - 1}.log").read_text(encoding="utf-8", errors="replace")
                current_log = log_path.read_text(encoding="utf-8", errors="replace")
                assert previous_server.poll() is None
                assert _pid_is_alive(server_pids[index - 1])
                assert "A newer archivebox process took over the runtime stack" in previous_log
                assert "Starting orchestrator, server" in current_log
                _wait_for_pid_to_disappear(daphne_pids[index - 1], timeout=15)
                _wait_for_pid_to_disappear(runner_pids[index - 1], timeout=15)

            status = subprocess.run(
                [sys.executable, "-m", "archivebox", "status"],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert status.returncode == 0, status.stderr or status.stdout
            time.sleep(5)

        assert servers[-1].poll() is None
        assert all(server.poll() is None for server in servers)
        listener = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert listener.returncode == 0, listener.stderr or listener.stdout
        assert listener.stdout.count(f":{port} (LISTEN)") == 1

        previous_log_path = tmp_path / "server-chaos-3.log"
        previous_takeovers = previous_log_path.read_text(encoding="utf-8", errors="replace").count(
            "Other newer archivebox process",
        )
        _stop_process(servers[-1], signal.SIGTERM)
        _wait_for_log_count(previous_log_path, "Other newer archivebox process", previous_takeovers + 1, timeout=35)
        assert servers[3].poll() is None
    finally:
        for server in reversed(servers):
            if server.poll() is None:
                _stop_process(server, signal.SIGTERM)
        _kill_processes_for_data_dir(tmp_path)
        _assert_no_processes_for_data_dir(tmp_path, timeout=12)


@pytest.mark.timeout(240)
def test_live_servers_in_different_data_dirs_do_not_interfere(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    first_data_dir = tmp_path
    second_data_dir = tmp_path.parent / f"{tmp_path.name}-second"
    second_data_dir.mkdir()
    second_env = _live_exit_env(second_data_dir)
    second_init = subprocess.run(
        [sys.executable, "-m", "archivebox", "init"],
        cwd=second_data_dir,
        env=second_env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert second_init.returncode == 0, second_init.stderr or second_init.stdout

    first_port = _free_port()
    second_port = _free_port()
    first = None
    second = None
    first_resumed = None
    try:
        first = _start_server(first_data_dir, port=first_port, log_name="server-first-data-dir.log", env=_live_exit_env(first_data_dir))[0]
        second = _start_server(second_data_dir, port=second_port, log_name="server-second-data-dir.log", env=second_env)[0]

        first_status = subprocess.run(
            [sys.executable, "-m", "archivebox", "status"],
            cwd=first_data_dir,
            env=_live_exit_env(first_data_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        second_status = subprocess.run(
            [sys.executable, "-m", "archivebox", "status"],
            cwd=second_data_dir,
            env=second_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert first_status.returncode == 0, first_status.stderr or first_status.stdout
        assert second_status.returncode == 0, second_status.stderr or second_status.stdout

        _stop_process(first, signal.SIGTERM)
        first = None
        assert second.poll() is None, "stopping one DATA_DIR server must not stop another DATA_DIR server"

        first_resumed = _start_server(
            first_data_dir,
            port=first_port,
            log_name="server-first-data-dir-resumed.log",
            env=_live_exit_env(first_data_dir),
        )[0]
        assert second.poll() is None, "restarting one DATA_DIR server must not take over another DATA_DIR supervisor"
    finally:
        for proc in (first, first_resumed, second):
            if proc is not None and proc.poll() is None:
                _stop_process(proc, signal.SIGTERM)
        _kill_processes_for_data_dir(first_data_dir)
        _kill_processes_for_data_dir(second_data_dir)
        _assert_no_processes_for_data_dir(first_data_dir, timeout=12)
        _assert_no_processes_for_data_dir(second_data_dir, timeout=12)


@pytest.mark.timeout(420)
def test_live_add_update_jobs_survive_server_and_cli_owner_exits(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    plugins_root = tmp_path / "runtime_plugins"
    marker_dir = tmp_path / "slow-plugin-markers"
    _write_slow_snapshot_plugin(plugins_root, marker_dir)
    env = _live_exit_env(tmp_path, plugins_root=plugins_root)
    port = _free_port()
    server = None
    server2 = None
    server3 = None
    add_proc = None
    add_proc2 = None
    try:
        server, server_log = _start_server(tmp_path, port=port, log_name="server-add-owner-1.log", env=env)
        supervisor_pid_before = _supervisor_pid_from_log(server_log)

        update_result = subprocess.run(
            [sys.executable, "-m", "archivebox", "update", "--index-only", "--batch-size=10"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert update_result.returncode == 0, update_result.stderr or update_result.stdout
        assert server.poll() is None
        assert _pid_is_alive(supervisor_pid_before)
        assert _supervisor_pid_from_log(server_log) == supervisor_pid_before

        add_proc, add_log = _start_live_add(
            tmp_path,
            env,
            url=["https://example.com", "https://blog.sweeting.me"],
            log_name="archivebox-add-1.log",
        )
        _wait_for_hook_runs(marker_dir, 1)
        _wait_for_crawl_state(
            tmp_path,
            lambda state: any(snapshot["status"] == Snapshot.StatusChoices.STARTED for snapshot in state["snapshots"]),
            timeout=30,
        )

        os.kill(server.pid, signal.SIGTERM)
        server.wait(timeout=20)
        assert add_proc.poll() is None, "foreground add should keep owning its crawl after the server exits"
        assert "Got SIGTERM" in server_log.read_text(encoding="utf-8", errors="replace")

        server2, _server2_log = _start_server(tmp_path, port=port, log_name="server-add-owner-2.log", env=env)
        os.killpg(add_proc.pid, signal.SIGTERM)
        add_proc.wait(timeout=30)
        add_output = add_log.read_text(encoding="utf-8", errors="replace")
        assert "Runner error" not in add_output
        _wait_for_hook_runs(marker_dir, 2, timeout=60)
        _wait_for_crawl_state(
            tmp_path,
            lambda state: (
                any(crawl["status"] in (Crawl.StatusChoices.STARTED, Crawl.StatusChoices.QUEUED) for crawl in state["crawls"])
                and any(result["plugin"] == "slow_exit" for result in state["results"])
            ),
            timeout=30,
        )

        add_proc2, add_log2 = _start_live_add(
            tmp_path,
            env,
            url="https://example.com/?exit-resume=2",
            max_urls="1",
            log_name="archivebox-add-2.log",
        )
        _wait_for_hook_runs(marker_dir, 3, timeout=60)
        os.killpg(add_proc2.pid, signal.SIGTERM)
        os.kill(server2.pid, signal.SIGTERM)
        add_proc2.wait(timeout=30)
        add_output2 = add_log2.read_text(encoding="utf-8", errors="replace")
        server2.wait(timeout=20)
        assert "Runner error" not in add_output2

        server3, _server3_log = _start_server(tmp_path, port=port, log_name="server-add-owner-3.log", env=env)
        _wait_for_hook_runs(marker_dir, 4, timeout=70)

        with use_archivebox_db(tmp_path):
            crawls = list(Crawl.objects.order_by("created_at").values_list("status", "retry_at"))
            snapshots = list(Snapshot.objects.order_by("created_at").values_list("url", "status", "retry_at"))
            failed_results = list(
                ArchiveResult.objects.filter(status=ArchiveResult.StatusChoices.FAILED).values_list("plugin", "output_str"),
            )
        assert crawls
        assert snapshots
        assert not failed_results
    finally:
        for proc in (add_proc, add_proc2, server, server2, server3):
            if proc is not None and proc.poll() is None:
                _stop_process(proc, signal.SIGTERM, timeout=10)
        _kill_processes_for_data_dir(tmp_path)
        _assert_no_processes_for_data_dir(tmp_path, timeout=12)


@pytest.mark.timeout(180)
def test_cli_add_real_urls_with_options_writes_inspectable_outputs(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    wget_urls = [
        "https://example.com",
        "https://pirate.github.io/stress-tests/challenge.html",
    ]
    chrome_url = "https://example.com/?archivebox-chrome-flow=1"
    env = os.environ.copy()
    env.pop("CHROME_BINARY", None)
    env.update(
        {
            "USE_COLOR": "false",
            "SHOW_PROGRESS": "false",
            "TIMEOUT": "60",
            "SAVE_WGET": "true",
            "SAVE_HEADERS": "false",
            "SAVE_TITLE": "false",
            "SAVE_READABILITY": "false",
            "SAVE_SINGLEFILE": "false",
            "SAVE_MERCURY": "false",
            "SAVE_SCREENSHOT": "false",
            "SAVE_PDF": "false",
            "SAVE_DOM": "false",
            "SAVE_ARCHIVEDOTORG": "false",
            "SAVE_GIT": "false",
            "SAVE_YTDLP": "false",
            "SAVE_FAVICON": "false",
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archivebox",
            "add",
            "--depth=0",
            "--max-urls=2",
            "--crawl-max-size=10mb",
            "--tag=real-flow,challenge",
            "--parser=url_list",
            "--plugins=wget",
            *wget_urls,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    chrome_env = env | {
        "SAVE_WGET": "false",
        "SAVE_HEADERS": "true",
        "SAVE_TITLE": "true",
        "CHROME_HEADLESS": "true",
        "CHROME_SANDBOX": "false",
        "CHROME_ISOLATION": "snapshot",
    }
    system_browser = _find_system_browser()
    if system_browser:
        chrome_env["CHROME_BINARY"] = str(system_browser)
    else:
        install_result = subprocess.run(
            [sys.executable, "-m", "archivebox", "install", "chrome"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=chrome_env,
            timeout=600,
        )
        assert install_result.returncode == 0, install_result.stderr or install_result.stdout
    chrome_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archivebox",
            "add",
            "--depth=0",
            "--max-urls=1",
            "--crawl-max-size=10mb",
            "--tag=chrome-flow",
            "--parser=url_list",
            "--plugins=chrome,wget,headers,title",
            chrome_url,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=chrome_env,
        timeout=180,
    )
    assert chrome_result.returncode == 0, chrome_result.stderr or chrome_result.stdout

    list_result = subprocess.run(
        [sys.executable, "-m", "archivebox", "list", "--tag=real-flow"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert list_result.returncode == 0, list_result.stderr or list_result.stdout
    listed = [json.loads(line) for line in list_result.stdout.splitlines() if line.strip()]
    assert {item["url"] for item in listed} >= set(wget_urls)

    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.order_by("-created_at").values_list("max_depth", "tags_str", "config").first()
        real_flow_crawl = Crawl.objects.filter(tags_str="real-flow,challenge").values_list("max_depth", "tags_str", "config").first()
        snapshots = list(Snapshot.objects.order_by("url").values_list("id", "url", "depth", "status", "title"))
        archive_results = list(
            ArchiveResult.objects.select_related("snapshot")
            .order_by("snapshot__url", "plugin")
            .values_list("snapshot__url", "plugin", "status", "output_files", "output_size", "output_str"),
        )
        processes = list(Process.objects.filter(process_type="hook").values_list("process_type", "status", "exit_code", "pwd", "cmd"))

    assert real_flow_crawl is not None
    assert real_flow_crawl[0] == 0
    assert real_flow_crawl[1] == "real-flow,challenge"
    real_flow_config = real_flow_crawl[2] or {}
    assert real_flow_config["CRAWL_MAX_URLS"] == 2
    assert real_flow_config["CRAWL_MAX_SIZE"] == 10 * 1024 * 1024
    assert real_flow_config.get("SNAPSHOT_MAX_SIZE", 0) == 0
    assert "wget" in real_flow_config["PLUGINS"]
    assert crawl is not None
    assert crawl[1] == "chrome-flow"
    assert "wget,headers,title" in json.dumps(crawl[2] or {})

    snapshot_urls = {url for _id, url, _depth, _status, _title in snapshots}
    assert snapshot_urls >= {*wget_urls, chrome_url}
    assert all(depth == 0 for _id, _url, depth, _status, _title in snapshots)

    by_url_plugin = {(url, plugin): status for url, plugin, status, _files, _size, _output in archive_results}
    assert by_url_plugin[("https://example.com", "wget")] == "succeeded"
    assert by_url_plugin[("https://pirate.github.io/stress-tests/challenge.html", "wget")] == "succeeded"
    assert (chrome_url, "headers") in by_url_plugin
    assert (chrome_url, "title") in by_url_plugin
    failed_results = [(url, plugin, output) for url, plugin, status, _files, _size, output in archive_results if status == "failed"]
    assert len(failed_results) <= 2, failed_results

    snapshot_root = tmp_path / "archive/users/system/snapshots"
    html_outputs = [path for path in snapshot_root.rglob("wget/**/*.html") if path.is_file()]
    header_outputs = [path for path in snapshot_root.rglob("headers/**/headers.json") if path.is_file() and path.stat().st_size > 0]
    title_outputs = [path for path in snapshot_root.rglob("title/title.txt") if path.is_file() and path.stat().st_size > 0]
    index_outputs = [path for path in snapshot_root.rglob("index.jsonl") if path.is_file()]
    assert html_outputs
    if by_url_plugin[(chrome_url, "headers")] == "succeeded":
        assert header_outputs
    if by_url_plugin[(chrome_url, "title")] == "succeeded":
        assert title_outputs
        assert any("Example Domain" in path.read_text(errors="ignore") for path in title_outputs)
    assert len(index_outputs) >= len(wget_urls) + 1

    combined_html = "\n".join(path.read_text(errors="ignore") for path in html_outputs)
    assert "Example Domain" in combined_html
    assert "Browser Agent Challenge for AI Browser Drivers" in combined_html

    assert processes
    assert any("wget" in (pwd or "") or "wget" in (cmd or "") for _type, _status, _exit, pwd, cmd in processes)
    assert any("headers" in (pwd or "") or "headers" in (cmd or "") for _type, _status, _exit, pwd, cmd in processes)


@pytest.mark.timeout(180)
def test_cli_recursive_crawl_processes_discovered_html_urls(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    env = os.environ.copy()
    env.update(
        {
            "USE_COLOR": "false",
            "SHOW_PROGRESS": "false",
            "TIMEOUT": "60",
            "SAVE_WGET": "true",
            "SAVE_HEADERS": "false",
            "SAVE_TITLE": "false",
            "SAVE_READABILITY": "false",
            "SAVE_SINGLEFILE": "false",
            "SAVE_MERCURY": "false",
            "SAVE_SCREENSHOT": "false",
            "SAVE_PDF": "false",
            "SAVE_DOM": "false",
            "SAVE_ARCHIVEDOTORG": "false",
            "SAVE_GIT": "false",
            "SAVE_YTDLP": "false",
            "SAVE_FAVICON": "false",
            "PARSE_HTML_URLS_ENABLED": "true",
            "PARSE_DOM_OUTLINKS_ENABLED": "false",
        },
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archivebox",
            "add",
            "--depth=2",
            "--max-urls=2",
            "--crawl-max-size=50mb",
            "--tag=recursive-flow",
            "--parser=url_list",
            "--plugins=wget,parse_html_urls",
            "https://example.com",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.order_by("-created_at").values_list("max_depth", "tags_str", "config").first()
        snapshots = list(Snapshot.objects.order_by("depth", "url").values_list("url", "depth", "status"))
        archive_results = list(
            ArchiveResult.objects.select_related("snapshot")
            .order_by("snapshot__depth", "snapshot__url", "plugin")
            .values_list("snapshot__url", "plugin", "status", "output_files"),
        )

    assert crawl[0] == 2
    assert crawl[1] == "recursive-flow"
    crawl_config = crawl[2] or {}
    assert crawl_config["CRAWL_MAX_URLS"] == 2
    assert crawl_config["CRAWL_MAX_SIZE"] == 50 * 1024 * 1024
    assert crawl_config.get("SNAPSHOT_MAX_SIZE", 0) == 0
    assert ("https://example.com", 0, "sealed") in snapshots
    assert any(url == "https://iana.org/domains/example" and depth == 1 and status == "sealed" for url, depth, status in snapshots)

    by_url_plugin = {(url, plugin): status for url, plugin, status, _files in archive_results}
    assert by_url_plugin[("https://example.com", "wget")] == "succeeded"
    assert by_url_plugin[("https://example.com", "parse_html_urls")] == "succeeded"
    assert by_url_plugin[("https://iana.org/domains/example", "wget")] == "succeeded"

    urls_outputs = list((tmp_path / "archive/users/system/snapshots").rglob("parse_html_urls/urls.jsonl"))
    assert urls_outputs
    assert any("https://iana.org/domains/example" in path.read_text() for path in urls_outputs)
