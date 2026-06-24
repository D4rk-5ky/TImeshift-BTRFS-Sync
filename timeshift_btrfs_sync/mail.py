"""Optional email notifications for timeshift-btrfs-sync.

All SMTP/email logic lives in this module so the rest of the project can keep
notification handling small and easy to audit. It uses only Python standard
library modules: smtplib and email.message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
import json
import socket
import smtplib


@dataclass(slots=True)
class MailConfig:
    """SMTP settings for optional email notifications.

    username/password are optional. If username is blank, the SMTP connection
    is made without login. password_file is supported so passwords do not have
    to be stored directly in config.toml. Use either password or password_file,
    not both.
    """

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_ssl: bool = False
    starttls: bool = True
    username: str | None = None
    password: str | None = None
    password_file: str | None = None
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    subject_prefix: str = "[timeshift-btrfs-sync]"
    timeout: int = 10
    notify_on_success: bool = True
    notify_on_failure: bool = True
    include_json: bool = True

    def resolved_password(self) -> str | None:
        """Return password from config value or password_file."""

        if self.password_file:
            return Path(self.password_file).expanduser().read_text(encoding="utf-8").strip()
        return self.password


def utc_timestamp() -> str:
    """Return an ISO-8601 UTC timestamp suitable for notification bodies."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_payload(
    *,
    job_name: str,
    command: str,
    state: str,
    success: bool,
    exit_code: int,
    stderr_tail: str = "",
    error: str = "",
    version: str = "",
) -> dict[str, Any]:
    """Build the status payload used in the email body."""

    return {
        "state": state,
        "status": state,
        "success": success,
        "job": job_name,
        "name": job_name,
        "command": command,
        "exit_code": exit_code,
        "error": error,
        "stderr": stderr_tail,
        "timestamp": utc_timestamp(),
        "host": socket.gethostname(),
        "app": "timeshift-btrfs-sync",
        "version": version,
    }


def _subject(config: MailConfig, payload: dict[str, Any]) -> str:
    """Create a short readable subject line."""

    state = "SUCCESS" if payload.get("success") else "FAILURE"
    job = payload.get("name") or payload.get("job") or "timeshift-btrfs-sync"
    prefix = config.subject_prefix.strip()
    if prefix:
        return f"{prefix} {state}: {job}"
    return f"{state}: {job}"


def _body(config: MailConfig, payload: dict[str, Any]) -> str:
    """Create a plain-text email body from the status payload."""

    lines = [
        f"timeshift-btrfs-sync {payload.get('state', 'unknown')}",
        "",
        f"Job:       {payload.get('name', payload.get('job', 'timeshift-btrfs-sync'))}",
        f"Command:   {payload.get('command', 'unknown')}",
        f"Exit code: {payload.get('exit_code', 'unknown')}",
        f"Host:      {payload.get('host', 'unknown')}",
        f"Time UTC:  {payload.get('timestamp', '')}",
        f"Version:   {payload.get('version', '')}",
    ]
    if payload.get("error"):
        lines += ["", "Error:", str(payload.get("error"))]
    if payload.get("stderr"):
        lines += ["", "Last stderr:", str(payload.get("stderr"))]
    if config.include_json:
        lines += ["", "JSON payload:", json.dumps(payload, indent=2, sort_keys=True)]
    return "\n".join(lines).rstrip() + "\n"


def send_status(config: MailConfig, payload: dict[str, Any]) -> None:
    """Send one optional SMTP status email.

    Sending errors are raised to the caller. CLI code catches them and prints a
    warning because notification failure should not hide the real backup exit
    code.
    """

    if not config.enabled:
        return
    if not config.smtp_host:
        raise RuntimeError("mail.smtp_host is required when mail.enabled = true")
    if not config.from_addr:
        raise RuntimeError("mail.from_addr is required when mail.enabled = true")
    if not config.to_addrs:
        raise RuntimeError("mail.to_addrs must contain at least one address when mail.enabled = true")

    msg = EmailMessage()
    msg["From"] = config.from_addr
    msg["To"] = ", ".join(config.to_addrs)
    msg["Subject"] = _subject(config, payload)
    msg.set_content(_body(config, payload))

    if config.smtp_ssl:
        smtp_cls = smtplib.SMTP_SSL
    else:
        smtp_cls = smtplib.SMTP

    with smtp_cls(config.smtp_host, config.smtp_port, timeout=config.timeout) as smtp:
        if config.starttls and not config.smtp_ssl:
            smtp.starttls()
        if config.username:
            smtp.login(config.username, config.resolved_password() or "")
        smtp.send_message(msg)
