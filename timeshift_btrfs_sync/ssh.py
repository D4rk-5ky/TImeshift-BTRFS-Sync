"""SSH wrapper for source-machine commands.

The app runs on the backup/destination machine. Source actions are performed by
running carefully-built remote commands through SSH.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from .commands import Completed, CommandError, run_local


@dataclass(slots=True)
class SSHConfig:
    """Connection settings for the source machine."""

    # Hostname or IP address of the machine that has the Timeshift snapshots.
    host: str

    # Optional SSH user. If omitted, SSH uses the current local user.
    user: str | None = None

    # Optional non-standard SSH port.
    port: int | None = None

    # Optional private key path.
    identity_file: str | None = None

    # Extra SSH arguments, for example ["-o", "BatchMode=yes"].
    extra_args: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        """Return the target as host or user@host."""

        return f"{self.user}@{self.host}" if self.user else self.host

    def base_command(self) -> list[str]:
        """Build the base `ssh ... target` argv list.

        The remote command string is appended by SSHRunner.command().
        """

        cmd = ["ssh"]
        if self.port:
            cmd += ["-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += self.extra_args
        cmd.append(self.target)
        return cmd


class SSHRunner:
    """Run remote commands on the configured source machine."""

    def __init__(self, config: SSHConfig):
        self.config = config

    def command(self, remote_command: str) -> list[str]:
        """Return the local argv that runs one remote shell command."""

        return self.config.base_command() + [remote_command]

    def run(self, remote_command: str, *, check: bool = True) -> Completed:
        """Run a remote command through SSH and capture stdout/stderr."""

        return run_local(self.command(remote_command), check=check)

    def test(self) -> None:
        """Verify SSH connectivity and that stdout is not polluted by banners."""

        result = self.run("printf connected", check=True)
        if result.stdout != "connected":
            raise CommandError(self.command("printf connected"), 1, result.stdout, result.stderr)
