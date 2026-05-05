from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from localnetftp.network.discovery import DeviceIdentity, Peer, safe_device_id


@dataclass(frozen=True)
class LocalPeerRegistry:
    path: Path
    stale_seconds: float = 20.0

    def publish(self, identity: DeviceIdentity) -> None:
        now = time.monotonic()
        data = self._read()
        data[identity.device_id] = {
            "device_id": identity.device_id,
            "device_name": identity.device_name,
            "host_name": identity.host_name,
            "listen_port": identity.listen_port,
            "updated_at": now,
        }
        self._write(self._without_stale(data, now))

    def peers(self, local_device_id: str) -> list[Peer]:
        now = time.monotonic()
        data = self._without_stale(self._read(), now)
        self._write(data)
        peers: list[Peer] = []
        for device_id, item in data.items():
            if device_id == local_device_id:
                continue
            try:
                peers.append(
                    Peer(
                        identity=DeviceIdentity(
                            device_id=safe_device_id(item["device_id"]),
                            device_name=_required_string(item, "device_name"),
                            host_name=_required_string(item, "host_name"),
                            listen_port=_required_port(item.get("listen_port")),
                        ),
                        address="127.0.0.1",
                        last_seen=float(item["updated_at"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(peers, key=lambda peer: peer.identity.device_name.casefold())

    def remove(self, local_device_id: str) -> None:
        data = self._read()
        data.pop(local_device_id, None)
        self._write(data)

    def _read(self) -> dict[str, dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temp_path.replace(self.path)

    def _without_stale(self, data: dict[str, dict], now: float) -> dict[str, dict]:
        fresh = {}
        for device_id, item in data.items():
            if not isinstance(item, dict):
                continue
            updated_at = item.get("updated_at")
            if isinstance(updated_at, (int, float)) and now - float(updated_at) <= self.stale_seconds:
                fresh[device_id] = item
        return fresh


def _required_string(item: dict, field: str) -> str:
    value = item.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"Local peer field '{field}' must be a non-empty string.")


def _required_port(value: object) -> int:
    if isinstance(value, int) and 0 < value <= 65535:
        return value
    raise ValueError("Local peer listen port must be valid.")
