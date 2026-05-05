from __future__ import annotations

import json
import socket
import uuid
from dataclasses import dataclass
from typing import Any


DISCOVERY_PORT = 47832
DISCOVERY_VERSION = 1
DISCOVERY_MESSAGE_TYPE = "localnetftp.hello"


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    device_name: str
    host_name: str
    listen_port: int


@dataclass(frozen=True)
class Peer:
    identity: DeviceIdentity
    address: str
    last_seen: float


def create_device_identity(device_name: str, listen_port: int, device_id: str | None = None) -> DeviceIdentity:
    normalized_name = device_name.strip()
    if not normalized_name:
        raise ValueError("Device name must not be empty.")
    if not 0 < listen_port <= 65535:
        raise ValueError("Listen port must be between 1 and 65535.")

    return DeviceIdentity(
        device_id=device_id or str(uuid.uuid4()),
        device_name=normalized_name,
        host_name=socket.gethostname(),
        listen_port=listen_port,
    )


def encode_hello(identity: DeviceIdentity) -> bytes:
    payload = {
        "type": DISCOVERY_MESSAGE_TYPE,
        "version": DISCOVERY_VERSION,
        "device_id": identity.device_id,
        "device_name": identity.device_name,
        "host_name": identity.host_name,
        "listen_port": identity.listen_port,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_hello(data: bytes) -> DeviceIdentity:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Discovery message must be valid UTF-8 JSON.") from exc

    if not isinstance(payload, dict):
        raise ValueError("Discovery message must be a JSON object.")
    if payload.get("type") != DISCOVERY_MESSAGE_TYPE:
        raise ValueError("Discovery message has an unsupported type.")
    if payload.get("version") != DISCOVERY_VERSION:
        raise ValueError("Discovery message has an unsupported version.")

    device_id = _required_string(payload, "device_id")
    device_name = _required_string(payload, "device_name")
    host_name = _required_string(payload, "host_name")
    listen_port = payload.get("listen_port")
    if not isinstance(listen_port, int) or not 0 < listen_port <= 65535:
        raise ValueError("Discovery field 'listen_port' must be a valid TCP port.")

    return DeviceIdentity(
        device_id=device_id,
        device_name=device_name,
        host_name=host_name,
        listen_port=listen_port,
    )


class PeerDirectory:
    def __init__(self, local_device_id: str, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Peer timeout must be positive.")
        self._local_device_id = local_device_id
        self._timeout_seconds = timeout_seconds
        self._peers: dict[str, Peer] = {}

    def update(self, identity: DeviceIdentity, address: str, seen_at: float) -> None:
        if identity.device_id == self._local_device_id:
            return
        self._peers[identity.device_id] = Peer(identity=identity, address=address, last_seen=seen_at)

    def prune(self, now: float) -> None:
        expired = [
            device_id
            for device_id, peer in self._peers.items()
            if now - peer.last_seen > self._timeout_seconds
        ]
        for device_id in expired:
            del self._peers[device_id]

    def peers(self, now: float | None = None) -> list[Peer]:
        if now is not None:
            self.prune(now)
        return sorted(self._peers.values(), key=lambda peer: peer.identity.device_name.casefold())


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"Discovery field '{field}' must be a non-empty string.")
