"""Shared, path-safe model inventory and readiness checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib import request


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "model_manifest.json"


def load_manifest(path=MANIFEST_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("models"), list):
        raise ValueError("Unsupported model manifest")
    return data


def _json_get(url, timeout=5):
    with request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _choices(object_info, node, field):
    try:
        value = object_info[node]["input"]["required"][field][0]
    except (KeyError, IndexError, TypeError):
        return set()
    return {str(item).replace("\\", "/").split("/")[-1] for item in value}


def readiness(ollama_url=None, comfy_url=None, ollama_model=None, fetch=_json_get):
    """Return presence only; never expose model directories or endpoint URLs."""
    manifest = load_manifest()
    ollama_url = (ollama_url or os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    comfy_url = (comfy_url or os.environ.get("COMFY_URL") or "http://127.0.0.1:8188").rstrip("/")
    errors = {}
    try:
        tags = fetch(f"{ollama_url}/api/tags")
        ollama = {str(item.get("name", "")).split(":")[0] for item in tags.get("models", [])}
        ollama |= {str(item.get("name", "")) for item in tags.get("models", [])}
    except Exception as exc:
        ollama = set();errors["ollama"] = type(exc).__name__
    try:
        info = fetch(f"{comfy_url}/object_info")
        comfy = {
            "unet": _choices(info, "UNETLoader", "unet_name"),
            "checkpoint": _choices(info, "CheckpointLoaderSimple", "ckpt_name"),
            "clip": _choices(info, "CLIPLoader", "clip_name"),
            "vae": _choices(info, "VAELoader", "vae_name"),
            "lora": _choices(info, "LoraLoader", "lora_name") | _choices(info, "LoraLoaderModelOnly", "lora_name"),
        }
    except Exception as exc:
        comfy = {};errors["comfyui"] = type(exc).__name__
    items = []
    for entry in manifest["models"]:
        if entry["engine"] == "ollama" and ollama_model:
            entry = dict(entry)
            entry["name"] = ollama_model
        item = {key:entry.get(key) for key in ("engine","category","name","required_for","download")}
        if entry.get("check") == "manual":item["status"] = "unverified"
        elif entry["engine"] == "ollama":item["status"] = "present" if entry["name"] in ollama else "missing"
        else:item["status"] = "present" if entry["name"] in comfy.get(entry["category"], set()) else "missing"
        items.append(item)
    return {"manifest_version":manifest["version"],"models":items,"errors":errors}
