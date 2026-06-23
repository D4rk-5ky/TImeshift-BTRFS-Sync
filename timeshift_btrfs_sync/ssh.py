"""SSH helper used by the destination-pull workflow.

The app runs on the backup machine and reaches into the source machine over SSH.
This wrapper builds the SSH command, runs remote shell snippets, and provides a
small connectivity test.
"""

from __future__ import annotations

from .commands import Completed, CommandError, quote_join, run_local, sudo_prefix
from .config import SSHConfig


class SSHRunner:
    """Run commands on the configured source machine over SSH."""

    def __init__(self, config: SSHConfig):
        # Store the validated SSHConfig so every command uses the same host,
        # user, port, identity file, and extra ssh options.
        self.config = config

    def command(self, remote_command: str) -> list[str]:
        """Return a local argv list that runs one remote shell command."""

        return self.config.base_command() + [remote_command]

    def run(self, remote_command: str, *, check: bool = True) -> Completed:
        """Run a remote command and capture the result.

        This still uses run_local() because `ssh ... <remote command>` is a
        local process from Python's point of view.
        """

        return run_local(self.command(remote_command), check=check)

    def sudo_command(self, sudo: str, args: list[str]) -> str:
        """Build a quoted remote command with the configured sudo prefix."""

        return quote_join(sudo_prefix(sudo) + args)

    def test(self) -> None:
        """Verify that SSH works and stdout is not polluted by login banners."""

        result = self.run("printf connected", check=True)
        if result.stdout != "connected":
            raise CommandError(self.command("printf connected"), 1, result.stdout, result.stderr)
