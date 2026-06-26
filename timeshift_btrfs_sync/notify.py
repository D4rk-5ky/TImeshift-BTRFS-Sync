"""Shared notification payload helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import socket


def utc_timestamp() -> str:
    """Return a compact ISO-8601 UTC timestamp for notifications."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_notification_payload(
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
    """Build the shared status payload used by MQTT and email."""

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
