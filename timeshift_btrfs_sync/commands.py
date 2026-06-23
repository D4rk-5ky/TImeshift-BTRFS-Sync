"""Shared command helpers.

All external command execution goes through this module so errors are captured
and the `ssh btrfs send | btrfs receive` pipeline is handled in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import shlex
import subprocess
import sys


class CommandError(RuntimeError):
    """Raised when a local or SSH command fails."""

    def __init__(self, cmd: list[str] | str, returncode: int, stdout: str = "", stderr: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        printable = cmd if isinstance(cmd, str) else shlex.join(cmd)
        super().__init__(f"Command failed ({returncode}): {printable}\n{stderr.strip()}")


@dataclass(slots=True)
class Completed:
    """Small subprocess result object."""

    cmd: list[str] | str
    returncode: int
    stdout: str
    stderr: str


def sudo_prefix(sudo: str | None) -> list[str]:
    """Split a configured sudo prefix.

    Examples:
      "sudo -n" -> ["sudo", "-n"]
      ""        -> []
    """

    if not sudo:
        return []
    return shlex.split(sudo)


def quote_join(parts: Iterable[str]) -> str:
    """Quote command parts into one safe remote-shell command string."""

    return " ".join(shlex.quote(str(p)) for p in parts)


def run_local(cmd: list[str], *, check: bool = True, input_text: str | None = None) -> Completed:
    """Run a local command and capture stdout/stderr."""

    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    result = Completed(cmd=cmd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return result


def stream_pipeline(left_cmd: list[str], right_cmd: list[str], *, verbose: bool = True) -> None:
    """Pipe one command into another without storing the stream on disk.

    Used for:
      ssh source 'sudo -n btrfs send ...' | sudo -n btrfs receive ...
    """

    if verbose:
        print("REMOTE SEND:", shlex.join(left_cmd), file=sys.stderr)
        print("LOCAL RECEIVE:", shlex.join(right_cmd), file=sys.stderr)

    left = subprocess.Popen(left_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert left.stdout is not None
    right = subprocess.Popen(right_cmd, stdin=left.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    left.stdout.close()

    right_out, right_err = right.communicate()
    left_err = left.stderr.read() if left.stderr else b""
    left_return = left.wait()

    if left_return != 0 or right.returncode != 0:
        raise CommandError(
            cmd=f"{shlex.join(left_cmd)} | {shlex.join(right_cmd)}",
            returncode=right.returncode if right.returncode != 0 else left_return,
            stdout=(right_out or b"").decode(errors="replace"),
            stderr=(left_err or b"").decode(errors="replace") + (right_err or b"").decode(errors="replace"),
        )
