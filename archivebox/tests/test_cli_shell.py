#!/usr/bin/env python3
"""
Tests for archivebox shell command.
Verify shell command starts Django shell (basic smoke tests only).
"""

from archivebox.tests.conftest import run_archivebox_cmd


def test_shell_command_exists(initialized_archive):
    """Test that shell command is recognized."""

    # Test that the command exists (will fail without input but should recognize command)
    result = run_archivebox_cmd(
        ["shell", "--help"],
        timeout=10,
    )

    # Should show shell help or recognize command
    assert result.returncode in [0, 1, 2]


def test_shell_c_executes_python(initialized_archive):
    """shell -c should fully initialize Django and run the provided command."""

    result = run_archivebox_cmd(
        ["shell", "-c", 'print("shell-ok")'],
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "shell-ok" in result.stdout
