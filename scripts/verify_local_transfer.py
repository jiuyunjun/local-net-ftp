from __future__ import annotations

import socket
import sys
import tempfile
import time
from pathlib import Path

from localnetftp.transfer import TransferServer, send_paths


def main() -> int:
    _prefer_utf8_stdio()
    with tempfile.TemporaryDirectory(prefix="localnetftp-verify-") as temp_dir:
        workspace = Path(temp_dir)
        a_source = workspace / "a-source"
        b_source = workspace / "b-source"
        a_receive = workspace / "a-download"
        b_receive = workspace / "b-download"
        for directory in (a_source, b_source, a_receive, b_receive):
            directory.mkdir(parents=True)

        _write_demo_payloads(a_source, b_source)

        a_port = _free_port()
        b_port = _free_port()
        a_server = TransferServer(receive_dir=a_receive, port=a_port, host="127.0.0.1")
        b_server = TransferServer(receive_dir=b_receive, port=b_port, host="127.0.0.1")

        try:
            a_server.start()
            b_server.start()

            print(f"A 接收端: 127.0.0.1:{a_port}")
            print(f"B 接收端: 127.0.0.1:{b_port}")

            send_paths("127.0.0.1", b_port, [a_source / "hello.txt", a_source / "folder"])
            send_paths("127.0.0.1", a_port, [b_source / "reply.txt"])

            _wait_for(lambda: (b_receive / "hello.txt").exists())
            _wait_for(lambda: (b_receive / "folder" / "nested.txt").exists())
            _wait_for(lambda: (a_receive / "reply.txt").exists())

            assert (b_receive / "hello.txt").read_text(encoding="utf-8") == "hello from A"
            assert (b_receive / "folder" / "nested.txt").read_text(encoding="utf-8") == "nested from A"
            assert (a_receive / "reply.txt").read_text(encoding="utf-8") == "reply from B"

            print("本机双向传输验证通过。")
            print(f"临时目录已自动清理: {workspace}")
            return 0
        finally:
            a_server.stop()
            b_server.stop()


def _write_demo_payloads(a_source: Path, b_source: Path) -> None:
    (a_source / "hello.txt").write_text("hello from A", encoding="utf-8")
    (a_source / "folder").mkdir()
    (a_source / "folder" / "nested.txt").write_text("nested from A", encoding="utf-8")
    (b_source / "reply.txt").write_text("reply from B", encoding="utf-8")


def _prefer_utf8_stdio() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError("Timed out while waiting for local transfer verification.")


if __name__ == "__main__":
    raise SystemExit(main())
