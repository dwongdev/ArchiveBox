#!/usr/bin/env python3

__package__ = "archivebox.cli"

import sys
import os
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

    os.environ["BIND_ADDR"] = f"{host}:{port}"
    from archivebox.core.host_utils import build_admin_url

    admin_url = build_admin_url("/admin/")

    from archivebox.workers.supervisord_util import (
        start_server_workers,
        stop_existing_supervisord_process,
        is_port_in_use,
    )
    from archivebox.machine.models import Process
    from archivebox.services.supervision_service import (
        command_owns_runtime_stack,
        current_command,
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

    try:
        with foreground_shutdown_signals(), foreground_parent_watchdog():
            while True:
                standby_until_runtime_stack_needed(command, data_dir=config.DATA_DIR)
                sys.stdout.write(f"[*] ArchiveBox server parent pid={os.getpid()} is now running the orchestrator and server...\n")
                sys.stdout.flush()
                stop_existing_supervisord_process()
                if is_port_in_use(host, int(port)):
                    print(f"[red][X] Error: Port {port} is already in use[/red]")
                    print(f"    Another process outside this ArchiveBox runtime is listening on {host}:{port}")
                    sys.exit(1)

                result = start_server_workers(
                    host=host,
                    port=port,
                    daemonize=daemonize,
                    debug=run_in_debug,
                    reload=reload,
                    nothreading=nothreading,
                    keep_running=lambda: command_owns_runtime_stack(command, data_dir=config.DATA_DIR),
                    should_stop_supervisord=lambda: command_owns_runtime_stack(command, data_dir=config.DATA_DIR),
                )
                if result == "interrupted":
                    break
                if not command_owns_runtime_stack(command, data_dir=config.DATA_DIR):
                    print("[yellow][*] Another ArchiveBox command took over the runtime stack; standing by.[/yellow]")
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
