"""Optional MQTT notifications for timeshift-btrfs-sync.

This module intentionally contains all MQTT publishing logic. The rest of the
project only builds a small status payload and calls publish_status().

The implementation imports paho-mqtt lazily. That keeps the main backup tool
usable without paho-mqtt installed when [mqtt].enabled is false.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import json
import socket


@dataclass(slots=True)
class MQTTConfig:
    """MQTT broker and publish settings.

    username/password are optional. If username is blank, anonymous MQTT is used.
    password_file is supported so passwords do not have to be stored directly in
    config.toml. Use either password or password_file, not both.
    """

    enabled: bool = False
    host: str = ""
    port: int = 1883
    topic: str = "timeshift-btrfs-sync/status"
    username: str | None = None
    password: str | None = None
    password_file: str | None = None
    client_id: str | None = None
    qos: int = 0
    retain: bool = False
    timeout: int = 10
    notify_on_success: bool = True
    notify_on_failure: bool = True

    def resolved_password(self) -> str | None:
        """Return password from config value or password_file."""

        if self.password_file:
            with open(self.password_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        return self.password


def utc_timestamp() -> str:
    """Return an ISO-8601 UTC timestamp suitable for JSON payloads."""

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
    """Build the Home-Assistant-friendly JSON status payload.

    The payload intentionally uses simple top-level keys so Home Assistant can
    use it directly with MQTT sensors, templates, or automations.
    """

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


def publish_status(config: MQTTConfig, payload: dict[str, Any]) -> None:
    """Publish one JSON MQTT status message.

    Publishing errors are raised to the caller. CLI code catches them and prints
    a warning because notification failure should not hide the real backup exit
    code.
    """

    if not config.enabled:
        return
    if not config.host:
        raise RuntimeError("mqtt.host is required when mqtt.enabled = true")

    try:
        import paho.mqtt.client as mqtt_client
    except ImportError as exc:
        raise RuntimeError(
            "paho-mqtt is required for MQTT notifications. Install with: "
            "python3 -m pip install -e '.[mqtt]'"
        ) from exc

    client_id = config.client_id or f"timeshift-btrfs-sync-{socket.gethostname()}"

    # paho-mqtt 2.x supports callback_api_version. paho-mqtt 1.x does not.
    try:
        client = mqtt_client.Client(
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
    except (AttributeError, TypeError):
        client = mqtt_client.Client(client_id=client_id)

    if config.username:
        client.username_pw_set(config.username, config.resolved_password())

    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    client.connect(config.host, config.port, keepalive=max(config.timeout, 5))
    try:
        info = client.publish(config.topic, payload=data, qos=config.qos, retain=config.retain)
        info.wait_for_publish(timeout=config.timeout)
        if not info.is_published():
            raise RuntimeError("MQTT publish did not finish before timeout")
    finally:
        client.disconnect()
