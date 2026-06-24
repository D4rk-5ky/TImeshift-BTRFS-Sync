"""Shared subprocess helpers.

This module centralizes local process execution and the streaming pipeline used
for `ssh ... btrfs send | [mbuffer] | btrfs receive`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import os
import shlex
import subprocess
import sys


class CommandError(RuntimeError):
    """Raised when an external command exits with a non-zero status."""

    def __init__(self, cmd: list[str] | str, returncode: int, stdout: str = "", stderr: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        printable = cmd if isinstance(cmd, str) else shlex.join(cmd)
        super().__init__(f"Command failed ({returncode}): {printable}\n{stderr.strip()}")


@dataclass(slots=True)
class Completed:
    """Small command result object."""

    cmd: list[str] | str
    returncode: int
    stdout: str
    stderr: str


def sudo_prefix(sudo: str | None) -> list[str]:
    """Split a configured sudo prefix into argv parts."""

    if not sudo:
        return []
    return shlex.split(sudo)


def quote_join(parts: Iterable[str]) -> str:
    """Quote argv parts into one safe remote-shell command string."""

    return " ".join(shlex.quote(str(p)) for p in parts)


def _merged_env(extra_env: dict[str, str] | None) -> dict[str, str] | None:
    """Merge optional child-process environment variables."""

    if not extra_env:
        return None
    env = os.environ.copy()
    env.update(extra_env)
    return env


def run_local(
    cmd: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> Completed:
    """Run a local command and capture stdout/stderr.

    `env` is mainly used for sshpass. Passwords are passed through SSHPASS in
    the child environment, not as command-line arguments.
    """

    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=_merged_env(env),
    )
    result = Completed(cmd=cmd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return result


def stream_pipeline(
    left_cmd: list[str],
    right_cmd: list[str],
    *,
    middle_cmd: list[str] | None = None,
    verbose: bool = True,
    left_env: dict[str, str] | None = None,
    middle_env: dict[str, str] | None = None,
    right_env: dict[str, str] | None = None,
) -> None:
    """Stream left command into optional middle command, then right command.

    Without mbuffer:
      ssh source 'btrfs send ...' | btrfs receive ...

    With mbuffer:
      ssh source 'btrfs send ...' | mbuffer -m 256M | btrfs receive ...
    """

    if verbose:
        # Print each pipeline command as its own readable block.  The blank
        # lines make it much easier to see when a transfer changes from one
        # subvolume to the next.
        print(file=sys.stderr)
        print("REMOTE SEND:", shlex.join(left_cmd), file=sys.stderr)
        print(file=sys.stderr)
        if middle_cmd:
            print("STREAM BUFFER:", shlex.join(middle_cmd), file=sys.stderr)
            print(file=sys.stderr)
        print("LOCAL RECEIVE:", shlex.join(right_cmd), file=sys.stderr)
        print(file=sys.stderr)

    left = subprocess.Popen(left_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_merged_env(left_env))
    assert left.stdout is not None

    middle = None
    receive_stdin = left.stdout
    if middle_cmd:
        middle = subprocess.Popen(
            middle_cmd,
            stdin=left.stdout,
            stdout=subprocess.PIPE,
            # mbuffer writes its useful progress and summary lines to stderr.
            # In verbose mode we let that stderr go directly to the terminal so
            # the user can see live throughput and the final summary.
            stderr=None if verbose else subprocess.PIPE,
            env=_merged_env(middle_env),
        )
        left.stdout.close()
        assert middle.stdout is not None
        receive_stdin = middle.stdout

    right = subprocess.Popen(
        right_cmd,
        stdin=receive_stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_merged_env(right_env),
    )
    receive_stdin.close()

    right_out, right_err = right.communicate()

    middle_err = b""
    middle_return = 0
    if middle:
        # When verbose=True, middle.stderr is inherited by the terminal and is
        # therefore None here. When verbose=False, it is captured for errors.
        middle_err = middle.stderr.read() if middle.stderr else b""
        middle_return = middle.wait()

    left_err = left.stderr.read() if left.stderr else b""
    left_return = left.wait()

    if left_return != 0 or middle_return != 0 or right.returncode != 0:
        returncode = right.returncode if right.returncode != 0 else middle_return if middle_return != 0 else left_return
        stderr = (left_err or b"").decode(errors="replace")
        stderr += (middle_err or b"").decode(errors="replace")
        stderr += (right_err or b"").decode(errors="replace")
        pipe_text = shlex.join(left_cmd)
        if middle_cmd:
            pipe_text += " | " + shlex.join(middle_cmd)
        pipe_text += " | " + shlex.join(right_cmd)
        raise CommandError(pipe_text, returncode, (right_out or b"").decode(errors="replace"), stderr)
