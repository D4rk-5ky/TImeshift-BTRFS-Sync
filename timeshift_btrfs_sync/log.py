"""Split run logging for timeshift-btrfs-sync.

All file logging logic lives in this module by design.

When logging is enabled by setting top-level `log_dir` in config.toml, one run
creates timestamped files:

  * .log       - normal app status, commands executed, and normal command output
  * .mbuffer   - mbuffer progress/summary and the transfer command header
  * .btrfs-out - btrfs send/receive verbose output and the send/receive commands
  * .err       - stderr/error output

The terminal output stays human-readable. The logger mirrors important lines to
files without forcing the rest of the project to know file naming details.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Iterator
import os
import shlex
import sys
import threading


@dataclass
class RunLogger:
    """Owns the .log, .mbuffer, .btrfs-out, and .err files for one run."""

    log_dir: Path
    name: str = "timeshift-btrfs-sync"

    def __post_init__(self) -> None:
        """Create the log directory and open the run log files."""

        self.log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        pid = os.getpid()
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in self.name) or "timeshift-btrfs-sync"
        base = self.log_dir / f"{timestamp}_{safe_name}_{pid}"

        self.log_path = base.with_suffix(".log")
        self.mbuffer_path = base.with_suffix(".mbuffer")
        self.btrfs_out_path = base.with_suffix(".btrfs-out")
        self.err_path = base.with_suffix(".err")

        self._log_fh: IO[str] = self.log_path.open("a", encoding="utf-8", buffering=1)
        self._mbuffer_fh: IO[str] = self.mbuffer_path.open("a", encoding="utf-8", buffering=1)
        self._btrfs_out_fh: IO[str] = self.btrfs_out_path.open("a", encoding="utf-8", buffering=1)
        self._err_fh: IO[str] = self.err_path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._stderr_tail = ""

        self.info(f"Logging started: {timestamp}")
        self.info(f"LOG file: {self.log_path}")
        self.info(f"MBUFFER file: {self.mbuffer_path}")
        self.info(f"BTRFS OUT file: {self.btrfs_out_path}")
        self.info(f"ERR file: {self.err_path}")

    def close(self) -> None:
        """Close all log files."""

        self.info("Logging finished")
        self._log_fh.close()
        self._mbuffer_fh.close()
        self._btrfs_out_fh.close()
        self._err_fh.close()

    def attachment_paths(self) -> list[Path]:
        """Return run log files in the order useful for mail attachments.

        The files are returned only if they currently exist. The order is:
        .log, .err, .mbuffer, .btrfs-out. This keeps email attachments easy to
        scan and matches the names shown in README.md.
        """

        paths = [self.log_path, self.err_path, self.mbuffer_path, self.btrfs_out_path]
        return [path for path in paths if path.exists()]

    def _write(self, fh: IO[str], text: str) -> None:
        """Write text safely from possible stream-reader threads."""

        with self._lock:
            fh.write(text)
            fh.flush()

    def _remember_stderr(self, text: str, *, max_chars: int = 4000) -> None:
        """Keep a small tail of stderr for failure notifications."""

        if not text:
            return
        with self._lock:
            self._stderr_tail = (self._stderr_tail + text)[-max_chars:]

    def last_stderr_tail(self, max_chars: int = 4000) -> str:
        """Return the newest stderr text remembered for MQTT/error reports."""

        with self._lock:
            return self._stderr_tail[-max_chars:]

    def _line(self, fh: IO[str], text: str) -> None:
        """Write exactly one logical line."""

        if not text.endswith("\n"):
            text += "\n"
        self._write(fh, text)

    def info(self, text: str = "") -> None:
        """Write a normal status line to .log."""

        self._line(self._log_fh, text)

    def mbuffer(self, text: str = "") -> None:
        """Write one line to the .mbuffer transfer-progress log."""

        self._line(self._mbuffer_fh, text)

    def btrfs_out(self, text: str = "") -> None:
        """Write one line to the .btrfs-out Btrfs verbose-output log."""

        self._line(self._btrfs_out_fh, text)

    def err(self, text: str = "") -> None:
        """Write an error/stderr line to .err and remember its tail."""

        line = text if text.endswith("\n") else text + "\n"
        self._remember_stderr(line)
        self._write(self._err_fh, line)

    def command(
        self,
        label: str,
        cmd: list[str] | str,
        *,
        include_in_mbuffer: bool = False,
        include_in_btrfs_out: bool = False,
    ) -> None:
        """Record a command that is about to run.

        All commands go to .log. Transfer commands are also copied to the more
        specific stream files so .mbuffer and .btrfs-out are understandable by
        themselves when debugging a single transfer.
        """

        cmd_text = cmd if isinstance(cmd, str) else shlex.join(cmd)
        line = f"{label}: {cmd_text}"
        self.info(line)
        if include_in_mbuffer:
            self.mbuffer(line)
        if include_in_btrfs_out:
            self.btrfs_out(line)

    def completed(self, cmd: list[str] | str, returncode: int, stdout: str, stderr: str) -> None:
        """Record the output from a normal captured command."""

        self.command("COMMAND", cmd)
        self.info(f"RETURN CODE: {returncode}")
        if stdout:
            self.info("STDOUT:")
            self._write(self._log_fh, stdout if stdout.endswith("\n") else stdout + "\n")
        if stderr:
            self.info("STDERR: see .err")
            self.err("COMMAND STDERR: " + (cmd if isinstance(cmd, str) else shlex.join(cmd)))
            self._write(self._err_fh, stderr if stderr.endswith("\n") else stderr + "\n")

    def pipeline_commands(self, left_cmd: list[str], right_cmd: list[str], middle_cmd: list[str] | None = None) -> None:
        """Record send/buffer/receive commands to the appropriate logs."""

        # .btrfs-out should identify exactly which send and receive produced the
        # verbose Btrfs lines. .mbuffer also gets the full pipeline header so its
        # progress lines can be matched to the transfer that produced them.
        self.command("REMOTE SEND", left_cmd, include_in_mbuffer=True, include_in_btrfs_out=True)
        if middle_cmd:
            self.command("STREAM BUFFER", middle_cmd, include_in_mbuffer=True)
        self.command("LOCAL RECEIVE", right_cmd, include_in_mbuffer=True, include_in_btrfs_out=True)

    def pipeline_summary(self, returncode: int) -> None:
        """Record final pipeline status."""

        line = f"PIPELINE RETURN CODE: {returncode}"
        self.info(line)
        self.mbuffer(line)
        self.btrfs_out(line)

    def stream_text(
        self,
        stream_name: str,
        data: str,
        *,
        to_terminal: IO[str] | None,
        to_mbuffer: bool,
        to_btrfs_out: bool,
        to_err: bool,
    ) -> None:
        """Write live pipeline text to terminal and/or split log files."""

        if to_terminal is not None:
            to_terminal.write(data)
            to_terminal.flush()
        if to_mbuffer:
            self._write(self._mbuffer_fh, data)
        if to_btrfs_out:
            self._write(self._btrfs_out_fh, data)
        if to_err:
            if data and not data.endswith("\n"):
                data += "\n"
            tagged = f"[{stream_name}] {data}"
            self._remember_stderr(tagged)
            self._write(self._err_fh, tagged)


_current_logger: RunLogger | None = None


def get_logger() -> RunLogger | None:
    """Return the active logger, if file logging is enabled."""

    return _current_logger


@contextmanager
def active_logger(logger: RunLogger | None) -> Iterator[None]:
    """Temporarily install a run logger as the process-wide logger."""

    global _current_logger
    old = _current_logger
    _current_logger = logger
    try:
        yield
    finally:
        _current_logger = old
        if logger:
            logger.close()


def create_run_logger(log_dir: Path | None, name: str) -> RunLogger | None:
    """Create a logger when log_dir is configured; otherwise return None."""

    if log_dir is None:
        return None
    return RunLogger(log_dir=log_dir, name=name)


def tee_pipe_to_log(
    pipe,
    *,
    stream_name: str,
    terminal,
    to_mbuffer: bool,
    to_btrfs_out: bool,
    to_err: bool,
    capture: list[str] | None = None,
) -> threading.Thread:
    """Start a thread that reads bytes from a process pipe and logs them live.

    The reader uses os.read() in chunks instead of pipe.readline(). This matters
    because mbuffer often updates progress with carriage returns (\r) and may
    not emit a newline for each update.
    """

    logger = get_logger()

    def _reader() -> None:
        try:
            fd = pipe.fileno()
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                text = chunk.decode(errors="replace")
                if capture is not None:
                    capture.append(text)
                if logger:
                    logger.stream_text(
                        stream_name,
                        text,
                        to_terminal=terminal,
                        to_mbuffer=to_mbuffer,
                        to_btrfs_out=to_btrfs_out,
                        to_err=to_err,
                    )
                elif terminal is not None:
                    terminal.write(text)
                    terminal.flush()
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    thread = threading.Thread(target=_reader, name=f"log-reader-{stream_name}", daemon=True)
    thread.start()
    return thread
