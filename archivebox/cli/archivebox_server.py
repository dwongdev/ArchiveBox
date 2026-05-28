#!/usr/bin/env python3

__package__ = "archivebox.cli"

import sys
import os
import socket
import subprocess
import time
from collections.abc import Iterable

import rich_click as click
from rich import print

from archivebox.misc.util import docstring, enforce_types


@enforce_types
def server(
    runserver_args: Iterable[str] | None = None,
    reload: bool = False,
    init: bool = False,
    debug: bool = False,
    daemonize: bool = False,
    nothreading: bool = False,
) -> None:
    """Run the ArchiveBox HTTP server"""
    from archivebox.config.common import get_config

    config = get_config()
    runserver_args = list(runserver_args or (config.BIND_ADDR,))

    if init:
        from archivebox.cli.archivebox_init import init as archivebox_init

        archivebox_init(quick=True)
        print()

    run_in_debug = config.DEBUG or debug or reload
    if debug or reload:
        os.environ["DEBUG"] = "True"

    from django.contrib.auth.models import User

    if not User.objects.filter(is_superuser=True).exclude(username="system").exists():
        print()
        print(
            "[violet]Hint:[/violet] To create an [bold]admin username & password[/bold] for the [deep_sky_blue3][underline][link=http://{host}:{port}/admin]Admin UI[/link][/underline][/deep_sky_blue3], run:",
        )
        print("      [green]archivebox manage createsuperuser[/green]")
        print()

    host = "127.0.0.1"
    port = "8000"

    try:
        host_and_port = [arg for arg in runserver_args if arg.replace(".", "").replace(":", "").isdigit()][0]
        if ":" in host_and_port:
            host, port = host_and_port.split(":")
        else:
            if "." in host_and_port:
                host = host_and_port
            else:
                port = host_and_port
    except IndexError:
        pass

    if daemonize and os.environ.get("ARCHIVEBOX_SERVER_DAEMON_CHILD") != "1":
        from archivebox.config import CONSTANTS

        log_path = CONSTANTS.LOGS_DIR / "server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        daemon_env = os.environ.copy()
        daemon_env["ARCHIVEBOX_SERVER_DAEMON_CHILD"] = "1"
        daemon_cmd = [sys.executable, "-m", "archivebox", "server"]
        if debug:
            daemon_cmd.append("--debug")
        if reload:
            daemon_cmd.append("--reload")
        if nothreading:
            daemon_cmd.append("--nothreading")
        daemon_cmd.extend(runserver_args)
        with log_path.open("a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                daemon_cmd,
                cwd=os.getcwd(),
                env=daemon_env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print(f"[red][X] ArchiveBox daemon server exited early with code {proc.returncode}. See {log_path}[/red]")
                sys.exit(proc.returncode or 1)
            try:
                with socket.create_connection((host, int(port)), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            print(f"[yellow][!] ArchiveBox daemon server pid={proc.pid} is still starting. See {log_path}[/yellow]")
        return

    os.environ["BIND_ADDR"] = f"{host}:{port}"
    from archivebox.core.host_utils import build_admin_url

    admin_url = build_admin_url("/admin/")

    from archivebox.workers.supervisord_util import (
        active_supervisord_runtime_components,
        format_runtime_components,
        start_server_workers,
        stop_existing_supervisord_process,
        is_port_in_use,
    )
    from archivebox.machine.models import Process
    from archivebox.services.supervision_service import (
        command_owns_runtime_stack,
        current_command,
        runtime_stack_owner,
        standby_until_runtime_stack_needed,
    )
    from archivebox.core.shutdown_util import foreground_parent_watchdog, foreground_shutdown_signals

    if run_in_debug:
        print("[green][+] Starting ArchiveBox webserver in DEBUG mode...[/green]")
    else:
        print("[green][+] Starting ArchiveBox webserver...[/green]")
    print(
        f"    [blink][green]>[/green][/blink] Starting ArchiveBox webserver on [deep_sky_blue4][link=http://{host}:{port}]http://{host}:{port}[/link][/deep_sky_blue4]",
    )
    print(
        f"    [green]>[/green] Log in to ArchiveBox Admin UI on [deep_sky_blue3][link={admin_url}]{admin_url}[/link][/deep_sky_blue3]",
    )
    print("    > Writing ArchiveBox error log to ./logs/errors.log")
    print()
    bind_url = f"http://{host}:{port}"
    command = current_command(Process.TypeChoices.SERVER, data_dir=config.DATA_DIR, url=bind_url)

    def still_owns_runtime_stack() -> bool:
        from django.db import connections

        try:
            return command_owns_runtime_stack(command, data_dir=config.DATA_DIR)
        finally:
            connections.close_all()

    try:
        with foreground_shutdown_signals(), foreground_parent_watchdog(enabled=os.environ.get("ARCHIVEBOX_SERVER_DAEMON_CHILD") != "1"):
            while True:
                standby_result = standby_until_runtime_stack_needed(command, data_dir=config.DATA_DIR)
                older_owner = runtime_stack_owner(data_dir=config.DATA_DIR, exclude_id=command.id)
                takeover_components = active_supervisord_runtime_components(config=config)
                if older_owner and takeover_components:
                    print(
                        "[yellow][*] Taking over "
                        f"{format_runtime_components(takeover_components)} from older existing archivebox process (pid={older_owner.pid}).[/yellow]",
                    )
                stop_existing_supervisord_process()
                if is_port_in_use(host, int(port)):
                    print(f"[red][X] Error: Port {port} is already in use[/red]")
                    print(f"    Another process outside this ArchiveBox runtime is listening on {host}:{port}")
                    sys.exit(1)

                result = start_server_workers(
                    host=host,
                    port=port,
                    daemonize=False,
                    debug=run_in_debug,
                    reload=reload,
                    nothreading=nothreading,
                    keep_running=still_owns_runtime_stack,
                    should_stop_supervisord=still_owns_runtime_stack,
                    resumed_from_pid=standby_result.get("previous_owner_pid") if standby_result.get("resumed") else None,
                )
                if result == "interrupted":
                    break
                if not still_owns_runtime_stack():
                    continue
                if result == "exited":
                    print("[yellow][*] Runtime stack exited while this parent is still leader; restarting...[/yellow]")
                    continue
                break
    except KeyboardInterrupt:
        pass
    finally:
        command.mark_exited()
    print("\n[i][green][🟩] ArchiveBox server shut down gracefully.[/green][/i]")


@click.command()
@click.argument("runserver_args", nargs=-1)
@click.option("--reload", is_flag=True, help="Enable auto-reloading when code or templates change")
@click.option("--debug", is_flag=True, help="Enable DEBUG=True mode with more verbose errors")
@click.option("--nothreading", is_flag=True, help="Force runserver to run in single-threaded mode")
@click.option("--init", is_flag=True, help="Run a full archivebox init/upgrade before starting the server")
@click.option("--daemonize", is_flag=True, help="Run the server in the background as a daemon")
@docstring(server.__doc__)
def main(**kwargs):
    server(**kwargs)


if __name__ == "__main__":
    main()
