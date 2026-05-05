from localnetftp.network import DeviceIdentity, LocalPeerRegistry


def test_local_peer_registry_returns_other_local_peers(tmp_path):
    registry = LocalPeerRegistry(tmp_path / "peers.json")
    local = DeviceIdentity("dev-a", "A", "host", 49210)
    remote = DeviceIdentity("dev-b", "B", "host", 49211)

    registry.publish(local)
    registry.publish(remote)

    peers = registry.peers(local.device_id)

    assert [(peer.identity.device_name, peer.address, peer.identity.listen_port) for peer in peers] == [
        ("B", "127.0.0.1", 49211)
    ]


def test_local_peer_registry_removes_local_peer(tmp_path):
    registry = LocalPeerRegistry(tmp_path / "peers.json")
    local = DeviceIdentity("dev-a", "A", "host", 49210)

    registry.publish(local)
    registry.remove(local.device_id)

    assert registry.peers(local.device_id) == []
