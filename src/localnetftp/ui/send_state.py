from __future__ import annotations

from pathlib import Path


def can_send(selected_peer_count: int, pending_path_count: int) -> bool:
    return selected_peer_count > 0 and pending_path_count > 0


def send_summary(selected_peer_names: list[str], pending_paths: list[Path]) -> str:
    peer_count = len(selected_peer_names)
    path_count = len(pending_paths)
    peer_text = "、".join(selected_peer_names)
    return f"准备发送 {path_count} 个项目给 {peer_count} 个用户：{peer_text}"
