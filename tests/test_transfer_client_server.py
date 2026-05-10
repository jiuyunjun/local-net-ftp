import socket
import threading
import time

from localnetftp.transfer import TransferServer, send_paths
from localnetftp.transfer.protocol import (
    TRANSFER_FILE_TYPE,
    TRANSFER_REQUEST_TYPE,
    TRANSFER_VERSION,
    send_json,
    sha256_file,
)


def test_send_paths_transfers_file_and_folder(tmp_path):
    receive_dir = tmp_path / "receive"
    source_dir = tmp_path / "source"
    nested = source_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("hello", encoding="utf-8")
    single_file = tmp_path / "single.txt"
    single_file.write_text("world", encoding="utf-8")

    server = TransferServer(receive_dir=receive_dir, port=0)
    server.start()
    try:
        send_paths("127.0.0.1", server.port, [source_dir, single_file])
        _wait_for(
            lambda: (receive_dir / "source" / "nested" / "a.txt").exists()
            and (receive_dir / "single.txt").exists()
        )
    finally:
        server.stop()

    assert (receive_dir / "source" / "nested" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (receive_dir / "single.txt").read_text(encoding="utf-8") == "world"


def test_send_paths_reports_received_roots(tmp_path):
    receive_dir = tmp_path / "receive"
    source_dir = tmp_path / "source"
    nested = source_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("hello", encoding="utf-8")
    single_file = tmp_path / "single.txt"
    single_file.write_text("world", encoding="utf-8")
    received = []

    server = TransferServer(receive_dir=receive_dir, port=0, on_received=received.append)
    server.start()
    try:
        send_paths("127.0.0.1", server.port, [source_dir, single_file])
        _wait_for(lambda: received)
    finally:
        server.stop()

    assert received[0].paths == [receive_dir / "source", receive_dir / "single.txt"]


def test_send_paths_reports_progress(tmp_path):
    receive_dir = tmp_path / "receive"
    single_file = tmp_path / "single.txt"
    single_file.write_text("world", encoding="utf-8")
    events = []

    server = TransferServer(receive_dir=receive_dir, port=0)
    server.start()
    try:
        send_paths("127.0.0.1", server.port, [single_file], on_progress=events.append)
        _wait_for(lambda: (receive_dir / "single.txt").exists())
    finally:
        server.stop()

    assert [(event.event, event.relative_path, event.item_index, event.item_count) for event in events] == [
        ("start", "single.txt", 1, 1),
        ("progress", "single.txt", 1, 1),
        ("done", "single.txt", 1, 1),
    ]
    assert events[-1].bytes_sent == single_file.stat().st_size
    assert events[-1].total_bytes == single_file.stat().st_size


def test_send_paths_reports_aggregate_progress_for_multiple_files(tmp_path):
    receive_dir = tmp_path / "receive"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    first_file = source_dir / "a.txt"
    second_file = source_dir / "b.txt"
    first_file.write_bytes(b"a" * 3)
    second_file.write_bytes(b"b" * 5)
    events = []

    server = TransferServer(receive_dir=receive_dir, port=0)
    server.start()
    try:
        send_paths("127.0.0.1", server.port, [source_dir], on_progress=events.append)
        _wait_for(lambda: (receive_dir / "source" / "b.txt").exists())
    finally:
        server.stop()

    byte_events = [event for event in events if event.total_bytes]
    assert byte_events
    assert all(event.total_bytes == 8 for event in byte_events)
    assert [event.bytes_sent for event in byte_events] == sorted(event.bytes_sent for event in byte_events)
    assert byte_events[-1].bytes_sent == 8


def test_receiver_reports_progress(tmp_path):
    receive_dir = tmp_path / "receive"
    single_file = tmp_path / "single.txt"
    single_file.write_bytes(b"abc")
    received_progress = []

    server = TransferServer(receive_dir=receive_dir, port=0, on_progress=received_progress.append)
    server.start()
    try:
        send_paths("127.0.0.1", server.port, [single_file])
        _wait_for(lambda: (receive_dir / "single.txt").exists())
    finally:
        server.stop()

    assert any(event.event == "progress" and event.relative_path == "single.txt" for event in received_progress)
    assert received_progress[-1].event == "all_done"
    assert received_progress[-1].bytes_done == single_file.stat().st_size
    assert received_progress[-1].total_bytes == single_file.stat().st_size


def test_send_paths_can_cancel_before_file_transfer(tmp_path):
    receive_dir = tmp_path / "receive"
    single_file = tmp_path / "single.txt"
    single_file.write_text("world", encoding="utf-8")
    cancel_event = threading.Event()
    cancel_event.set()

    server = TransferServer(receive_dir=receive_dir, port=0)
    server.start()
    try:
        try:
            send_paths("127.0.0.1", server.port, [single_file], cancel_event=cancel_event)
        except InterruptedError:
            pass
        else:
            raise AssertionError("Expected cancelled transfer to raise InterruptedError.")
    finally:
        server.stop()

    assert not (receive_dir / "single.txt").exists()


def test_send_paths_keeps_existing_file_when_names_conflict(tmp_path):
    receive_dir = tmp_path / "receive"
    receive_dir.mkdir()
    (receive_dir / "single.txt").write_text("old", encoding="utf-8")
    single_file = tmp_path / "single.txt"
    single_file.write_text("new", encoding="utf-8")

    server = TransferServer(receive_dir=receive_dir, port=0)
    server.start()
    try:
        send_paths("127.0.0.1", server.port, [single_file])
        _wait_for(lambda: len(list(receive_dir.glob("single_*.txt"))) == 1)
    finally:
        server.stop()

    assert (receive_dir / "single.txt").read_text(encoding="utf-8") == "old"
    renamed_file = next(receive_dir.glob("single_*.txt"))
    assert renamed_file.read_text(encoding="utf-8") == "new"


def test_send_paths_resumes_partial_file(tmp_path):
    receive_dir = tmp_path / "receive"
    source_file = tmp_path / "large.bin"
    source_file.write_bytes(b"a" * 128 + b"b" * 128)

    server = TransferServer(receive_dir=receive_dir, port=0)
    server.start()
    try:
        _send_partial_file(server.port, source_file, byte_count=128)
        _wait_for(lambda: len(list(receive_dir.rglob("*.localnetftp.part"))) == 1)

        events = []
        send_paths("127.0.0.1", server.port, [source_file], on_progress=events.append)
        _wait_for(lambda: (receive_dir / "large.bin").exists())
    finally:
        server.stop()

    assert (receive_dir / "large.bin").read_bytes() == source_file.read_bytes()
    assert not list(receive_dir.rglob("*.localnetftp.part"))
    assert any(event.event == "start" and event.bytes_sent == 128 for event in events)


def test_receiver_does_not_finalize_file_with_bad_checksum(tmp_path):
    receive_dir = tmp_path / "receive"
    source_file = tmp_path / "bad.bin"
    source_file.write_bytes(b"correct content")

    server = TransferServer(receive_dir=receive_dir, port=0)
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as client:
            _send_manifest(client, source_file)
            send_json(
                client,
                {
                    "type": TRANSFER_FILE_TYPE,
                    "relative_path": source_file.name,
                    "size": source_file.stat().st_size,
                    "sha256": sha256_file(source_file),
                    "offset": 0,
                },
            )
            client.sendall(b"x" * source_file.stat().st_size)
        time.sleep(0.2)
    finally:
        server.stop()

    assert not (receive_dir / "bad.bin").exists()


def _send_partial_file(port: int, source_file, byte_count: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        _send_manifest(client, source_file)
        send_json(
            client,
            {
                "type": TRANSFER_FILE_TYPE,
                "relative_path": source_file.name,
                "size": source_file.stat().st_size,
                "sha256": sha256_file(source_file),
                "offset": 0,
            },
        )
        with source_file.open("rb") as file:
            client.sendall(file.read(byte_count))


def _send_manifest(client: socket.socket, source_file) -> None:
    from localnetftp.transfer.protocol import recv_json

    send_json(
        client,
        {
            "type": TRANSFER_REQUEST_TYPE,
            "version": TRANSFER_VERSION,
            "items": [
                {
                    "relative_path": source_file.name,
                    "is_dir": False,
                    "size": source_file.stat().st_size,
                    "sha256": sha256_file(source_file),
                }
            ],
        },
    )
    ack = recv_json(client)
    assert ack["accepted"] is True


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for transfer.")
