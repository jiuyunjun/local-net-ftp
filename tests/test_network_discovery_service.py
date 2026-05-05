import socket

import pytest

from localnetftp.network import DeviceIdentity, DiscoveryService, encode_hello


class FakeSocket:
    def __init__(self) -> None:
        self.options = []
        self.timeout = None
        self.bound = None
        self.sent = []
        self.closed = False

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def settimeout(self, timeout):
        self.timeout = timeout

    def bind(self, address):
        self.bound = address

    def sendto(self, data, address):
        self.sent.append((data, address))

    def recvfrom(self, buffer_size):
        raise socket.timeout()

    def close(self):
        self.closed = True


def test_announce_once_sends_broadcast_hello():
    fake_socket = FakeSocket()
    identity = DeviceIdentity("local", "Local", "host-local", 49200)
    service = DiscoveryService(
        identity,
        discovery_port=47832,
        socket_factory=lambda family, kind: fake_socket,
    )

    service.start()
    service.announce_once()
    service.stop()

    assert fake_socket.bound == ("", 47832)
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in fake_socket.options
    assert fake_socket.sent[-1] == (encode_hello(identity), ("255.255.255.255", 47832))
    assert fake_socket.closed is True


def test_announce_once_requires_start():
    service = DiscoveryService(DeviceIdentity("local", "Local", "host-local", 49200))

    with pytest.raises(RuntimeError, match="not started"):
        service.announce_once()


def test_handle_datagram_updates_remote_peer():
    local = DeviceIdentity("local", "Local", "host-local", 49200)
    remote = DeviceIdentity("remote", "Remote", "host-remote", 49200)
    service = DiscoveryService(local, clock=lambda: 10.0)

    service.handle_datagram(encode_hello(remote), "192.168.1.7", seen_at=9.0)

    peers = service.peers()
    assert len(peers) == 1
    assert peers[0].identity == remote
    assert peers[0].address == "192.168.1.7"


def test_handle_datagram_ignores_invalid_messages():
    service = DiscoveryService(DeviceIdentity("local", "Local", "host-local", 49200))

    service.handle_datagram(b"{", "192.168.1.7", seen_at=9.0)

    assert service.peers() == []
