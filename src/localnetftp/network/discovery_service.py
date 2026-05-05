from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from localnetftp.network.discovery import (
    DISCOVERY_PORT,
    DeviceIdentity,
    Peer,
    PeerDirectory,
    decode_hello,
    encode_hello,
)


SocketFactory = Callable[[int, int], socket.socket]


class DiscoveryService:
    def __init__(
        self,
        identity: DeviceIdentity,
        *,
        discovery_port: int = DISCOVERY_PORT,
        broadcast_interval: float = 3.0,
        peer_timeout: float = 15.0,
        socket_factory: SocketFactory = socket.socket,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if broadcast_interval <= 0:
            raise ValueError("Broadcast interval must be positive.")

        self._identity = identity
        self._discovery_port = discovery_port
        self._broadcast_interval = broadcast_interval
        self._clock = clock
        self._socket_factory = socket_factory
        self._directory = PeerDirectory(identity.device_id, timeout_seconds=peer_timeout)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._socket = self._open_socket()
        self._thread = threading.Thread(target=self._run, name="LocalNetFTPDiscovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def peers(self) -> list[Peer]:
        with self._lock:
            return self._directory.peers(now=self._clock())

    def announce_once(self) -> None:
        if self._socket is None:
            raise RuntimeError("Discovery service is not started.")
        self._send_hello(self._socket)

    def handle_datagram(self, data: bytes, address: str, seen_at: float | None = None) -> None:
        try:
            identity = decode_hello(data)
        except ValueError:
            return
        with self._lock:
            self._directory.update(identity, address, seen_at if seen_at is not None else self._clock())

    def _open_socket(self) -> socket.socket:
        sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
        sock.bind(("", self._discovery_port))
        return sock

    def _run(self) -> None:
        assert self._socket is not None
        next_broadcast = 0.0

        while not self._stop_event.is_set():
            now = self._clock()
            if now >= next_broadcast:
                self._send_hello(self._socket)
                next_broadcast = now + self._broadcast_interval

            try:
                data, sender = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            self.handle_datagram(data, sender[0], seen_at=self._clock())

    def _send_hello(self, sock: socket.socket) -> None:
        sock.sendto(encode_hello(self._identity), ("255.255.255.255", self._discovery_port))
