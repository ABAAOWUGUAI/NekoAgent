"""Business synchronization helpers around operator delivery actions."""

from __future__ import annotations


def delivery_task_id(delivery: dict | None) -> str:
    payload = delivery.get("payload") if isinstance(delivery, dict) and isinstance(delivery.get("payload"), dict) else {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    return str(payload.get("task_id") or task.get("id") or "").strip()


def requeue_delivery(outbox, set_task_delivery, pending_status: str, delivery_id: str) -> dict | None:
    delivery = outbox.requeue_dead_letter(delivery_id)
    task_id = delivery_task_id(delivery)
    if task_id:
        set_task_delivery(task_id, pending_status, "")
    return delivery


__all__ = ["delivery_task_id", "requeue_delivery"]
