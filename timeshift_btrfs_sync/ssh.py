"""SSH command construction.

Supports key-based auth, optional sshpass password auth, SSH compression, and a
chosen SSH cipher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from .commands import Completed, CommandError, run_local


@dataclass(slots=True)
class SSHConfig:
    """Connection and SSH transport settings."""

    host: str
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    password: str | None = None
    password_file: str | None = None
    compression: bool = False
    cipher: str | None = None
    extra_args: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        """Return host or user@host."""

        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def uses_password_auth(self) -> bool:
        """Return True when sshpass is needed."""

        return bool(self.password or self.password_file)

    def _read_password(self) -> str | None:
        """Read password from TOML or password_file."""

        if self.password is not None:
            return self.password
        if self.password_file:
            return Path(self.password_file).expanduser().read_text(encoding="utf-8").rstrip("\n")
        return None

    def environment(self) -> dict[str, str] | None:
        """Return environment variables required by sshpass."""

        password = self._read_password()
        if password is None:
            return None
        return {"SSHPASS": password}

    def base_command(self) -> list[str]:
        """Build base SSH argv; remote command is appended later."""

        cmd: list[str] = []
        if self.uses_password_auth:
            cmd += ["sshpass", "-e"]
        cmd.append("ssh")
        if self.port:
            cmd += ["-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        if self.compression:
            cmd += ["-C"]
        if self.cipher:
            cmd += ["-c", self.cipher]
        cmd += self.extra_args
        cmd.append(self.target)
        return cmd


class SSHRunner:
    """Run remote commands through SSH."""

    def __init__(self, config: SSHConfig):
        self.config = config

    def command(self, remote_command: str) -> list[str]:
        """Return argv for one SSH remote command."""

        return self.config.base_command() + [remote_command]

    def run(self, remote_command: str, *, check: bool = True) -> Completed:
        """Run a remote command and capture stdout/stderr."""

        return run_local(self.command(remote_command), check=check, env=self.config.environment())

    def environment(self) -> dict[str, str] | None:
        """Return SSH environment for streaming pipeline calls."""

        return self.config.environment()

    def test(self) -> None:
        """Verify SSH works and stdout is not polluted by banners."""

        result = self.run("printf connected", check=True)
        if result.stdout != "connected":
            raise CommandError(self.command("printf connected"), 1, result.stdout, result.stderr)
