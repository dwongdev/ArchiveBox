import signal

import pytest

from archivebox.cli import archivebox_run
from archivebox.core import shutdown_util


def test_foreground_shutdown_second_signal_exits_immediately(monkeypatch):
    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(shutdown_util.os, "_exit", fake_exit)

    with shutdown_util.foreground_shutdown_signals() as state:
        handler = signal.getsignal(signal.SIGTERM)

        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
        assert state.signal_name == "SIGTERM"

        with pytest.raises(SystemExit) as err:
            handler(signal.SIGTERM, None)
        assert err.value.code == 130


def test_foreground_shutdown_can_request_cooperative_shutdown_without_raising(monkeypatch):
    def fake_exit(code):
        raise SystemExit(code)

    seen = []
    monkeypatch.setattr(shutdown_util.os, "_exit", fake_exit)

    with shutdown_util.foreground_shutdown_signals(
        first_signal_message=None,
        on_signal=seen.append,
        raise_on_first_signal=False,
    ) as state:
        handler = signal.getsignal(signal.SIGTERM)

        handler(signal.SIGTERM, None)
        assert state.signal_name == "SIGTERM"
        assert seen == [signal.SIGTERM]

        with pytest.raises(SystemExit) as err:
            handler(signal.SIGTERM, None)
        assert err.value.code == 130


def test_daemon_runner_signal_exit_is_unexpected_for_supervisor(monkeypatch):
    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(archivebox_run.os, "_exit", fake_exit)

    with pytest.raises(SystemExit) as err:
        archivebox_run._exit_daemon_runner_on_signal(signal.SIGTERM)

    assert err.value.code == 143


def test_crawl_runner_daemon_signal_exits_before_async_cleanup(monkeypatch):
    from archivebox.services import runner as runner_module
    from archivebox.services.runner import CrawlRunner

    def fake_exit(code):
        raise SystemExit(code)

    runner = object.__new__(CrawlRunner)
    monkeypatch.setenv("ARCHIVEBOX_RUNNER_DAEMON", "1")
    monkeypatch.setattr(runner_module.os, "_exit", fake_exit)

    with pytest.raises(SystemExit) as err:
        runner._request_abort_from_signal(signal.SIGTERM)

    assert err.value.code == 143
