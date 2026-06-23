"""SSH wrapper for source-machine commands.

The app runs on the backup/destination machine. Source actions are performed by
running carefully-built remote commands through SSH.

SSH authentication supports:
  - normal SSH agent/default config,
  - an identity file configured in TOML,
  - optional password/password_file through sshpass on the destination.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
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

    # Optional private key path. This is the recommended unattended auth method.
    identity_file: str | None = None

    # Optional SSH password stored directly in TOML. This works, but is less
    # secure than identity_file. It requires sshpass installed on the destination.
    password: str | None = None

    # Optional file containing the SSH password. This is safer than putting the
    # password directly in TOML, especially if the file is chmod 600.
    password_file: str | None = None

    # Extra SSH arguments, for example ["-o", "StrictHostKeyChecking=accept-new"].
    # Do not use BatchMode=yes with password/password_file, because BatchMode
    # disables password prompts that sshpass needs to answer.
    extra_args: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        """Return the target as host or user@host."""

        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def uses_password_auth(self) -> bool:
        """Return True when sshpass should be used for SSH password auth."""

        return bool(self.password or self.password_file)

    def _read_password(self) -> str | None:
        """Return the SSH password from TOML or password_file.

        The password is not printed and is not added to the command line. It is
        passed to sshpass through the SSHPASS environment variable.
        """

        if self.password is not None:
            return self.password
        if self.password_file:
            return Path(self.password_file).expanduser().read_text(encoding="utf-8").rstrip("\n")
        return None

    def environment(self) -> dict[str, str] | None:
        """Return environment variables needed by the SSH command.

        sshpass with `-e` reads the password from SSHPASS. Returning None means
        no special environment is needed.
        """

        password = self._read_password()
        if password is None:
            return None
        return {"SSHPASS": password}

    def base_command(self) -> list[str]:
        """Build the base SSH argv list.

        If password/password_file is configured, the command becomes:

            sshpass -e ssh ...

        The remote command string is appended by SSHRunner.command().
        """

        cmd: list[str] = []
        if self.uses_password_auth:
            cmd += ["sshpass", "-e"]

        cmd.append("ssh")
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

        return run_local(self.command(remote_command), check=check, env=self.config.environment())

    def environment(self) -> dict[str, str] | None:
        """Expose SSH environment for streaming pipelines."""

        return self.config.environment()

    def test(self) -> None:
        """Verify SSH connectivity and that stdout is not polluted by banners."""

        result = self.run("printf connected", check=True)
        if result.stdout != "connected":
            raise CommandError(self.command("printf connected"), 1, result.stdout, result.stderr)
