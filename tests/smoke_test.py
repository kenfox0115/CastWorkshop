"""CastWorkshop smoke-test net — ZERO real-dollar burn.

Runs with plain `python` (no pytest, no network). Every external hop (Ollama,
ComfyUI, Telegram) is monkeypatched via a fake `requests`, so nothing touches
the GPU, a live model, or the network. Exercises the REAL modules (worker.py,
comfy_client.py, vps/app.py) against in-memory/temp fixtures.

How to run:
    cd path/to/CastWorkshop
    python tests/smoke_test.py

Exit code: 0 = all pass, 1 = at least one failure.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import copy
import traceback

PASS = "PASS"
FAIL = "FAIL"

CC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(CC_ROOT, "worker", "workflows")


# --------------------------------------------------------------------------- #
# Fake `requests` — swaps the real module's get/post with network-free stubs.
# Routing table lets tests say "GET /settings/content_mode -> {mode:'sfw'}".
# --------------------------------------------------------------------------- #
_ROUTES: dict = {}


class FakeResponse:
    def __init__(self, payload, status=200, url=""):
        self._payload = payload
        self.status_code = status
        self.url = url
        self.ok = status < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _dispatch(method, url, **kw):
    hit = _ROUTES.get(url)
    if hit is None:
        # Fall back to a 404 to surface unexpected network attempts.
        return FakeResponse({"error": "no-route"}, 404, url=url)
    if callable(hit):
        return hit(method, url, **kw)
    return hit


def fake_get(url, *a, timeout=None, **k):
    return _dispatch("GET", url, **k)


def fake_post(url, *a, json=None, timeout=None, **k):
    resp = _dispatch("POST", url, _json=json, **k)
    resp.json_payload = getattr(resp, "_payload", None)
    return resp


def install_fake_requests():
    import requests as real_requests
    real_requests.get = fake_get
    real_requests.post = fake_post
    # Also handle direct `from requests import Session` callers used by workers.
    if hasattr(real_requests, "Session"):
        real_requests.Session = FakeSession
    return real_requests


class FakeSession:
    def __init__(self):
        pass

    def get(self, url, *a, **k):
        return fake_get(url, **k)

    def post(self, url, *a, json=None, **k):
        return fake_post(url, json=json, **k)


def route(url, resp):
    _ROUTES[url] = resp


# --------------------------------------------------------------------------- #
# Minimal runner
# --------------------------------------------------------------------------- #
_results = []


def check(name, cond, detail=""):
    _results.append((PASS if cond else FAIL, name, detail))
    print(f"{'ok ' if cond else 'xx '}{name}" + (f"  [{detail}]" if detail else ""))
    return cond


def load_workflow(name):
    with open(os.path.join(WORKFLOW_DIR, name), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# GATE 1: SFW/NSFW mode-precedence regression.
# Root cause of the user's leak: a CONTENT_MODE env var shadowed the DB /mode
# switch, so the worker stayed NSFW when UI/VPS reported SFW. DB must win.
# --------------------------------------------------------------------------- #
def test_mode_precedence():
    # Simulate the poisoned worker environment.
    os.environ["VPS_URL"] = "http://127.0.0.1:8787"
    os.environ["CONTENT_MODE"] = "nsfw"   # stale .env poison — must NOT win

    # Program the live VPS to report SFW.
    route("http://127.0.0.1:8787/settings/content_mode", FakeResponse({"mode": "sfw"}))
    route("http://127.0.0.1:8787/settings/model", FakeResponse({"model": "x"}))

    # Import the real worker (fresh module). main() is guarded to only run
    # under __main__, so importing is safe.
    import importlib.util

    # worker/ must be importable so `import comfy_client` (a sibling) resolves.
    worker_dir = os.path.join(CC_ROOT, "worker")
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)

    spec = importlib.util.spec_from_file_location(
        "wk_smoke", os.path.join(CC_ROOT, "worker", "worker.py")
    )
    wk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wk)

    m = wk.get_content_mode()
    check(
        "GATE1 mode-precedence: DB(sfw) beats env(CONTENT_MODE=nsfw)",
        m == "sfw", f"get_content_mode()={m!r}",
    )


# --------------------------------------------------------------------------- #
# GATE 2: SFW strip actually neutralizes the NSFW model/LoRA stack.
# --------------------------------------------------------------------------- #
def test_sfw_strip():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cc_smoke", os.path.join(CC_ROOT, "worker", "comfy_client.py")
    )
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)

    for wf_name in (
        "k2_txt2img_api.json",
    ):
        wf = copy.deepcopy(load_workflow(wf_name))
        cc.apply_sfw_transforms(wf)
        banned = ("filterbypass", "knpv", "gonzo", "gonzolomo", "uncensored", "realism_engine")
        unsafe_loras = []
        for node in wf.values():
            if str(node.get("class_type", "")).lower() not in ("loraloader", "loraloadermodelonly"):
                continue
            inputs = node.get("inputs", {})
            name = str(inputs.get("lora_name", "")).lower()
            if any(token in name for token in banned) and inputs.get("strength_model") != 0.0:
                unsafe_loras.append(name)
        unets = [
            str(node.get("inputs", {}).get("unet_name", "")).lower()
            for node in wf.values()
            if str(node.get("class_type", "")).lower() == "unetloader"
        ]
        gonzo_ok = all(not any(t in name for t in ("gonzalomo", "gonzo", "gonzolomo")) for name in unets)
        check(
            f"GATE2 sfw-strip({wf_name}): NSFW LoRAs neutralized, non-gonzo UNET",
            not unsafe_loras and gonzo_ok,
            f"unsafe_loras={unsafe_loras!r} unets={unets!r}",
        )


# --------------------------------------------------------------------------- #
# GATE 3: workflow validity — every workflow parses and key node IDs exist.
# --------------------------------------------------------------------------- #
def test_workflow_validity():
    expected = {
        # NOTE: keys are matched case-INSENSITIVELY below (Windows filenames are
        # case-insensitive; a mismatch here silently skipped 13 checks once).
        "k2_txt2img_api.json": ["30:19", "52", "30:5", "57", "30:10"],
        "tts_suite_moss_unload.json": ["6", "7", "10", "11"],
    }
    all_json = sorted(glob.glob(os.path.join(WORKFLOW_DIR, "**", "*.json"), recursive=True))
    required = ("presets/minimax_h3_2char_ref2v_vertical.json",
                "presets/minimax_h3_2char_ref2v_widescreen.json",
                "presets/minimax_h3_ref2v_vertical.json",
                "presets/minimax_h3_ref2v_widescreen.json",
                "TTS_Suite_MOSS_Unload.json")
    for filename in required:
        check(f"GATE3 required({filename})", os.path.isfile(os.path.join(WORKFLOW_DIR, filename)))
    for aspect in ("vertical", "widescreen"):
        expected[f"minimax_h3_2char_ref2v_{aspect}.json"] = ["136", "137", "143", "145", "141", "146", "138", "129", "132", "115", "92"]
    for path in all_json:
        base = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                wf = json.load(f)
            ok_parse = isinstance(wf, dict) and len(wf) > 0
        except Exception as e:  # noqa: BLE001
            check(f"GATE3 workflow({base}): parses", False, str(e))
            continue
        check(f"GATE3 workflow({base}): parses", ok_parse, "dict")
        for nid in expected.get(base.lower(), []):
            present = nid in wf
            check(f"GATE3 workflow({base}): node {nid}", present,
                  "" if present else f"missing {nid}")


# --------------------------------------------------------------------------- #
def main() -> int:
    print("CC smoke net (no GPU / no live Ollama / no Comfy / no network)")
    install_fake_requests()
    for fn in (test_mode_precedence, test_sfw_strip, test_workflow_validity):
        print(f"\n--- {fn.__name__} ---")
        try:
            fn()
        except Exception:  # noqa: BLE001
            check(fn.__name__, False, "raised")
            traceback.print_exc()

    passed = sum(1 for s, *_ in _results if s == PASS)
    failed = sum(1 for s, *_ in _results if s == FAIL)
    print(f"\n=== {passed} passed, {failed} failed ===")
    if failed:
        print("FAILED:")
        for s, name, detail in _results:
            if s == FAIL:
                print(f"  - {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
