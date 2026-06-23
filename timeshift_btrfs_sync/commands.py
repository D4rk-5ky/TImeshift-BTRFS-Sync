"""Shared command helpers.

Every external command goes through this module. Keeping subprocess handling in
one place makes the rest of the project easier to read and makes failures from
SSH, sudo, Btrfs, and Timeshift look consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import shlex
import subprocess
import sys


class CommandError(RuntimeError):
    """Raised when a local command or SSH command exits with an error.

    The exception keeps stdout and stderr because Btrfs/Timeshift error text is
    usually the most useful troubleshooting information.
    """

    def __init__(self, cmd: list[str] | str, returncode: int, stdout: str = "", stderr: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

        # Convert argv lists into a readable shell-like string for error output.
        printable = cmd if isinstance(cmd, str) else shlex.join(cmd)
        super().__init__(f"Command failed ({returncode}): {printable}\n{stderr.strip()}")


@dataclass(slots=True)
class Completed:
    """Small result object returned by run_local() and SSHRunner.run()."""

    cmd: list[str] | str
    returncode: int
    stdout: str
    stderr: str


def sudo_prefix(sudo: str | None) -> list[str]:
    """Split the configured sudo prefix into argv parts.

    Examples:
      "sudo -n" -> ["sudo", "-n"]
      ""        -> []

    Empty sudo is useful if a dedicated user can run the needed command without
    privilege escalation.
    """

    if not sudo:
        return []
    return shlex.split(sudo)


def quote_join(parts: Iterable[str]) -> str:
    """Quote argv parts into one safe remote-shell command string.

    SSH usually receives one remote shell command string, not an argv list. This
    function quotes every part so paths with spaces or special characters do not
    get interpreted by the remote shell.
    """

    return " ".join(shlex.quote(str(p)) for p in parts)


def run_local(cmd: list[str], *, check: bool = True, input_text: str | None = None) -> Completed:
    """Run a local process and capture stdout/stderr as text.

    `check=True` turns non-zero exits into CommandError. `check=False` is used
    for harmless probes where failure is expected, such as checking if a Btrfs
    subvolume exists.
    """

    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,  # We raise our own CommandError below for clearer messages.
    )
    result = Completed(cmd=cmd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return result


def stream_pipeline(left_cmd: list[str], right_cmd: list[str], *, verbose: bool = True) -> None:
    """Pipe one command into another without writing the stream to disk.

    This is the critical transfer primitive:

        ssh source 'sudo -n btrfs send ...' | sudo -n btrfs receive ...

    Btrfs send streams can be very large, so storing them as temporary files is
    avoided. The left command's stdout is connected directly to the right
    command's stdin.
    """

    if verbose:
        # These go to stderr so normal stdout can stay readable or scriptable.
        print("REMOTE SEND:", shlex.join(left_cmd), file=sys.stderr)
        print("LOCAL RECEIVE:", shlex.join(right_cmd), file=sys.stderr)

    # Start the producing side, normally SSH running remote `btrfs send`.
    left = subprocess.Popen(left_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert left.stdout is not None

    # Start the consuming side, normally local `btrfs receive`.
    right = subprocess.Popen(right_cmd, stdin=left.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Close our extra copy of left.stdout so the receiver can see EOF correctly.
    left.stdout.close()

    # Wait for receive first, then collect the send side's stderr/exit code.
    right_out, right_err = right.communicate()
    left_err = left.stderr.read() if left.stderr else b""
    left_return = left.wait()

    # Either side failing means the transfer is not trustworthy.
    if left_return != 0 or right.returncode != 0:
        raise CommandError(
            cmd=f"{shlex.join(left_cmd)} | {shlex.join(right_cmd)}",
            returncode=right.returncode if right.returncode != 0 else left_return,
            stdout=(right_out or b"").decode(errors="replace"),
            stderr=(left_err or b"").decode(errors="replace") + (right_err or b"").decode(errors="replace"),
        )
