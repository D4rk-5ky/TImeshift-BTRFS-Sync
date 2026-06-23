"""Small helpers for running shell commands safely.

The rest of the app calls these helpers instead of calling subprocess directly.
That gives us one common place for error handling, sudo handling, and the
`btrfs send | btrfs receive` streaming pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
import shlex
import sys
from typing import Iterable


class CommandError(RuntimeError):
    """Raised when an external command exits with a non-zero status.

    We keep stdout/stderr on the exception because command output is often the
    only useful clue when btrfs, ssh, sudo, or timeshift fails.
    """

    def __init__(self, cmd: list[str] | str, returncode: int, stdout: str = "", stderr: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

        # Convert list-style commands into the same printable format a user
        # could paste into a shell for troubleshooting.
        printable = cmd if isinstance(cmd, str) else shlex.join(cmd)
        super().__init__(f"Command failed ({returncode}): {printable}\n{stderr.strip()}")


@dataclass(slots=True)
class Completed:
    """Lightweight command result returned by run_local and SSHRunner.run."""

    cmd: list[str] | str
    returncode: int
    stdout: str
    stderr: str


def sudo_prefix(sudo: str | None) -> list[str]:
    """Convert the configured sudo string into command arguments.

    Examples:
      "sudo -n" -> ["sudo", "-n"]
      ""        -> []

    This makes it possible to disable sudo entirely for dedicated backup users
    that already have permission to run the needed commands.
    """

    if not sudo:
        return []
    return shlex.split(sudo)


def run_local(cmd: list[str], *, check: bool = True, input_text: str | None = None) -> Completed:
    """Run a local command and capture stdout/stderr as text.

    `check=True` means failures become CommandError. `check=False` is used for
    probes like `test -d` where non-zero exit codes are expected and useful.
    """

    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,  # We do our own error handling below for clearer messages.
    )
    completed = Completed(cmd=cmd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return completed


def stream_pipeline(
    left_cmd: list[str],
    right_cmd: list[str],
    *,
    verbose: bool = True,
) -> None:
    """Stream one command into another without storing the stream on disk.

    This is the core transfer primitive:

        ssh source 'sudo btrfs send ...' | sudo btrfs receive ...

    Btrfs send streams can be very large, so we pipe stdout directly from the
    remote send command into stdin of the local receive command.
    """

    if verbose:
        # Print the exact commands to stderr so normal stdout remains cleaner.
        print("REMOTE SEND:", shlex.join(left_cmd), file=sys.stderr)
        print("LOCAL RECEIVE:", shlex.join(right_cmd), file=sys.stderr)

    # Start the remote btrfs send side first. Its stdout becomes the receive
    # side's stdin. stderr is captured separately so failures are visible.
    left = subprocess.Popen(left_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert left.stdout is not None

    # Start the local receive side and connect the pipe.
    right = subprocess.Popen(right_cmd, stdin=left.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Close our duplicate reference to left.stdout. Without this, the receive
    # command may not see EOF when the send side exits.
    left.stdout.close()

    # Wait for receive to finish, then collect send's stderr and return code.
    right_out, right_err = right.communicate()
    left_err = left.stderr.read() if left.stderr else b""
    left_return = left.wait()

    # Either side failing means the whole transfer failed. We combine both
    # stderr streams because btrfs errors may appear on either side of the pipe.
    if left_return != 0 or right.returncode != 0:
        raise CommandError(
            cmd=f"{shlex.join(left_cmd)} | {shlex.join(right_cmd)}",
            returncode=right.returncode if right.returncode != 0 else left_return,
            stdout=(right_out or b"").decode(errors="replace"),
            stderr=(left_err or b"").decode(errors="replace") + (right_err or b"").decode(errors="replace"),
        )


def quote_join(parts: Iterable[str]) -> str:
    """Quote command parts into one safe remote-shell command string.

    SSH normally receives a single remote command string. Each part is shell-
    quoted here so paths with spaces or special characters are handled safely.
    """

    return " ".join(shlex.quote(str(p)) for p in parts)
