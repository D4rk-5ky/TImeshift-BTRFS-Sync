"""Shared subprocess helpers.

This module centralizes local process execution and the streaming pipeline used
for `ssh ... btrfs send | [mbuffer] | btrfs receive`.

File logging is delegated to timeshift_btrfs_sync.log. This keeps naming,
.log/.err/.btrfs/.mbuffer/.succes splitting, and stream tee logic in one module as requested.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import os
import shlex
import subprocess
import sys

from . import log as runlog


class CommandError(RuntimeError):
    """Raised when an external command exits with a non-zero status."""

    def __init__(self, cmd: list[str] | str, returncode: int, stdout: str = "", stderr: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        printable = cmd if isinstance(cmd, str) else shlex.join(cmd)
        details: list[str] = []
        if stdout.strip():
            details.append("COMMAND STDOUT:\n" + stdout.rstrip())
        if stderr.strip():
            details.append("COMMAND STDERR:\n" + stderr.rstrip())
        suffix = "\n" + "\n".join(details) if details else ""
        super().__init__(f"Command failed ({returncode}): {printable}{suffix}")


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


def remote_double_quote(value: str) -> str:
    """Return a shell-safe double-quoted argument for a remote shell command.

    Most remote commands are built with :func:`quote_join`, which uses
    single-quote based shell escaping. That is very safe, but when a remote
    command itself is later displayed as one quoted SSH argument, nested
    single quotes are rendered as the classic ``'"'"'`` sequence.

    Human-entered Timeshift comments are a good case for double quoting: it
    keeps spaces safe, escapes the characters that are still special inside
    double quotes, and makes the logged SSH command much easier to read.
    """

    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("$", "\\$")
    text = text.replace("`", "\\`")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "\\r")
    return f'"{text}"'


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
    log_stderr: bool = True,
    mirror_stderr: bool = True,
    mirror_stdout_on_failure: bool = False,
) -> Completed:
    """Run a local command and capture stdout/stderr.

    Normal commands are recorded in .log when logging is enabled. Every byte of
    stderr is always copied to .err and mirrored to the terminal when present.

    The log_stderr/mirror_stderr parameters are kept for compatibility with older
    callers, but stderr is no longer suppressible. Even expected probe failures
    are visible, because hiding stderr made real troubleshooting harder.
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

    logger = runlog.get_logger()
    if logger:
        logger.completed(cmd, proc.returncode, proc.stdout, proc.stderr)

    # Make normal command stderr visible in the terminal too. Pipeline stderr is
    # handled separately by stream_pipeline(), so this only affects captured
    # commands such as btrfs subvolume show/delete.
    if proc.stderr:
        print(f"COMMAND STDERR: {shlex.join(cmd)}", file=runlog.terminal_stderr())
        print(proc.stderr.rstrip(), file=runlog.terminal_stderr())

    # Some tools, including Timeshift, may print useful failure details to
    # stdout instead of stderr. Do not mirror stdout for every successful
    # command because that would make normal output noisy, but allow selected
    # callers to expose stdout when a command fails.
    if check and proc.returncode != 0 and mirror_stdout_on_failure and proc.stdout:
        print(f"COMMAND STDOUT: {shlex.join(cmd)}", file=runlog.terminal_stderr())
        print(proc.stdout.rstrip(), file=runlog.terminal_stderr())

    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return result


def _join_text(parts: list[str]) -> str:
    """Join captured stream text chunks into one string."""

    return "".join(parts)


