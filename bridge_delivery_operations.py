"""Business synchronization helpers around operator delivery actions."""

from __future__ import annotations


def delivery_task_id(delivery: dict | None) -> str:
    payload = delivery.get("payload") if isinstance(delivery, dict) and isinstance(delivery.get("payload"), dict) else {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    return str(payload.get("task_id") or task.get("id") or "").strip()


def is_terminal_task_delivery(delivery: dict | None) -> bool:
    """Only canonical run results own the Task terminal-delivery projection."""

    payload = (
        delivery.get("payload")
        if isinstance(delivery, dict) and isinstance(delivery.get("payload"), dict)
        else {}
    )
    return bool(delivery_task_id(delivery) and payload.get("kind") == "run_result")


def requeue_delivery(outbox, set_task_delivery, pending_status: str, delivery_id: str) -> dict | None:
    delivery = outbox.requeue_dead_letter(delivery_id)
    if is_terminal_task_delivery(delivery):
        set_task_delivery(delivery_task_id(delivery), pending_status, "")
    return delivery


__all__ = ["delivery_task_id", "is_terminal_task_delivery", "requeue_delivery"]
