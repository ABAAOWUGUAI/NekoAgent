#!/usr/bin/env python3
"""Install a declared public Starter Pack through existing authenticated APIs.

The tool never opens a database, reads chat history, or prints token material.
It only imports the explicitly declared resources and, when requested, creates
a new Persona Version for the current Assistant Instance.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path.name}")
    return value


def load_pack(pack_dir: Path) -> tuple[Path, dict]:
    pack_dir = pack_dir.resolve()
    manifest = _read_json(pack_dir / "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("visibility") != "public_optional_template":
        raise ValueError("unsupported_starter_pack")
    if not isinstance(manifest.get("persona"), dict) or not isinstance(manifest.get("appearance"), dict):
        raise ValueError("starter_pack_identity_missing")
    if not isinstance(manifest.get("memes"), list):
        raise ValueError("starter_pack_memes_invalid")
    return pack_dir, manifest


def _asset(pack_dir: Path, relative: object, expected_hash: object) -> bytes:
    path = (pack_dir / str(relative or "")).resolve()
    if pack_dir not in path.parents or not path.is_file():
        raise ValueError("starter_pack_asset_not_found")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if not data or actual != str(expected_hash or "").lower():
        raise ValueError(f"starter_pack_asset_hash_mismatch:{path.name}")
    return data


def _request(base_url: str, token: str, method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"Accept": "application/json", "X-Bridge-Token": token}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"http_{exc.code}:{path}") from exc
    except URLError as exc:
        raise RuntimeError(f"request_failed:{path}:{exc.reason}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"invalid_response:{path}")
    return result


def preview(pack_dir: Path, manifest: dict) -> dict:
    appearance = manifest["appearance"]
    _asset(pack_dir, appearance.get("asset"), appearance.get("sha256"))
    _asset(pack_dir, appearance.get("manifest"), appearance.get("manifest_sha256"))
    for item in manifest["memes"]:
        if not isinstance(item, dict):
            raise ValueError("starter_pack_meme_invalid")
        _asset(pack_dir, item.get("asset"), item.get("sha256"))
    return {
        "ok": True,
        "pack_id": manifest["id"],
        "version": manifest.get("version", ""),
        "display_name": manifest["display_name"],
        "appearance": str(appearance.get("name") or ""),
        "meme_count": len(manifest["memes"]),
        "default_activation": False,
        "state_imported": [],
    }


def _existing_pet_id(pet_state: dict, appearance: dict) -> str:
    for item in (pet_state.get("pet") or {}).get("packs", []):
        if isinstance(item, dict) and item.get("name") == appearance.get("name") and item.get("author") == appearance.get("author"):
            return str(item.get("id") or "")
    return ""


def apply_to_current(pack_dir: Path, manifest: dict, *, base_url: str, token: str) -> dict:
    checked = preview(pack_dir, manifest)
    current = _request(base_url, token, "GET", "/assistant/instances/current").get("result")
    if not isinstance(current, dict) or not current.get("id") or not current.get("updated_at"):
        raise RuntimeError("active_assistant_missing")
    appearance = manifest["appearance"]
    pet_state = _request(base_url, token, "GET", "/assistant/pets")
    pet_id = _existing_pet_id(pet_state, appearance)
    imported_pet = False
    if not pet_id:
        asset = _asset(pack_dir, appearance.get("asset"), appearance.get("sha256"))
        appearance_manifest = _read_json(pack_dir / str(appearance.get("manifest") or ""))
        result = _request(base_url, token, "POST", "/assistant/pets/import", {
            "name": appearance["name"], "author": appearance["author"], "license": appearance["license"],
            "mime_type": appearance["mime_type"], "asset_base64": base64.b64encode(asset).decode("ascii"),
            "manifest": appearance_manifest,
        })
        pet_id = str(((result.get("result") or {}).get("pack") or {}).get("id") or "")
        if not pet_id:
            raise RuntimeError("pet_import_result_missing")
        imported_pet = True
    meme_results = []
    for item in manifest["memes"]:
        data = _asset(pack_dir, item["asset"], item["sha256"])
        meme = _request(base_url, token, "POST", "/assistant/memes/upload", {
            "id": item["id"], "pack": manifest["id"], "name": item["name"], "emotion": item["emotion"],
            "tags": ",".join(str(value) for value in item.get("tags", [])),
            "creator": "Assistant Platform starter pack", "source": f"starter-pack:{manifest['id']}",
            "license_note": "See starter pack LICENSE.md", "review_status": "approved", "enabled": True,
            "cooldown_minutes": 90, "max_daily": 3,
            "data_base64": base64.b64encode(data).decode("ascii"),
        })
        meme_results.append(str((meme.get("meme") or {}).get("id") or item["id"]))
    persona = manifest["persona"]
    updated = _request(base_url, token, "PATCH", f"/assistant/instances/{current['id']}", {
        "expected_updated_at": current["updated_at"], "display_name": manifest["display_name"],
        "persona": persona["persona"], "style": persona["style"], "relationship": persona["relationship"],
        "behavior_boundaries": persona["behavior_boundaries"], "appearance_pack_id": pet_id,
    }).get("result")
    return {
        **checked, "applied_to_assistant_id": current["id"], "assistant_updated": bool(updated),
        "appearance_pack_id": pet_id, "appearance_imported": imported_pet, "meme_ids": meme_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install an explicit public Starter Pack")
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-to-current", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:18777")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    if args.dry_run == args.apply_to_current:
        parser.error("choose_exactly_one_of_dry_run_or_apply_to_current")
    pack_dir, manifest = load_pack(args.pack_dir)
    if args.dry_run:
        result = preview(pack_dir, manifest)
    else:
        if args.token_file is None:
            parser.error("token_file_required_when_applying")
        token = args.token_file.read_text(encoding="utf-8").strip()
        if not token:
            parser.error("token_file_empty")
        result = apply_to_current(pack_dir, manifest, base_url=args.base_url, token=token)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
