import json
import socket

import pytest

from localnetftp.network import (
    DISCOVERY_MESSAGE_TYPE,
    DISCOVERY_VERSION,
    DeviceIdentity,
    PeerDirectory,
    create_device_identity,
    decode_hello,
    encode_hello,
)


def test_create_device_identity_uses_device_name_and_host(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "host-a")

    identity = create_device_identity("  A PC  ", listen_port=49200, device_id="device-a")

    assert identity == DeviceIdentity(
        device_id="device-a",
        device_name="A PC",
        host_name="host-a",
        listen_port=49200,
    )


def test_create_device_identity_rejects_invalid_values():
    with pytest.raises(ValueError, match="Device name"):
        create_device_identity(" ", listen_port=49200)

    with pytest.raises(ValueError, match="Listen port"):
        create_device_identity("A PC", listen_port=70000)


def test_encode_and_decode_hello_round_trip():
    identity = DeviceIdentity(
        device_id="device-a",
        device_name="A PC",
        host_name="host-a",
        listen_port=49200,
    )

    decoded = decode_hello(encode_hello(identity))

    assert decoded == identity


def test_encode_hello_uses_expected_wire_fields():
    identity = DeviceIdentity(
        device_id="device-a",
        device_name="A PC",
        host_name="host-a",
        listen_port=49200,
    )

    payload = json.loads(encode_hello(identity).decode("utf-8"))

    assert payload == {
        "type": DISCOVERY_MESSAGE_TYPE,
        "version": DISCOVERY_VERSION,
        "device_id": "device-a",
        "device_name": "A PC",
        "host_name": "host-a",
        "listen_port": 49200,
    }


def test_decode_hello_rejects_invalid_message():
    with pytest.raises(ValueError, match="UTF-8 JSON"):
        decode_hello(b"{")

    with pytest.raises(ValueError, match="unsupported type"):
        decode_hello(json.dumps({"type": "other", "version": DISCOVERY_VERSION}).encode("utf-8"))


def test_peer_directory_tracks_remote_peers_and_ignores_self():
    directory = PeerDirectory(local_device_id="local", timeout_seconds=10)
    local = DeviceIdentity("local", "Local", "host-local", 49200)
    remote = DeviceIdentity("remote", "Remote", "host-remote", 49200)

    directory.update(local, "192.168.1.2", seen_at=1)
    directory.update(remote, "192.168.1.3", seen_at=2)

    peers = directory.peers()

    assert len(peers) == 1
    assert peers[0].identity == remote
    assert peers[0].address == "192.168.1.3"


def test_peer_directory_prunes_expired_peers():
    directory = PeerDirectory(local_device_id="local", timeout_seconds=10)
    old_peer = DeviceIdentity("old", "Old", "host-old", 49200)
    fresh_peer = DeviceIdentity("fresh", "Fresh", "host-fresh", 49200)

    directory.update(old_peer, "192.168.1.3", seen_at=1)
    directory.update(fresh_peer, "192.168.1.4", seen_at=8)

    assert [peer.identity.device_id for peer in directory.peers(now=12)] == ["fresh"]
