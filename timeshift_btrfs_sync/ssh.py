"""SSH command construction.

Supports key-based auth, optional sshpass password auth, SSH compression, and a
chosen SSH cipher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from .commands import Completed, CommandError, run_local


def _is_relative_to(path: Path, root: Path) -> bool:
    """Return True when path is root or below root without broad string matching."""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_control_path_safety(control_path: str | None) -> None:
    """Validate that an SSH ControlPath socket directory is private.

    OpenSSH ControlMaster creates a local Unix-domain control socket. Any local
    user that can access that socket may be able to reuse the already
    authenticated SSH connection without unlocking the private key again. The app
    therefore requires an explicit absolute ControlPath whose parent directory
    already exists, is owned by the current user, and is not accessible by group
    or other users. Shared temporary locations are rejected even when a nested
    directory appears private, because they are easy to configure incorrectly.
    """

    if not control_path:
        raise ValueError(
            "ssh.control_path must be set when ssh.control_master is true; "
            "use a private directory such as /run/ts-btrfs-ssh/%C"
        )

    expanded = Path(control_path).expanduser()
    if not expanded.is_absolute():
        raise ValueError("ssh.control_path must be an absolute path when ssh.control_master is true")

    parent = expanded.parent
    unsafe_roots = [Path("/tmp"), Path("/var/tmp"), Path("/dev/shm")]
    for unsafe_root in unsafe_roots:
        if _is_relative_to(parent, unsafe_root):
            raise ValueError(
                f"ssh.control_path parent must not be inside shared temporary storage: {parent}. "
                "Use a private directory such as /run/ts-btrfs-ssh owned by the user running ts-btrfs."
            )

    if not parent.exists():
        raise ValueError(
            f"ssh.control_path parent directory does not exist: {parent}. "
            "Create it first with mkdir -p, chown it to the user running ts-btrfs, and chmod it 0700."
        )
    if not parent.is_dir():
        raise ValueError(f"ssh.control_path parent is not a directory: {parent}")

    stat_result = parent.stat()
    current_uid = os.geteuid()
    if stat_result.st_uid != current_uid:
        raise ValueError(
            f"ssh.control_path parent must be owned by the user running ts-btrfs: {parent}. "
            f"owner uid is {stat_result.st_uid}, current uid is {current_uid}."
        )

    if stat_result.st_mode & 0o077:
        raise ValueError(
            f"ssh.control_path parent must be private: {parent}. "
            "Run chmod 0700 on that directory before enabling ssh.control_master."
        )


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
    control_master: bool = False
    control_persist: str | None = None
    control_path: str | None = None
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
        if self.control_master:
            cmd += ["-o", "ControlMaster=auto"]
            cmd += ["-o", f"ControlPersist={self.control_persist or '10m'}"]
            if self.control_path:
                cmd += ["-o", f"ControlPath={self.control_path}"]
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

    def run(
        self,
        remote_command: str,
        *,
        check: bool = True,
        log_stderr: bool = True,
        mirror_stderr: bool = True,
        mirror_stdout_on_failure: bool = False,
    ) -> Completed:
        """Run a remote command and capture stdout/stderr.

        Stderr is always mirrored to the terminal and to .err when file logging
        is enabled. The log_stderr/mirror_stderr arguments are kept only for
        compatibility with older callers and no longer suppress stderr.
        """

        return run_local(
            self.command(remote_command),
            check=check,
            env=self.config.environment(),
            log_stderr=log_stderr,
            mirror_stderr=mirror_stderr,
            mirror_stdout_on_failure=mirror_stdout_on_failure,
        )

    def environment(self) -> dict[str, str] | None:
        """Return SSH environment for streaming pipeline calls."""

        return self.config.environment()

    def test(self) -> None:
        """Verify SSH works and stdout is not polluted by banners."""

        result = self.run("printf connected", check=True)
        if result.stdout != "connected":
            raise CommandError(self.command("printf connected"), 1, result.stdout, result.stderr)
