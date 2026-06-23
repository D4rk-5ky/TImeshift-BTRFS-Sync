"""SSH wrapper for running commands on the source machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from .commands import Completed, CommandError, run_local


@dataclass(slots=True)
class SSHConfig:
    """Connection settings for the source machine."""

    host: str
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    extra_args: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def base_command(self) -> list[str]:
        cmd = ["ssh"]
        if self.port:
            cmd += ["-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += self.extra_args
        cmd.append(self.target)
        return cmd


class SSHRunner:
    """Run remote commands through SSH."""

    def __init__(self, config: SSHConfig):
        self.config = config

    def command(self, remote_command: str) -> list[str]:
        return self.config.base_command() + [remote_command]

    def run(self, remote_command: str, *, check: bool = True) -> Completed:
        return run_local(self.command(remote_command), check=check)

    def test(self) -> None:
        result = self.run("printf connected", check=True)
        if result.stdout != "connected":
            raise CommandError(self.command("printf connected"), 1, result.stdout, result.stderr)
