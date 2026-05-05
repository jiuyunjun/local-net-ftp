import socket
import time

from localnetftp.transfer import TransferServer, send_paths


def test_send_paths_transfers_file_and_folder(tmp_path):
    receive_dir = tmp_path / "receive"
    source_dir = tmp_path / "source"
    nested = source_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("hello", encoding="utf-8")
    single_file = tmp_path / "single.txt"
    single_file.write_text("world", encoding="utf-8")

    server = TransferServer(receive_dir=receive_dir, port=_free_port())
    server.start()
    try:
        send_paths("127.0.0.1", server._port, [source_dir, single_file])
        _wait_for(lambda: (receive_dir / "source" / "nested" / "a.txt").exists())
    finally:
        server.stop()

    assert (receive_dir / "source" / "nested" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (receive_dir / "single.txt").read_text(encoding="utf-8") == "world"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for transfer.")
