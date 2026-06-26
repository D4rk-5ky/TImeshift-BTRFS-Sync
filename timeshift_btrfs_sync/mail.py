"""Optional email notifications for timeshift-btrfs-sync.

All SMTP/email logic lives in this module so the rest of the project can keep
notification handling small and easy to audit. It uses only Python standard
library modules: smtplib and email.message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
import json
import mimetypes
import smtplib


@dataclass(slots=True)
class MailConfig:
    """SMTP settings for optional email notifications.

    username/password are optional. If username is blank, the SMTP connection
    is made without login. password_file is supported so passwords do not have
    to be stored directly in config.toml. Use either password or password_file,
    not both.

    attach_logs attaches the run's split log files when file logging is enabled:
    .log, .err, .btrfs, .mbuffer, and .succes. Empty files are not attached.
    max_attachment_bytes can optionally prevent very large verbose logs from
    being attached. Set it to 0 for no cap.
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
    attach_logs: bool = True
    max_attachment_bytes: int = 0

    def resolved_password(self) -> str | None:
        """Return password from config value or password_file."""

        if self.password_file:
            return Path(self.password_file).expanduser().read_text(encoding="utf-8").strip()
        return self.password



def _subject(config: MailConfig, payload: dict[str, Any]) -> str:
    """Create a short readable subject line."""

    state = "SUCCESS" if payload.get("success") else "FAILURE"
    job = payload.get("name") or payload.get("job") or "timeshift-btrfs-sync"
    prefix = config.subject_prefix.strip()
    if prefix:
        return f"{prefix} {state}: {job}"
    return f"{state}: {job}"


def _body(config: MailConfig, payload: dict[str, Any], *, attached: list[Path], skipped: list[str]) -> str:
    """Create a fallback plain-text email body from the status payload."""

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
    if attached:
        lines += ["", "Attached log files:"]
        lines += [f"- {path.name}" for path in attached]
    if skipped:
        lines += ["", "Log files not attached:"]
        lines += [f"- {item}" for item in skipped]
    if config.include_json:
        lines += ["", "JSON payload:", json.dumps(payload, indent=2, sort_keys=True)]
    return "\n".join(lines).rstrip() + "\n"



def _success_body_from_paths(paths: Iterable[str | Path] | None) -> str | None:
    """Return the text content of the non-empty .succes file, if present."""

    if not paths:
        return None
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.suffix != ".succes":
            continue
        try:
            if path.is_file() and path.stat().st_size > 0:
                text = path.read_text(encoding="utf-8", errors="replace")
                return text if text.endswith("\n") else text + "\n"
        except OSError:
            continue
    return None

def _filter_attachments(config: MailConfig, paths: Iterable[str | Path] | None) -> tuple[list[Path], list[str]]:
    """Return existing attachment paths and human-readable skipped reasons."""

    attached: list[Path] = []
    skipped: list[str] = []
    if not config.attach_logs or not paths:
        return attached, skipped

    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue
        if not path.is_file():
            skipped.append(f"{path.name}: not a regular file")
            continue
        size = path.stat().st_size
        if size == 0:
            skipped.append(f"{path.name}: empty file, not attached")
            continue
        if config.max_attachment_bytes > 0 and size > config.max_attachment_bytes:
            skipped.append(f"{path.name}: {size} bytes exceeds mail.max_attachment_bytes={config.max_attachment_bytes}")
            continue
        attached.append(path)
    return attached, skipped


def _attach_file(msg: EmailMessage, path: Path) -> None:
    """Attach one file to an EmailMessage."""

    content_type, _encoding = mimetypes.guess_type(path.name)
    if content_type is None:
        content_type = "text/plain"
    maintype, subtype = content_type.split("/", 1)
    data = path.read_bytes()
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)


def send_status(config: MailConfig, payload: dict[str, Any], *, attachments: Iterable[str | Path] | None = None) -> None:
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

    success_body = _success_body_from_paths(attachments)
    attached, skipped = _filter_attachments(config, attachments)

    msg = EmailMessage()
    msg["From"] = config.from_addr
    msg["To"] = ", ".join(config.to_addrs)
    msg["Subject"] = _subject(config, payload)
    msg.set_content(success_body or _body(config, payload, attached=attached, skipped=skipped))

    for path in attached:
        _attach_file(msg, path)

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