def stream_pipeline(
    left_cmd: list[str],
    right_cmd: list[str],
    *,
    middle_cmd: list[str] | None = None,
    verbose: bool = True,
    left_env: dict[str, str] | None = None,
    middle_env: dict[str, str] | None = None,
    right_env: dict[str, str] | None = None,
    passthrough_left_stderr: bool = False,
    passthrough_right_stdout: bool = False,
    passthrough_right_stderr: bool = False,
) -> None:
    """Stream left command into optional middle command, then right command.

    Without mbuffer:
      ssh source 'btrfs send ...' | btrfs receive ...

    With mbuffer:
      ssh source 'btrfs send ...' | mbuffer -m 256M | btrfs receive ...

    Logging behavior when log_dir is set:
      * command/control output goes to .log
      * mbuffer progress/summary goes to .mbuffer and terminal
      * btrfs send/receive status/verbose output goes to .btrfs
      * pipeline stderr is buffered and copied to .err only if the pipeline fails
      * .log is not flooded with mbuffer or verbose Btrfs output

    Important Btrfs detail: `btrfs send` writes normal status lines such as
    `At subvol ...` to stderr even when the send succeeds. Those lines are not
    errors, so successful pipeline stderr must not make the run .err file
    non-empty.
    """

    logger = runlog.get_logger()

    if verbose:
        # Print each pipeline command as its own readable block. The blank lines
        # make it much easier to see when a transfer changes from one subvolume
        # to the next.
        term_err = runlog.terminal_stderr()
        print(file=term_err)
        print("REMOTE SEND:", shlex.join(left_cmd), file=term_err)
        print(file=term_err)
        if middle_cmd:
            print("STREAM BUFFER:", shlex.join(middle_cmd), file=term_err)
            print(file=term_err)
        print("LOCAL RECEIVE:", shlex.join(right_cmd), file=term_err)
        print(file=term_err)

    if logger:
        logger.pipeline_commands(left_cmd, right_cmd, middle_cmd)

    # Always pipe diagnostic output and consume it in reader threads. This avoids
    # deadlocks caused by a long-running process filling stdout/stderr while the
    # main thread waits for another process to finish.
    left = subprocess.Popen(
        left_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_merged_env(left_env),
    )
    assert left.stdout is not None

    middle = None
    receive_stdin = left.stdout
    if middle_cmd:
        middle = subprocess.Popen(
            middle_cmd,
            stdin=left.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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

    threads = []
    left_err_chunks: list[str] = []
    middle_err_chunks: list[str] = []
    right_out_chunks: list[str] = []
    right_err_chunks: list[str] = []

    # Remote btrfs send writes normal successful status lines like
    # "At subvol ..." to stderr. Keep it visible on the terminal and in .btrfs,
    # but do not mark the run as having .err output unless the pipeline fails.
    threads.append(runlog.tee_pipe_to_log(
        left.stderr,
        stream_name="remote-send-stderr",
        terminal=runlog.terminal_stderr(),
        to_mbuffer=False,
        to_btrfs_out=bool(logger),
        to_err=False,
        capture=left_err_chunks,
    ))

    # mbuffer writes normal progress to stderr. That belongs in .mbuffer. If the
    # mbuffer process fails, the captured text is copied to .err below.
    if middle and middle.stderr is not None:
        threads.append(runlog.tee_pipe_to_log(
            middle.stderr,
            stream_name="mbuffer",
            terminal=runlog.terminal_stderr(),
            to_mbuffer=bool(logger),
            to_btrfs_out=False,
            to_err=False,
            capture=middle_err_chunks,
        ))

    # btrfs receive stdout is only shown on the terminal in verbose mode, but it
    # is still useful in .btrfs because receive also reports normal status there.
    # btrfs receive stderr is captured and copied to .err only on pipeline failure.
    threads.append(runlog.tee_pipe_to_log(
        right.stdout,
        stream_name="local-receive-stdout",
        terminal=runlog.terminal_stdout() if passthrough_right_stdout else None,
        to_mbuffer=False,
        to_btrfs_out=bool(logger),
        to_err=False,
        capture=right_out_chunks,
    ))
    threads.append(runlog.tee_pipe_to_log(
        right.stderr,
        stream_name="local-receive-stderr",
        terminal=runlog.terminal_stderr(),
        to_mbuffer=False,
        to_btrfs_out=bool(logger),
        to_err=False,
        capture=right_err_chunks,
    ))

    right_return = right.wait()
    middle_return = middle.wait() if middle else 0
    left_return = left.wait()

    for thread in threads:
        thread.join()

    returncode = right_return if right_return != 0 else middle_return if middle_return != 0 else left_return
    if logger:
        logger.pipeline_summary(returncode)

    if left_return != 0 or middle_return != 0 or right_return != 0:
        stderr = _join_text(left_err_chunks) + _join_text(middle_err_chunks) + _join_text(right_err_chunks)
        stdout = _join_text(right_out_chunks)
        pipe_text = shlex.join(left_cmd)
        if middle_cmd:
            pipe_text += " | " + shlex.join(middle_cmd)
        pipe_text += " | " + shlex.join(right_cmd)

        # Pipeline stderr is buffered during the transfer because successful
        # btrfs send and mbuffer both use stderr for normal status/progress. Only
        # copy those captured streams to .err when the pipeline actually failed.
        if logger:
            logger.err("PIPELINE FAILED: " + pipe_text)
            if left_err_chunks:
                logger.err("[remote-send-stderr]")
                logger.err(_join_text(left_err_chunks))
            if middle_err_chunks:
                logger.err("[mbuffer-stderr]")
                logger.err(_join_text(middle_err_chunks))
            if right_err_chunks:
                logger.err("[local-receive-stderr]")
                logger.err(_join_text(right_err_chunks))

        raise CommandError(pipe_text, returncode, stdout, stderr)
