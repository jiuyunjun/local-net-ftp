from __future__ import annotations

from pathlib import Path

MAX_CONFIRMATION_ITEMS = 8
MAX_CONFIRMATION_RECIPIENTS = 6


def can_send(selected_peer_count: int, pending_path_count: int) -> bool:
    return selected_peer_count > 0 and pending_path_count > 0


def send_summary(selected_peer_names: list[str], pending_paths: list[Path]) -> str:
    peer_count = len(selected_peer_names)
    path_count = len(pending_paths)
    peer_text = "、".join(selected_peer_names)
    return f"准备发送 {path_count} 个项目给 {peer_count} 个用户：{peer_text}"


def confirmation_text(selected_peer_names: list[str], pending_paths: list[Path]) -> str:
    recipients = _summarize_names(selected_peer_names, MAX_CONFIRMATION_RECIPIENTS)
    items = "\n".join(f"- {path.name}" for path in pending_paths[:MAX_CONFIRMATION_ITEMS])
    remaining_count = len(pending_paths) - MAX_CONFIRMATION_ITEMS
    if remaining_count > 0:
        items = f"{items}\n... 另 {remaining_count} 个项目"
    return f"发送给（{len(selected_peer_names)}）：{recipients}\n\n发送内容（{len(pending_paths)} 个项目）：\n{items}"


def _summarize_names(names: list[str], limit: int) -> str:
    visible_names = names[:limit]
    text = "、".join(visible_names)
    remaining_count = len(names) - limit
    if remaining_count > 0:
        text = f"{text} 等 {remaining_count} 个"
    return text
