"""Source command runner for SSH and local source modes."""

from __future__ import annotations

from dataclasses import dataclass

from .commands import Completed, CommandError, run_local
from .ssh import SSHRunner


@dataclass(slots=True)
class SourceRunner:
    """Run source-side commands either over SSH or locally.

    The rest of the app treats the source as a command endpoint. In the
    original mode that endpoint is an SSH session. In local mode it is the same
    machine running ``ts-btrfs``. Local mode still uses the source-side sudo and
    command settings; it only removes the SSH wrapper.
    """

    mode: str
    ssh: SSHRunner | None = None

    @classmethod
    def from_config(cls, config) -> "SourceRunner":
        """Create a source runner from validated app config."""

        if config.source.mode == "local":
            return cls(mode="local")
        return cls(mode="ssh", ssh=SSHRunner(config.ssh))

    @property
    def uses_ssh(self) -> bool:
        """Return True when source commands are executed through SSH."""

        return self.ssh is not None

    @property
    def location(self) -> str:
        """Return the metadata location label used by Btrfs helpers."""

        return "remote" if self.uses_ssh else "local"

    @property
    def display_location(self) -> str:
        """Return human text for source status output."""

        return "remote SSH source" if self.uses_ssh else "local source"

    def command(self, source_shell_command: str) -> list[str]:
        """Return argv that runs one source-side shell command."""

        if self.ssh is not None:
            return self.ssh.command(source_shell_command)
        return ["sh", "-c", source_shell_command]

    def run(
        self,
        source_shell_command: str,
        *,
        check: bool = True,
        log_stderr: bool = True,
        mirror_stderr: bool = True,
        mirror_stdout_on_failure: bool = False,
    ) -> Completed:
        """Run one source-side command and capture stdout/stderr."""

        if self.ssh is not None:
            return self.ssh.run(
                source_shell_command,
                check=check,
                log_stderr=log_stderr,
                mirror_stderr=mirror_stderr,
                mirror_stdout_on_failure=mirror_stdout_on_failure,
            )
        return run_local(
            self.command(source_shell_command),
            check=check,
            log_stderr=log_stderr,
            mirror_stderr=mirror_stderr,
            mirror_stdout_on_failure=mirror_stdout_on_failure,
        )

    def environment(self) -> dict[str, str] | None:
        """Return environment needed for streaming source commands."""

        if self.ssh is None:
            return None
        return self.ssh.environment()

    def test(self) -> None:
        """Verify that the source command endpoint is usable."""

        if self.ssh is not None:
            self.ssh.test()
            return
        result = self.run("printf connected", check=True)
        if result.stdout != "connected":
            raise CommandError(self.command("printf connected"), 1, result.stdout, result.stderr)
