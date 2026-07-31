#!/usr/bin/env python3
"""Authenticated HTTP adapter for PetPack management."""

from __future__ import annotations

import hashlib
from typing import Callable
from urllib.parse import unquote

from bridge_pet_service import delete_pet_pack, import_pet_pack, pet_asset, pet_state, save_pet_settings


class PetHttpApi:
    def __init__(self, db_connect: Callable, json_response: Callable, binary_response: Callable) -> None:
        self._db_connect = db_connect
        self._json_response = json_response
        self._binary_response = binary_response

    def handle_get(self, request, path: str) -> bool:
        if path == "/assistant/pets":
            with self._db_connect() as conn:
                state = pet_state(conn)
            self._json_response(request, 200, {"ok": True, "pet": state})
            return True
        if path.startswith("/assistant/pets/assets/"):
            pack_id = unquote(path.rsplit("/", 1)[-1])
            with self._db_connect() as conn:
                asset = pet_asset(conn, pack_id)
            if asset is None:
                self._json_response(request, 404, {"ok": False, "error": "pet_asset_not_found"})
                return True
            data, mime = asset
            self._binary_response(
                request, 200, data, mime, cache_control="private, max-age=3600",
                etag=hashlib.sha256(data).hexdigest(),
            )
            return True
        return False

    def handle_post(self, request, path: str, payload: dict) -> bool:
        handlers = {
            "/assistant/pets/settings": save_pet_settings,
            "/assistant/pets/import": import_pet_pack,
            "/assistant/pets/delete": delete_pet_pack,
        }
        handler = handlers.get(path)
        if handler is None:
            return False
        try:
            with self._db_connect() as conn:
                result = handler(conn, payload)
        except Exception as exc:
            self._json_response(request, 400, {"ok": False, "error": str(exc)})
            return True
        key = "pet" if path != "/assistant/pets/import" else "result"
        self._json_response(request, 201 if path == "/assistant/pets/import" else 200, {"ok": True, key: result})
        return True


__all__ = ["PetHttpApi"]
