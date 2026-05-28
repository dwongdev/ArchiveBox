from __future__ import annotations

import signal
import subprocess
import sys
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psutil


@dataclass
class ShutdownSignalState:
    """Tracks the exact OS signal that asked a foreground command to exit."""

    signal_name: str | None = None


def configured_stopwaitsecs(workers: list[dict[str, str]] | tuple[dict[str, str], ...], *, default: int = 5, buffer: int = 5) -> int:
    """Return a deterministic shutdown bound from generated worker definitions."""

    stop_grace_seconds = default
    for worker in workers:
        try:
            stop_grace_seconds = max(stop_grace_seconds, int(worker.get("stopwaitsecs") or default) + buffer)
        except (TypeError, ValueError):
            continue
    return stop_grace_seconds


def wait_popen_and_kill_children(
    proc: subprocess.Popen,
    children: list[psutil.Process],
    *,
    timeout: float,
    kill_timeout: float = 2.0,
) -> None:
    """Wait for a Popen parent and then hard-kill any surviving descendants."""

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=kill_timeout)
    kill_remaining_processes(children, timeout=kill_timeout)


def wait_psutil_and_kill_children(
    proc: psutil.Process,
    children: list[psutil.Process],
    *,
    timeout: float,
    kill_timeout: float = 2.0,
) -> None:
    """Wait for a psutil parent and then hard-kill any surviving descendants."""

    try:
        if proc.status() == psutil.STATUS_ZOMBIE:
            # Another ArchiveBox foreground parent owns this Popen and must reap
            # it. By the time supervisord is a zombie it has already stopped
            # accepting work, so the caller can clear stale pid/socket files
            # without blocking for a process it cannot reap itself.
            kill_remaining_processes(children, timeout=kill_timeout)
            return
        proc.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        proc.kill()
    kill_remaining_processes(children, timeout=kill_timeout)
    try:
        proc.wait(timeout=kill_timeout)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass


def kill_remaining_processes(processes: list[psutil.Process], *, timeout: float = 2.0) -> None:
    _gone, alive = psutil.wait_procs(processes, timeout=timeout)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=timeout)


@contextmanager
def foreground_shutdown_signals(
    handled_signals: tuple[signal.Signals, ...] = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
) -> Iterator[ShutdownSignalState]:
    """Install foreground signal handlers that print an immediate exit notice.

    Some log-tail loops intentionally swallow KeyboardInterrupt so that callers
    can centralize cleanup in finally blocks. The handler writes the signal name
    immediately, then raises KeyboardInterrupt to break out of the blocking read.
    """

    state = ShutdownSignalState()
    previous_handlers = {sig: signal.getsignal(sig) for sig in handled_signals}

    def raise_keyboard_interrupt(signum, _frame):
        state.signal_name = signal.Signals(signum).name
        sys.stdout.write(f"\n[🛑] Got {state.signal_name}, stopping gracefully...\n")
        sys.stdout.flush()
        raise KeyboardInterrupt

    try:
        for sig in handled_signals:
            signal.signal(sig, raise_keyboard_interrupt)
        yield state
    finally:
        for sig, previous_handler in previous_handlers.items():
            signal.signal(sig, previous_handler)


@contextmanager
def foreground_parent_watchdog(
    *,
    enabled: bool = True,
    check_interval: float = 2.0,
    shutdown_signal: signal.Signals = signal.SIGINT,
) -> Iterator[None]:
    """Ask a foreground command to exit if its launcher/wrapper disappears.

    `uv run archivebox ...` and similar wrappers can be killed without
    delivering a signal to the real Python child. If that child keeps crawling
    as an orphan, it can hold SQLite write locks long after the user-facing
    command timed out. This watchdog is only for foreground command lifetimes;
    daemon/supervisord workers should not use it because their parent may
    intentionally hand them off.
    """

    original_ppid = os.getppid()
    if not enabled or original_ppid <= 1:
        yield
        return

    stopped = threading.Event()

    def watch_parent() -> None:
        while not stopped.wait(check_interval):
            if os.getppid() == original_ppid:
                continue
            sys.stderr.write("\n[🛑] ArchiveBox parent process exited; stopping foreground command gracefully...\n")
            sys.stderr.flush()
            os.kill(os.getpid(), shutdown_signal)
            return

    thread = threading.Thread(target=watch_parent, name="archivebox-parent-watchdog", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
